"""Fail-closed, factual production dispatch shared by every BaseTool.

Strict enrollment is explicit in project.json. Legacy diagnostics are untouched.
Approval sidecar production_scopes.json contains version=1.0 and scopes with:
{id, status:approved, approved_by, evidence:{path,sha256}, project_id,
story_revision, phase:first_pass|repair|image|audio, provider, requests:{shot_id:
request_digest}, attempts_per_shot:{shot_id:int}, approval_plan_sha256 (motion),
replaces_attempt_ids (repair)}. An approval is evidence, never a spending ceiling.

Call inputs carry governance={scope_id,shot_id}; planned_request_digest computes
a concrete approval binding. planned_request_template freezes static inputs while
allowing explicit $upstream handoffs. A shot request can also be an ordered list;
reservations record its zero-based scope_attempt_index. No reservation is made by
preflight/dry-run.
Journals preserve all results, including failed and uncertain original sessions.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

from lib.shot_contract import contract_digest, file_sha256, validate_shot_contract

GOVERNANCE_KEYS = {'governance', 'project_dir', 'shot_id', 'scope_id', 'production_phase', 'shot_contract_path', 'compiled_request_id', 'preparation_review_id', 'credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256', 'unknown_cost_authorization_id', 'unknown_cost_evidence_id'}
INPUT_PATH_KEYS = {'image', 'image_path', 'image_paths', 'reference_image_path', 'reference_image_paths',
                   'first_frame', 'last_frame', 'last_image_path', 'images', 'audio_path',
                   'reference_audio_path', 'reference_audio_paths', 'video_path', 'reference_video_path',
                   'reference_video_paths', 'end_image_path', 'image_url', 'reference_image_url',
                   'last_image_url', 'end_image_url', 'video_url', 'reference_video_url',
                   'reference_image_urls', 'reference_audio_urls', 'reference_video_urls',
                   'reference_images', 'reference_videos', 'reference_audios', 'url', 'workspace_path'}
# Native media inputs exposed by the October provider refresh (SchemaMedia/FalMedia
# contracts). They must be hashed/frozen like the canonical keys and URL forms must
# fail closed. Boolean controls such as ``audio``/``generate_audio`` are not assets.
INPUT_PATH_KEYS |= {'image_urls', 'image_input', 'image_uri', 'last_image', 'last_frame_uri',
                    'start_image_url', 'middle_image_url', 'mask', 'mask_path', 'mask_url',
                    'audio_uri', 'audio_url', 'target_audio_url', 'video_uri', 'file', 'file_url',
                    'web_url', 'link'}
# Of those, only ``mask_path`` is encoded from local bytes by the adapters
# (SchemaMedia/FalMedia ``local_image``). The rest are forwarded verbatim, so a
# strict snapshot path would reach a paid route as a raw filesystem string and a
# URL is mutable. Strict production fails closed on them; callers use canonical
# image_path/image_paths/last_image_path/mask_path fields instead.
STRICT_UNENCODED_NATIVE_KEYS = {'image_urls', 'image_input', 'image_uri', 'last_image',
                                'last_frame_uri', 'start_image_url', 'middle_image_url', 'mask',
                                'mask_url', 'audio_uri', 'audio_url', 'target_audio_url',
                                'video_uri', 'file', 'file_url', 'web_url', 'link'}
_ACTIVE = contextvars.ContextVar('production_execution', default=None)
_GROK_COMPATIBILITY = contextvars.ContextVar('production_grok_compatibility', default=None)
_MCP_REVALIDATING = contextvars.ContextVar('production_mcp_revalidating', default=None)


class ProductionGovernanceError(ValueError):
    """An objective prerequisite failed; execution must not continue."""


def _fail(message):
    raise ProductionGovernanceError('production governance: ' + message)


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        _fail(f'cannot read {path}: {exc}')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _write_new(path, value):
    """Atomically publish immutable evidence, never replacing an existing record."""
    import os
    path = Path(path)
    temp = path.with_name('.' + path.name + '-' + str(uuid.uuid4()) + '.tmp')
    try:
        with temp.open('x') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)  # exclusive atomic publication; replace would overwrite
    finally:
        temp.unlink(missing_ok=True)
    path.chmod(0o444)


def _inside(path, root):
    path = Path(path).expanduser()
    resolved = (root / path if not path.is_absolute() else path).resolve()
    if not resolved.is_relative_to(root):
        _fail(f'path escapes project: {path}')
    return resolved


def discover_project(inputs):
    """Resolve explicit context or nearest project marker, including custom roots."""
    explicit = inputs.get('project_dir')
    root = Path(explicit).expanduser().resolve() if explicit else None
    if root is not None and not (root / 'project.json').is_file():
        _fail('explicit project_dir is unknown (missing project.json)')
    output = inputs.get('output_path')
    inferred = None
    if output:
        path = Path(output).expanduser()
        path = root / path if root and not path.is_absolute() else path.absolute()
        # Inspect lexical ancestry too: resolving an escaping symlink first must
        # never erase the strict project context and downgrade to legacy.
        for candidate in (path, path.resolve()):
            for parent in candidate.parents:
                if (parent / 'project.json').is_file():
                    discovered = parent.resolve()
                    if inferred and inferred != discovered:
                        _fail('conflicting output project ancestry')
                    inferred = discovered
                    break
        path = path.resolve()
        if inferred:
            _inside(path, inferred)
        if root and inferred and root != inferred:
            _fail('conflicting project context and output project')
        if root:
            _inside(path, root)
    root = root or inferred
    if root is None and inputs.get('governance') is not None:
        _fail('production request has no known project')
    return root


def _artifact_path(root, name):
    canonical, legacy = root / 'artifacts' / name, root / name
    if canonical.exists() and legacy.exists() and _read(canonical) != _read(legacy):
        _fail(f'conflicting canonical and legacy {name}')
    return canonical if canonical.exists() or not legacy.exists() else legacy


def load_selected_attempts(project_dir):
    root = Path(project_dir)
    path = _artifact_path(root, 'selected_attempts.json')
    selections = _read(path) if path.exists() else {}
    if not isinstance(selections, dict):
        _fail('selected_attempts.json must map shot IDs to selections')
    return selections


def _kind(tool, inputs):
    # Some stock adapters retain the historical *_generation capability name.
    if getattr(getattr(tool, 'tier', None), 'value', None) in {'source', 'analyze'}:
        return None
    # Rank/inspection does not dispatch media. Caller phase never determines kind.
    if inputs.get('operation') == 'rank' and tool.provider == 'selector':
        return None
    if tool.name == 'hyperframes_compose' and tool.provider == 'hyperframes' and inputs.get('operation') in {'render_existing', 'render'}:
        return 'local_render'
    capability = getattr(tool, 'capability', '')
    if capability == 'video_generation':
        return 'motion'
    if capability == 'image_generation':
        return 'image'
    if capability in {'music_generation', 'tts', 'voice_generation', 'audio_generation'}:
        return 'audio'
    if capability == 'avatar':
        # Read-only catalog lookups never dispatch paid media.
        if inputs.get('operation') in {'list_looks', 'inspect_look'}:
            return None
        return 'avatar'
    return None


def _clean(inputs):
    return {k: copy.deepcopy(v) for k, v in inputs.items() if k not in GOVERNANCE_KEYS and k != 'scene_id'}


def _paths(inputs, root, visitor, *, allow_upstream=False):
    """Visit supported provider local-asset fields without modifying caller data."""
    def walk(value, key=None):
        if key in {'workflow_path', 'workflow_json'}:
            _fail('strict production cannot bind opaque workflow media dependencies')
        if key in STRICT_UNENCODED_NATIVE_KEYS:
            _fail(f'{key}: strict production cannot bind native provider media fields that are '
                  'sent unencoded; use canonical image_path/image_paths/last_image_path/mask_path')
        if key == 'input_assets':
            if not isinstance(value, list):
                _fail('input_assets must be ordered local role bindings')
            names = {'first_frame': 'first_frame', 'last_frame': 'last_frame',
                     'reference_image': 'reference_image_paths', 'reference_video': 'reference_video_paths',
                     'reference_audio': 'reference_audio_paths',
                     'environment_reference': 'reference_image_paths', 'character_reference': 'reference_image_paths'}
            result = []
            for item in value:
                if not isinstance(item, dict) or item.get('role') not in names or not item.get('source_path'):
                    _fail('input_assets requires an explicit supported role and local source_path')
                source = _inside(item['source_path'], root)
                if not source.is_file():
                    _fail('input_assets source bytes missing from role binding')
                source_sha = _input_sha256(source)
                if 'source_sha256' in item and source_sha != item['source_sha256']:
                    _fail('input_assets source bytes differ from role binding')
                if 'upload_id' in item and 'reference_id' in item and item['upload_id'] != item['reference_id']:
                    _fail('input_assets has conflicting upload_id and reference_id aliases')
                result.append({**copy.deepcopy(item), 'source_path': visitor(names[item['role']], source), 'source_sha256': source_sha})
            return result
        if key in INPUT_PATH_KEYS:
            if allow_upstream and isinstance(value, dict) and '$upstream' in value:
                _upstream_binding(value)
                return copy.deepcopy(value)
            if isinstance(value, list):
                return [walk(item, key) for item in value]
            if not isinstance(value, str) or '://' in value:
                _fail(f'{key}: strict production needs a local immutable asset')
            path = _inside(value, root)
            if not (path.is_dir() if key == 'workspace_path' else path.is_file()):
                _fail(f'{key}: missing input asset {path}')
            return visitor(key, path)
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value
    return walk(inputs)


def workspace_manifest(path):
    """Closed authored tree: every regular dependency byte, no symlink aliases."""
    path = Path(path)
    if not path.is_dir() or not (path / 'index.html').is_file():
        _fail('local render requires an authored workspace with index.html')
    records = []
    for child in sorted(path.rglob('*')):
        if child.is_symlink():
            _fail('authored workspace cannot contain symlinks')
        if child.is_file():
            records.append({'path':child.relative_to(path).as_posix(), 'sha256':file_sha256(child)})
    return records


def validate_workspace_dependencies(path):
    """Bind normal static HTML/CSS/module resources; not a JavaScript sandbox."""
    import re
    from html.parser import HTMLParser
    from urllib.parse import unquote, urlsplit
    path = Path(path).resolve()
    workspace_manifest(path)

    def local_reference(ref, source):
        ref = ref.strip()
        if ref.startswith('#'):
            return
        parsed = urlsplit(ref)
        if (not ref or parsed.scheme or parsed.netloc or ref.startswith(('/', '\\'))
                or '\\' in ref or any(ord(char) < 32 for char in ref)):
            _fail('authored workspace dependency must be a retained relative local file: ' + ref)
        resolved = (source.parent / unquote(parsed.path)).resolve()
        if not resolved.is_relative_to(path) or not resolved.is_file():
            _fail('authored workspace dependency missing or escapes retained tree: ' + ref)

    def css_references(text, source):
        # Decode CSS escapes before recognizing url()/@import, including escaped
        # identifiers and schemes. Comments cannot divide a CSS identifier.
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
        def unescape(match):
            value = match.group(1)
            if re.fullmatch(r'[0-9a-fA-F]{1,6}\s?', value):
                code = int(value.strip(), 16)
                return chr(code) if 0 < code <= 0x10ffff else '\ufffd'
            return value
        text = re.sub(r'\\([0-9a-fA-F]{1,6}\s?|[^\r\n])', unescape, text)
        refs = re.findall(r'url\(\s*(?:"([^"\n]*)"|\'([^\'\n]*)\'|([^\s)]+))\s*\)', text, re.I)
        refs += re.findall(r'@import\s+(?:"([^"\n]*)"|\'([^\'\n]*)\')', text, re.I)
        for contents in re.findall(r'(?:-webkit-)?image-set\(([^)]*)\)', text, re.I):
            refs += re.findall(r'"([^"\n]*)"|\'([^\'\n]*)\'', contents)
        for match in refs:
            local_reference(next(part for part in match if part), source)

    def module_references(text, source):
        for ref in re.findall(r'\b(?:import|from)\s*["\']([^"\']+)["\']', text):
            local_reference(ref, source)

    class Resources(HTMLParser):
        def __init__(self, source):
            super().__init__(convert_charrefs=True)
            self.source = source
            self.in_style = False
            self.in_script = False

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'base' or (tag == 'meta' and values.get('http-equiv', '').lower() == 'refresh'):
                _fail('authored workspace cannot change resource base or redirect')
            if tag in {'animate','set'} and values.get('attributename','').lower() in {'href','xlink:href','src'}:
                for name in ('to','from','values'):
                    for ref in values.get(name,'').split(';'):
                        if ref: local_reference(ref, self.source)
            self.in_style = tag == 'style' or self.in_style
            self.in_script = tag == 'script' or self.in_script
            for name, value in attrs:
                if value is None:
                    continue
                if name in {'src','href','xlink:href','poster','background','data','codebase','manifest'}:
                    local_reference(value, self.source)
                elif name in {'srcset','imagesrcset'}:
                    for candidate in value.split(','):
                        fields = candidate.strip().split()
                        if not fields:
                            _fail('empty authored srcset candidate')
                        local_reference(fields[0], self.source)
                elif name == 'srcdoc':
                    Resources(self.source).feed(value)
                elif name in {'style','filter','fill','stroke','mask','clip-path',
                              'marker','marker-start','marker-mid','marker-end','cursor'}:
                    css_references(value, self.source)

        handle_startendtag = handle_starttag

        def handle_endtag(self, tag):
            if tag == 'style':
                self.in_style = False
            if tag == 'script':
                self.in_script = False

        def handle_data(self, text):
            if self.in_style:
                css_references(text, self.source)
            if self.in_script:
                module_references(text, self.source)

    for child in path.rglob('*'):
        if not child.is_file() or child.suffix.lower() not in {'.html','.htm','.css','.js','.mjs','.svg'}:
            continue
        text = child.read_text(encoding='utf-8')
        if re.search(r'\b(?:fetch|XMLHttpRequest|WebSocket|importScripts)\s*\(|\bimport\s*\(', text):
            _fail('authored workspace cannot load dynamic external dependencies')
        if child.suffix.lower() in {'.html','.htm','.svg'}:
            parser = Resources(child)
            parser.feed(text)
            parser.close()
        elif child.suffix.lower() == '.css':
            css_references(text, child)
        else:
            module_references(text, child)


def _input_sha256(path):
    return _digest(workspace_manifest(path)) if Path(path).is_dir() else file_sha256(path)


def _check_provisioned_local_runtime():
    from tools.video.hyperframes_compose import HyperFramesCompose
    command = HyperFramesCompose._cli_command()
    if (len(command) != 1 or Path(command[0]).name.lower() not in {'hyperframes','hyperframes.cmd'}
            or not Path(command[0]).is_absolute() or not Path(command[0]).is_file()):
        _fail('strict local render requires a provisioned HyperFrames executable; npx install is not authorized')


def _check_local_render_inputs(contract, shot_id, inputs, root):
    shot = next(item for item in contract['shots'] if item['id'] == shot_id)
    if inputs.get('operation') != 'render_existing' or inputs.get('strict_check') is not True or inputs.get('skip_contrast'):
        _fail('local render requires render_existing and complete strict checks')
    if explicit_motion_duration(inputs) != shot['duration_seconds']:
        _fail('submitted duration differs from shot contract')
    workspace = _inside(inputs.get('workspace_path', ''), root)
    manifest = workspace_manifest(workspace)
    validate_workspace_dependencies(workspace)
    if set(inputs) - {'operation','workspace_path','output_path','duration','fps','quality','profile','strict_check','skip_contrast','snapshots'}:
        _fail('unsupported local render controls')
    if _inside(inputs['output_path'], root).is_relative_to(workspace):
        _fail('local render output must be outside authored workspace')
    hashes = {item['sha256'] for item in manifest}
    assets = {item['id']:item for item in contract['assets']}
    if any(assets[aid]['sha256'] not in hashes for aid in shot['asset_ids']):
        _fail('authored workspace omits an approved shot asset')


def planned_request_digest(inputs, *, project_dir):
    """Bind exact prompt, route controls, output and current input bytes for approval."""
    root = Path(project_dir).resolve()
    cleaned = _clean(inputs)
    if 'cli_session_id' in cleaned:
        _fail('cli_session_id is reserved by governance')
    if cleaned.get('output_path'):
        cleaned['output_path'] = str(_inside(cleaned['output_path'], root))
    bound = _paths(cleaned, root, lambda key, path: {'path': str(path), 'sha256': _input_sha256(path)})
    return _digest(bound)


def approval_plan_digest(contract):
    """Stable user scope: retain semantics, omit only observed serial handoff facts."""
    plan = copy.deepcopy(contract)
    plan.pop("project_review", None)
    for asset in plan.get("assets", []):
        asset.pop("review", None)
        if asset.get("upstream_source"):
            asset.pop("path", None)
            asset.pop("sha256", None)
    for shot in plan.get("shots", []):
        shot.pop("review", None)
        shot["upstream"] = [{"shot_id": item["shot_id"]} for item in shot.get("upstream", [])]
    return _digest(plan)


def _upstream_binding(item):
    binding = item.get('$upstream')
    if (set(item) != {'$upstream'} or not isinstance(binding, dict)
            or set(binding) != {'shot_id', 'role'} or binding['role'] != 'outgoing_frame'):
        _fail('invalid dynamic upstream request binding')
    return binding


def planned_request_template(inputs, *, project_dir):
    """Freeze static media once for approval; only explicit upstreams stay dynamic."""
    root = Path(project_dir).resolve()
    cleaned = _clean(inputs)
    if 'cli_session_id' in cleaned:
        _fail('cli_session_id is reserved by governance')
    if cleaned.get('output_path'):
        cleaned['output_path'] = str(_inside(cleaned['output_path'], root))
    assets = []
    def bind(key, path):
        assets.append({'role':key, 'path':str(path), 'sha256':_input_sha256(path)})
        return str(path)
    cleaned = _paths(cleaned, root, bind, allow_upstream=True)
    return {'inputs':cleaned, 'static_input_assets':assets}


def approved_request_digest(value, *, project_dir, selected_attempts=None):
    """Hash frozen approval inputs without reading mutable original source files."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or set(value) != {'inputs', 'static_input_assets'}:
        _fail('approval request must be an exact digest or template with frozen static input assets')
    root = Path(project_dir).resolve()
    selected = load_selected_attempts(root) if selected_attempts is None else selected_attempts
    assets = value['static_input_assets']
    if not isinstance(assets, list):
        _fail('template static input bindings must be a list')
    position = 0
    def resolve(item, key=None):
        nonlocal position
        if isinstance(item, dict) and '$upstream' in item:
            if key not in INPUT_PATH_KEYS:
                _fail('dynamic upstream must be an asset input')
            binding = _upstream_binding(item)
            record = selected.get(binding['shot_id'], {}).get(binding['role'], {})
            if not record.get('path') or not record.get('sha256'):
                _fail('dynamic upstream request lacks matching selected bytes')
            return {'path':str(_inside(record['path'], root)), 'sha256':record['sha256']}
        if key == 'input_assets':
            names = {'first_frame': 'first_frame', 'last_frame': 'last_frame',
                     'reference_image': 'reference_image_paths', 'reference_video': 'reference_video_paths',
                     'reference_audio': 'reference_audio_paths',
                     'environment_reference': 'reference_image_paths', 'character_reference': 'reference_image_paths'}
            if not isinstance(item, list):
                _fail('template input_assets must be ordered canonical role bindings')
            result = []
            for row in item:
                if not isinstance(row, dict) or row.get('role') not in names or not row.get('source_path'):
                    _fail('template input_assets requires supported role and source_path')
                source = resolve(row['source_path'], names[row['role']])
                if 'source_sha256' in row and source['sha256'] != row['source_sha256']:
                    _fail('template canonical source bytes differ from frozen role binding')
                if 'upload_id' in row and 'reference_id' in row and row['upload_id'] != row['reference_id']:
                    _fail('template has conflicting upload/reference aliases')
                result.append({**copy.deepcopy(row), 'source_path': source, 'source_sha256': source['sha256']})
            return result
        if key in INPUT_PATH_KEYS:
            if isinstance(item, list):
                return [resolve(child, key) for child in item]
            if not isinstance(item, str) or '://' in item:
                _fail('template requires a local static asset')
            path = str(_inside(item, root))
            if position >= len(assets):
                _fail('template lacks frozen static input binding')
            record = assets[position]
            position += 1
            if record.get('role') != key or record.get('path') != path or not record.get('sha256'):
                _fail('template static input binding differs from input layout')
            return {'path':path, 'sha256':record['sha256']}
        if key in {'workflow_path', 'workflow_json'}:
            _fail('strict production cannot bind opaque workflow media dependencies')
        if isinstance(item, dict):
            return {name:resolve(child, name) for name, child in item.items()}
        if isinstance(item, list):
            return [resolve(child) for child in item]
        return item
    cleaned = _clean(value['inputs'])
    if 'cli_session_id' in cleaned:
        _fail('cli_session_id is reserved by governance')
    if cleaned.get('output_path'):
        cleaned['output_path'] = str(_inside(cleaned['output_path'], root))
    bound = resolve(cleaned)
    if position != len(assets):
        _fail('template contains unused static input bindings')
    return _digest(bound)


