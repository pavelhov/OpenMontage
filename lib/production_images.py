"""Canonical board-image admission and original host-result import.

Native images are agent-invoked host tools. ``begin_native_image`` emits one
handoff; this module never invokes or queries the host. Local call IDs identify
our journal, not a provider job. Grok images retain their existing governed
scope/session journal; the same approved image allowance counts both routes.
"""
from __future__ import annotations

import base64
import copy
import datetime
import hashlib
import io
import json
import os
import re
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator
from schemas.artifacts import load_schema
from lib.production_request import digest
from lib.shot_contract import file_sha256

ARTIFACT = Path('artifacts/production_image_calls.json')
NATIVE_ROUTE = 'native_imagegen'
NATIVE_TOOL = 'image_gen.imagegen'
PENDING = {'reserved', 'submitted', 'uncertain', 'returned'}


def _ex():
    from lib import production_execution as execution
    return execution


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read(path):
    return json.loads(Path(path).read_text())


def _root(project_dir):
    root = Path(project_dir).expanduser().resolve()
    marker = _read(root / 'project.json')
    if (marker.get('governance', {}).get('version') != '1.0'
            or marker.get('governance', {}).get('mode') != 'strict'):
        raise ValueError('image lifecycle requires a strict governed project')
    if not marker.get('project_id') or not marker.get('story_revision'):
        raise ValueError('project lacks current story binding')
    return root, marker


def _bound_file(root, item, label):
    if not isinstance(item, dict) or not item.get('path') or not item.get('sha256'):
        raise ValueError(label + ' requires retained path and hash')
    path = _ex()._inside(item['path'], root)
    if not path.is_file() or file_sha256(path) != item['sha256']:
        raise ValueError(label + ' bytes changed or missing')
    return path


def _canonical_file(root, item, label):
    path = _bound_file(root, item, label)
    return {**copy.deepcopy(item), 'path':str(path.relative_to(root))}


def _validate_review(review, sha256, revision):
    schema = load_schema('shot_contract')
    Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/review'}).validate(review)
    if (review['subject_sha256'] != sha256 or review['story_revision'] != revision
            or review['status'] != 'pass' or any(p.get('severity') == 'critical'
                and p['status'] != 'pass' for p in review['predicates'])):
        raise ValueError('reuse needs a passing current-byte applicable review')


def validate_image_reuse(project_dir, reference, *, role):
    """Validate exact bytes, role/story applicability and retained observation.

    This reads evidence and consumes no generation. It never creates a review.
    """
    root, marker = _root(project_dir)
    path = _bound_file(root, reference, 'reference')
    applicability = reference.get('applicability') or {}
    if (applicability.get('story_revision') != marker['story_revision']
            or role not in applicability.get('roles', [])):
        raise ValueError('reference reuse lacks current story/role applicability')
    _validate_review(reference.get('review'), reference['sha256'], marker['story_revision'])
    return {'id':reference['id'], 'role':role, 'path':str(path.relative_to(root)),
            'sha256':reference['sha256'], 'review_sha256':digest(reference['review']),
            'story_revision':marker['story_revision']}


def build_preboard_packet(project_dir, *, story, approval, references, board_slots):
    """Prepare approved current story and references before boards exist.

    ``approval`` is retained human evidence {path, sha256, approved_by} for the
    story bytes. Slot declarations are explicit unresolved outputs, not motion
    eligibility. Exact generation authority is separately checked at reserve.
    """
    root, marker = _root(project_dir)
    if not approval.get('approved_by'):
        raise ValueError('story needs explicit approval provenance')
    packet = {'version':'1.0', 'kind':'pre_board', 'project_id':marker['project_id'],
        'story_revision':marker['story_revision'], 'project_dir':str(root),
        'story':_canonical_file(root, story, 'story'),
        'approval':_canonical_file(root, approval, 'story approval'),
        'references':[_canonical_file(root, ref, 'reference') for ref in references],
        'board_slots':copy.deepcopy(board_slots), 'prepared_at':_now()}
    packet['packet_sha256'] = digest(packet)
    _validate_packet(root, packet)
    return packet


def _validate_packet(root, packet):
    _, marker = _root(root)
    schema = load_schema('production_image_calls')
    Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/packet'}).validate(packet)
    if (packet['project_id'] != marker['project_id'] or packet['story_revision'] != marker['story_revision']
            or Path(packet['project_dir']).resolve() != root):
        raise ValueError('pre-board packet has stale project/story binding')
    if packet['packet_sha256'] != digest({k:v for k,v in packet.items() if k != 'packet_sha256'}):
        raise ValueError('pre-board packet digest changed')
    _bound_file(root, packet['story'], 'story')
    _bound_file(root, packet['approval'], 'story approval')
    ids = [ref['id'] for ref in packet['references']]
    slots = [slot['id'] for slot in packet['board_slots']]
    if len(set(ids)) != len(ids) or len(set(slots)) != len(slots):
        raise ValueError('duplicate reference or board slot')
    for ref in packet['references']:
        _bound_file(root, ref, 'reference')
        if ref['applicability']['story_revision'] != marker['story_revision']:
            raise ValueError('reference applicability has stale story binding')


def _native_arguments(packet, arguments):
    if (not isinstance(arguments, dict) or set(arguments) - {'prompt','referenced_image_paths','transparent_background'}
            or not isinstance(arguments.get('prompt'), str) or not arguments['prompt'].strip()):
        raise ValueError('unsupported native image arguments; one prompt per host call')
    if 'transparent_background' in arguments and type(arguments['transparent_background']) is not bool:
        raise ValueError('native transparent_background must be boolean')
    root = Path(packet['project_dir']).resolve()
    references = {_ex()._inside(ref['path'],root):ref for ref in packet['references']}
    native = copy.deepcopy(arguments)
    if 'referenced_image_paths' in arguments:
        paths = arguments['referenced_image_paths']
        if not isinstance(paths, list) or not paths:
            raise ValueError('native referenced_image_paths must be a nonempty ordered list')
        resolved = [_ex()._inside(path,root) for path in paths]
        if any(path not in references for path in resolved):
            raise ValueError('native arguments introduce an unbound reference')
        native['referenced_image_paths'] = [str(path) for path in resolved]
    return native


def image_request_digest(packet, slot_id, arguments, *, output_path):
    """Digest to retain in the approved image scope's exact requests map."""
    native = _native_arguments(packet, arguments)
    root = Path(packet['project_dir']).resolve()
    output = _ex()._inside(output_path, root)
    if output == root:
        raise ValueError('image output_path must name a project file')
    bound = copy.deepcopy(native)
    if 'referenced_image_paths' in bound:
        refs = {str(_ex()._inside(ref['path'],root)):ref for ref in packet['references']}
        bound['referenced_image_paths'] = [{'path':refs[p]['path'],'sha256':refs[p]['sha256']}
                                          for p in bound['referenced_image_paths']]
    return digest({'packet_sha256':packet['packet_sha256'], 'slot_id':slot_id,
                   'tool_name':NATIVE_TOOL, 'arguments':bound, 'output_path':str(output.relative_to(root))})


def _load(root):
    path = root / ARTIFACT
    value = _read(path) if path.exists() else {'version':'1.0','project_id':_root(root)[1]['project_id'],'calls':[]}
    Draft202012Validator(load_schema('production_image_calls')).validate(value)
    if value['project_id'] != _root(root)[1]['project_id']:
        raise ValueError('image artifact project differs')
    ids = [call['call_id'] for call in value['calls']]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate canonical image call ID')
    return value


