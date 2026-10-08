"""Pure, source-bound OpenArt connector requests. Observations are never authority."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

TRANSPORT = 'agent_mediated_connector'
_PINS = {'schema': 'd407e39696e0d0ddef64e1a82d08cfd972d4e7d84236a02437b5be75b0fc856f',
         'account': '5dae4cc15e47b233ac7a5896339f3aa8c79180b5853b548a16734c354982c0ec'}
OPERATIONS = {'text2video':'text_to_video', 'image2video':'image_to_video', 'element2video':'reference_to_video', 'generate-shot-video':'shot_video'}
ROLES = {'first_frame': ('startFrame', 'image'), 'last_frame': ('endFrame', 'image'),
         'reference_image': ('visualReferences', 'image'), 'reference_video': ('visualReferences', 'video'),
         'reference_audio': ('visualReferences', 'audio'),
         'character_reference': ('characterReferenceImageUrls', 'image'),
         'environment_reference': ('environmentReferenceImageUrl', 'image')}

class OpenArtMCPError(ValueError):
    def __init__(self, kind, message):
        self.kind, self.message = kind, message
        super().__init__(f'{kind}: {message}')
    def public(self):
        return {'kind': self.kind, 'message': self.message}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def _fail(kind, message):
    raise OpenArtMCPError(kind, message)

def _observation(kind):
    directory = Path(os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR', '~/.openmontage/openart/mcp-discovery')).expanduser()
    path = directory / f'2026-10-07-{kind}-observation.json'
    expected = os.environ.get('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_' + kind.upper(), _PINS.get(kind))
    index = directory / 'current-observations.json'
    if index.exists() and not os.environ.get('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_' + kind.upper()):
        try:
            entry = json.loads(index.read_text())[kind]
            name = entry['file']
            if Path(name).name != name: _fail('observation_invalid', 'observation index path escapes private discovery directory')
            path, expected = directory / name, entry['sha256']
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            _fail('observation_invalid', 'current observation index invalid')
    for private in (directory, path, index) if index.exists() else (directory,path):
        try:
            st = private.lstat()
            if private.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
                _fail('observation_invalid','connector observations require private owned nonsymlink paths')
        except OSError:
            _fail('observation_unavailable', 'private observation path unavailable')
    try:
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if not expected or sha != expected:
            _fail('observation_invalid', f'{kind} observation hash is not pinned')
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        _fail('observation_unavailable', f'retained {kind} connector observation unavailable')
    synthetic = str(value.get('classification', '')).startswith('synthetic_fixture')
    if synthetic and (not os.environ.get('OPENMONTAGE_OPENART_STATE_DIR') or not os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR') or directory == Path('~/.openmontage/openart/mcp-discovery').expanduser()):
        _fail('fixture_not_isolated', 'synthetic observations require isolated discovery and state directories')
    if not synthetic:
        if kind == 'schema':
            if value.get('transport') != 'openart_mcp' or value.get('catalog',{}).get('tool') != 'openart_model_list':
                _fail('observation_invalid','schema observation is not from exact connector catalog transport')
        elif kind == 'account' and (value.get('tool') != 'openart_account_get' or value.get('transport') != 'openart_mcp'):
            _fail('observation_invalid','account observation is not from exact connector account transport')
    return value, sha

def load_schema():
    return _observation('schema')[0]

def load_account():
    return _observation('account')[0]

def load_cost_observation():
    # Catalog credit advertisements are not request-specific quotes.
    return {'kind': 'unknown_cost', 'credits': None, 'usd': None, 'guaranteed_ceiling': False}

def account_summary():
    value, sha = _observation('account')
    data = value.get('data', {})
    uid = data.get('user', {}).get('uid')
    if not isinstance(uid, str) or not uid:
        _fail('account_invalid', 'connector observation lacks stable account UID')
    return {'provider': 'openart_mcp', 'transport': TRANSPORT, 'uid_sha256': digest(uid),
            'account_uid_sha256': digest(uid), 'account_observation_sha256': sha,
            'classification': value.get('classification'), 'observed_at': value.get('observed_at'),
            'plan': data.get('plan'), 'credits': data.get('credits'), 'dispatch_authority': False}

def video_catalog():
    rows = []
    for model in load_schema().get('catalog', {}).get('data', {}).get('models', []):
        for mode in model.get('modes', {}).get('video', []):
            rows.append({'model': model['id'], 'mode': mode['mode'], 'display_name': model.get('displayName'), 'operation': OPERATIONS.get(mode['mode']),
                         **copy.deepcopy(mode)})
    return rows

def form(model, mode):
    for entry in load_schema().get('forms', []):
        args = entry.get('arguments', {})
        if args.get('model') == model and args.get('mode') == mode:
            data = copy.deepcopy(entry['data'])
            if entry.get('tool') != 'openart_model_form_get' or data.get('model') != model or data.get('mode') != mode or data.get('media') != 'video':
                _fail('observation_invalid','form response does not match exact connector video model/mode')
            _validator(data['jsonSchema'])
            return data
    _fail('model_mode_unobserved', 'exact model/mode has no retained connector form')

def _branches(schema):
    result = [schema] if schema.get('properties') is not None else []
    for key in ('anyOf', 'oneOf', 'allOf'):
        for child in schema.get(key, []):
            result.extend(_branches(child))
    return result

def _properties(schema):
    result = {}
    for branch in _branches(schema):
        for key, spec in branch.get('properties', {}).items():
            if key not in result:
                result[key] = copy.deepcopy(spec)
            elif digest(result[key]) != digest(spec):
                old = result[key]
                result[key] = {'anyOf': old['anyOf'] + [copy.deepcopy(spec)]} if set(old) == {'anyOf'} else {'anyOf': [old, copy.deepcopy(spec)]}
    return result

def _validator(schema):
    from jsonschema import validators, FormatChecker
    def external_reference(value):
        if isinstance(value,dict):
            return any((key in {'$ref','$dynamicRef','$recursiveRef'} and isinstance(child,str) and not child.startswith('#')) or external_reference(child) for key,child in value.items())
        return isinstance(value,list) and any(external_reference(child) for child in value)
    if external_reference(schema):
        _fail('form_invalid','pure connector schemas cannot resolve external references')
    try:
        base = validators.validator_for(schema)
        base.check_schema(schema)
        # bools and floats are never native integers, even when Python compares equal.
        checker = base.TYPE_CHECKER.redefine('integer', lambda c,v: type(v) is int)
        return validators.extend(base, type_checker=checker)(schema, format_checker=FormatChecker())
    except Exception as exc:
        _fail('form_invalid', 'retained form schema cannot be validated: ' + type(exc).__name__)

def _capabilities(model, mode, schema):
    props = _properties(schema)
    catalog = next((r for r in video_catalog() if r['model']==model and r['mode']==mode), {})
    roles = {}
    for role, (field, kind) in ROLES.items():
        supported = field in props
        if field == 'visualReferences':
            supported = supported and kind in catalog.get('elementTypes', [])
        roles[role] = {'supported': supported, 'native_property': field, 'binding': 'role', 'type': kind,
                       'schema': copy.deepcopy(props.get(field))}
    return {'params': {name: {'binding': 'prompt' if name in ('prompt','sceneDescription') else ('role' if name in {v[0] for v in ROLES.values()} else 'native_param'), 'schema': spec} for name,spec in props.items()},
            'roles': roles, 'root_schema': copy.deepcopy(schema), 'all_controls_preserved': True, 'unreachable': [], 'unreachable_reason': None}

# Native readiness: a current pinned catalog row, its retained exact form whose
# jsonSchema validates, and a current account UID make a real profile
# production-ready. A prior generated result is optional evidence that only
# changes ``readiness.empirical_result``; it never grants or removes readiness.
_ALLOW_FIXTURE_PRODUCTION = False  # offline tests only; fixtures stay non-production by default

def _empirical(qualified, error=None):
    if error is not None:
        return {'status':'invalid','qualification_sha256':None,'reason':error}
    if not qualified:
        return {'status':'not_tested','qualification_sha256':None}
    kind = qualified.get('evidence_kind')
    return {'status':'fixture_only' if kind == 'fixture_only' else 'result_verified',
            'qualification_sha256':qualified.get('qualification_sha256')}

def _in_current_catalog(model, mode):
    return any(row['model'] == model and row['mode'] == mode for row in video_catalog())

def _native_ready(candidate):
    if candidate.get('mode') not in OPERATIONS or not _in_current_catalog(candidate.get('model'), candidate.get('mode')):
        return False
    if candidate['source'] == 'fixture':
        return _ALLOW_FIXTURE_PRODUCTION
    return candidate['source'] == 'real'

def _optional_evidence(candidate):
    """Optional empirical evidence; invalid/stale proof is reported, never blocking."""
    try:
        from lib import openart_mcp_jobs as jobs
        return jobs.qualified_profile(candidate['model'], candidate['mode'], candidate), None
    except (OpenArtMCPError, ValueError, KeyError, OSError) as exc:
        return None, getattr(exc, 'kind', None) or type(exc).__name__

def _ready_view(candidate, qualified=None, evidence_error=None):
    """Production view of an exact candidate. Readiness is native only; evidence neither grants nor removes it."""
    ready = _native_ready(candidate)
    if qualified:
        out = copy.deepcopy(qualified); status = 'qualified'
    else:
        out = copy.deepcopy(candidate); status = 'supported'
    out.update(status=status, profile_status=status, production_ready=ready,
               readiness={'basis':'native_catalog_form_account','production_ready':ready,
                          'empirical_result':_empirical(qualified, evidence_error)})
    return out

def load_profile(model, mode, *, require='supported'):
    """``candidate`` returns the raw observation; ``supported`` (legacy alias
    ``qualified``) returns the production view and fails when not ready."""
    if require not in ('candidate', 'supported', 'qualified'):
        _fail('invalid_argument', 'profile requirement must be candidate or supported')
    observed, sha = _observation('schema')
    current_form = form(model, mode)
    account = account_summary()
    synthetic = (str(observed.get('classification','')).startswith('synthetic_fixture') or str(account.get('classification','')).startswith('synthetic_fixture') or bool(os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR')) or bool(os.environ.get('OPENMONTAGE_OPENART_STATE_DIR')) or any(os.environ.get('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_'+kind.upper(),_PINS[kind]) != _PINS[kind] for kind in ('schema','account')))
    if synthetic and (not os.environ.get('OPENMONTAGE_OPENART_STATE_DIR') or not os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR') or Path(os.environ['OPENMONTAGE_OPENART_STATE_DIR']).expanduser().resolve() == Path('~/.openmontage/openart').expanduser().resolve()):
        _fail('fixture_not_isolated','fixture profiles require isolated discovery and state')
    profile = {'version':'1.0', 'provider':'openart_mcp', 'transport':TRANSPORT, 'model':model, 'mode':mode,
        'source':'fixture' if synthetic else 'real', 'observation_provenance':'agent_recorded_connector_observation_not_provider_authenticated', 'status':'candidate', 'profile_status':'candidate', 'production_ready':False,
        'account_uid_sha256':account['uid_sha256'], 'form':current_form, 'form_sha256':digest(current_form),
        'schema_observation_sha256':sha, 'native_capabilities':_capabilities(model, mode, current_form['jsonSchema'])}
    _validator(current_form['jsonSchema'])  # retained form must be a valid JSON Schema
    profile['profile_sha256'] = digest(profile)
    if require == 'candidate':
        return profile
    view = _ready_view(profile, *_optional_evidence(profile))
    if view['production_ready'] is not True:
        _fail('profile_not_ready', 'exact connector account/model/mode is not a production-ready native route')
    return view

def model_catalog():
    result = {}
    for row in video_catalog():
        profile = load_profile(row['model'], row['mode'], require='candidate')
        view = _ready_view(profile, *_optional_evidence(profile))
        result.setdefault(row['model'], {'display_name':row.get('display_name'), 'modes':{}})['modes'][row['mode']] = {
            **row, 'native_capabilities':profile['native_capabilities'], 'profile_status':view['profile_status'],
            'production_ready':view['production_ready'] is True, 'profile_sha256':profile['profile_sha256'],
            'readiness':view['readiness'], 'empirical_result_status':view['readiness']['empirical_result']['status']}
    return result

def _check_profile(profile):
    candidate = load_profile(profile.get('model'), profile.get('mode'), require='candidate')
    # Readiness/evidence views add status only; the schema/account identity stays exact.
    for key in ('provider','transport','model','mode','source','observation_provenance','account_uid_sha256','form','form_sha256','schema_observation_sha256','native_capabilities'):
        if digest(profile.get(key)) != digest(candidate.get(key)):
            _fail('profile_changed', 'connector profile identity or schema changed')
    if digest(profile) in (digest(candidate), digest(_ready_view(candidate))):
        return candidate
    # The exact current readiness view (including invalid/stale optional evidence
    # reported truthfully) is native-ready; optional proof never blocks it.
    qualified, evidence_error = _optional_evidence(candidate)
    if digest(profile) == digest(_ready_view(candidate, qualified, evidence_error)):
        return candidate
    # A legacy/evidence-bound artifact must still validate its own exact proof.
    from lib import openart_mcp_jobs as jobs
    qualified = jobs.qualified_profile(profile['model'], profile['mode'], candidate)
    if qualified is None or digest(profile) not in (digest(qualified), digest(_ready_view(candidate, qualified))):
        _fail('profile_changed', 'connector profile readiness or evidence changed')
    return candidate

def _project_reference(raw, schema):
    # Receipt remains intact on disk; project only fields admitted by the actual branch.
    options = _branches(schema) or [schema]
    for option in options:
        props = option.get('properties', {})
        projected = {key:copy.deepcopy(value) for key,value in raw.items() if key in props}
        def auxiliary_media(value, path=()):
            if isinstance(value,dict):
                return any(auxiliary_media(v,path+(k,)) for k,v in value.items())
            if isinstance(value,list):
                return any(auxiliary_media(v,path+(str(i),)) for i,v in enumerate(value))
            if path in (('imageUrl',),('thumbnailUrl',)) and value == raw.get('url'):
                return False
            return isinstance(value,str) and path != ('url',) and (value.startswith(('https://','http://','data:')) or (path and 'url' in str(path[-1]).lower()))
        if auxiliary_media(projected):
            _fail('unbound_media','ready reference contains auxiliary media that lacks its own approved source binding')
        if _validator(option).is_valid(projected):
            return projected
    _fail('reference_invalid', 'ready connector reference does not satisfy native media schema')

def prepare_native_request(controls, profile):
    _check_profile(profile)
    if not isinstance(controls, dict) or controls.get('model') != profile['model'] or controls.get('mode') != profile['mode']:
        _fail('profile_changed', 'controls differ from exact profile model/mode')
    allowed = {'model','mode','prompt','duration','aspect_ratio','resolution','native_params','input_assets','project_dir','openart_project_id','operation','image_path','last_image_path','output_path','first_frame','last_frame'}
    extra = set(controls) - allowed
    if extra: _fail('control_not_in_form', 'unsupported public controls: ' + ', '.join(sorted(extra)))
    operation = controls.get('operation')
    if operation is not None and operation != OPERATIONS.get(profile['mode']) and operation != 'first_last_frame':
        _fail('mode_unreachable', 'operation differs from exact native connector mode')
    schema = profile['form']['jsonSchema']; props = _properties(schema)
    params = copy.deepcopy(controls.get('native_params', {}))
    if not isinstance(params, dict):
        _fail('control_invalid', 'native_params must be an object')
    media = {field for field,_ in ROLES.values()}
    if set(params) & media:
        _fail('unbound_media', 'native media controls require source-bound input_assets')
    aliases = {'prompt': 'sceneDescription' if 'sceneDescription' in props else 'prompt',
        'duration':'videoDuration' if 'videoDuration' in props else 'duration',
        'aspect_ratio':'videoAspectRatio' if 'videoAspectRatio' in props else 'aspectRatio',
        'resolution':'videoResolution' if 'videoResolution' in props else 'resolution'}
    for public, native in aliases.items():
        if public in controls:
            if native not in props:
                _fail('control_not_in_form', 'requested control is absent from exact connector form: ' + public)
            if native in params and digest(params[native]) != digest(controls[public]):
                _fail('control_conflict', 'conflicting native and public control: ' + public)
            params[native] = copy.deepcopy(controls[public])
    account = account_summary(); assets = copy.deepcopy(controls.get('input_assets', []))
    if not isinstance(assets,list): _fail('control_invalid','input_assets must be an array')
    for legacy,role in (('image_path','first_frame'),('first_frame','first_frame'),('last_image_path','last_frame'),('last_frame','last_frame')):
        if legacy in controls:
            if any(a.get('role') == role for a in assets): _fail('control_conflict','duplicate frame alias')
            assets.append({'role':role,'source_path':controls[legacy]})
    if controls.get('operation') == 'first_last_frame' and (profile['mode'] != 'image2video' or not {'first_frame','last_frame'} <= {a.get('role') for a in assets}):
        _fail('missing_frame', 'first_last_frame requires both source-bound frame roles')
    bound = []
    for asset in assets:
        if not isinstance(asset,dict) or set(asset) - {'role','source_path','source_sha256','upload_id','reference_id'}:
            _fail('input_asset_invalid','unsupported input asset fields')
        role = asset.get('role'); capability = profile['native_capabilities']['roles'].get(role,{})
        if capability.get('supported') is not True:
            _fail('role_not_in_form','input role absent from exact connector model/mode: ' + str(role))
        if 'upload_id' in asset and 'reference_id' in asset and asset['upload_id'] != asset['reference_id']:
            _fail('control_conflict','upload and reference IDs conflict')
        if 'reference_id' in asset: asset['upload_id'] = asset.pop('reference_id')
        from lib import openart_mcp_jobs as jobs
        binding = copy.deepcopy(jobs.resolve_input_asset(controls.get('project_dir'), asset, account_binding=account,
            openart_project_id=controls.get('openart_project_id')))
        raw = binding.pop('visual_reference')
        field,kind = ROLES[role]
        if raw.get('type') != kind: _fail('reference_invalid','ready reference type differs from input role')
        spec = props[field]
        if field in ('characterReferenceImageUrls','environmentReferenceImageUrl'):
            value = raw.get('url')
        else:
            value = _project_reference(raw, spec.get('items',spec))
        array = field in ('visualReferences','characterReferenceImageUrls')
        if array:
            index = len(params.setdefault(field,[])); params[field].append(value); pointer = f'/params/{field}/{index}'
        else:
            if field in params: _fail('ambiguous_role','multiple assets for singleton media role')
            params[field] = value; pointer = f'/params/{field}'
        bound.append({**binding,'role':role,'param_path':pointer,'body_pointer':pointer,'native_reference_sha256':digest(value)})
    errors = list(_validator(schema).iter_errors(params))
    if errors: _fail('control_invalid','native params violate exact retained form: ' + errors[0].validator)
    # One governed shot collects exactly one original video; multi-output
    # canonical collection is not implemented, so never dispatch it.
    if 'videoCount' in params and (type(params['videoCount']) is not int or params['videoCount'] != 1):
        _fail('output_count_unsupported','one governed shot requires native videoCount exactly 1; multi-output collection is unsupported')
    body = {'model':profile['model'],'mode':profile['mode'],'params':params}
    if controls.get('openart_project_id') is not None:
        if not isinstance(controls['openart_project_id'],str) or not controls['openart_project_id']: _fail('control_invalid','OpenArt project ID must be a nonempty string')
        body['projectId'] = controls['openart_project_id']
    return {'provider':'openart_mcp','transport':TRANSPORT,'model':profile['model'],'mode':profile['mode'],'source':profile['source'],
        'body':body,'body_sha256':digest(body),'source_binding_sha256':digest(bound),'input_assets':bound,
        'account_binding':{'uid_sha256':account['uid_sha256'],'account_observation_sha256':account['account_observation_sha256']},
        'profile_sha256':profile['profile_sha256'],'form_sha256':profile['form_sha256'],
        'schema_observation_sha256':profile['schema_observation_sha256'],
        'prompt_param_path':'/params/' + aliases['prompt'], 'project_dir':str(Path(controls['project_dir']).resolve()) if controls.get('project_dir') else None,
        'controls':copy.deepcopy(controls)}

def validate_native_request(native, profile=None):
    if not isinstance(native,dict): _fail('native_request_invalid','native request must be an object')
    profile = profile or load_profile(native.get('model'),native.get('mode'),require='candidate')
    if profile['profile_sha256'] != native.get('profile_sha256'):
        profile = load_profile(native.get('model'),native.get('mode'),require='supported')
    expected = prepare_native_request(native.get('controls',{}),profile)
    if digest(expected) != digest(native): _fail('native_request_changed','source-bound native connector request changed')
    return copy.deepcopy(native)


def retain_observation(kind, observation):
    """Retain an explicit agent-recorded refresh; this grants no dispatch authority.

    Schema/account are recorded independently. A refreshed UID or exact schema
    changes profile identity, while balance-only refresh changes request binding.
    """
    if kind not in ('schema','account') or not isinstance(observation,dict):
        _fail('invalid_argument','only connector schema/account observations can be retained')
    synthetic = str(observation.get('classification','')).startswith('synthetic_fixture')
    directory = Path(os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR','~/.openmontage/openart/mcp-discovery')).expanduser()
    if synthetic and (not os.environ.get('OPENMONTAGE_OPENART_STATE_DIR') or not os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR')):
        _fail('fixture_not_isolated','synthetic observations require isolated discovery and state')
    if not synthetic:
        if kind == 'schema':
            if observation.get('classification') != 'schema_observation_only_not_dispatch_authority' or observation.get('transport') != 'openart_mcp' or observation.get('catalog',{}).get('tool') != 'openart_model_list':
                _fail('observation_invalid','refresh requires actual connector schema observation provenance')
            for entry in observation.get('forms',[]):
                data,args = entry.get('data',{}),entry.get('arguments',{})
                if entry.get('tool') != 'openart_model_form_get' or data.get('model') != args.get('model') or data.get('mode') != args.get('mode') or data.get('media') != 'video':
                    _fail('observation_invalid','refreshed form provenance does not match request')
                _validator(data['jsonSchema'])
        elif observation.get('tool') != 'openart_account_get' or observation.get('transport') != 'openart_mcp' or observation.get('classification') != 'agent_recorded_connector_observation_not_dispatch_authority':
            _fail('observation_invalid','refresh requires actual connector account observation provenance')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        _fail('observation_invalid','refresh directory must be private owned and nonsymlink')
    observation = copy.deepcopy(observation)
    if synthetic or os.environ.get('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR') or os.environ.get('OPENMONTAGE_OPENART_STATE_DIR'):
        observation['classification'] = 'synthetic_fixture_retained_from_isolated_state_not_live_qualification'
    observation['retention_provenance'] = 'agent_recorded_not_provider_authenticated'
    raw = (json.dumps(observation,sort_keys=True,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode()
    sha=hashlib.sha256(raw).hexdigest(); name=kind+'-'+sha+'.json'
    target=directory/name
    if target.exists() and (target.is_symlink() or target.read_bytes()!=raw):
        _fail('observation_invalid','retention target conflicts with observation bytes')
    with os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600),'wb') as stream:
        stream.write(raw)
    index=directory/'current-observations.json'
    entries={}
    if index.exists():
        if index.is_symlink() or index.stat().st_mode & 0o077: _fail('observation_invalid','observation index is not private')
        entries=json.loads(index.read_text())
    entries[kind]={'file':name,'sha256':sha}
    temporary=directory/('.current-observations-'+os.urandom(8).hex()+'.json')
    with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
        json.dump(entries,stream,sort_keys=True)
    os.replace(temporary,index)
    return {'kind':kind,'observation_sha256':sha,'dispatch_authority':False,'classification':observation.get('classification')}