def _approved_request(value, root):
    # Dispatch additionally verifies the live static bytes and dynamic selection.
    digest = approved_request_digest(value, project_dir=root)
    if isinstance(value, dict):
        if planned_request_template(value['inputs'], project_dir=root)['static_input_assets'] != value['static_input_assets']:
            _fail('approved static input bytes changed')
        def check_dynamic(item):
            if isinstance(item, dict) and '$upstream' in item:
                binding = _upstream_binding(item)
                record = load_selected_attempts(root).get(binding['shot_id'], {}).get(binding['role'], {})
                if not record.get('path') or file_sha256(_inside(record['path'],root)) != record.get('sha256'):
                    _fail('dynamic upstream request lacks matching selected bytes')
            elif isinstance(item, dict):
                for child in item.values(): check_dynamic(child)
            elif isinstance(item, list):
                for child in item: check_dynamic(child)
        check_dynamic(value['inputs'])
    return digest


def _attempts(root):
    rows = [_read(path) for path in sorted((root / 'production_attempts').glob('*/request.json'))]
    try:
        from lib.openart_mcp_jobs import list_attempts
    except ImportError:
        return rows
    rows.extend({**row, 'story_revision': row['scope_snapshot']['story_revision']}
                for row in list_attempts(root))
    own = _MCP_REVALIDATING.get()
    return [row for row in rows if row.get('attempt_id') != own]


def _state(root, attempt):
    if attempt.get('provider') == 'openart_mcp':
        return _mcp_state(root, attempt['attempt_id'])
    directory = root / 'production_attempts' / attempt['attempt_id']
    reconciled = directory / 'reconciliation.json'
    claim = directory / 'local_continuation_claim.json'
    continued = directory / 'local_continuation_result.json'
    if claim.exists():
        validate_local_grok_continuation_record(root, attempt)
        if not continued.exists() and not reconciled.exists():
            raw = directory / 'local_continuation_raw_result.json'
            return {'status': 'uncertain', 'result': _read(raw) if raw.exists() else None}
    result = continued if claim.exists() else directory / 'result.json'
    try:
        state = _read(reconciled if reconciled.exists() else result) if result.exists() or reconciled.exists() else {'status': 'uncertain'}
        if state.get('status') == 'uncertain':
            from lib.openart_dispatch import existing_terminal_state
            terminal = existing_terminal_state(root, attempt['attempt_id'])
            if terminal is not None: return terminal
        return state
    except ProductionGovernanceError as exc:
        # An old/incomplete journal remains consumed and recoverable, never retryable.
        return {'status':'uncertain', 'journal_error':str(exc)}


def explicit_motion_duration(inputs):
    """One explicit native duration, without defaults or output-goal coercion."""
    import math
    params = inputs.get('native_params', {})
    if not isinstance(params, dict):
        _fail('native_params must be an exact object')
    values = []
    if 'duration' in inputs:
        values.append(inputs['duration'])
    for key in ('duration', 'videoDuration'):
        if key in params:
            if key == 'videoDuration' and inputs.get('model') != 'smart-shot':
                _fail('videoDuration requires the explicit SmartShot model')
            values.append(params[key])
    if len(values) != 1:
        _fail('exactly one explicit motion duration is required; duplicate aliases are ambiguous')
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        _fail('motion duration must be a positive finite JSON number')
    return value


def _check_motion_inputs(contract, shot_id, inputs, root, *, policy_composite=False):
    shot = next(item for item in contract['shots'] if item['id'] == shot_id)
    assets = {item['id']: item for item in contract['assets']}
    actual = []
    _paths(inputs, root, lambda key, path: actual.append((key, _input_sha256(path))) or str(path))
    role_keys = {'start_frame': {'first_frame','image_path','reference_image_path'},
                 'end_frame': {'last_frame','last_image_path'},
                 'identity_reference': {'reference_image_paths','images'},
                 'reference_image': {'reference_image_paths','images'},
                 'reference_video': {'reference_video_paths','reference_video_path'},
                 'reference_audio': {'reference_audio_paths','reference_audio_path'}}
    guided = shot.get('reference_mode', contract.get('reference_mode')) == 'reference_guided'
    # Eligibility has already validated these boards' bytes, cast-identity
    # reviews and upstream provenance. Only an exact native pair can carry
    # identity in place of auxiliary reference submissions on a boarded route.
    local_assets = [assets[aid] for aid in shot['asset_ids']]
    pin_boards = {
        role: [a for a in local_assets if a['role'] == role]
        for role in ('start_frame', 'end_frame')
    }
    reviewed_pin_pair = not guided and inputs.get('operation') == 'first_last_frame'
    for role, boards in pin_boards.items():
        reviewed_pin_pair = reviewed_pin_pair and len(boards) == 1 and any(
            key in role_keys[role] and digest == boards[0]['sha256'] for key, digest in actual)
    start_cast = set(pin_boards['start_frame'][0]['cast_ids']) if reviewed_pin_pair else set()
    submitted_asset_ids = set(shot['asset_ids'])
    submitted_asset_ids.update(a['id'] for a in contract['assets'] if a['role'] == 'identity_reference'
                               and set(a['cast_ids']).intersection(shot['cast_ids']))
    for asset_id in submitted_asset_ids:
        asset = assets[asset_id]
        keys = role_keys.get(asset['role'])
        if guided and asset['role'] in {'start_frame', 'end_frame'}:
            keys = None  # explicit reviewed targets; required native pins validate at preparation
        if asset['role'] == 'end_frame' and not (
                inputs.get('operation') == 'first_last_frame' or inputs.get('endpoint_requirement_id')
                or inputs.get('last_frame') or inputs.get('last_image_path')):
            keys = None  # reviewed ending target; canonical required native pins validate separately
        if asset['role'] == 'identity_reference' and policy_composite:
            keys = None  # rooted named prep/actual board proof already validated every member
        if asset['role'] == 'identity_reference' and not guided and inputs.get('operation') == 'image_to_video':
            keys = None  # approved single-image method carries identity through reviewed start board
        if (asset['role'] == 'identity_reference' and reviewed_pin_pair
                and set(asset['cast_ids']).intersection(shot['cast_ids']).issubset(start_cast)):
            keys = None  # exact reviewed native start/end pins carry the declared cast
        if keys and not any(key in keys and digest == asset['sha256'] for key, digest in actual):
            _fail(f'shot {shot_id}: submitted inputs omit or change {asset["role"]} {asset_id}')
    approved_hashes = {assets[item]['sha256'] for item in submitted_asset_ids}
    if any(digest not in approved_hashes for key, digest in actual):
        _fail('submitted inputs contain an unapproved asset')
    if explicit_motion_duration(inputs) != shot['duration_seconds']:
        _fail('submitted duration differs from shot contract')