def _write(root, value):
    Draft202012Validator(load_schema('production_image_calls')).validate(value)
    path = root/ARTIFACT; path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x') as stream:
            json.dump(value,stream,indent=2,allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def _call(value, call_id):
    for call in value['calls']:
        if call['call_id'] == call_id:
            return call
    raise ValueError('unknown canonical image call')


def _event(call, kind, **details):
    call['events'].append({'kind':kind,'observed_at':_now(), 'details':details})


def _scope(root, scope_id):
    marker = _root(root)[1]
    scopes = _read(root/'production_scopes.json')
    matches = [s for s in scopes.get('scopes',[]) if s.get('id') == scope_id]
    if scopes.get('version') != '1.0' or len(matches) != 1:
        raise ValueError('image approval scope must exist exactly once')
    scope = matches[0]
    if (scope.get('phase') != 'image' or scope.get('status') != 'approved' or not scope.get('approved_by')
            or scope.get('project_id') != marker['project_id'] or scope.get('story_revision') != marker['story_revision']):
        raise ValueError('image scope lacks current explicit approval')
    _bound_file(root,scope.get('evidence'), 'image approval')
    binding = scope.get('derived_from_image_envelope')
    if binding is not None:
        retained = _image_envelope(root,scopes); definition = retained['definition']
        if (not isinstance(binding,dict)
                or set(binding) not in ({'envelope_sha256', 'slot_id', 'replace_exception_call_id'},
                    {'envelope_sha256', 'slot_id', 'replace_exception_call_id', 'board_correction_intent_sha256'})
                or binding.get('envelope_sha256') != retained['envelope_sha256']
                or binding.get('slot_id') not in definition['templates']
                or scope['id'] != 'native-envelope-'+digest(binding)[:32]
                or scope.get('provider') != NATIVE_ROUTE
                or scope.get('allowed_image_routes') != definition['allowed_image_routes']
                or scope.get('image_allowance') != definition['image_allowance']
                or scope.get('approved_by') != retained['approval']['approved_by']
                or scope.get('evidence') != retained['approval']):
            raise ValueError('derived image scope authority differs from approved envelope')
        intent = scope.get('board_correction_intent')
        if ((intent is None) != ('board_correction_intent_sha256' not in binding)
                or (intent is not None and (not isinstance(intent, dict) or not retained.get('activation_basis')
                    or binding['board_correction_intent_sha256'] != digest(intent)
                    or intent.get('slot_id') != binding['slot_id']
                    or binding.get('replace_exception_call_id') is not None))):
            raise ValueError('derived image correction proof binding changed')
    return scope


def prepare_native_image_envelope(project_dir, *, packet, board_plan, templates,
                                  source_inventory, image_allowance, allowed_image_routes=None):
    """Freeze authored image inputs for explicit approval; creates no authority.

    Templates contain exactly prompt, transparent_background, output_path and
    ordered reference_ids. Static inventory entries are complete packet references;
    dynamic entries name id/role/path/source_slot_id/applicability. The board plan
    retains the complete authored package, including cast and review declarations.
    """
    root, marker = _root(project_dir)
    _validate_packet(root, packet)
    definition = {'version':'1.0', 'project_id':marker['project_id'],
        'story_revision':marker['story_revision'], 'project_dir':str(root),
        'packet_binding':{k:copy.deepcopy(packet[k]) for k in ('story','approval','board_slots')},
        'board_plan':_canonical_file(root,board_plan,'board plan'),
        'templates':copy.deepcopy(templates), 'source_inventory':[
            copy.deepcopy(source) if source.get('source_slot_id') is not None
            else _canonical_file(root,source,'source inventory') for source in source_inventory],
        'image_allowance':image_allowance, 'allowed_image_routes':copy.deepcopy(
            [NATIVE_ROUTE] if allowed_image_routes is None else allowed_image_routes)}
    _validate_image_envelope(root, definition)
    return definition


def _validate_image_envelope(root, definition):
    marker = _root(root)[1]
    routes = definition.get('allowed_image_routes')
    if (definition.get('version') != '1.0' or definition.get('project_id') != marker['project_id']
            or definition.get('story_revision') != marker['story_revision']
            or definition.get('project_dir') != str(root)
            or not isinstance(routes,list) or NATIVE_ROUTE not in routes
            or len(routes) != len(set(routes)) or set(routes)-{NATIVE_ROUTE,'grok_cli'}
            or type(definition.get('image_allowance')) is not int or definition['image_allowance'] < 1):
        raise ValueError('native image envelope project/story/route/ceiling differs')
    _bound_file(root,definition.get('board_plan'),'board plan')
    binding = definition['packet_binding']
    _bound_file(root,binding['story'],'story'); _bound_file(root,binding['approval'],'story approval')
    templates = definition['templates']; inventory = definition['source_inventory']
    ids = [source['id'] for source in inventory]
    slots = binding['board_slots']
    if (not templates or len(set(ids)) != len(ids) or set(templates) != {slot['id'] for slot in slots}
            or len(templates) != len(slots) or any(slot['status'] != 'unresolved' for slot in slots)):
        raise ValueError('native image envelope needs unique complete slots and source inventory')
    for slot, template in templates.items():
        if (set(template) != {'prompt','transparent_background','output_path','reference_ids'}
                or not isinstance(template['prompt'],str) or not template['prompt'].strip()
                or type(template['transparent_background']) is not bool
                or not isinstance(template['reference_ids'],list)
                or any(ref not in ids for ref in template['reference_ids'])
                or _ex()._inside(template['output_path'],root) == root):
            raise ValueError('native image envelope template differs from supported authored inputs')
    for source in inventory:
        if source.get('source_slot_id') is not None:
            producer = source['source_slot_id']
            if (set(source) != {'id','role','path','source_slot_id','applicability'} or producer not in templates
                    or source['id'] != producer or source['role'] != next(s['role'] for s in slots if s['id']==producer)
                    or _ex()._inside(source['path'],root) != _ex()._inside(templates[producer]['output_path'],root)):
                raise ValueError('dynamic source inventory differs from frozen producer slot')
        else:
            _bound_file(root,source,'source inventory')
        if source.get('applicability',{}).get('story_revision') != marker['story_revision']:
            raise ValueError('source inventory has stale story applicability')


def _write_image_scopes(root, value):
    path = root/'production_scopes.json'
    temp = path.with_name('.'+path.name+'-'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x') as stream:
            json.dump(value,stream,indent=2,allow_nan=False); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def _image_envelope(root, data):
    retained = data.get('image_envelope')
    if not isinstance(retained,dict):
        raise ValueError('explicit native image envelope activation is missing')
    definition = retained['definition']; approval = retained['approval']
    _validate_image_envelope(root,definition)
    if retained['envelope_sha256'] != digest(definition) or not approval.get('approved_by'):
        raise ValueError('native image envelope approval binding changed')
    evidence_path = _bound_file(root,approval,'image envelope approval')
    if retained.get('activation_basis') is not None:
        from lib.production_autonomy import require_active_policy
        policy, sha, decision_id = require_active_policy(root)
        expected = {'kind': 'standing_run', 'policy_sha256': sha, 'decision_id': decision_id,
            'run_command': policy.get('activation_basis', {}).get('run_command')}
        if (retained['activation_basis'] != expected or approval != {
                **policy['evidence'], 'approved_by': 'standing_media_authority:' + decision_id}
                or definition != _standing_image_definition(root, policy, check_first_pass_capacity=False)):
            raise ValueError('standing image envelope differs from actual current run authority')
        return retained
    try:
        evidence = _read(evidence_path)
    except ValueError as exc:
        raise ValueError('native image envelope needs structured activation evidence') from exc
    expected = {'kind':'native_image_envelope_activation','project_id':definition['project_id'],
        'story_revision':definition['story_revision'],'envelope_sha256':digest(definition),
        'approved_by':approval['approved_by']}
    if not isinstance(evidence,dict) or any(evidence.get(k) != v for k,v in expected.items()):
        raise ValueError('native image envelope lacks explicit hash-bound activation evidence')
    return retained


def _standing_image_definition(root, policy, *, check_first_pass_capacity=True, retained_pack=None):
    """Derive exact board slots under real setup/command media authority."""
    from lib import production_boards as boards
    from lib.production_autonomy import _bound_document, StandingSetupRequired, _validate_standing_source_role
    pack = boards.load_board_pack(root) if retained_pack is None else retained_pack
    setup = _bound_document(root, policy['activation_basis']['standing_setup'], 'standing setup')
    terms = setup.get('native_images', {})
    if (set(terms) != {'account', 'image_allowance', 'allowed_image_routes'}
            or not isinstance(terms['account'], str) or not terms['account'].strip()
            or type(terms['image_allowance']) is not int or terms['image_allowance'] < 1
            or terms['allowed_image_routes'] != [NATIVE_ROUTE]):
        raise StandingSetupRequired('standing setup needs named native image account, route and image allowance')
    if not pack.get('package_binding'):
        raise StandingSetupRequired('standing image run needs exact retained authored package')
    usage = image_usage(root)
    counted_slots = {row['slot_id'] for row in usage['occurrences'] if row['counted']}
    # Upstream reuse slots bind actual selected outgoing frames; never image work.
    image_nodes = [node for node in pack['specification']['image_nodes'] if 'upstream_source' not in node]
    current = boards._bindings_current(root, pack, [node['slot_id'] for node in image_nodes])
    missing = [node for node in image_nodes
        if not current[node['slot_id']] and node['slot_id'] not in counted_slots]
    if check_first_pass_capacity and usage['total'] + len(missing) > terms['image_allowance']:
        raise StandingSetupRequired('fixed native image allowance cannot cover retained usage plus missing first-pass boards')
    spec = pack['specification']
    inventory = copy.deepcopy(spec['references'])
    for node in image_nodes:
        inventory.append({'id': node['slot_id'], 'role': node['role'], 'path': node['output_path'],
            'source_slot_id': node['slot_id'], 'applicability': {'story_revision': pack['story_revision'], 'roles': [node['role']]}})
    templates = {node['slot_id']: {'prompt': boards._prompt(node),
        'transparent_background': node.get('transparent_background', False), 'output_path': node['output_path'],
        'reference_ids': node.get('reference_ids', []) + node.get('image_dependencies', [])} for node in image_nodes}
    transfers = [term for term in setup['source_transfer'] if term.get('provider') == NATIVE_ROUTE
        and term.get('account') == terms['account']]
    if len(transfers) != 1:
        raise StandingSetupRequired('standing native image source transfer needs one exact transport/account term')
    transfer = transfers[0]
    if (not isinstance(transfer.get('roles'), list) or type(transfer.get('generated_board_sources')) is not bool
            or any(type(transfer.get(key)) is not int or transfer[key] < 1
                for key in ('max_files', 'max_file_bytes', 'max_batch_bytes', 'max_batches'))):
        raise StandingSetupRequired('standing native transfer needs exact roles and positive file/batch limits')
    for source in inventory:
        _validate_standing_source_role(source, transfer, NATIVE_ROUTE)
        if source.get('source_slot_id'):
            if not transfer['generated_board_sources']:
                raise StandingSetupRequired('run-generated native image source transfer not authorized')
        elif source['sha256'] not in setup['approved_source_sha256']:
            raise StandingSetupRequired('native image original hash exceeds standing source inventory')
    return prepare_native_image_envelope(root, packet=pack['preboard_packet'], board_plan=pack['package_binding'],
        templates=templates, source_inventory=inventory, image_allowance=terms['image_allowance'],
        allowed_image_routes=terms['allowed_image_routes'])


def activate_standing_image_envelope(project_dir):
    """Use existing image authority owner; never claim a human reviewed this plan."""
    from lib.production_autonomy import require_active_policy, StandingSetupRequired
    root, _ = _root(project_dir)
    with _ex()._lock(root):
        policy, sha, decision_id = require_active_policy(root)
        if not policy.get('activation_basis'):
            raise StandingSetupRequired('actual active standing run required for image envelope')
        path = root / 'production_scopes.json'
        data = _read(path) if path.exists() else {'version': '1.0', 'scopes': []}
        definition = _standing_image_definition(root, policy, check_first_pass_capacity='image_envelope' not in data)
        retained = {'definition': definition, 'envelope_sha256': digest(definition),
            'approval': {**policy['evidence'], 'approved_by': 'standing_media_authority:' + decision_id},
            'activation_basis': {'kind': 'standing_run', 'policy_sha256': sha, 'decision_id': decision_id,
                'run_command': copy.deepcopy(policy['activation_basis']['run_command'])}}
        if 'image_envelope' in data and data['image_envelope'] != retained:
            raise StandingSetupRequired('existing immutable image envelope differs from standing run')
        if 'image_envelope' not in data:
            data['image_envelope'] = retained; _write_image_scopes(root, data)
        return copy.deepcopy(retained)


def _standing_native_transfer(root, retained, packet, *, exclude_call_id=None):
    if 'activation_basis' not in retained:
        return
    from lib.production_autonomy import require_active_policy, effective_standing_setup, StandingSetupRequired
    from lib import production_boards as boards
    policy, _, _ = require_active_policy(root)
    setup = effective_standing_setup(root, policy)
    account = setup['native_images']['account']
    term = next(t for t in setup['source_transfer'] if t['provider'] == NATIVE_ROUTE and t['account'] == account)
    total = 0
    pack = boards.load_board_pack(root)
    for ref in packet['references']:
        if ref['role'] not in term['roles'] or boards._review_blockers(pack, ref['id'], ref['role']):
            raise StandingSetupRequired('native transfer requires actual current authorized role review')
        size = _bound_file(root, ref, 'native transfer source').stat().st_size
        if size > term['max_file_bytes']:
            raise StandingSetupRequired('native source exceeds approved file limit')
        total += size
    if len(packet['references']) > term['max_files'] or total > term['max_batch_bytes']:
        raise StandingSetupRequired('native transfer exceeds approved batch limit')
    counted = {r['call_id'] for r in image_usage(root)['occurrences'] if r['counted']}
    source_calls = [r for r in _load(root)['calls'] if r['call_id'] in counted
        and r['call_id'] != exclude_call_id and r['route'] == NATIVE_ROUTE and r['packet'].get('references')]
    if packet['references'] and len(source_calls) >= term['max_batches']:
        raise StandingSetupRequired('native source batch allowance exhausted')


def record_native_image_envelope(project_dir, definition, *, approval):
    """Activate only a creator-approved exact package; never infer prior consent.

    Retained JSON evidence must explicitly declare kind native_image_envelope_activation,
    project_id, story_revision, envelope_sha256 and approved_by. This API checks
    declared evidence, not the truth of a human approval. Re-entry is immutable.
    """
    root, _ = _root(project_dir)
    with _ex()._lock(root):
        path = root/'production_scopes.json'
        data = _read(path) if path.exists() else {'version':'1.0','scopes':[]}
        if data.get('version') != '1.0' or not isinstance(data.get('scopes'),list):
            raise ValueError('invalid production scopes record')
        retained = {'definition':copy.deepcopy(definition),'envelope_sha256':digest(definition),
                    'approval':_canonical_file(root,approval,'image envelope approval')}
        _image_envelope(root,{'image_envelope':retained})
        if 'image_envelope' in data:
            if data['image_envelope'] != retained:
                raise ValueError('existing native image envelope cannot be replaced or expanded')
        else:
            data['image_envelope'] = retained; _write_image_scopes(root,data)
        return copy.deepcopy(retained)


def _derived_image_scope(root, retained, packet, slot_id, arguments, output_path):
    definition = retained['definition']; templates = definition['templates']
    _validate_packet(root,packet)
    if {k:packet[k] for k in definition['packet_binding']} != definition['packet_binding']:
        raise ValueError('native image packet differs from frozen board plan')
    if slot_id not in templates:
        raise ValueError('native image slot is outside approved envelope')
    template = templates[slot_id]; native = _native_arguments(packet,arguments)
    prompt = template['prompt']; reference_ids = template['reference_ids']
    if {k:v for k,v in native.items() if k != 'referenced_image_paths'} != {
            'prompt':prompt,'transparent_background':template['transparent_background']}:
        raise ValueError('native image arguments differ from frozen template')
    expected_output = _ex()._inside(template['output_path'],root)
    if _ex()._inside(output_path,root) != expected_output:
        raise ValueError('native image output differs from frozen template')
    inventory = {source['id']:source for source in definition['source_inventory']}
    if ([ref['id'] for ref in packet['references']] != reference_ids
            or any(ref_id not in inventory for ref_id in reference_ids)
            or [ref_id for ref_id in reference_ids if ref_id in template['reference_ids']] != template['reference_ids']):
        raise ValueError('native image source order differs from inventory')
    calls, scopes = None, None
    for ref in packet['references']:
        source = inventory[ref['id']]
        if not source.get('source_slot_id'):
            if ref != source: raise ValueError('native image source differs from frozen inventory')
        else:
            if {k:ref.get(k) for k in ('id','role','applicability')} != {
                    k:source[k] for k in ('id','role','applicability')}:
                raise ValueError('native image source role differs from frozen inventory')
            if calls is None:
                calls = _load(root)['calls']; scopes = _read(root/'production_scopes.json')['scopes']
            producers = [call for call in calls if call['slot_id']==source['source_slot_id']
                and call['state']=='imported' and call.get('output',{}).get('sha256')==ref['sha256']
                and _ex()._inside(call['output']['path'],root)==_ex()._inside(ref['path'],root)]
            if not any(any(s['id']==call['scope_id'] and digest(s)==call['scope_sha256']
                    and s.get('derived_from_image_envelope',{}).get('envelope_sha256')==retained['envelope_sha256']
                    and s.get('requests',{}).get(call['slot_id'])==call['request_sha256']
                    for s in scopes) for call in producers):
                raise ValueError('dynamic source needs attributable imported approved producer output')
    paths = [str(_ex()._inside(ref['path'],root)) for ref in packet['references']]
    if native.get('referenced_image_paths',[]) != paths:
        raise ValueError('native image source arguments differ from frozen inventory')
    binding = {'envelope_sha256':retained['envelope_sha256'],'slot_id':slot_id,
               'replace_exception_call_id':None}
    return {'id':'native-envelope-'+digest(binding)[:32], 'phase':'image','status':'approved',
        'project_id':definition['project_id'],'story_revision':definition['story_revision'],
        'approved_by':retained['approval']['approved_by'],'evidence':copy.deepcopy(retained['approval']),
        'provider':NATIVE_ROUTE,'allowed_image_routes':copy.deepcopy(definition['allowed_image_routes']),
        'image_allowance':definition['image_allowance'],'attempts_per_shot':{slot_id:1},
        'requests':{slot_id:image_request_digest(packet,slot_id,arguments,output_path=output_path)},
        'derived_from_image_envelope':binding}


def _validate_derived_image_scope(root, scope, packet, slot_id, arguments, output_path):
    """Admit only first-generation envelope scopes. Retained historical
    exception-successor or correction scopes stay readable through _scope but
    never authorize another request."""
    if 'derived_from_image_envelope' not in scope: return
    retained = _image_envelope(root,_read(root/'production_scopes.json'))
    if scope != _derived_image_scope(root,retained,packet,slot_id,arguments,output_path):
        raise ValueError('derived native image scope differs from verified envelope')


def derive_native_image_scope(project_dir, packet, slot_id, arguments, *, output_path):
    """Retain the exact one-generation scope for a declared envelope image."""
    root, _ = _root(project_dir)
    with _ex()._lock(root):
        data = _read(root/'production_scopes.json'); retained = _image_envelope(root,data)
        _standing_native_transfer(root, retained, packet)
        scope = _derived_image_scope(root,retained,packet,slot_id,arguments,output_path)
        matches = [s for s in data['scopes'] if s['id']==scope['id']]
        if matches:
            if matches != [scope]: raise ValueError('derived native image scope changed or duplicated')
            return scope['id']
        _admission(root,scope,slot_id,NATIVE_ROUTE)
        data['scopes'].append(scope); _write_image_scopes(root,data)
        return scope['id']


def _grok_never_submitted(root, request):
    """Qualify the real image adapter's retained pre-dispatch refusal receipt.

    A bare caller label or missing session log never supplies this proof.
    """
    directory = root/'production_attempts'/request['attempt_id']
    try:
        result = _read(directory/'result.json')
        raw = _read(directory/'raw_result.json')
        data = raw.get('data') or {}
        receipt = data.get('conditioning_receipt') or {}
        if (request.get('tool_name') != 'grok_cli_image' or request.get('cli_session_id') != request['attempt_id']
                or result.get('status') != 'failed' or result.get('result') != raw
                or result.get('exception') is not None or result.get('output') is not None
                or raw.get('success') is not False or data.get('provider') != 'grok_cli'
                or data.get('dispatch_status') != 'not_dispatched'
                or data.get('session_id') != request['attempt_id']
                or receipt.get('version') != '1.0' or receipt.get('provider') != 'grok_cli'
                or receipt.get('session_id') != request['attempt_id']
                or receipt.get('dispatch_status') != 'not_dispatched'
                or (directory/'provider_request.json').exists() or (directory/'provider_result.json').exists()
                or (directory/'reconciliation.json').exists()):
            return False
        inputs = request['submitted_inputs']
        arguments = _ex()._grok_native_arguments(inputs,'image')
        assets = [{'role':'reference','index':index,'path':path,'sha256':file_sha256(path)}
                  for index,path in enumerate(arguments.get('image',[]))]
        if (receipt.get('native_tool') != _ex()._grok_native_operation(inputs,'image')
                or receipt.get('submitted_arguments') != arguments or receipt.get('input_assets') != assets):
            return False
        original = {k:v for k,v in receipt.items() if k not in {
            'request_sha256','cli_version','session_id','dispatch_status','submission_evidence'}}
        return receipt.get('request_sha256') == _ex()._digest(original)
    except (KeyError,ValueError,OSError,TypeError):
        return False


def image_usage(project_dir):
    """One cumulative image occurrence per native call or existing Grok session.

    Missing original result counts conservatively. No status/collection operation
    adds an occurrence; motion journals are deliberately excluded.
    """
    root, _ = _root(project_dir)
    occurrences = {}
    for call in _load(root)['calls']:
        occurrences[call['call_id']] = {'call_id':call['call_id'],'slot_id':call['slot_id'],
            'route':call['route'],'scope_id':call['scope_id'],'allowance_scope_id':call['scope_id'],
            'state':call['state'],'counted':call['state'] != 'never_submitted'}
    for path in sorted((root/'production_attempts').glob('*/request.json')):
        request = _read(path)
        if request.get('media_kind') != 'image':
            continue
        call_id = request['attempt_id']
        if call_id in occurrences:
            raise ValueError('image call ID collides with existing provider session')
        state = _ex()._state(root,request).get('status','uncertain')
        scope = request.get('scope') or {}
        # Only the qualified actual adapter refusal can release a generation
        # slot; generic failed/not_dispatched labels remain conservative.
        if state == 'generated': state = 'imported'
        if state not in {'imported','failed'}: state = 'uncertain'
        never_submitted = _grok_never_submitted(root,request)
        occurrences[call_id] = {'call_id':call_id,'slot_id':request['shot_id'],
            'route':scope.get('provider'),'scope_id':request['scope_id'],
            'allowance_scope_id':scope.get('image_allowance_scope_id'),
            'state':'never_submitted' if never_submitted else state,
            'counted':not never_submitted}
    rows = list(occurrences.values())
    return {'total':sum(row['counted'] for row in rows), 'occurrences':rows,
        'unresolved_originals':[row['call_id'] for row in rows if row['counted'] and row['state'] in PENDING],
        'excluded_never_submitted':[row['call_id'] for row in rows if not row['counted']]}


def image_call_status(project_dir, call_id):
    """Read native call history and current-byte availability; zero new calls."""
    root,_ = _root(project_dir)
    call = _call(_load(root),call_id)
    status = {key:copy.deepcopy(value) for key,value in call.items()
              if key not in {'packet','host_arguments','submitted_arguments'}}
    if call.get('host_exception') and call['state'] in PENDING:
        status['backend_submission_status'] = 'unknown'
        status['next_action'] = 'collect_original_or_authoritative_reconciliation'
    if call.get('output'):
        output = call['output']
        def current(path):
            path = _ex()._inside(path,root)
            return path.is_file() and file_sha256(path) == output['sha256']
        status['output_current'] = current(output['path'])
        status['preserved_output_valid'] = current(output['preserved_path'])
    return status


def _admission(root, scope, slot_id, route, *, exclude_call_id=None):
    limit = scope.get('image_allowance')
    if type(limit) is not int or limit < 1:
        raise ValueError('approved image scope lacks a positive image allowance')
    routes = scope.get('allowed_image_routes')
    if (not isinstance(routes,list) or not routes or set(routes)-{NATIVE_ROUTE,'grok_cli'}
            or route not in routes):
        raise ValueError('image route is outside the approved supported routes')
    rows = [r for r in image_usage(root)['occurrences'] if r['counted'] and r['call_id'] != exclude_call_id]
    binding = scope.get('derived_from_image_envelope')
    if binding and route == NATIVE_ROUTE and any(r['slot_id']==slot_id for r in rows):
        raise ValueError('native image envelope slot allowance exhausted')
    if any(r['slot_id'] == slot_id and r['state'] in PENDING for r in rows):
        raise ValueError('an uncertain or pending original image blocks replacement')
    # The ceiling is episode-wide. Historic supported image journals count too;
    # moving a request to another exact scope cannot reset cumulative usage.
    if len(rows) >= limit:
        raise ValueError('approved episode image allowance exhausted')
    return {'used':len(rows),'limit':limit,'remaining':limit-len(rows),'scope_id':scope['id']}


def check_grok_image_admission(project_dir, *, scope, shot_id, request_sha256):
    """Read-only shared ceiling check AFTER normal Grok scope/session preflight.

    Call again under production_execution's project lock before its reservation.
    No shared approval artifact means unchanged legacy bootstrap behavior.
    """
    root, _ = _root(project_dir)
    available = _read(root/'production_scopes.json').get('scopes',[])
    shared = [s for s in available if s.get('phase') == 'image' and 'image_allowance' in s]
    if not shared:
        return None
    shared_id = scope.get('image_allowance_scope_id')
    if not shared_id:
        raise ValueError('Grok image scope must bind the shared image allowance')
    current = _scope(root,scope.get('id'))
    if current != scope or scope.get('provider') != 'grok_cli':
        raise ValueError('Grok exact scope/provider authority differs')
    approved = scope.get('requests',{}).get(shot_id)
    if not isinstance(approved,list): approved = [approved]
    if request_sha256 not in approved:
        raise ValueError('Grok image request differs from exact scope authority')
    return _admission(root,_scope(root,shared_id),shot_id,'grok_cli')


def reserve_native_image(project_dir, packet, slot_id, arguments, *, output_path, scope_id, call_id=None):
    """Reserve the one exact approved native call for a declared image slot.

    A retained uncertain original stays counted and collectable; nothing here
    admits a linked successor or replacement request.
    """
    root, marker = _root(project_dir)
    call_id = call_id or 'image-' + uuid.uuid4().hex
    if not isinstance(call_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',call_id):
        raise ValueError('image call_id must be a safe local token')
    with _ex()._lock(root):
        _validate_packet(root,packet)
        matches = [s for s in packet['board_slots'] if s['id'] == slot_id and s['status'] == 'unresolved']
        if len(matches) != 1: raise ValueError('image request requires one explicit unresolved board slot')
        sha = image_request_digest(packet,slot_id,arguments,output_path=output_path)
        value = _load(root)
        existing = next((c for c in value['calls'] if c['call_id'] == call_id),None)
        if existing:
            if existing['request_sha256'] != sha or existing['scope_id'] != scope_id:
                raise ValueError('local call ID already binds another exact request')
            if existing['state'] == 'reserved':
                if existing.get('replaces_host_exception'):
                    raise ValueError('historical native exception successor is retained only; it is never dispatched')
                scope = _scope(root, scope_id)
                if digest(scope) != existing['scope_sha256']:
                    raise ValueError('native image approval changed after reservation')
                _validate_derived_image_scope(root,scope,packet,slot_id,arguments,output_path)
                if scope.get('derived_from_image_envelope'):
                    _standing_native_transfer(root, _image_envelope(root, _read(root / 'production_scopes.json')),
                        packet, exclude_call_id=call_id)
                _admission(root, scope, slot_id, NATIVE_ROUTE, exclude_call_id=call_id)
            return {k:copy.deepcopy(v) for k,v in existing.items() if k not in {'host_arguments','packet'}}
        if (root/'production_attempts'/call_id/'request.json').exists():
            raise ValueError('local image call ID collides with provider session')
        scope = _scope(root,scope_id)
        _validate_derived_image_scope(root,scope,packet,slot_id,arguments,output_path)
        if scope.get('derived_from_image_envelope'):
            _standing_native_transfer(root, _image_envelope(root, _read(root / 'production_scopes.json')), packet)
        if scope.get('provider') != NATIVE_ROUTE: raise ValueError('native scope locks a different provider route')
        admission = _admission(root,scope,slot_id,NATIVE_ROUTE)
        peers = [r for r in image_usage(root)['occurrences'] if r['counted'] and r['scope_id'] == scope_id and r['slot_id'] == slot_id]
        allowance = scope.get('attempts_per_shot',{}).get(slot_id)
        if type(allowance) is not int or allowance < 1 or len(peers) >= allowance:
            raise ValueError('native exact board-slot allowance exhausted or missing')
        approved = scope.get('requests',{}).get(slot_id)
        if isinstance(approved,list): approved = approved[len(peers)] if len(peers)<len(approved) else None
        if approved != sha: raise ValueError('native image request differs from exact approved request')
        call = {'call_id':call_id,'slot_id':slot_id,'route':NATIVE_ROUTE,'tool_name':NATIVE_TOOL,
            'state':'reserved','project_id':marker['project_id'],'story_revision':marker['story_revision'],
            'scope_id':scope_id,'scope_sha256':digest(scope),'scope_attempt_index':len(peers),
            'request_sha256':sha,'packet':copy.deepcopy(packet),
            'host_arguments':_native_arguments(packet,arguments),
            'output_path':str(_ex()._inside(output_path,root).relative_to(root)), 'events':[]}
        call['events'].append({'kind':'packet_ready','observed_at':packet['prepared_at'],'details':{'packet_sha256':packet['packet_sha256']}})
        _event(call,'reserved',used=admission['used'],limit=admission['limit'])
        value['calls'].append(call); _write(root,value)
        # Only begin grants tool invocation; reserve output does not expose args.
        return {k:copy.deepcopy(v) for k,v in call.items() if k not in {'host_arguments','packet'}}


def begin_native_image(project_dir, call_id):
    """Emit one real host-call envelope. Re-entry never emits another invocation."""
    root,_ = _root(project_dir)
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['state'] != 'reserved' or 'submitted_arguments' in call:
            raise ValueError('native image invocation envelope already emitted or call closed')
        _validate_packet(root,call['packet'])
        scope = _scope(root,call['scope_id'])
        if digest(scope) != call['scope_sha256']:
            raise ValueError('native image approval changed after reservation')
        if scope.get('derived_from_image_envelope'):
            retained = _image_envelope(root, _read(root / 'production_scopes.json'))
            _standing_native_transfer(root, retained, call['packet'], exclude_call_id=call_id)
        if call.get('replaces_host_exception'):
            raise ValueError('historical native exception successor is retained only; it is never dispatched')
        _validate_derived_image_scope(root,scope,call['packet'],call['slot_id'],call['host_arguments'],call['output_path'])
        _admission(root,scope,call['slot_id'],NATIVE_ROUTE,exclude_call_id=call_id)
        arguments = copy.deepcopy(call['host_arguments'])
        snapshots = root/'production_images'/call_id/'inputs'
        if arguments.get('referenced_image_paths'):
            snapshots.mkdir(parents=True,exist_ok=True)
            paths = []
            for source in arguments['referenced_image_paths']:
                source = Path(source); original = source.read_bytes()
                sha = hashlib.sha256(original).hexdigest()
                expected = next(r['sha256'] for r in call['packet']['references'] if _ex()._inside(r['path'],root)==source)
                if sha != expected: raise ValueError('reference bytes changed during host handoff')
                target = snapshots/(sha+source.suffix)
                if not target.exists(): target.write_bytes(original); target.chmod(0o444)
                if file_sha256(target) != sha: raise ValueError('immutable image reference snapshot changed')
                paths.append(str(target))
            arguments['referenced_image_paths'] = paths
        call['submitted_arguments'] = arguments
        call['state'] = 'submitted'; _event(call,'submitted',arguments_sha256=digest(arguments))
        _write(root,value)
        return {'call_id':call_id,'tool_name':NATIVE_TOOL,'arguments':copy.deepcopy(arguments),
                'arguments_sha256':digest(arguments)}


def mark_image_uncertain(project_dir, call_id, *, reason):
    root,_ = _root(project_dir)
    if not isinstance(reason,str) or not reason.strip(): raise ValueError('uncertainty requires a reason')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['state'] not in PENDING: raise ValueError('terminal image call cannot become uncertain')
        if call['state'] != 'uncertain':
            call['state'] = 'uncertain'; _event(call,'uncertain',reason=reason); _write(root,value)
        return copy.deepcopy(call)


def record_native_image_host_exception(project_dir, call_id, *, host_exception):
    """Retain a raised host exception without manufacturing a tool result.

    Agent-recorded JSON must bind {call_id, tool_name, arguments, exception:
    {message, optional type}, optional tool_call_id} to the emitted envelope.
    This proves the observed exception, never backend acceptance or termination.
    The original stays counted and blocks ordinary replacement. One explicit
    linked native successor may use reserve_native_image's exception recovery;
    late authentic returns remain collectable with import_native_image.
    Python never invokes the host.
    """
    root,_ = _root(project_dir)
    path = _bound_file(root,host_exception,'host exception')
    raw = path.read_bytes(); receipt = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != host_exception['sha256']:
        raise ValueError('host exception bytes changed during recording')
    allowed = {'call_id','tool_name','arguments','exception','tool_call_id'}
    if not isinstance(receipt,dict) or set(receipt) - allowed:
        raise ValueError('host exception requires observed exception evidence, not a result or terminal assertion')
    exception = receipt.get('exception')
    if (not isinstance(exception,dict) or set(exception) - {'message','type'}
            or not isinstance(exception.get('message'),str) or not exception['message'].strip()
            or ('type' in exception and (not isinstance(exception['type'],str) or not exception['type'].strip()))
            or ('tool_call_id' in receipt and (not isinstance(receipt['tool_call_id'],str)
                                               or not receipt['tool_call_id'].strip()))):
        raise ValueError('host exception requires a nonempty actual exception message and optional type/call ID')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if 'submitted_arguments' not in call:
            raise ValueError('host exception requires the original emitted invocation envelope')
        if (receipt.get('call_id') != call_id or receipt.get('tool_name') != NATIVE_TOOL
                or receipt.get('arguments') != call['submitted_arguments']):
            raise ValueError('host exception call, tool or exact submitted arguments differ')
        if call.get('host_exception'):
            if call['host_exception']['sha256'] != host_exception['sha256']:
                raise ValueError('image call already binds a different actual host exception')
            _bound_file(root,call['host_exception'],'retained host exception')
            return copy.deepcopy(call)
        if call.get('host_receipt'):
            raise ValueError('actual host return already retained; host exception cannot be recorded')
        if call['state'] not in PENDING:
            raise ValueError('terminal image call cannot record a new host exception')
        directory = root/'production_images'/call_id; directory.mkdir(parents=True,exist_ok=True)
        preserved = directory/'host-exception.json'
        if preserved.exists() and preserved.read_bytes() != raw:
            raise ValueError('retained original host exception differs')
        if not preserved.exists(): preserved.write_bytes(raw); preserved.chmod(0o444)
        call['host_exception'] = {'kind':'agent_recorded_host_exception',
            'path':str(preserved.relative_to(root)), 'sha256':host_exception['sha256'],
            'arguments_sha256':digest(call['submitted_arguments'])}
        _event(call,'host_exception',receipt_sha256=host_exception['sha256'],error=exception['message'])
        call['state'] = 'uncertain'
        _event(call,'uncertain',reason='Host raised an exception without a result; backend submission remains unknown.')
        _write(root,value)
        return copy.deepcopy(call)


OPENART_IMAGE_TOOL = 'mcp__codex_apps__openart_openart_generate_image'
# Positive refusal phrases only; infrastructure wording never qualifies.
_POLICY_DENIAL = ('content_policy_violation', 'content policy violation', 'violates our content policy',
                  'violated our content policy', 'did not follow our content policy',
                  'does not comply with our content policy', 'rejected by our content policy',
                  'blocked by our content policy', 'rejected by the safety system', 'rejected by our safety system')
_NOT_POLICY = ('403', 'forbidden', 'timeout', 'timed out', 'unavailable', 'access denied')


def _refusal_text(value):
    """Only the actual error/refusal fields: code, error, message, type and tool
    text blocks. Echoed arguments, prompts and metadata never qualify."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return _refusal_text(parsed) if isinstance(parsed, dict) else [value]
    if not isinstance(value, dict):
        return []
    texts = []
    for key in ('code', 'error', 'message', 'type'):
        field = value.get(key)
        if isinstance(field, (str, dict)):
            texts += _refusal_text(field)
    content = value.get('content')
    if isinstance(content, str):
        texts += _refusal_text(content)
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str):
                texts += _refusal_text(b['text'])
    return texts


def _policy_denial(root, call):
    """Retained receipt sha only for an explicit native content-policy refusal.

    Reads the original's retained isError return or raised host exception.
    Access/403/timeout/unavailable/unknown failures never qualify.
    """
    if call['route'] != NATIVE_ROUTE or call.get('output'):
        return None
    if call['state'] == 'failed' and call.get('host_receipt'):
        evidence = call['host_receipt']
        texts = _refusal_text(_read(_bound_file(root, evidence, 'retained host return')).get('result'))
    elif call['state'] == 'uncertain' and call.get('host_exception') and not call.get('host_receipt'):
        evidence = call['host_exception']
        texts = _refusal_text(_read(_bound_file(root, evidence, 'retained host exception')).get('exception'))
    else:
        return None
    text = '\n'.join(texts).lower()
    if not any(term in text for term in _POLICY_DENIAL) or any(term in text for term in _NOT_POLICY):
        return None
    return evidence['sha256']


def prepare_policy_fallback_image(project_dir, call_id, *, account):
    """One OpenArt image envelope for a native explicit content-policy denial.

    Requires this run command's explicit ``image_policy_fallback`` term (model
    plus its retained current forms), the standing OpenArt MCP account/project,
    the same native prompt and remaining episode image allowance. Reference
    boards use image2image with the same source files, transferred through the
    existing standing upload authority and receipts: until each source has one
    retained receipt this returns ``upload_required`` and records nothing. The
    denied original stays retained and counted; the replacement counts once and
    imports through import_native_image.
    """
    from lib import production_autonomy as autonomy, production_boards as boards
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs
    root, marker = _root(project_dir)
    with _ex()._lock(root):
        standing = autonomy.standing_image_policy_fallback(root)
        if standing is None:
            raise ValueError('this run command grants no OpenArt image policy fallback')
        term, spec = standing
        if not isinstance(account, dict) or account.get('uid_sha256') != spec['uid_sha256']:
            raise ValueError('OpenArt account differs from the standing setup')
        value = _load(root); original = _call(value, call_id)
        if any((c.get('replaces_policy_denial') or {}).get('call_id') == call_id for c in value['calls']):
            raise ValueError('policy-denied image already has its one fallback')
        receipt_sha = _policy_denial(root, original)
        if receipt_sha is None:
            raise ValueError('only an explicit native content-policy denial admits the OpenArt image fallback')
        # The denied original must still be the current exact request.
        _validate_packet(root, original['packet'])
        scope = _scope(root, original['scope_id'])
        if (digest(scope) != original['scope_sha256']
                or original['slot_id'] not in boards.load_board_pack(root)['prompt_pack']
                or image_request_digest(original['packet'], original['slot_id'], original['host_arguments'],
                    output_path=original['output_path']) != original['request_sha256']
                or original['submitted_arguments'].get('prompt') != original['host_arguments'].get('prompt')):
            raise ValueError('policy-denied original no longer matches its retained current request')
        limit = scope.get('image_allowance')
        used = image_usage(root)['total']
        if type(limit) is not int or used >= limit:
            raise ValueError('approved episode image allowance exhausted')
        sources = [str(_ex()._inside(p, root)) for p in original['host_arguments'].get('referenced_image_paths', [])]
        mode = 'image2image' if sources else 'text2image'
        form = term['forms'].get(mode)
        if form is None:
            raise ValueError('run fallback term retains no current OpenArt ' + mode + ' form')
        params = {**copy.deepcopy(term.get('params', {})), 'prompt': original['host_arguments']['prompt']}
        # The user approved just that one image: never a multi-image request.
        if params.get('imageCount', 1) != 1:
            raise ValueError('policy fallback generates exactly one image (imageCount 1)')
        if 'imageCount' in form.get('properties', {}):
            params['imageCount'] = 1
        receipts = []
        if sources:
            refs, missing = [], []
            for source in sources:
                try:
                    bound = jobs.resolve_input_asset(root, {'source_path': source},
                        account_binding={'uid_sha256': spec['uid_sha256']}, openart_project_id=spec['project_id'],
                        policy_fallback_call_id=call_id)
                except mcp.OpenArtMCPError as exc:
                    if exc.kind != 'upload_reference_unavailable':
                        raise ValueError('reference source transfer invalid: ' + exc.message) from exc
                    missing.append(source); continue
                # Only the bound reference identity; auxiliary thumbnails stay on the receipt.
                refs.append(mcp._project_reference({k: bound['visual_reference'][k] for k in
                    ('type', 'id', 'url', 'label') if k in bound['visual_reference']},
                    form['properties']['visualReferences']['items']))
                receipts.append(bound['receipt_sha256'])
            if missing:
                return {'upload_required': {'shot_id': boards.load_board_pack(root)['prompt_pack'][original['slot_id']]['shot_id'],
                    'provider': 'openart_mcp', 'files': missing, 'account': {'uid_sha256': spec['uid_sha256']},
                    'openart_project_id': spec['project_id'], 'policy_fallback_call_id': call_id}, 'provider_calls': 0}
            params['visualReferences'] = refs
        errors = list(mcp._validator(form).iter_errors(params))
        if errors:
            raise ValueError('fallback params violate the retained current form: ' + errors[0].message)
        arguments = {'model': term['model'], 'mode': mode, 'projectId': spec['project_id'], 'params': params}
        call = {'call_id': 'openart-' + uuid.uuid4().hex, 'slot_id': original['slot_id'],
            'route': 'openart_mcp', 'tool_name': OPENART_IMAGE_TOOL, 'state': 'submitted',
            'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
            **{k: copy.deepcopy(original[k]) for k in ('scope_id', 'scope_sha256', 'scope_attempt_index',
                'request_sha256', 'packet', 'host_arguments')},
            # Distinct file: a late authentic original keeps its own bytes.
            'output_path': str(Path(original['output_path']).with_name(
                Path(original['output_path']).stem + '-policy-fallback' + Path(original['output_path']).suffix)),
            'submitted_arguments': arguments,
            'replaces_policy_denial': {'call_id': call_id, 'receipt_sha256': receipt_sha,
                'form_sha256': digest(form), 'upload_receipt_sha256s': receipts},
            'events': []}
        _event(call, 'reserved', used=used, limit=limit)
        _event(call, 'submitted', arguments_sha256=digest(arguments))
        value['calls'].append(call); _write(root, value)
        return {'call_id': call['call_id'], 'tool_name': OPENART_IMAGE_TOOL,
                'arguments': copy.deepcopy(arguments), 'arguments_sha256': digest(arguments)}


def release_unsubmitted_image(project_dir, call_id, *, reason):
    """Close only a reservation whose canonical host envelope was never emitted.

    After begin, absence of a receipt is uncertainty, never no-submission proof.
    """
    root,_ = _root(project_dir)
    if not isinstance(reason,str) or not reason.strip(): raise ValueError('release requires a reason')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['state'] != 'reserved' or 'submitted_arguments' in call:
            raise ValueError('cannot prove never submitted after host envelope emission')
        call['state'] = 'never_submitted'
        call['never_submitted_proof'] = {'kind':'canonical_envelope_not_emitted','request_sha256':call['request_sha256'],'reason':reason}
        _event(call,'never_submitted',reason=reason); _write(root,value)
        return copy.deepcopy(call)


def _original_image(result):
    """Qualified single-image returns: data URI or explicit local artifact path.

    No prose parsing, URL download, caller-selected source or image re-encoding.
    """
    if not isinstance(result,dict): raise ValueError('host result must be retained structured return evidence')
    uri = result.get('image_url')
    if isinstance(uri,str) and uri.startswith('data:image/'):
        header, payload = uri.split(',',1)
        if ';base64' not in header: raise ValueError('host image data URI is not base64')
        original = base64.b64decode(payload,validate=True)
        source = {'kind':'data_uri','field':'image_url'}
    else:
        candidates = [(key,result[key]) for key in ('image_path','output_path','path')
                      if isinstance(result.get(key),str) and result[key]]
        if len(candidates)!=1: raise ValueError('host result lacks one attributable original local image artifact or data URI')
        field,path = candidates[0]; path = Path(path).expanduser()
        if not path.is_absolute() or not path.is_file(): raise ValueError('returned original image path is not a readable absolute local artifact')
        original = path.read_bytes(); source = {'kind':'local_artifact','field':field,'path':str(path)}
    from PIL import Image
    with Image.open(io.BytesIO(original)) as image:
        media_type = Image.MIME.get(image.format)
        image.verify()
    if media_type not in {'image/png','image/jpeg','image/webp','image/gif'}:
        raise ValueError('host return is not a supported original image')
    return original,source,media_type


def import_native_image(project_dir, call_id, *, host_receipt, supplemental_to=None):
    """Import original bytes from retained actual tool return, once.

    host_receipt accepts a bound {path, sha256} pointer or inline observed JSON
    {tool_name, arguments, result, optional tool_call_id}. Agent
    records the host event as observed; provenance is honestly agent_recorded,
    not provider-attested. The source path must occur inside that actual return.
    supplemental_to links an additive complete return to an unresolved frozen
    receipt SHA, preserving the original journal, provenance and call count.
    """
    root,_ = _root(project_dir)
    def canonical_json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    inline = isinstance(host_receipt, dict) and bool(set(host_receipt) & {'tool_name', 'arguments', 'result', 'tool_call_id'})
    if inline:
        if (not {'tool_name', 'arguments', 'result'} <= set(host_receipt)
                or set(host_receipt) - {'tool_name', 'arguments', 'result', 'tool_call_id'}):
            raise ValueError('inline host return requires exact observed tool_name, arguments and result')
        if (not isinstance(call_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', call_id)
                or ('tool_call_id' in host_receipt and (not isinstance(host_receipt['tool_call_id'], str)
                    or not host_receipt['tool_call_id'].strip()))):
            raise ValueError('inline host return requires a safe original call ID and observed tool call ID')
        receipt = copy.deepcopy(host_receipt)
        try:
            raw = canonical_json(receipt)
        except (TypeError, ValueError) as exc:
            raise ValueError('inline host return must be JSON observation data') from exc
        # Retention below owns this same canonical receipt path for both forms.
        name = 'host-return-supplement.json' if supplemental_to is not None else 'host-return.json'
        host_receipt = {'path': 'production_images/' + call_id + '/' + name,
                        'sha256': hashlib.sha256(raw).hexdigest()}
    else:
        receipt_path = _bound_file(root,host_receipt,'host return')
        raw = receipt_path.read_bytes(); receipt = json.loads(raw)
        if hashlib.sha256(raw).hexdigest() != host_receipt['sha256']:
            raise ValueError('host return bytes changed during import')
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if ('submitted_arguments' not in call
                or receipt.get('tool_name') != call['tool_name']
                or canonical_json(receipt.get('arguments')) != canonical_json(call['submitted_arguments'])):
            raise ValueError('host return tool or exact submitted arguments differ')
        if inline:
            retained_path = _ex()._inside(host_receipt['path'], root)
            retained = call.get('supplemental_host_receipt' if supplemental_to is not None else 'host_receipt')
            if retained:
                _bound_file(root, retained, 'retained host return')
            # An inline observation can replay a pointer receipt with different
            # JSON whitespace. Keep the exact original retained bytes and SHA.
            if retained_path.exists():
                retained_raw = retained_path.read_bytes()
                retained_canonical = canonical_json(json.loads(retained_raw))
                if retained_canonical == raw:
                    raw = retained_raw
                    host_receipt['sha256'] = hashlib.sha256(raw).hexdigest()
        if call['state'] in {'imported','failed'}:
            active = call.get('supplemental_host_receipt', call.get('host_receipt',{}))
            if supplemental_to is not None and active.get('original_receipt_sha256') != supplemental_to:
                raise ValueError('supplement requires the exact unresolved original host receipt')
            if active.get('sha256') != host_receipt['sha256']:
                raise ValueError('image call already binds a different actual return')
            _bound_file(root,call['host_receipt'],'retained host return')
            if call.get('supplemental_host_receipt'):
                _bound_file(root,call['supplemental_host_receipt'],'retained supplemental host return')
            if call['state'] == 'imported':
                _bound_file(root,call['output'],'imported image')
                _bound_file(root,{'path':call['output']['preserved_path'],'sha256':call['output']['sha256']},'preserved image')
            return copy.deepcopy(call)
        result = receipt.get('result')
        directory = root/'production_images'/call_id; directory.mkdir(parents=True,exist_ok=True)
        if supplemental_to is not None:
            prior_binding = call.get('host_receipt')
            existing_supplement = call.get('supplemental_host_receipt')
            if (call['state'] not in {'uncertain','returned'} or not prior_binding
                    or prior_binding['sha256'] != supplemental_to
                    or (existing_supplement and existing_supplement['sha256'] != host_receipt['sha256'])):
                raise ValueError('supplement requires the exact unresolved original host receipt')
            prior = _read(_bound_file(root,prior_binding,'retained original host return'))
            prior_result = prior.get('result')
            if (not isinstance(prior_result,dict) or not isinstance(result,dict)
                    or prior_result.get('isError') is True or result.get('isError') is True
                    or any(key not in result or canonical_json(result[key]) != canonical_json(value)
                           for key,value in prior_result.items())
                    or prior.get('tool_name') != receipt.get('tool_name')
                    or canonical_json(prior.get('arguments')) != canonical_json(receipt.get('arguments'))
                    or prior.get('tool_call_id') != receipt.get('tool_call_id')):
                raise ValueError('supplement must extend the same actual host return without changing retained fields')
            try:
                _original_image(prior_result)
            except (ValueError,OSError,SyntaxError):
                pass
            else:
                raise ValueError('original host return already binds usable image bytes')
            original, _, _ = _original_image(result)
            target = _ex()._inside(call['output_path'],root)
            for output in (target, directory/('output'+target.suffix)):
                if output.exists() and output.read_bytes() != original:
                    raise ValueError('image import would overwrite different retained bytes')
            preserved_receipt = directory/'host-return-supplement.json'
            if preserved_receipt.exists() and preserved_receipt.read_bytes() != raw:
                raise ValueError('retained supplemental host return differs')
            if not preserved_receipt.exists():
                preserved_receipt.write_bytes(raw); preserved_receipt.chmod(0o444)
            call['supplemental_host_receipt'] = {
                'path':str(preserved_receipt.relative_to(root)), 'sha256':host_receipt['sha256'],
                'original_receipt_sha256':supplemental_to}
            if not existing_supplement:
                _event(call,'supplemental_tool_return',receipt_sha256=host_receipt['sha256'])
        else:
            preserved_receipt = directory/'host-return.json'
            if preserved_receipt.exists() and preserved_receipt.read_bytes() != raw:
                raise ValueError('retained original host return differs')
            if not preserved_receipt.exists(): preserved_receipt.write_bytes(raw); preserved_receipt.chmod(0o444)
            call['host_receipt'] = {'path':str(preserved_receipt.relative_to(root)),'sha256':host_receipt['sha256']}
            call['provenance'] = {'kind':'agent_recorded_host_return','tool_name':call['tool_name'],
                'arguments_sha256':digest(call['submitted_arguments']), 'receipt_sha256':host_receipt['sha256']}
            if receipt.get('tool_call_id'):
                call['provenance']['host_tool_call_id'] = receipt['tool_call_id']
        if not any(event['kind'] == 'tool_return' for event in call['events']):
            _event(call,'tool_return',receipt_sha256=host_receipt['sha256'])
        call['state'] = 'returned'; _write(root,value)
        if isinstance(result,dict) and result.get('isError') is True:
            detail = result.get('error') or result.get('content') or 'Native host returned isError=true.'
            call['state'] = 'failed'
            call['error'] = detail if isinstance(detail,str) else json.dumps(detail,ensure_ascii=False)
            _event(call,'failed',error=call['error']); _write(root,value)
            return copy.deepcopy(call)
        if call['route'] == 'openart_mcp' and isinstance(result,dict) and isinstance(result.get('historyId'),str) \
                and result['historyId'].strip() and result.get('status') in {'PENDING','RUNNING'}:
            # Asynchronous creation: stays counted and returned until the same
            # historyId is collected by record_openart_image_collection.
            return copy.deepcopy(call)
        try:
            original,source,media_type = _original_image(result)
        except (ValueError,OSError,SyntaxError) as exc:
            call['state'] = 'uncertain'
            _event(call,'uncertain',reason='Actual return could not bind usable original image bytes: '+str(exc))
            _write(root,value)
            raise ValueError('actual image return is unresolved: '+str(exc)) from exc
        sha = hashlib.sha256(original).hexdigest()
        target = _ex()._inside(call['output_path'],root)
        preserved = directory/('output'+target.suffix)
        for path in (preserved,target):
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and path.read_bytes() != original:
                raise ValueError('image import would overwrite different retained bytes')
            if not path.exists(): path.write_bytes(original)
            if file_sha256(path) != sha: raise ValueError('original image bytes changed during import')
        preserved.chmod(0o444)
        call['output'] = {'path':str(target.relative_to(root)),'preserved_path':str(preserved.relative_to(root)),
            'sha256':sha,'media_type':media_type,'source':source}
        call['state'] = 'imported'; _event(call,'imported',output_sha256=sha); _write(root,value)
        return copy.deepcopy(call)


OPENART_STATUS_TOOLS = ('mcp__codex_apps__openart_openart_creation_get', 'mcp__codex_apps__openart_openart_creation_wait')


def record_openart_image_collection(project_dir, call_id, *, status_receipt, download=None):
    """Import a policy fallback's bytes from the SAME original OpenArt history.

    status_receipt is the actual observed {tool_name, arguments, result,
    optional tool_call_id} of creation_get/creation_wait for the original
    generate_image historyId; download is {path, sha256, resource_id, url} for
    the file fetched from that terminal resource. The original generate_image
    receipt stays the call's host_receipt; nothing is regenerated.
    """
    root,_ = _root(project_dir)
    if (not isinstance(status_receipt,dict) or not {'tool_name','arguments','result'} <= set(status_receipt)
            or set(status_receipt) - {'tool_name','arguments','result','tool_call_id'}
            or status_receipt['tool_name'] not in OPENART_STATUS_TOOLS):
        raise ValueError('collection requires the actual OpenArt creation status receipt')
    if download is not None and (not isinstance(download,dict) or set(download) != {'path','sha256','resource_id','url'}):
        raise ValueError('collection requires an exact download receipt')
    raw = json.dumps(status_receipt, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False).encode()
    with _ex()._lock(root):
        value = _load(root); call = _call(value,call_id)
        if call['route'] != 'openart_mcp' or not call.get('host_receipt'):
            raise ValueError('collection applies only to a returned OpenArt policy fallback')
        if call['state'] in {'imported','failed'} and call.get('collection'):
            bound = call['collection']
            if (bound['status_receipt']['sha256'] != hashlib.sha256(raw).hexdigest()
                    or (call['state'] == 'failed') != (download is None)
                    or (call['state'] == 'imported' and (bound.get('resource_id'),bound.get('url'),
                        bound.get('download_path'),bound.get('download_sha256')) != (download['resource_id'],
                        download['url'],str(Path(download['path']).expanduser()),download['sha256']))):
                raise ValueError('image call already binds a different collection')
            _bound_file(root,call['host_receipt'],'retained generate_image return')
            _bound_file(root,bound['status_receipt'],'retained collection status')
            if call['state'] == 'imported':
                for key in ('path','preserved_path'):
                    _bound_file(root,{'path':call['output'][key],'sha256':call['output']['sha256']},'imported image')
            return copy.deepcopy(call)
        if call['state'] != 'returned':
            raise ValueError('collection requires a returned pending OpenArt image call')
        history = _read(_bound_file(root,call['host_receipt'],'retained generate_image return')).get('result') or {}
        history_id = history.get('historyId')
        result = status_receipt['result']
        if (not isinstance(history_id,str) or not history_id.strip()
                or status_receipt['arguments'] != {'historyId':history_id}
                or not isinstance(result,dict) or result.get('historyId') != history_id):
            raise ValueError('collection status must be for the exact original historyId')
        directory = root/'production_images'/call_id
        status_path = directory/'collection-status.json'
        if status_path.exists() and status_path.read_bytes() != raw:
            raise ValueError('retained collection status differs')
        receipt_sha = hashlib.sha256(raw).hexdigest()
        if result.get('status') in {'FAILED','CANCELLED','CANCELED'}:
            if download is not None:
                raise ValueError('terminal failed creation has no downloadable resource')
            # Terminal provider failure: the image stays consumed; never retried.
            if not status_path.exists(): status_path.write_bytes(raw); status_path.chmod(0o444)
            call['collection'] = {'status_receipt':{'path':str(status_path.relative_to(root)),'sha256':receipt_sha},
                'history_id':history_id}
            if status_receipt.get('tool_call_id'):
                call['collection']['status_tool_call_id'] = status_receipt['tool_call_id']
            _event(call,'collection_status',receipt_sha256=receipt_sha)
            call['state'] = 'failed'; call['error'] = 'OpenArt image creation ' + result['status']
            _event(call,'failed',error=call['error']); _write(root,value)
            return copy.deepcopy(call)
        if result.get('status') != 'COMPLETED' or result.get('warning'):
            raise ValueError('original OpenArt image creation is not COMPLETED')
        if download is None:
            raise ValueError('collection requires an exact download receipt')
        rows = result.get('resources')
        images = [r for r in rows if isinstance(r,dict) and r.get('mediaType') == 'image'] if isinstance(rows,list) else []
        if (not isinstance(rows,list) or len(rows) != 1 or len(images) != 1
                or str(images[0].get('status','COMPLETED')).upper() not in {'COMPLETED','SUCCESS','SUCCEEDED'}
                or images[0].get('id') != download['resource_id'] or not images[0].get('url')
                or images[0]['url'] != download['url']):
            raise ValueError('terminal status must hold exactly the one downloaded image resource')
        path = Path(download['path']).expanduser()
        if not path.is_absolute() or not path.is_file() or file_sha256(path) != download['sha256']:
            raise ValueError('downloaded image bytes differ from the download receipt')
        original = path.read_bytes()
        from PIL import Image
        with Image.open(io.BytesIO(original)) as image:
            media_type = Image.MIME.get(image.format); image.verify()
        if media_type not in {'image/png','image/jpeg','image/webp','image/gif'}:
            raise ValueError('downloaded resource is not a supported image')
        target = _ex()._inside(call['output_path'],root); preserved = directory/('output'+target.suffix)
        for out in (preserved,target):
            if out.exists() and out.read_bytes() != original:
                raise ValueError('image import would overwrite different retained bytes')
        if not status_path.exists(): status_path.write_bytes(raw); status_path.chmod(0o444)
        for out in (preserved,target):
            out.parent.mkdir(parents=True,exist_ok=True)
            if not out.exists(): out.write_bytes(original)
        preserved.chmod(0o444)
        call['collection'] = {'status_receipt':{'path':str(status_path.relative_to(root)),'sha256':receipt_sha},
            'history_id':history_id, 'resource_id':download['resource_id'], 'url':download['url'],
            'download_path':str(path), 'download_sha256':download['sha256']}
        if status_receipt.get('tool_call_id'):
            call['collection']['status_tool_call_id'] = status_receipt['tool_call_id']
        _event(call,'collection_status',receipt_sha256=receipt_sha)
        call['output'] = {'path':str(target.relative_to(root)),'preserved_path':str(preserved.relative_to(root)),
            'sha256':download['sha256'],'media_type':media_type,
            'source':{'kind':'openart_collection','field':'resources','path':str(path)}}
        call['state'] = 'imported'; _event(call,'imported',output_sha256=download['sha256']); _write(root,value)
        return copy.deepcopy(call)