# U3 installs a pure compiled-request validator; no transport/ledger work here.
def _compiled_request_check(inputs, native, profile):
    from lib.production_request import validate_preparation
    from jsonschema.exceptions import ValidationError
    try:
        return validate_preparation(inputs, native, profile)
    except (ValueError, OSError, KeyError, TypeError, ValidationError) as exc:
        _fail('OpenArt preparation: ' + str(exc))


_OPENART_COMPILED_REQUEST_CHECK = _compiled_request_check


def _is_openart(tool):
    return getattr(tool, 'provider', None) == 'openart_cli'


def _openart_controls(inputs):
    controls = _clean(inputs)
    for canonical, aliases in {'image_path': ('first_frame', 'reference_image_path'),
                               'last_image_path': ('last_frame',)}.items():
        for alias in aliases:
            if alias not in controls:
                continue
            if canonical in controls and Path(controls[canonical]).expanduser().resolve() != Path(controls[alias]).expanduser().resolve():
                _fail('conflicting OpenArt ' + canonical + ' and ' + alias)
            controls[canonical] = controls.pop(alias)
    for key in ('preferred_tool','hosting_provider','preferred_provider','preferred_provider_gap',
                'allowed_providers','task_context','target_operation'):
        controls.pop(key, None)
    return controls


def _openart_mcp_controls(inputs):
    """Explicit connector control alias; shares canonical local role/path rules."""
    result = _openart_controls(inputs)
    result.pop('attempt_id', None)
    if inputs.get('project_dir'):
        result['project_dir'] = str(Path(inputs['project_dir']).resolve())
    return result


def _openart_prepare(inputs):
    from lib import openart_jobs as jobs
    profile = jobs.load_qualification(model=inputs.get('model'), mode=inputs.get('mode'), require='pre_submit')
    native = jobs.prepare_native_request(_openart_controls(inputs), profile)
    if _OPENART_COMPILED_REQUEST_CHECK is None:
        _fail('OpenArt compiled-request preparation validator is not installed (U3)')
    _OPENART_COMPILED_REQUEST_CHECK(inputs, native, profile)
    return profile, native


def active_openart_dispatch(inputs):
    """Adapter entry cannot accept caller-supplied reservation/context authority."""
    from lib import openart_jobs as jobs
    active = _ACTIVE.get()
    if not active or active['provider'] != 'openart_cli' or not active.get('openart_binding'):
        _fail('OpenArt requires strict active governed dispatch and ledger reservation')
    native = jobs.native_request(_openart_controls(inputs), active['openart_profile'],
                                 dry_run=active['openart_native'].get('dry_run'))
    if native != active['openart_native'] or _openart_controls(inputs) != _openart_controls(active['submitted_inputs']):
        _fail('OpenArt native request differs from frozen approved dispatch')
    reservation = jobs.get_active_reservation(active['root'], active['session_id'],
                                              active['openart_binding']['request_sha256'])
    if reservation['reservation_id'] != active['openart_binding']['reservation_id']:
        _fail('OpenArt reservation changed')
    return active


def preflight(tool, inputs, *, _local_continuation=None):
    """Perform the same factual checks used by dispatch, without writing or calling."""
    if _local_continuation is None and isinstance(inputs.get('governance'), dict) and 'continue_local_attempt_id' in inputs['governance']:
        return _check_local_grok_continuation(tool, inputs)['checked']
    mcp = tool.provider == 'openart_mcp' or (tool.provider == 'selector' and inputs.get('preferred_provider') == 'openart_mcp')
    unknown_keys = ('unknown_cost_authorization_id', 'unknown_cost_evidence_id')
    if mcp:
        if inputs.get('preferred_provider', 'openart_mcp') != 'openart_mcp' or ('allowed_providers' in inputs and inputs['allowed_providers'] != ['openart_mcp']):
            _fail('OpenArt MCP requires its exact explicit provider route')
        if inputs.get('preferred_tool', 'openart_mcp_video') != 'openart_mcp_video' or inputs.get('hosting_provider', 'openart_mcp') != 'openart_mcp':
            _fail('OpenArt MCP cannot use a foreign tool or hosting provider')
        if any(key in inputs for key in ('credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256', 'unknown_cost_evidence_id')):
            _fail('OpenArt MCP cannot reuse CLI billing or evidence authority')
    elif any(key in inputs for key in unknown_keys):
        if any(key in inputs for key in ('credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256')):
            _fail('invalid_argument: unknown-cost and exact credit authorization are mutually exclusive')
        if not (mcp or _is_openart(tool) or (tool.provider == 'selector' and inputs.get('preferred_provider') == 'openart_cli')):
            _fail('invalid_argument: unknown-cost authorization requires an OpenArt route')
        if any(not isinstance(inputs.get(key), str) or not inputs[key] for key in unknown_keys):
            _fail('invalid_argument: unknown-cost authorization and evidence IDs are required together')
        from lib.production_request import _id
        try:
            _id(inputs['unknown_cost_authorization_id'])
        except ValueError:
            _fail('invalid_argument: unknown-cost authorization ID must be an opaque safe ID')
        evidence_id = inputs['unknown_cost_evidence_id']
        if len(evidence_id) != 64 or any(c not in '0123456789abcdef' for c in evidence_id):
            _fail('invalid_argument: unknown-cost evidence ID must be a lowercase SHA-256 digest')
    kind = _kind(tool, inputs)
    if mcp or _is_openart(tool) or (tool.provider == 'selector' and inputs.get('preferred_provider') == 'openart_cli'):
        root = discover_project(inputs)
        if root is None or _read(root / 'project.json').get('governance', {}).get('mode') != 'strict':
            _fail('OpenArt requires strict enrollment and a governed attempt')
        if kind != 'motion':
            _fail('OpenArt requires strict video-generation dispatch')
        if tool.name not in {'openart_cli_video', 'openart_mcp_video', 'video_selector'}:
            _fail('OpenArt requires canonical video adapter')
    if kind is None:
        return {'governed': False, 'label': 'ungoverned_diagnostic'}
    root = discover_project(inputs)
    if root is None:
        return {'governed': False, 'label': 'ungoverned_legacy'}
    marker = _read(root / 'project.json')
    if marker.get('governance', {}).get('mode') != 'strict':
        if inputs.get('governance') is not None:
            _fail('production project is not enrolled in strict governance')
        return {'governed': False, 'label': 'ungoverned_legacy'}
    if marker['governance'].get('version') != '1.0':
        _fail('unsupported governance version')
    qualification = marker.get('pipeline_type') == 'provider-qualification'
    if qualification and (kind != 'motion' or not (mcp or _is_openart(tool) or (
            tool.provider == 'selector' and inputs.get('preferred_provider') == 'openart_cli'))):
        _fail('provider-qualification pipeline admits only canonical OpenArt strict video generation')
    if kind == 'avatar':
        # Avatar generation/resume has no shot-contract, scope phase or attempt
        # provenance binding yet. Fail closed in strict projects rather than
        # issuing an ungoverned paid generation; legacy projects are unchanged.
        _fail('strict governance does not support avatar generation or resume yet')
    context = inputs.get('governance')
    if not isinstance(context, dict) or not context.get('scope_id') or not context.get('shot_id'):
        _fail('strict generation requires governance scope_id and shot_id')
    if inputs.get('resume_job'):
        # Provider-job resume (SchemaMedia/FalMedia/HeyGen/fal TTS) polls an
        # already-paid submission. It is not a new approved attempt, and
        # reconcile_attempt has no qualified binding for generic providers yet, so
        # fail closed rather than reserve/count it as a fresh paid dispatch.
        _fail('strict governance cannot resume provider jobs as new attempts; '
              'reconcile the original attempt (unsupported for this provider)')
    scope_id, shot_id = context['scope_id'], context['shot_id']
    # This read-only early check is repeated by execute_governed under the
    # project lock, before attempt directories or provider reservations exist.
    # Private unpublished reservations take precedence over public journals.
    from lib.production_video_guard import read_video_duplicate_blocks, classify_production_kind
    if kind == 'motion':
        blockers = read_video_duplicate_blocks(root, shot_id)
        if blockers:
            _fail('; '.join(blockers))
    scopes = _read(root / 'production_scopes.json')
    if scopes.get('version') != '1.0':
        _fail('unsupported approval scopes version')
    matches = [item for item in scopes.get('scopes', []) if item.get('id') == scope_id]
    if len(matches) != 1:
        _fail('approval scope must exist exactly once')
    scope = matches[0]
    if 'derived_from_policy' in scope and not isinstance(scope['derived_from_policy'], dict):
        _fail('malformed derived_from_policy')
    if scope.get('status') != 'approved' or not scope.get('approved_by'):
        _fail('scope lacks explicit approval provenance')
    evidence = scope.get('evidence', {})
    if not evidence.get('path') or not evidence.get('sha256') or file_sha256(_inside(evidence['path'], root)) != evidence['sha256']:
        _fail('approval evidence is missing or changed')
    if not marker.get('story_revision') or scope.get('story_revision') != marker['story_revision'] or scope.get('project_id') != marker.get('project_id'):
        _fail('approval scope has stale project/story binding')
    phase = scope.get('phase')
    if phase not in ({'first_pass','repair'} if kind == 'motion' else {kind}):
        _fail('approval phase does not authorize this media kind')
    provider = scope.get('provider')
    if not provider or (tool.provider != 'selector' and provider != tool.provider):
        _fail('provider differs from approved locked route')
    if tool.provider == 'selector' and inputs.get('preferred_provider') != provider:
        _fail('selector requires the exact approved preferred_provider')
    if provider in {'openart_cli', 'openart_mcp'} and tool.provider == 'selector' and inputs.get('allowed_providers') != [provider]:
        _fail('OpenArt selector requires singleton exact allowed_providers')
    if mcp:
        if 'derived_from_policy' in scope:
            if 'unknown_cost_authorization_id' in inputs:
                _fail('MCP policy scope cannot accept caller billing authorization IDs')
        else:
            from lib.production_request import _id
            try:
                _id(inputs.get('unknown_cost_authorization_id'))
            except ValueError:
                _fail('OpenArt MCP requires its distinct retained billing authorization ID')
    attempts = _attempts(root)
    # A carried scope shares quota lineage with its source first-pass scope so a
    # carry-forward can never reset a shot already consumed under the original.
    lineage = {scope_id}
    carried = scope.get('carried_from')
    if carried is not None:
        if not isinstance(carried, dict) or not isinstance(carried.get('scope_id'), str):
            _fail('malformed carried_from')
        lineage.add(carried['scope_id'])
    scope_used = sum(item['scope_id'] in lineage and item['shot_id'] == shot_id for item in attempts)
    # The reserved occurrence is counted permanently; only its frozen request
    # index is replayed instead of selecting a new batch entry.
    if _local_continuation is not None:
        scope_used -= 1
    approved_request = scope.get('requests', {}).get(shot_id)
    if isinstance(approved_request, list):
        if scope_used >= len(approved_request):
            _fail('approved request batch exhausted')
        approved_request = approved_request[scope_used]
    digest = planned_request_digest(inputs, project_dir=root)
    if _approved_request(approved_request, root) != digest:
        _fail('request differs from the exact approved request')
    output = _inside(inputs.get('output_path', ''), root)
    if not inputs.get('output_path') or output == root:
        _fail('strict generation requires output_path')
    if output.exists():
        _fail('output already exists; reconcile the original attempt instead of file-size reuse')
    contract = None
    policy_validated = False
    if kind in {'motion','local_render'}:
        contract = _read(_artifact_path(root, 'shot_contract.json'))
        if contract.get('project_id') != marker.get('project_id'):
            _fail('shot contract project mismatch')
        scoped_shot = next((shot for shot in contract.get('shots', []) if shot.get('id') == shot_id), {})
        if scoped_shot.get('reference_mode', contract.get('reference_mode')) == 'reference_guided' and provider != 'openart_mcp':
            _fail('reference-guided production requires its explicit OpenArt MCP route')
        checked = validate_shot_contract(contract, project_dir=root, shot_id=shot_id,
            story_revision=marker['story_revision'], selected_upstream=load_selected_attempts(root))
        if not checked['eligible']:
            _fail('; '.join(checked['errors']))
        effective = contract
        if kind == 'motion':
            # A prior-plan scope continues only through an authorized revision
            # that names this exact scope (and its carry lineage); upstream
            # selections of revised shots must be fresh reviews of the revision.
            from lib.production_continuity import (revision_chain, undo_revision, require_retained_scope,
                                                   require_revised_upstream)
            chain = revision_chain(root, contract)
            if chain:
                require_revised_upstream(chain, contract, shot_id, load_selected_attempts(root))
            for revision in chain:
                if scope.get('approval_plan_sha256') == approval_plan_digest(effective):
                    break
                require_retained_scope(revision, scope, shot_id)
                effective = undo_revision(effective, revision)
        if carried is not None:
            from lib.production_continuity import validate_carried_scope
            if validate_carried_scope(root, scope, effective, scopes.get('scopes', [])) != carried['scope_id']:
                _fail('carried scope lineage differs')
        elif scope.get('approval_plan_sha256') != approval_plan_digest(effective):
            _fail('approval scope has a stale contract binding')
        if 'derived_from_policy' in scope:
            from lib.production_autonomy import validate_derived_scope
            try:
                validate_derived_scope(root, scope, inputs=inputs, observation=_GROK_COMPATIBILITY.get())
            except (ValueError, KeyError, TypeError, OSError) as exc:
                _fail('derived policy dispatch refused: ' + str(exc))
            policy_validated = True
        if kind == 'local_render':
            _check_provisioned_local_runtime()
            _check_local_render_inputs(contract, shot_id, _clean(inputs), root)
        else:
            if policy_validated:
                _check_motion_inputs(contract, shot_id, _clean(inputs), root, policy_composite=True)
            else:
                _check_motion_inputs(contract, shot_id, _clean(inputs), root)
    allowance = scope.get('attempts_per_shot', {}).get(shot_id)
    if isinstance(allowance, bool) or not isinstance(allowance, int) or allowance < 1:
        _fail('scope lacks a positive exact shot allowance')
    previous = []
    for item in attempts:
        if item.get('shot_id') != shot_id:
            continue
        if _local_continuation is not None and item['attempt_id'] == _local_continuation['attempt_id']:
            continue  # this occurrence is resumed, never a new reservation
        prior_kind = classify_production_kind(item)
        if prior_kind is None:
            _fail('an original attempt has missing or unclassifiable media kind')
        if prior_kind == kind:
            previous.append(item)
    if any(_state(root, item)['status'] == 'uncertain' for item in previous):
        _fail('an original job is uncertain; reconcile it before another attempt')
    if any(item.get('provider') == 'openart_mcp' and _state(root, item)['status'] in {'prepared', 'submitted', 'awaiting_original_result'} for item in previous):
        _fail('an original OpenArt MCP job is reserved or pending; resolve that original before another attempt')
    if phase == 'first_pass' and previous:
        _fail('first-pass ceiling never authorizes a corrective reroll; approve an exact repair scope')
    if phase == 'repair':
        replaces = scope.get('replaces_attempt_ids')
        authorized_shots = set(scope.get('requests', {})) & set(scope.get('attempts_per_shot', {}))
        eligible = {item['attempt_id'] for item in attempts if item['shot_id'] in authorized_shots
                    and classify_production_kind(item) == kind}
        if (not isinstance(replaces, list) or not replaces or not set(replaces).issubset(eligible)
                or not set(replaces).intersection(item['attempt_id'] for item in previous)):
            _fail('repair scope must name existing exact attempts to replace')
        if scope.get('repair_basis') == 'creator_batch':
            from lib.production_repair_batches import validate_creator_repair_intent
            validate_creator_repair_intent(root, scope.get('creator_repair'), shot_id=shot_id,
                provider=provider, model=_clean(inputs).get('model'),
                replaces_attempt_ids=scope.get('replaces_attempt_ids'), inputs=inputs)
    from lib import episode_production_controls as episode_controls
    try:
        controls = episode_controls.effective_controls(root) if kind == 'motion' else None
    except episode_controls.EpisodeControlsError as exc:
        _fail(str(exc))
    if controls is None:
        used = sum(item['scope_id'] in lineage and item['shot_id'] == shot_id for item in attempts)
        if used > allowance or (used == allowance and _local_continuation is None):
            _fail('approved attempt allowance exhausted')
    else:
        # Opted-in episodes count trusted generation occurrences: proven
        # never-submitted preparations and status/collection cost no slot, so
        # the scope allowance (still binding) is measured the same way.
        resumed = _local_continuation['attempt_id'] if _local_continuation is not None else _MCP_REVALIDATING.get()
        try:
            usage = episode_controls.generation_usage(root)
        except episode_controls.EpisodeControlsError as exc:
            _fail(str(exc))
        lineage_ids = {item['attempt_id'] for item in attempts if item['scope_id'] in lineage}
        used = sum(1 for item in usage['occurrences'] if item['counted'] and item['shot_id'] == shot_id
                   and item['attempt_id'] in lineage_ids and item['attempt_id'] != resumed)
        if used >= allowance:
            _fail('approved attempt allowance exhausted')
        purpose = ('local_continuation' if _local_continuation is not None
                   else 'mcp_begin' if _MCP_REVALIDATING.get() else phase)
        try:
            episode_controls.require_admission(
                root, shot_id=shot_id, provider=provider, model=_clean(inputs).get('model'), purpose=purpose,
                replaces_attempt_ids=tuple(scope.get('replaces_attempt_ids') or ()), exclude_attempt_id=resumed,
                repair_basis=scope.get('repair_basis', 'critical_review'), creator_repair=scope.get('creator_repair'))
        except episode_controls.EpisodeControlsError as exc:
            _fail(str(exc))
    if any(_inside(item.get('submitted_inputs', {}).get('output_path', ''), root) == output
           for item in attempts if _local_continuation is None or item['attempt_id'] != _local_continuation['attempt_id']):
        _fail('output path already reserved; reconcile the original attempt')
    if mcp:
        from lib import openart_mcp as connector
        profile = connector.load_profile(inputs.get('model'), inputs.get('mode'), require='candidate' if qualification else 'supported')
        native = connector.prepare_native_request(_openart_mcp_controls(inputs), profile)
        _compiled_request_check(inputs, native, profile)
        openart = (profile, native)
    else:
        openart = _openart_prepare(inputs) if _is_openart(tool) or provider == 'openart_cli' else None
    if qualification:
        # The generic gate above applies to every route; this pipeline additionally
        # requires the current human-approved packet bound to actual native/profile.
        if provider not in {'openart_cli', 'openart_mcp'} or openart is None:
            _fail('provider-qualification pipeline admits only canonical OpenArt strict video generation')
        from lib.provider_qualification import validate_qualification_stage
        try:
            validate_qualification_stage(root, inputs, digest, native=openart[1], profile=openart[0])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            _fail(str(exc))
    if kind == 'image' and provider == 'grok_cli':
        # Grok image calls share the episode image ceiling with native board
        # images. Keep this read-only admission in factual preflight so dry-runs
        # expose exhaustion and same-slot pending originals before dispatch.
        # Dispatch repeats preflight under the project lock, preserving the
        # race-safe check immediately before any reservation or provider call.
        from lib.production_images import check_grok_image_admission
        try:
            check_grok_image_admission(root, scope=scope, shot_id=shot_id,
                                       request_sha256=digest)
        except ValueError as exc:
            _fail(str(exc))
    return {'openart':openart, 'governed': True, 'root': root, 'marker': marker, 'scope': scope,
            'shot_id': shot_id, 'kind': kind, 'contract': contract, 'request_sha256': digest,
            'scope_attempt_index':scope_used, 'policy_validated': policy_validated}


@contextlib.contextmanager
def _lock(root):
    # Portable OS-level lock; importing BaseTool never requires POSIX modules.
    import os
    with (root / '.production-execution.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _provider_inputs(tool, inputs, session_id):
    result = _clean(inputs)
    if 'cli_session_id' in getattr(tool, 'input_schema', {}).get('properties', {}):
        result['cli_session_id'] = session_id
    return result


def _preserve_output(source, target, digest):
    """Publish complete output bytes exclusively, leaving failures recoverable."""
    import os
    import shutil
    temp = target.with_name('.output-snapshot-' + str(uuid.uuid4()) + '.tmp')
    try:
        with source.open('rb') as incoming, temp.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if file_sha256(temp) != digest:
            _fail('output changed while preserving original bytes')
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    target.chmod(0o444)


def _save_result(directory, result=None, error=None, *, prefix=""):
    if result is not None:
        _write_new(directory / (prefix + 'raw_result.json'), asdict(result))
    request = _read(directory / 'request.json')
    output_path = Path(request['submitted_inputs']['output_path'])
    status = 'uncertain'
    output = None
    if result is not None:
        if result.success and output_path.is_file():
            status = 'generated'
            output = {'path': str(output_path), 'sha256': file_sha256(output_path)}
        elif not result.success and result.data.get('dispatch_status') in {'not_dispatched','failed'}:
            status = 'failed'
    preserved = None
    if output is not None:
        target = directory / ('output' + output_path.suffix)
        _preserve_output(output_path, target, output['sha256'])
        preserved = {'path':str(target),'sha256':output['sha256']}
    public_error = repr(error) if error is not None else None
    if request.get('openart') and error is not None:
        public_error = 'OpenArt execution error: ' + str(getattr(error, 'kind', type(error).__name__))
    record = {'preserved_output':preserved,'status': status, 'result': asdict(result) if result is not None else None,
              'output': output, 'exception': public_error}
    _write_new(directory / (prefix + 'result.json'), record)
    return record


def _observe_retained_output(root, request, record):
    """Observe a newly published successful recovery; never backfill replay clocks."""
    try:
        output = record.get('output') or {}
        if (record.get('status') != 'generated' or not output.get('sha256')
                or (record.get('preserved_output') or {}).get('sha256') != output['sha256']):
            return
        from lib.events import emit_event
        identity = {key: request[key] for key in ('attempt_id', 'request_sha256', 'project_id',
                    'story_revision', 'shot_id', 'scope_id', 'media_kind')}
        if not all(identity.values()) or not request['tool_name']:
            return
        emit_event(root, {'event': 'governed-result-retained', **identity,
                         'tool': request['tool_name'], 'status': 'generated',
                         'output_sha256': output['sha256']})
    except Exception:
        # Timing remains best-effort after canonical evidence has been published.
        pass



_LOCAL_PIN_ERROR = 'Grok CLI invalid_argument error: reference_to_video requires between 1 and 14 local image path(s)'
_LOCAL_CONTINUATION_FILES = ('request.json', 'result.json', 'raw_result.json',
                             'shot_contract.json', 'selected_attempts.json')


def _local_grok_failure(directory, request):
    """Recognize only the retained pre-CLI empty-optional-reference defect."""
    prior = _read(directory / 'result.json')
    raw = _read(directory / 'raw_result.json')
    data = raw.get('data', {})
    expected = {'provider': 'grok_cli', 'cli_version': None,
                'session_id': request['attempt_id'], 'dispatch_session_id': request['attempt_id'],
                'error_category': 'invalid_argument', 'dispatch_status': 'not_dispatched',
                'retry_attempted': False, 'fallback_attempted': False}
    allowed = set(expected) | {'model', 'agent_model', 'model_role', 'media_model', 'media_model_status'}
    if (prior.get('status') != 'failed' or prior.get('result') != raw
            or any(prior.get(k) is not None for k in ('output', 'preserved_output', 'exception'))
            or raw.get('success') is not False or raw.get('artifacts') != []
            or raw.get('error') != _LOCAL_PIN_ERROR or not isinstance(data, dict)
            or set(data) - allowed or any(data.get(k) != v for k, v in expected.items())):
        _fail('local continuation requires the exact pre-CLI empty-reference failure')
    submitted = request.get('submitted_inputs', {})
    if (submitted.get('operation') != 'first_last_frame' or submitted.get('reference_image_paths') != []
            or submitted.get('cli_session_id') != request['attempt_id']):
        _fail('local continuation requires the exact frozen empty-reference native pin pair')
    return raw


def validate_local_grok_continuation_record(project_dir, request):
    """Replay immutable continuation authority without permitting another dispatch."""
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / request['attempt_id'], root)
    claim = _read(directory / 'local_continuation_claim.json')
    expected = {'version', 'attempt_id', 'request_sha256', 'submitted_inputs_sha256',
                'original_sha256', 'native_request_sha256'}
    if (not isinstance(claim, dict) or set(claim) != expected or claim.get('version') != '1.0'
            or claim['attempt_id'] != request['attempt_id']
            or claim['request_sha256'] != request['request_sha256']
            or claim['submitted_inputs_sha256'] != _digest(request['submitted_inputs'])
            or set(claim.get('original_sha256', {})) != set(_LOCAL_CONTINUATION_FILES)):
        _fail('local continuation claim does not bind the reserved occurrence')
    for name, digest in claim['original_sha256'].items():
        if file_sha256(directory / name) != digest:
            _fail('local continuation original evidence changed: ' + name)
    _local_grok_failure(directory, request)
    return claim


def _local_grok_inputs(root, request):
    """Restore original approved locations while verifying every preserved byte."""
    bindings = iter(request['input_assets'])
    def restore(role, path):
        record = next(bindings, None)
        if (not isinstance(record, dict) or record.get('role') != role or record.get('path') != str(path)
                or file_sha256(path) != record.get('sha256')):
            _fail('local continuation frozen input bytes/roles changed')
        _inside(path, root / 'production_attempts' / request['attempt_id'] / 'inputs')
        return str(_inside(record['original_path'], root))
    submitted = _clean(request['submitted_inputs'])
    submitted.pop('cli_session_id', None)
    restored = _paths(submitted, root, restore)
    if next(bindings, None) is not None or planned_request_digest(restored, project_dir=root) != request['request_sha256']:
        _fail('local continuation request differs from frozen reservation')
    return dict(restored, project_dir=str(root), governance={'scope_id': request['scope_id'], 'shot_id': request['shot_id']})


def _check_local_grok_continuation(tool, inputs):
    """Read-only exact defect qualification, followed by normal factual preflight."""
    from tools.video.grok_cli_video import GrokCLIVideo, build_native_video_request
    import re
    import os
    context = inputs.get('governance', {})
    aid = context.get('continue_local_attempt_id')
    digest = context.get('continue_local_request_sha256')
    if (type(tool) is not GrokCLIVideo or tool.name != 'grok_cli_video' or tool.provider != 'grok_cli'
            or not isinstance(aid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', aid)
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        _fail('local continuation requires a canonical Grok video attempt and exact request hash')
    root = discover_project(inputs)
    if root is None:
        _fail('local continuation requires its enrolled project')
    directory = _inside(Path('production_attempts') / aid, root)
    request = _read(directory / 'request.json')
    if (request.get('version') != '1.0' or type(request.get('scope_attempt_index')) is not int
            or request.get('attempt_id') != aid or request.get('cli_session_id') != aid
            or request.get('request_sha256') != digest or request.get('tool_name') != tool.name
            or request.get('media_kind') != 'motion' or request.get('scope', {}).get('provider') != 'grok_cli'
            or 'derived_from_policy' in request.get('scope', {})
            or context.get('scope_id') != request.get('scope_id') or context.get('shot_id') != request.get('shot_id')):
        _fail('local continuation reserved route/request/scope differs')
    for name in ('local_continuation_claim.json', 'local_continuation_result.json',
                 'local_continuation_raw_result.json', 'reconciliation.json', 'provider_request.json',
                 'provider_result.json', 'output.mp4'):
        if (directory / name).exists() or (directory / name).is_symlink():
            _fail('local continuation already claimed or has prior dispatch/output evidence')
    _local_grok_failure(directory, request)
    output = _inside(request['submitted_inputs']['output_path'], root)
    if output.exists():
        _fail('local continuation output already exists')
    sessions_root = Path(tool._sessions_root or os.environ.get('GROK_SESSIONS_ROOT') or Path.home() / '.grok/sessions').expanduser()
    # A captured original session is never classified as a local validation miss.
    session_roots = {sessions_root, Path.home() / '.grok/sessions'}
    if os.environ.get('GROK_SESSIONS_ROOT'):
        session_roots.add(Path(os.environ['GROK_SESSIONS_ROOT']).expanduser())
    if any((base / aid).exists() or (base / aid).is_symlink()
           or any(base.glob('*/' + aid)) for base in session_roots):
        _fail('local continuation original CLI session already exists')
    restored = _local_grok_inputs(root, request)
    if _clean(inputs) != _clean(restored) or set(context) != {
            'scope_id', 'shot_id', 'continue_local_attempt_id', 'continue_local_request_sha256'}:
        _fail('local continuation inputs differ from reserved frozen request')
    scopes = _read(root / 'production_scopes.json')['scopes']
    matches = [s for s in scopes if s.get('id') == request['scope_id']]
    if len(matches) != 1 or matches[0] != request['scope']:
        _fail('local continuation original approval scope changed')
    frozen = _read(directory / 'shot_contract.json')
    current = load_shot_contract(root)
    if (contract_digest(frozen) != request['contract_sha256']
            or approval_plan_digest(current) != request['scope']['approval_plan_sha256']
            or approval_plan_digest(frozen) != approval_plan_digest(current)):
        _fail('local continuation contract plan changed')
    if load_selected_attempts(root) != _read(directory / 'selected_attempts.json'):
        _fail('local continuation selected upstream evidence changed')
    evidence = request.get('approval_evidence', {})
    if file_sha256(_inside(evidence.get('path', ''), directory)) != request['scope']['evidence']['sha256']:
        _fail('local continuation preserved approval changed')
    # Both original and preserved inputs are validated; the exact frozen [] is
    # retained. The fixed builder omits only the empty optional native images.
    native = build_native_video_request(request['submitted_inputs'], adapter_version=tool.version)
    if not {'first_frame', 'last_frame'}.issubset(native['arguments']):
        _fail('local continuation requires valid native first and last pins')
    checked = preflight(tool, restored, _local_continuation=request)
    if checked['scope_attempt_index'] != request['scope_attempt_index']:
        _fail('local continuation reserved allowance index changed')
    return {'root': root, 'directory': directory, 'request': request, 'inputs': restored,
            'checked': checked, 'native': native}


def continue_local_grok_attempt(tool, project_dir, attempt_id, *, request_sha256, dry_run=True):
    """Normal wrapper entry for one defect-specific already-reserved occurrence."""
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    request = _read(directory / 'request.json')
    inputs = _local_grok_inputs(root, request)
    inputs['governance'].update(continue_local_attempt_id=attempt_id,
                                continue_local_request_sha256=request_sha256)
    if dry_run:
        checked = _check_local_grok_continuation(tool, inputs)
        return {'tool': tool.name, 'governed': True, 'would_execute': True,
                'paid_submission': False, 'provider_calls': 0, 'reservations': 0,
                'attempt_id': attempt_id, 'request_sha256': checked['request']['request_sha256'],
                'label': 'same_reserved_local_grok_continuation'}
    return tool.execute(inputs)


def _execute_local_grok_continuation(tool, inputs, invoke):
    """Claim atomically once; execute instrumented implementation without re-reserving."""
    if _ACTIVE.get() is not None:
        _fail('local continuation cannot enter from a nested provider dispatch')
    root = discover_project(inputs)
    if root is None:
        _fail('local continuation requires its enrolled project')
    with _lock(root):
        qualified = _check_local_grok_continuation(tool, inputs)
        directory, request = qualified['directory'], qualified['request']
        claim = {'version': '1.0', 'attempt_id': request['attempt_id'],
                 'request_sha256': request['request_sha256'],
                 'submitted_inputs_sha256': _digest(request['submitted_inputs']),
                 'native_request_sha256': qualified['native']['request_sha256'],
                 'original_sha256': {name: file_sha256(directory / name) for name in _LOCAL_CONTINUATION_FILES}}
        _write_new(directory / 'local_continuation_claim.json', claim)
    submitted = copy.deepcopy(request['submitted_inputs'])
    checked = qualified['checked']
    active = {'provider': request['scope']['provider'], 'provider_called': True,
              'session_id': request['attempt_id'], 'root': root, 'directory': directory,
              'submitted_inputs': submitted,
              'policy_validated': checked.get('policy_validated', False),
              'snapshot_paths': {item['path'] for item in request['input_assets']},
              'contract': checked['contract'], 'shot_id': checked['shot_id']}
    token = _ACTIVE.set(active)
    try:
        result = invoke(submitted)
    except BaseException as exc:
        _save_result(directory, error=exc, prefix='local_continuation_')
        raise
    else:
        retained = _save_result(directory, result=result, prefix='local_continuation_')
        _observe_retained_output(root, request, retained)
        result.data['production_attempt_id'] = request['attempt_id']
        result.data['production_request_sha256'] = request['request_sha256']
        return result
    finally:
        _ACTIVE.reset(token)


def execute_governed(tool, inputs, invoke):
    """Fresh Grok observation occurs outside project/ledger locks for policy dispatch."""
    if isinstance(inputs, dict) and isinstance(inputs.get("governance"), dict) and "continue_local_attempt_id" in inputs["governance"]:
        return _execute_local_grok_continuation(tool, inputs, invoke)
    observation = None
    if (not _ACTIVE.get() and isinstance(inputs, dict) and _kind(tool, inputs) == 'motion'
            and (tool.provider == 'grok_cli' or
                 tool.provider == 'selector' and inputs.get('preferred_provider') == 'grok_cli')):
        context = inputs.get('governance')
        root = None
        if isinstance(context, dict) and context.get('scope_id') and context.get('shot_id'):
            root = discover_project(inputs)
        marker = _read(root / 'project.json') if root is not None else {}
        if (root is not None and marker.get('governance', {}).get('mode') == 'strict'
                and marker.get('governance', {}).get('version') == '1.0'
                and (root / 'production_scopes.json').exists()):
            scopes = _read(root / 'production_scopes.json').get('scopes', [])
            selected = [s for s in scopes if s.get('id') == context.get('scope_id')]
            if len(selected) == 1 and 'derived_from_policy' in selected[0] and selected[0].get('provider') == 'grok_cli':
                from tools._grok_cli_media import observe_grok_cli_compatibility, DEFAULT_GROK_PATH
                import os
                configured = getattr(tool, '_grok_path', None) or os.environ.get('GROK_CLI_PATH', DEFAULT_GROK_PATH)
                observation = observe_grok_cli_compatibility(configured, cwd=inputs.get('cwd') or root)
    token = _GROK_COMPATIBILITY.set(observation or _GROK_COMPATIBILITY.get())
    try:
        return _execute_governed(tool, inputs, invoke)
    finally:
        _GROK_COMPATIBILITY.reset(token)


def _execute_governed(tool, inputs, invoke):
    """Invoke exactly once after durable reservation; nesting cannot reserve/fallback."""
    if tool.provider == 'openart_mcp' or (tool.provider == 'selector' and isinstance(inputs, dict) and inputs.get('preferred_provider') == 'openart_mcp'):
        # The connector adapter reserves at prepare and emits one envelope at
        # begin. Python cannot execute this session-owned connector itself.
        if _ACTIVE.get():
            _fail('nested dispatch cannot enter the OpenArt MCP handoff')
        return invoke(inputs)
    if not isinstance(inputs, dict):
        if _is_openart(tool): _fail('OpenArt requires strict request object')
        return invoke(inputs)
    if _kind(tool, inputs) is None:
        if _is_openart(tool): preflight(tool, inputs)
        return invoke(inputs)
    active = _ACTIVE.get()
    if active:
        if tool.provider == 'selector' or tool.provider != active['provider'] or active['provider_called']:
            _fail('nested dispatch cannot switch providers, retry, or fall back')
        active['provider_called'] = True
        cleaned = _provider_inputs(tool, inputs, active['session_id'])
        # Selectors may adapt aliases but cannot replace the immutable file bytes.
        assets = []
        _paths(cleaned, active['root'], lambda key, path: assets.append(str(path)) or str(path))
        if any(path not in active['snapshot_paths'] for path in assets):
            _fail('nested dispatch introduced an unsnapshotted input')
        if active['contract']:
            if active.get('policy_validated'):
                _check_motion_inputs(active['contract'], active['shot_id'], cleaned, active['root'], policy_composite=True)
            else:
                _check_motion_inputs(active['contract'], active['shot_id'], cleaned, active['root'])
        for key in ('prompt','negative_prompt','duration','resolution','aspect_ratio','voices','endpoint_requirement_id','seed','model','model_name'):
            expected = active['submitted_inputs'].get(key)
            actual = cleaned.get(key)
            if key == 'duration' and isinstance(expected,str) and expected.isdecimal():
                expected = int(expected)
            if expected is not None and actual != expected:
                _fail('nested dispatch changed approved control: ' + key)
        if active['provider'] == 'openart_cli':
            from lib import openart_jobs as jobs
            if jobs.native_request(_openart_controls(cleaned), active['openart_profile'],
                                   dry_run=active['openart_native'].get('dry_run')) != active['openart_native']:
                _fail('nested OpenArt dispatch changed frozen native request')
            if _openart_controls(cleaned) != _openart_controls(active['submitted_inputs']):
                _fail('nested OpenArt dispatch changed frozen provider inputs')
        else:
            _write_new(active['directory'] / 'provider_request.json', cleaned)
        result = invoke(cleaned)
        _write_new(active['directory'] / 'provider_result.json', asdict(result))
        return result
    import time
    from tools import _openart_cli as cli
    dispatch_deadline = time.monotonic()+cli.MAX_TIMEOUT
    checked = preflight(tool, inputs)
    if not checked['governed']:
        return invoke(inputs)
    root = checked['root']
    openart_prepared = None
    session_id = str(uuid.uuid4())
    if checked.get('openart'):
        import time
        from lib import openart_dispatch as credit_dispatch
        from tools import _openart_cli as cli
        openart_prepared = credit_dispatch.prepare_dispatch(inputs, checked, session_id, dispatch_deadline)
    with _lock(root):
        checked = preflight(tool, inputs)  # allowance and source hashes rechecked under lock
        openart_binding = None
        if checked.get('openart'):
            from lib import openart_jobs as jobs
            profile, native = checked['openart']
            openart_binding = {'attempt_id':session_id, 'request_sha256':checked['request_sha256'],
                               'reservation_id':session_id,
                               **{key:native[key] for key in ('native_controls_sha256', 'native_argv_sha256',
                                  'profile_sha256', 'account_id_sha256', 'native_body_sha256')}}
        directory = root / 'production_attempts' / session_id
        (directory / 'inputs').mkdir(parents=True)
        asset_records = []
        def snapshot(key, path):
            digest = _input_sha256(path)
            target = directory / 'inputs' / (digest + path.suffix)
            if not target.exists():
                if path.is_dir():
                    import shutil
                    shutil.copytree(path, target)
                    for child in target.rglob('*'):
                        if child.is_file(): child.chmod(0o444)
                else:
                    target.write_bytes(path.read_bytes())
                    target.chmod(0o444)
            if _input_sha256(target) != digest:
                _fail('input changed while snapshotting')
            asset_records.append({'role':key, 'original_path':str(path), 'path':str(target), 'sha256':digest})
            return str(target)
        submitted = _paths(_clean(inputs), root, snapshot)
        submitted['output_path'] = str(_inside(inputs['output_path'], root))
        if checked['contract']:
            if checked['kind'] == 'local_render':
                _check_local_render_inputs(checked['contract'], checked['shot_id'], submitted, root)
            else:
                if checked.get('policy_validated'):
                    _check_motion_inputs(checked['contract'], checked['shot_id'], submitted, root, policy_composite=True)
                else:
                    _check_motion_inputs(checked['contract'], checked['shot_id'], submitted, root)
        # Rebind actual submitted snapshot bytes to ORIGINAL approved locations.
        # Rereading original files alone is insufficient if they changed then reverted.
        bindings = iter(asset_records)
        def snapshot_binding(key, path):
            record = next(bindings)
            if record['role'] != key or record['path'] != str(path):
                _fail('snapshot occurrence differs from approved input layout')
            return {'path':record['original_path'],'sha256':_input_sha256(path)}
        snapshot_request = _paths(submitted, root, snapshot_binding)
        if _digest(snapshot_request) != checked['request_sha256']:
            _fail('input changed during reservation')
        submitted = _provider_inputs(tool, submitted, session_id)
        if openart_binding:
            for key in ('compiled_request_id', 'preparation_review_id', 'credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256', 'unknown_cost_authorization_id', 'unknown_cost_evidence_id'):
                if key in inputs:
                    submitted[key] = inputs[key]
        scope = checked['scope']
        request = {'version':'1.0', 'attempt_id':session_id, 'cli_session_id':session_id,
            'project_id':checked['marker']['project_id'], 'story_revision':checked['marker']['story_revision'],
            'shot_id':checked['shot_id'], 'scope_id':scope['id'], 'phase':scope['phase'],
            'media_kind':checked['kind'], 'tool_name':tool.name, 'scope_attempt_index':checked['scope_attempt_index'],
            'approval_evidence':{'path':str(directory / ('approval-evidence' + Path(scope['evidence']['path']).suffix)),
                                 'sha256':scope['evidence']['sha256']},
            'request_sha256':checked['request_sha256'], 'contract_sha256':contract_digest(checked['contract']) if checked['contract'] else None,
            'scope':scope, 'submitted_inputs':submitted, 'input_assets':asset_records}
        if openart_binding:
            native = jobs.native_request(_openart_controls(submitted), profile, dry_run=checked['openart'][1].get('dry_run'))
            if native != checked['openart'][1]:
                _fail('OpenArt native request changed during snapshotting')
            preparation_snapshot = None
            if _OPENART_COMPILED_REQUEST_CHECK is _compiled_request_check:
                from lib.production_request import freeze_preparation
                preparation_snapshot = freeze_preparation(session_id, inputs, native, profile)
            frozen = jobs.freeze_request(session_id, submitted, native, profile)
            openart_binding['snapshot_sha256'] = frozen['snapshot_sha256']
            from lib.production_request import freeze_approval, public_scope
            approval_snapshot = freeze_approval(session_id, scope, _inside(scope['evidence']['path'], root).read_bytes())
            request['openart'] = {'binding':openart_binding, 'snapshot':frozen, 'approval_snapshot':approval_snapshot}
            if preparation_snapshot:
                request['openart']['preparation_snapshot'] = preparation_snapshot
            request['scope'] = public_scope(scope)
            request['approval_evidence'] = {'snapshot_id':session_id, 'sha256':scope['evidence']['sha256']}
            request['submitted_inputs'] = _openart_public_inputs(submitted)
        if openart_prepared:
            credit_dispatch.reserve_dispatch(openart_prepared, checked, request, frozen)
        if 'derived_from_policy' in scope and scope['provider'] == 'grok_cli':
            from lib import production_request as preparation
            from lib.production_autonomy import current_projection
            native_policy = preparation.prepare_grok_native(inputs, _GROK_COMPATIBILITY.get())
            compiled_policy = preparation._read(root, 'compiled_request-' + preparation._id(inputs['compiled_request_id']) + '.json')
            review_policy = preparation._read(root, 'preparation_review-' + preparation._id(inputs['preparation_review_id']) + '.json')
            payload = {'compiled': compiled_policy, 'review': review_policy,
                       'source_packet': preparation.source_packet(root, checked['shot_id'], provider='grok_cli'),
                       'native': native_policy, 'projection': current_projection(root, checked['shot_id'])}
            snapshot_path = directory / 'autonomy_preparation.json'
            _write_new(snapshot_path, payload)
            request['autonomy_preparation'] = {'path': str(snapshot_path), 'sha256': file_sha256(snapshot_path)}
        _write_new(directory / 'request.json', request)
        evidence_path = _inside(scope['evidence']['path'], root)
        evidence_copy = directory / ('approval-evidence' + evidence_path.suffix)
        if not openart_binding:
            evidence_copy.write_bytes(evidence_path.read_bytes())
            evidence_copy.chmod(0o444)
            if file_sha256(evidence_copy) != scope['evidence']['sha256']:
                _fail('approval evidence changed during reservation')
        if checked['contract']:
            _write_new(directory / 'shot_contract.json', checked['contract'])
        _write_new(directory / 'selected_attempts.json', load_selected_attempts(root))
        if openart_prepared:
            credit_dispatch.journal_ready(session_id)
    active = {'provider':scope['provider'], 'provider_called':tool.provider != 'selector',
        'session_id':session_id,'root':root,'directory':directory,'submitted_inputs':submitted,
        'policy_validated': checked.get('policy_validated', False), 'snapshot_paths':{item['path'] for item in asset_records}, 'contract':checked['contract'],'shot_id':checked['shot_id']}
    if openart_binding:
        active.update(openart_binding=openart_binding, openart_profile=profile, openart_native=native, openart_deadline=openart_prepared['deadline'] if openart_prepared else None)
    token = _ACTIVE.set(active)
    from lib.events import emit_event
    observation = {'attempt_id': session_id, 'request_sha256': checked['request_sha256'],
                   'project_id': checked['marker']['project_id'],
                   'story_revision': checked['marker']['story_revision'],
                   'shot_id': checked['shot_id'], 'scope_id': scope['id'], 'tool': tool.name,
                   'media_kind': checked['kind']}
    emit_event(root, {'event': 'governed-invocation-start', **observation})
    try:
        result = invoke(submitted)
    except BaseException as exc:
        emit_event(root, {'event': 'governed-invocation-error', **observation})
        _save_result(directory, error=exc)
        raise
    else:
        emit_event(root, {'event': 'governed-invocation-return', **observation})
        try:
            retained = _save_result(directory, result=result)
        except BaseException as exc:
            # Preserve the raw return before fallible output copying, then record
            # uncertainty if storage still permits it. Never report persistence success.
            if not (directory / 'result.json').exists():
                try:
                    _write_new(directory / 'result.json', {'status':'uncertain', 'result':asdict(result),
                        'output':None, 'preserved_output':None, 'exception':repr(exc)})
                except (OSError, ValueError):
                    pass
            raise
        emit_event(root, {'event': 'governed-result-retained', **observation,
                         'status': retained['status'],
                         'output_sha256': (retained.get('output') or {}).get('sha256')})
        result.data['production_attempt_id'] = session_id
        result.data['production_request_sha256'] = checked['request_sha256']
        return result
    finally:
        _ACTIVE.reset(token)


def governed_dry_run(tool, inputs):
    result = preflight(tool, inputs)
    return {'tool':tool.name,'governed':result['governed'], 'would_execute':True,
            'paid_submission':False,'provider_calls':0,'reservations':0,
            'request_sha256':result.get('request_sha256'), 'label':result.get('label', 'governed_preflight')}


def _grok_native_operation(inputs, media_kind='motion'):
    if media_kind == 'image':
        operation = inputs.get('operation')
        if operation == 'generate': operation = None
        mapped = {'generate':'image_gen', 'edit':'image_edit'}.get(inputs.get('generation_mode'))
        if operation and mapped and operation != mapped:
            _fail('conflicting image operation and generation_mode')
        return operation or mapped or 'image_gen'
    operation = inputs.get('operation') or 'image_to_video'
    return 'reference_to_video' if operation == 'first_last_frame' else operation


def _grok_native_arguments(inputs, media_kind='motion'):
    from tools._grok_cli_media import validate_prompt, validate_local_image_paths
    prompt = validate_prompt(inputs.get('prompt'))
    if media_kind == 'image':
        arguments = {'prompt':prompt, 'aspect_ratio':inputs.get('aspect_ratio','auto')}
        if _grok_native_operation(inputs, media_kind) == 'image_edit':
            images = inputs.get('image_paths')
            if images is None and inputs.get('image_path') is not None:
                images = [inputs['image_path']]
            arguments['image'] = validate_local_image_paths(images, field='image_edit', minimum=1, maximum=5)
        return arguments
    arguments = {'prompt': prompt, 'duration':int(inputs.get('duration',6)),
                 'resolution_name':inputs.get('resolution','480p')}
    if inputs.get('operation','image_to_video') == 'image_to_video':
        arguments['image'] = inputs.get('image_path',inputs.get('reference_image_path'))
        return arguments
    arguments['aspect_ratio'] = inputs.get('aspect_ratio','16:9')
    for target, aliases in {'first_frame':('first_frame','image_path','reference_image_path'),
                            'last_frame':('last_frame','last_image_path')}.items():
        for alias in aliases:
            if inputs.get(alias) is not None:
                arguments[target] = inputs[alias]
                break
    if inputs.get('reference_image_paths'):
        arguments['images'] = validate_local_image_paths(inputs['reference_image_paths'],
            field='reference_to_video', minimum=1, maximum=14)
    if inputs.get('keyframes'):
        from tools.video.grok_cli_video import GrokCLIVideo
        arguments['keyframes'] = GrokCLIVideo._normalize_keyframes(inputs['keyframes'], duration=arguments['duration'])
    if inputs.get('voices'):
        arguments['voices'] = inputs['voices']
    return arguments


def _grok_prompt_boundary_original(root, request):
    """Qualify only the old sealed-prompt boundary rejection from original logs."""
    from urllib.parse import quote
    from tools._grok_cli_media import (retained_grok_session_stream, _parse_stream,
        _trusted_session_artifact, _sealed_arguments_match, _probe_artifact, _validate_media_contract)
    directory = root / 'production_attempts' / request['attempt_id']
    state = _state(root, request)
    previous = state.get('result') or {}
    data = previous.get('data', {})
    if (request['scope']['provider'] != 'grok_cli' or request['media_kind'] != 'motion'
            or state['status'] != 'failed' or previous.get('success') is not False
            or previous.get('error') != 'Grok CLI protocol error: Grok changed the sealed media-tool arguments; the artifact was rejected'
            or data.get('error_category') != 'protocol' or data.get('dispatch_status') != 'failed'
            or data.get('session_id') != request['cli_session_id']
            or data.get('dispatch_session_id') != request['cli_session_id']
            or _read(directory / 'raw_result.json') != previous
            or data.get('retry_attempted') is not False or data.get('fallback_attempted') is not False):
        _fail('only an uncertain original or exact sealed-prompt boundary rejection can be reconciled')
    submitted = request['submitted_inputs']
    receipt = data.get('conditioning_receipt', {})
    expected = _grok_native_arguments(submitted, request['media_kind'])
    native_tool = _grok_native_operation(submitted, request['media_kind'])
    if (receipt.get('submitted_arguments') != expected or receipt.get('native_tool') != native_tool
            or receipt.get('provider') != 'grok_cli' or receipt.get('session_id') != request['cli_session_id']):
        _fail('original prompt rejection conditioning differs from frozen request')
    # The original journal chooses the session root, never a caller-supplied source.
    cwd = Path(submitted.get('cwd') or Path(submitted['output_path']).parent).resolve()
    session_directory = Path(data.get('session_directory', '')).expanduser()
    if (session_directory.name != request['cli_session_id']
            or session_directory.parent.name != quote(str(cwd), safe='')
            or session_directory.is_symlink() or not session_directory.is_dir()):
        _fail('original session directory differs from frozen working directory')
    stream = retained_grok_session_stream(session_directory, request['cli_session_id'])
    calls = [json.loads(line) for line in stream.splitlines() if json.loads(line).get('type') == 'tool_call']
    if len(calls) != 1:
        _fail('original prompt recovery requires exactly one native call')
    observed = calls[0].get('rawInput')
    if (not isinstance(observed, dict) or observed.get('prompt') == expected.get('prompt')
            or not _sealed_arguments_match(observed, expected)):
        _fail('original protocol change is not prompt-only ASCII boundary whitespace')
    path, _, session_id = _parse_stream(stream, tool_name=native_tool, expected_arguments=expected)
    if session_id != request['cli_session_id']:
        _fail('original completion session differs')
    source = _trusted_session_artifact(path, session_directory.parent.parent, session_id)
    if not source.is_relative_to(session_directory.resolve()):
        _fail('original artifact differs from exact session directory')
    for asset in request['input_assets']:
        if file_sha256(_inside(asset['path'], root)) != asset['sha256']:
            _fail('original recovery input bytes changed')
    source_sha256 = file_sha256(source)
    metadata = _probe_artifact(source, media_kind='video')
    _validate_media_contract(metadata, tool_name=native_tool, arguments=expected)
    if file_sha256(source) != source_sha256:
        _fail('original artifact changed during probe')
    proof = {'version':'1.0', 'mode':'prompt_boundary_ascii_whitespace', 'session_id':session_id,
             'updates_sha256':file_sha256(session_directory / 'updates.jsonl'),
             'source_sha256':source_sha256, 'media_metadata':metadata,
             'original_sha256':{name:file_sha256(directory / name) for name in ('request.json','raw_result.json','result.json')}}
    return proof, source, receipt, expected


def collect_original_grok_prompt_boundary(project_dir, attempt_id, *, request_sha256, dry_run=True):
    """Collect one original completed artifact; no CLI, network, dispatch or reservation."""
    from tools._grok_cli_media import _copy_and_validate
    from tools.base_tool import ToolResult
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        if request_sha256 != request['request_sha256']:
            _fail('original collection request digest differs')
        proof, source, original_receipt, arguments = _grok_prompt_boundary_original(root, request)
        if dry_run:
            return {'attempt_id':attempt_id, 'request_sha256':request_sha256,
                    'provider_calls':0, 'reservations':0, 'would_collect_original':True,
                    'original_session_recovery':proof}
        output = Path(request['submitted_inputs']['output_path'])
        if output.exists():
            if not output.is_file() or output.is_symlink() or file_sha256(output) != proof['source_sha256']:
                _fail('original collection conflicts with existing output')
            from tools._grok_cli_media import _probe_artifact, _validate_media_contract
            metadata = _probe_artifact(output, media_kind='video')
            _validate_media_contract(metadata, tool_name=original_receipt['native_tool'], arguments=arguments)
        else:
            metadata = _copy_and_validate(source, output, media_kind='video',
                                          tool_name=original_receipt['native_tool'], arguments=arguments)
        if file_sha256(output) != proof['source_sha256'] or file_sha256(source) != proof['source_sha256']:
            _fail('original artifact changed during collection')
        data = copy.deepcopy(_state(root, request)['result']['data'])
        data.update(dispatch_status='completed', source_artifact=str(source), output=str(output),
                    reported_session_id=request['cli_session_id'], original_session_recovery=proof, **metadata)
        data.pop('error_category', None)
        data['conditioning_receipt'].update(dispatch_status='completed', submission_evidence='verified_native_call')
        result = ToolResult(success=True, data=data, artifacts=[str(output)], cost_usd=None)
    reconcile_attempt(root, attempt_id, result, request_sha256=request_sha256)
    result.data.update(production_attempt_id=attempt_id, production_request_sha256=request_sha256)
    return result


def reconcile_attempt(project_dir, attempt_id, result, *, request_sha256):
    """Persist evidence recovered from the original session; never call a provider.

    The recovery caller must supply the original journal request digest and an
    adapter result containing the same session and matching conditioning assets.
    There is intentionally no retry/cache/file-size fallback in this API.
    """
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        if request['scope']['provider'] == 'openart_cli':
            _fail('OpenArt reconciliation requires collect_openart_attempt on the original attempt')
        if _state(root, request)['status'] != 'uncertain':
            proof, source, original_receipt, arguments = _grok_prompt_boundary_original(root, request)
            if (not result.success or result.data.get('original_session_recovery') != proof
                    or result.data.get('source_artifact') != str(source)
                    or file_sha256(Path(request['submitted_inputs']['output_path'])) != proof['source_sha256']):
                _fail('failed reconciliation requires verified original prompt-boundary recovery')
            if any(result.data.get(key) != value for key, value in proof['media_metadata'].items()):
                _fail('original recovery metadata differs from original artifact probe')
        if request_sha256 != request['request_sha256'] or result.data.get('session_id') != request['cli_session_id']:
            _fail('reconciliation does not match original request/session provenance')
        receipt = result.data.get('conditioning_receipt', {})
        if receipt.get('session_id') != request['cli_session_id'] or receipt.get('submission_evidence') != 'verified_native_call':
            _fail('reconciliation lacks verified original-session submission evidence')
        previous = _state(root, request).get('result') or {}
        if not previous and (directory / 'raw_result.json').exists():
            previous = _read(directory / 'raw_result.json')
        original_receipt = previous.get('data', {}).get('conditioning_receipt')
        if original_receipt:
            runtime_fields = {'session_id','cli_version','dispatch_status','submission_evidence'}
            original_request = {key:value for key,value in original_receipt.items() if key not in runtime_fields}
            recovered_request = {key:value for key,value in receipt.items() if key not in runtime_fields}
            if original_request != recovered_request:
                _fail('reconciliation changed the original native request/conditioning receipt')
        if request['scope']['provider'] == 'grok_cli':
            # A process interruption can precede a returned receipt. The durable
            # provider request still binds the recovered native call's controls.
            provider_path = directory / 'provider_request.json'
            submitted = _read(provider_path) if provider_path.exists() else request['submitted_inputs']
            expected_arguments = _grok_native_arguments(submitted, request['media_kind'])
            if (receipt.get('submitted_arguments') != expected_arguments
                    or receipt.get('provider') != request['scope']['provider']
                    or receipt.get('native_tool') != _grok_native_operation(submitted, request['media_kind'])):
                _fail('reconciliation native arguments differ from reserved provider request')
        else:
            _fail('reconciliation lacks qualified native control binding for this provider')
        expected = {(item['path'], item['sha256']) for item in request['input_assets']}
        actual = {(item.get('path'), item.get('sha256')) for item in receipt.get('input_assets', [])}
        if expected != actual:
            _fail('reconciliation input provenance does not match')
        if not result.success:
            if result.data.get('dispatch_status') != 'failed' or receipt.get('dispatch_status') != 'failed':
                _fail('reconciliation outcome is not an authoritative terminal failure')
            record = {'status':'failed', 'result':asdict(result), 'output':None, 'preserved_output':None}
            _write_new(directory / 'reconciliation.json', record)
            return record
        output = Path(request['submitted_inputs']['output_path'])
        if str(output) not in result.artifacts or not output.is_file():
            _fail('reconciliation has no matching original output evidence')
        digest = file_sha256(output)
        preserved = directory / ('output' + output.suffix)
        if preserved.exists():
            if file_sha256(preserved) != digest:
                _fail('reconciliation conflicts with preserved original output')
        else:
            _preserve_output(output, preserved, digest)
        if file_sha256(preserved) != digest:
            _fail('recovered output changed during preservation')
        record = {'status':'generated', 'result':asdict(result),
                  'output':{'path':str(output),'sha256':digest},
                  'preserved_output':{'path':str(preserved),'sha256':digest}}
        _write_new(directory / 'reconciliation.json', record)
        _observe_retained_output(root, request, record)
        return record


def reconcile_veo_invalid_key_rejection(project_dir, attempt_id, *, request_sha256):
    """Close a journaled Veo API-key rejection without reissuing the provider call.

    This only recognizes the provider's explicit API_KEY_INVALID 400 response.
    Other adapter errors remain uncertain and require their own evidence path.
    """
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        if request['attempt_id'] != attempt_id or request['request_sha256'] != request_sha256:
            _fail('invalid-key reconciliation does not match original attempt/request')
        if request['scope']['provider'] != 'veo' or _state(root, request)['status'] != 'uncertain':
            _fail('invalid-key reconciliation requires an uncertain Veo attempt')
        recorded = _read(directory / 'result.json')
        raw = _read(directory / 'raw_result.json')
        result = recorded.get('result')
        error = result.get('error', '') if isinstance(result, dict) else ''
        if (result != raw or result.get('success') is not False
                or result.get('data') != {} or result.get('artifacts') != []
                or result.get('cost_usd') != 0.0 or result.get('duration_seconds') != 0.0
                or recorded.get('output') is not None or recorded.get('preserved_output') is not None
                or recorded.get('exception') is not None
                or '400 INVALID_ARGUMENT' not in error or "'reason': 'API_KEY_INVALID'" not in error):
            _fail('journal lacks an authoritative zero-output API_KEY_INVALID rejection')
        output = Path(request['submitted_inputs']['output_path'])
        if output.exists() or any(directory.glob('output.*')):
            _fail('invalid-key rejection conflicts with an output artifact')
        record = {'status':'failed','result':result,'output':None,'preserved_output':None,
                  'reconciliation_evidence':{'kind':'veo_api_key_invalid_400',
                     'original_request_sha256':request_sha256,
                     'original_result_sha256':file_sha256(directory / 'result.json'),
                     'original_raw_result_sha256':file_sha256(directory / 'raw_result.json')}}
        _write_new(directory / 'reconciliation.json', record)
        return record


def reconcile_veo_unsupported_audio_flag_rejection(project_dir, attempt_id, *, request_sha256):
    """Close a zero-output Developer API rejection of generate_audio=True.

    The provider rejected the request before a media job existed. This is an
    evidence-only journal correction; it never submits or retries generation.
    """
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        if request['attempt_id'] != attempt_id or request['request_sha256'] != request_sha256:
            _fail('unsupported-audio reconciliation does not match original attempt/request')
        if request['scope']['provider'] != 'veo' or _state(root, request)['status'] != 'uncertain':
            _fail('unsupported-audio reconciliation requires an uncertain Veo attempt')
        if request['submitted_inputs'].get('generate_audio') is not True:
            _fail('original Veo request did not submit generate_audio=True')
        recorded = _read(directory / 'result.json')
        raw = _read(directory / 'raw_result.json')
        result = recorded.get('result')
        error = result.get('error', '') if isinstance(result, dict) else ''
        if (result != raw or result.get('success') is not False
                or result.get('data') != {} or result.get('artifacts') != []
                or result.get('cost_usd') != 0.0 or result.get('duration_seconds') != 0.0
                or recorded.get('output') is not None or recorded.get('preserved_output') is not None
                or recorded.get('exception') is not None
                or 'generate_audio parameter is only supported in Gemini Enterprise Agent Platform mode' not in error
                or 'not in Gemini Developer API mode' not in error):
            _fail('journal lacks an authoritative zero-output unsupported-audio rejection')
        output = Path(request['submitted_inputs']['output_path'])
        if output.exists() or any(directory.glob('output.*')):
            _fail('unsupported-audio rejection conflicts with an output artifact')
        record = {'status':'failed','result':result,'output':None,'preserved_output':None,
                  'reconciliation_evidence':{'kind':'veo_generate_audio_unsupported_developer_api',
                     'original_request_sha256':request_sha256,
                     'original_result_sha256':file_sha256(directory / 'result.json'),
                     'original_raw_result_sha256':file_sha256(directory / 'raw_result.json')}}
        _write_new(directory / 'reconciliation.json', record)
        return record


def load_shot_contract(project_dir):
    return _read(_artifact_path(Path(project_dir).resolve(), 'shot_contract.json'))


def _mcp_state(root, attempt_id):
    """Canonical MCP original state: raw 'collected' is generated only after the
    retained history/resource/download/output chain and current bytes verify."""
    from lib import openart_mcp_jobs as jobs
    state = jobs.attempt_state(root, attempt_id)
    if state['status'] != 'collected':
        return state
    retained = jobs.provenance_record(root, attempt_id)
    output = retained['output']
    if file_sha256(_inside(output['path'], root)) != output['sha256']:
        _fail('OpenArt MCP collected original output bytes differ')
    return {'attempt_id': attempt_id, 'status': 'generated', 'output': copy.deepcopy(output),
            'history_id': state.get('history_id'),
            'submission_evidence': 'agent_recorded_connector',
            'transport': 'agent_mediated_connector'}


def load_attempt_result(project_dir, attempt_id):
    root = Path(project_dir).resolve()
    connector_directory = _inside(Path('openart_mcp') / 'attempts' / attempt_id, root)
    if connector_directory.is_dir():
        return _mcp_state(root, attempt_id)
    directory = _inside(Path('production_attempts') / attempt_id, root)
    return _state(root, _read(directory / 'request.json'))


def record_derived_edit(project_dir, record):
    """Retain an explicitly approved local edit without altering native journals.

    The caller supplies exact recipe, execution and sampling receipts plus root
    approval. This publishes provenance only, never a passing semantic review.
    """
    import shutil
    from lib.production_provenance import validate_derived_edit
    root = Path(project_dir).resolve()
    with _lock(root):
        digest = record.get('output', {}).get('sha256')
        import re
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            _fail('derived edit requires exact output hash')
        directory = root / 'production_derived_edits' / digest
        path = directory / 'record.json'
        source = _inside(record['output']['path'], root)
        preserved = _inside(record['preserved_output']['path'], root)
        if preserved != directory / 'output.mp4' or source == preserved or source.is_relative_to(root / 'production_attempts'):
            _fail('derived edit needs separate preserved bytes outside native attempts')
        if file_sha256(source) != digest or record['preserved_output']['sha256'] != digest:
            _fail('derived edit bytes differ from exact binding')
        directory.mkdir(parents=True, exist_ok=True)
        if not preserved.exists():
            with source.open('rb') as incoming, preserved.open('xb') as retained:
                shutil.copyfileobj(incoming, retained)
            preserved.chmod(0o444)
        checked = validate_derived_edit(root, path, attempt_id=record['parent_attempt_id'],
            shot_id=record['shot_id'], story_revision=record['story_revision'],
            expected_output=record['output'], record=record)
        if path.exists():
            if _read(path) != record:
                _fail('immutable derived record already exists with different evidence')
        else:
            _write_new(path, record)
        return checked


def sample_local_render_outgoing(project_dir, attempt_id):
    """Run the registered sampler on the exact retained clip's final frame.

    Writes one immutable actual invocation/result receipt; never supplies review.
    """
    from tools.tool_registry import registry
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        state = load_attempt_result(root, attempt_id)
        if request.get('media_kind') != 'local_render' or state.get('status') != 'generated':
            _fail('outgoing sampling requires a completed local render')
        output = state['output']
        if file_sha256(_inside(output['path'],root)) != output['sha256']:
            _fail('local render bytes changed before outgoing sampling')
        receipt_path = directory / 'outgoing_sampling.json'
        if receipt_path.exists():
            _fail('outgoing sampling already recorded')
        data = state['result']['data']
        duration = request['submitted_inputs']['duration']
        timestamp = duration - 1 / data['fps']
        inputs = {'input_path':output['path'],'strategy':'timestamps','timestamps':[timestamp],
                  'format':'png','output_dir':str(directory / 'outgoing_frames')}
        registry.discover()
        sampler = registry.get('frame_sampler')
        if sampler is None or sampler.provider != 'ffmpeg':
            _fail('registered frame sampler unavailable')
        result = sampler.execute(inputs)
        frame = None
        if result.success and len(result.data.get('frames', [])) == 1:
            path = _inside(result.data['frames'][0]['path'],root)
            frame = {'path':str(path),'sha256':file_sha256(path)}
        receipt = {'version':'1.0','tool':sampler.name,'provider':sampler.provider,
                   'input':output,'submitted_inputs':inputs,'tool_result':asdict(result),'outgoing_frame':frame}
        _write_new(receipt_path,receipt)
        if frame is None:
            _fail('actual outgoing sampler failed; retained receipt requires investigation')
        Path(frame['path']).chmod(0o444)
        return frame


def record_selection(project_dir, shot_id, selection):
    """Publish a reviewer-authored selection after factual provenance checks.

    The passed review supplies semantic judgment; this helper only validates its
    binding and current evidence. Each superseding selection remains in history.
    """
    from lib.shot_contract import selection_digest, UPSTREAM_PREDICATES, provisional_audio_review
    from schemas.artifacts import load_schema
    from jsonschema import Draft202012Validator
    import os
    root = Path(project_dir).resolve()
    with _lock(root):
        attempt_id = selection.get('attempt_id')
        if not isinstance(attempt_id, str):
            _fail('selection requires an attempt_id')
        directory = _inside(Path('production_attempts') / attempt_id, root)
        connector_directory = _inside(Path('openart_mcp') / 'attempts' / attempt_id, root)
        if connector_directory.is_dir():
            directory = connector_directory
        marker = _read(root / 'project.json')
        from lib.production_provenance import validate_attempt_provenance
        validated = validate_attempt_provenance(root, attempt_id, shot_id=shot_id,
            story_revision=marker['story_revision'], expected_output=selection.get('output'))
        request, result = validated['request'], validated['result']
        if request['shot_id'] != shot_id or request['project_id'] != marker['project_id'] or request['story_revision'] != marker['story_revision']:
            _fail('selection does not match current project/story/shot')
        selected_output = validated.get('selected_output', result.get('output'))
        if result['status'] != 'generated' or selection.get('output') != selected_output:
            _fail('selection lacks exact generated output provenance')
        if request.get('media_kind') == 'local_render' and not validated.get('local_render_outgoing'):
            _fail('local render selection requires actual outgoing sampling')
        if validated.get('local_render_outgoing') and selection.get('outgoing_frame') != validated['local_render_outgoing']:
            _fail('selection outgoing frame differs from actual local render sampling')
        if validated.get('derived_edit') and selection.get('outgoing_frame') != validated['selected_outgoing_frame']:
            _fail('selection outgoing frame differs from approved derived edit')
        for role in ('output','outgoing_frame'):
            record = selection.get(role, {})
            if not record.get('path') or file_sha256(_inside(record['path'],root)) != record.get('sha256'):
                _fail('selection bytes missing or changed: ' + role)
        review = selection.get('review', {})
        schema = load_schema('shot_contract')
        failures = list(Draft202012Validator({'$defs':schema['$defs'],'$ref':'#/$defs/review'}).iter_errors(review))
        if failures:
            _fail('selection requires a valid named review: ' + failures[0].message)
        from lib.production_draft import accepted_draft_predicate
        accepted = accepted_draft_predicate(selection, root, shot_id=shot_id)
        audio_provisional = provisional_audio_review(review, root)
        provisional = audio_provisional or accepted is not None
        if (review['status'] != 'pass' and not provisional) or review['subject_sha256'] != selection_digest(selection) or review['story_revision'] != marker['story_revision']:
            _fail('selection review is failed or stale')
        predicates = {item['name']:item for item in review['predicates']}
        if (len(predicates) != len(review['predicates']) or not UPSTREAM_PREDICATES.issubset(predicates)
                or any(item.get('severity') == 'cosmetic' for name, item in predicates.items() if name in UPSTREAM_PREDICATES)
                or any(item['status'] != 'pass' and not ((audio_provisional and name == 'speaker_source' and item['status'] == 'unknown') or (name == accepted and item['status'] == 'fail'))
                       for name, item in predicates.items() if item.get('severity','critical') == 'critical' or name in UPSTREAM_PREDICATES)):
            _fail('selection has missing/failed critical predicates')
        # A shot changed by an authorized planning revision needs a fresh
        # selection bound to that revision and the current contract bytes.
        from lib.production_continuity import revision_chain, require_selection_binding
        current_contract = load_shot_contract(root)
        chain = revision_chain(root, current_contract)
        if chain:
            require_selection_binding(chain, selection, current_contract, shot_id)
        selected = load_selected_attempts(root)
        selected[shot_id] = copy.deepcopy(selection)
        history = root / 'production_selections'
        history.mkdir(exist_ok=True)
        _write_new(history / (str(uuid.uuid4()) + '.json'), {'shot_id':shot_id,'selection':selection})
        canonical = root / 'artifacts' / 'selected_attempts.json'
        canonical.parent.mkdir(exist_ok=True)
        # Avoid silently diverging an existing supported legacy selection file.
        path = _artifact_path(root, 'selected_attempts.json')
        temp = path.with_name('.selected-attempts-' + str(uuid.uuid4()) + '.tmp')
        with temp.open('x') as stream:
            json.dump(selected, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        return copy.deepcopy(selection)


def record_rejection(project_dir, attempt_id, review):
    """Preserve a named failed review; a rejection never grants repair authority."""
    from schemas.artifacts import load_schema
    from jsonschema import Draft202012Validator
    root = Path(project_dir).resolve()
    schema = load_schema('shot_contract')
    validator = Draft202012Validator({'$defs':schema['$defs'],'$ref':'#/$defs/review'})
    connector_directory = _inside(Path('openart_mcp') / 'attempts' / attempt_id, root)
    if connector_directory.is_dir():
        # MCP originals live in the connector journal; the review binds to the frozen
        # authority scope and the intact collected original, never a fresh request.
        from lib import openart_mcp_jobs as mcp_jobs
        from lib.production_provenance import validate_attempt_provenance
        from lib.production_request import digest as request_digest
        with _lock(root):
            frozen = mcp_jobs.frozen_request(root, attempt_id)
            authority = frozen['authority']
            scope = authority['scope']
            if (request_digest(scope) != authority['scope_sha256'] or scope.get('provider') != 'openart_mcp'
                    or authority.get('provider') != 'openart_mcp'):
                _fail('MCP rejection original scope identity differs')
            state = load_attempt_result(root, attempt_id)
            if state['status'] != 'generated':
                _fail('rejection requires a named failed review bound to the generated output')
            validate_attempt_provenance(root, attempt_id, shot_id=authority['shot_id'],
                story_revision=scope['story_revision'], expected_output=state['output'])
            invalid = list(validator.iter_errors(review))
            if (invalid or review.get('status') != 'fail'
                    or review.get('subject_sha256') != state['output']['sha256']
                    or review.get('story_revision') != scope['story_revision']):
                _fail('rejection requires a named failed review bound to the generated output')
            reviews = connector_directory / 'rejections'
            reviews.mkdir(exist_ok=True)
            _write_new(reviews / (str(uuid.uuid4()) + '.json'), review)
            return copy.deepcopy(review)
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        state = _state(root, request)
        invalid = list(validator.iter_errors(review))
        if invalid or state['status'] != 'generated' or review.get('status') != 'fail' or review.get('subject_sha256') != state['output']['sha256'] or review.get('story_revision') != request['story_revision']:
            _fail('rejection requires a named failed review bound to the generated output')
        reviews = directory / 'rejections'
        reviews.mkdir(exist_ok=True)
        _write_new(reviews / (str(uuid.uuid4()) + '.json'), review)
        return copy.deepcopy(review)


def _openart_public_inputs(inputs):
    from tools._openart_cli import redact
    public = redact(copy.deepcopy(inputs))
    public['prompt'] = '<private OpenArt prompt>'
    return public


def load_openart_frozen(request):
    """Pure private snapshot replay; public journal never carries native secrets."""
    from lib import openart_jobs as jobs
    from lib.production_request import load_private_approval
    load_private_approval(request)
    frozen = jobs.load_frozen_request(request['attempt_id'])
    if frozen['snapshot_sha256'] != request['openart']['snapshot']['snapshot_sha256']:
        _fail('OpenArt private request snapshot changed')
    if _openart_public_inputs(frozen['inputs']) != request['submitted_inputs']:
        _fail('OpenArt public/private input binding differs')
    native = jobs.native_request(_openart_controls(frozen['inputs']), frozen['profile'],
                                 dry_run=frozen['native'].get('dry_run'))
    if native != frozen['native']:
        _fail('OpenArt frozen native request differs')
    binding = request['openart']['binding']
    if binding['attempt_id'] != request['attempt_id'] or binding['request_sha256'] != request['request_sha256']:
        _fail('OpenArt original attempt/request binding differs')
    for key in ('native_controls_sha256','native_argv_sha256','profile_sha256','account_id_sha256','native_body_sha256'):
        if binding[key] != native[key]:
            _fail('OpenArt original native/profile/account binding differs')
    return frozen


def collect_openart_attempt(project_dir, attempt_id, *, request_sha256, timeout=30):
    """Recover/collect only the original launch; all CLI work is outside project locks."""
    from lib import openart_jobs as jobs
    from tools.base_tool import ToolResult
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        if request.get('attempt_id') != attempt_id or request.get('request_sha256') != request_sha256:
            _fail('OpenArt collection original request/attempt differs')
        if request['scope']['provider'] != 'openart_cli':
            _fail('OpenArt collection requires original OpenArt attempt')
        frozen = load_openart_frozen(request)
        if (directory / 'reconciliation.json').exists():
            retained = _read(directory / 'reconciliation.json')
            stable = (jobs.verify_collection_receipt(attempt_id, frozen['profile'])
                      if retained.get('status') == 'generated' else
                      jobs.verify_terminal_failure(attempt_id, frozen['profile']))
            if retained.get('result',{}).get('data',{}).get('openart_evidence') != stable:
                _fail('OpenArt retained reconciliation differs from terminal receipt')
            return retained
        launch = jobs.launch_record(attempt_id)
        if not launch or launch.get('binding') != request['openart']['binding']:
            _fail('OpenArt private original launch binding differs')
        output = _inside(frozen['inputs']['output_path'], root)
    jobs.recover_launch(attempt_id)
    collected = jobs.collect_job(attempt_id, output_path=output, output_root=root,
                                 profile=frozen['profile'], timeout=timeout)
    evidence = jobs.reconcile_job(attempt_id)
    with _lock(root):
        if _read(directory / 'request.json') != request:
            _fail('OpenArt request changed during collection')
        if (directory / 'reconciliation.json').exists():
            return _read(directory / 'reconciliation.json')
        events = directory / 'collection_events'
        events.mkdir(exist_ok=True)
        public_event = {key:collected.get(key) for key in ('status','billing','release_authorized')}
        public_event['events_sha256'] = evidence.get('events_sha256')
        _write_new(events / (str(uuid.uuid4()) + '.json'), public_event)
        if collected.get('status') not in {'collected','failed_terminal'}:
            return public_event
        if evidence.get('binding') != request['openart']['binding'] or evidence.get('state') != collected['status']:
            _fail('OpenArt terminal job evidence differs')
        stable = (jobs.verify_collection_receipt(attempt_id, frozen['profile'])
                  if collected['status']=='collected' else
                  jobs.verify_terminal_failure(attempt_id, frozen['profile']))
        proof_snapshot = (stable.get('evidence',{}).get('snapshot_sha256')
                          if collected['status']=='collected' else stable.get('binding',{}).get('snapshot_sha256'))
        if (stable.get('attempt_id') != attempt_id or stable.get('binding') != request['openart']['binding']
                or proof_snapshot != request['openart']['snapshot']['snapshot_sha256']
                or not stable.get('job_id_sha256')):
            _fail('OpenArt terminal collection snapshot/profile/account differs')
        if collected['status']=='failed_terminal' and (not stable.get('terminal_failure_sha256')
                or stable.get('account_id_sha256') != request['openart']['binding']['account_id_sha256']
                or stable.get('process_state') not in {'exited','dead'}):
            _fail('OpenArt terminal failure lacks original account/job/process evidence')
        data = {'provider':'openart_cli','attempt_id':attempt_id,
                'dispatch_status':'completed' if collected['status']=='collected' else 'failed',
                'openart_evidence':stable,
                'billing':'unknown', 'release_authorized':False}
        result = ToolResult(success=collected['status']=='collected', data=data, cost_usd=None,
                            model=frozen['inputs'].get('model'))
        record = {'status':'failed','result':None,'output':None,'preserved_output':None}
        if result.success:
            got = collected.get('output')
            if not isinstance(got,dict) or stable.get('output') != got:
                _fail('OpenArt collected output differs from original-job terminal receipt')
            output = _inside(got['path'], root)
            digest = file_sha256(output)
            if digest != got['sha256']:
                _fail('OpenArt collected output bytes changed')
            preserved = directory / ('output' + output.suffix)
            if not preserved.exists(): _preserve_output(output, preserved, digest)
            if file_sha256(preserved) != digest: _fail('OpenArt preserved output differs')
            record.update(status='generated',output={'path':str(output),'sha256':digest},
                          preserved_output={'path':str(preserved),'sha256':digest})
            result.artifacts = [str(output)]
        record['result'] = asdict(result)
        _write_new(directory / 'reconciliation.json', record)
        _observe_retained_output(root, request, record)
        return record


def _approved_openart_upload(project_root, upload_id, source_sha256):
    from lib.production_request import approved_upload_lookup
    return approved_upload_lookup(project_root, upload_id, source_sha256)


from lib import openart_jobs as _openart_jobs
_openart_jobs.register_upload_approval_lookup(_approved_openart_upload)


def prepare_openart_mcp_handoff(inputs, *, attempt_id=None):
    """Rooted Strict authority for connector prepare and its one original begin.

    This returns factual bindings only after rereading retained project approval;
    callers cannot provide an authority object to grant themselves dispatch.
    """
    from types import SimpleNamespace
    from lib.production_request import digest, validate_preparation
    tool = SimpleNamespace(provider='openart_mcp', name='openart_mcp_video', capability='video_generation', tier=None)
    token = _MCP_REVALIDATING.set(attempt_id)
    try:
        checked = preflight(tool, inputs)
        if not checked.get('governed'):
            _fail('OpenArt MCP requires rooted Strict governance')
        profile, native = checked['openart']
        proof = validate_preparation(inputs, native, profile)
        from lib.openart_mcp_dispatch import validate_billing_authority
        billing = validate_billing_authority(inputs, checked, native)
        return {'provider': 'openart_mcp', 'scope_sha256': digest(checked['scope']),
                'scope': copy.deepcopy(checked['scope']),
                'scope_id': checked['scope']['id'], 'shot_id': checked['shot_id'],
                'scope_attempt_index': checked['scope_attempt_index'],
                'request_sha256': checked['request_sha256'],
                'account_uid_sha256': native['account_binding']['uid_sha256'],
                'model': native['model'], 'mode': native['mode'],
                'purpose': 'qualification' if checked['marker'].get('pipeline_type') == 'provider-qualification' else 'production',
                'native_body_sha256': native['body_sha256'],
                'source_binding_sha256': native['source_binding_sha256'],
                'compiled_source_binding_sha256': digest(_read(_artifact_path(checked['root'], 'compiled_request-' + inputs['compiled_request_id'] + '.json'))['source_binding']),
                'preparation': proof, 'billing': billing,
                'output_path': str(_inside(inputs['output_path'], checked['root']))}
    finally:
        _MCP_REVALIDATING.reset(token)
