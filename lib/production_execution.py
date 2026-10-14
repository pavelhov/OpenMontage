"""Fail-closed, factual production dispatch shared by every BaseTool.

Strict enrollment is explicit in project.json. Legacy diagnostics are untouched.
Approval sidecar production_scopes.json contains version=1.0 and scopes with:
{id, status:approved, approved_by, evidence:{path,sha256}, project_id,
story_revision, phase:first_pass|image|audio, provider, requests:{shot_id:
request_digest}, attempts_per_shot:{shot_id:int}, approval_plan_sha256 (motion)}.
Each planned clip has exactly one original request; nothing is resubmitted.

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
                if allow_upstream and isinstance(item['source_path'], dict) and '$board' in item['source_path']:
                    from lib.shot_contract import shot_local
                    if not shot_local(load_shot_contract(root)):
                        _fail('declared board inputs require prospective shot-local planning')
                    _board_binding(item['source_path'])
                    result.append(copy.deepcopy(item))
                    continue
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
            if allow_upstream and isinstance(value, dict) and '$board' in value:
                from lib.shot_contract import shot_local
                if not shot_local(load_shot_contract(root)):
                    _fail('declared board inputs require prospective shot-local planning')
                _board_binding(value)
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


def approval_plan_digest(contract, shot_id=None):
    """Stable user scope: retain semantics, omit only observed serial handoff facts."""
    from lib.shot_contract import shot_local, shot_planning_subject
    if shot_id is not None and shot_local(contract):
        return _digest(shot_planning_subject(contract, shot_id, approval=True))
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


def _board_binding(item):
    binding = item.get('$board')
    if (set(item) != {'$board'} or not isinstance(binding, dict)
            or set(binding) != {'asset_id'} or not isinstance(binding['asset_id'], str)
            or not binding['asset_id'].strip()):
        _fail('board input must name one exact declared asset_id')
    return binding


def resolve_board_input(project_dir, binding):
    """Resolve a reviewed canonical board to real bytes, without host work."""
    from lib import production_boards as boards, production_images as images
    root = Path(project_dir).resolve()
    aid = binding['asset_id']
    pack = boards.load_board_pack(root)
    current = pack['board_bindings'].get(aid)
    if not current or not boards._binding_current(root, pack, aid):
        _fail('declared board input has no current original bytes')
    roles = boards._roles(pack, aid)
    if not roles or any(boards._review_blockers(pack, aid, role) for role in roles):
        _fail('declared board input lacks current role review')
    if current.get('origin') == 'canonical_image_import':
        state = images.image_call_status(root, current['call_id'])
        if (state['slot_id'] != aid or state['state'] != 'imported' or not state.get('output_current')
                or state['output']['sha256'] != current['sha256']):
            _fail('declared board input differs from same-project original call')
    elif current.get('origin') != 'reuse':
        _fail('declared board input lacks qualified canonical attribution')
    elif 'upstream_source' in current or 'upstream_source' in pack['prompt_pack'].get(aid, {}):
        # Reused outgoing frame stays tied to the exact current selection.
        node = pack['prompt_pack'].get(aid, {})
        source = current.get('upstream_source') or {}
        selection = load_selected_attempts(root).get(source.get('shot_id'))
        contract = load_shot_contract(root)
        if (not isinstance(selection, dict) or source.get('shot_id') != node.get('upstream_source', {}).get('shot_id')
                or _validated_upstream_binding(root, contract, node['shot_id'], source['shot_id'], selection) != source
                or _inside(selection['outgoing_frame']['path'], root) != _inside(current['path'], root)):
            _fail('reused upstream board differs from the current selected outgoing frame')
    return {'path': str(_inside(current['path'], root)), 'sha256': current['sha256']}


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
        if isinstance(item, dict) and '$board' in item:
            if key not in INPUT_PATH_KEYS:
                _fail('declared board must be an asset input')
            return resolve_board_input(root, _board_binding(item))
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


def explicit_motion_duration(inputs, *, provider=None):
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
    # Historical compact MCP templates emitted the same authored duration in
    # both public and native fields. This exact connector spelling is one
    # value; conflicting aliases, booleans and other transports stay refused.
    if (provider == 'openart_mcp' and len(values) == 2 and 'duration' in inputs and 'duration' in params
            and 'videoDuration' not in params
            and all(isinstance(inputs.get(k), str) and inputs[k] for k in ('model', 'mode', 'openart_project_id'))
            and type(values[0]) is type(values[1]) and type(values[0]) in (int, float)
            and values[0] == values[1]):
        values = values[:1]
    if len(values) != 1:
        _fail('exactly one explicit motion duration is required; duplicate aliases are ambiguous')
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        _fail('motion duration must be a positive finite JSON number')
    return value


def _check_motion_inputs(contract, shot_id, inputs, root, *, policy_composite=False, provider=None):
    shot = next(item for item in contract['shots'] if item['id'] == shot_id)
    assets = {item['id']: item for item in contract['assets']}
    actual = []
    _paths(inputs, root, lambda key, path: actual.append((key, _input_sha256(path))) or str(path))
    role_keys = {'start_frame': {'first_frame','image_path','reference_image_path'},
                 'end_frame': {'last_frame','last_image_path'},
                 'identity_reference': {'reference_image_paths','images'},
                 'reference_image': {'reference_image_paths','images'},
                 'reference_video': {'reference_video_paths','reference_video_path'},
                 'reference_audio': {'reference_audio_paths','reference_audio_path'},
                 'timed_keyframe': {'image'}}
    from lib import production_boards as boards, production_request as preparation
    consumers, compact_source = [], False
    if (root / boards.ARTIFACT).exists():
        pack = boards.load_board_pack(root)
        consumers = [role for row in pack['specification']['shots']
                     if row['shot_id'] == shot_id for role in row['roles']]
        if pack.get('package_binding'):
            from lib import production_images as images
            authored = json.loads(images._bound_file(root, pack['package_binding'], 'production package').read_text())
            compact_source = authored.get('authoring_form') == preparation.COMPACT_AUTHORING_FORM
    guided = shot.get('reference_mode', contract.get('reference_mode')) == 'reference_guided'
    # Eligibility has already validated these boards' bytes, cast-identity
    # reviews and upstream provenance. Only an exact native pair can carry
    # identity in place of auxiliary reference submissions on a boarded route.
    local_assets = [assets[aid] for aid in shot['asset_ids']]
    pin_boards = {
        role: [a for a in local_assets if any(c['asset_id'] == a['id'] and c['role'] == role for c in consumers)
               or not any(c['asset_id'] == a['id'] and c['role'] in preparation.NATIVE_BOARD_ROLES for c in consumers)
               and a['role'] == role]
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
        native_consumers = [role for role in consumers if role['asset_id'] == asset_id
                            and role['role'] in preparation.NATIVE_BOARD_ROLES]
        if native_consumers:
            for consumer in native_consumers:
                role = consumer['role']
                if guided and not compact_source and role in {'start_frame', 'end_frame'}:
                    continue  # Legacy guided boards are reviewed semantic targets.
                if role == 'end_frame' and not compact_source and not (
                        inputs.get('operation') == 'first_last_frame' or inputs.get('endpoint_requirement_id')
                        or inputs.get('last_frame') or inputs.get('last_image_path')):
                    continue  # Legacy ending targets retain their optional native pin.
                if not any(key in role_keys[role] and digest == asset['sha256'] for key, digest in actual):
                    _fail(f'shot {shot_id}: submitted inputs omit or change {role} {asset_id}')
            continue
        keys = role_keys.get(asset['role'])
        if shot.get('frame_input', {}).get('asset_id') == asset_id:
            keys = role_keys['start_frame']
        if guided and asset['role'] in {'start_frame', 'end_frame'}:
            keys = None  # explicit reviewed targets; required native pins validate at preparation
        if asset['role'] == 'end_frame' and shot.get('frame_input', {}).get('asset_id') != asset_id and not (
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
    for asset_id in shot['asset_ids']:
        asset = assets[asset_id]
        times = [role['time_seconds'] for role in consumers if role['asset_id'] == asset_id and role['role'] == 'timed_keyframe']
        if not times and asset['role'] == 'timed_keyframe' and not any(role['asset_id'] == asset_id for role in consumers):
            times = [asset.get('time_seconds')]
        if any(not any(
                isinstance(frame, dict) and frame.get('timestamp_s') == timestamp
                and isinstance(frame.get('image'), str) and _input_sha256(_inside(frame['image'], root)) == asset['sha256']
                for frame in inputs.get('keyframes', [])) for timestamp in times):
            _fail('submitted timed keyframe source/time differs from shot contract')
    approved_hashes = {assets[item]['sha256'] for item in submitted_asset_ids}
    if any(digest not in approved_hashes for key, digest in actual):
        _fail('submitted inputs contain an unapproved asset')
    if explicit_motion_duration(inputs, provider=provider) != shot['duration_seconds']:
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
    # Existing root upload selectors bind exact local frame aliases. Preserve
    # governed input layout; project only the native builder's ordered role rows.
    selectors = {'image_path': ('first_frame', 'image_upload_id'),
                 'last_image_path': ('last_frame', 'end_image_upload_id')}
    if any(selector in result for _, selector in selectors.values()):
        assets = copy.deepcopy(result.get('input_assets', []))
        for field, (role, selector) in selectors.items():
            if selector in result and field not in result:
                _fail('OpenArt MCP upload selector requires its exact local ' + field)
            if field not in result:
                continue
            if any(asset.get('role') == role for asset in assets):
                _fail('duplicate OpenArt MCP frame alias and canonical role')
            asset = {'role': role, 'source_path': result.pop(field)}
            if selector in result:
                asset['upload_id'] = result.pop(selector)
            assets.append(asset)
        result['input_assets'] = assets
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
    if (tool.provider == 'grok_cli' or tool.provider == 'selector' and inputs.get('preferred_provider') == 'grok_cli') and _kind(tool, inputs) == 'motion':
        from tools.video.grok_cli_video import validate_execution_settings
        validate_execution_settings(inputs)
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
    if phase not in ({'first_pass'} if kind == 'motion' else {kind}):
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
    lineage = {scope_id}
    if scope.get('carried_from') is not None:
        _fail('carried scopes are retired; each planned clip has one original request')
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
        local_applicable = (_local_continuation is not None
            and _same_original_local_grok_applicability(root, _local_continuation))
        if not local_applicable and scope.get('approval_plan_sha256') != approval_plan_digest(
                effective, shot_id if scope.get('planning_mode') == 'shot_local_v1' else None):
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
                _check_motion_inputs(contract, shot_id, _clean(inputs), root, policy_composite=True, provider=tool.provider)
            else:
                _check_motion_inputs(contract, shot_id, _clean(inputs), root, provider=tool.provider)
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
    if scope.get('replaces_attempt_ids'):
        _fail('repair scopes are retired; each planned clip has one original request')
    if kind == 'motion' and previous:
        _fail('the shot already has its one original request; it is never resubmitted')
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
                exclude_attempt_id=resumed)
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
    if provider == 'grok_cli' and kind == 'motion':
        from tools.video.grok_cli_video import build_native_video_request
        native_inputs = _paths(_clean(inputs), root, lambda key, path: str(path))
        # The native builder projects out ROUTE_PLANNING_KEYS itself (shared by
        # direct execute, recovery and reconciliation); the governed digest and
        # submitted_inputs keep the full request.
        build_native_video_request(native_inputs, adapter_version=getattr(tool, 'version', None))
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
        from lib import production_autonomy as _autonomy
        try:
            with _autonomy.no_status_reuse():   # fresh rechecks under the lock
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


def _local_grok_missing_output_cwd(directory, request, prior, raw, *, retained_continuation=False):
    """Replay the exact 0.4.0 pre-compatibility cwd failure, never a label alone.

    That adapter resolves cwd before its first CLI process; its OSError wrapper
    emits this exact shape. Later process failures use capability/headless or a
    non-null version. Modern journals additionally retain the local phase.
    Original records remain immutable; continuation claims bind their hashes.
    """
    import errno
    import os
    from tools._grok_cli_media import MODEL_PROVENANCE, PINNED_MODEL
    from tools.video.grok_cli_video import build_native_video_request
    submitted = request.get('submitted_inputs', {})
    output = Path(submitted.get('output_path', '')).expanduser()
    cwd = output.parent
    if (not output.is_absolute() or not cwd.is_absolute()
            or submitted.get('cwd', str(cwd)) != str(cwd)
            or submitted.get('cli_session_id') != request.get('attempt_id')
            or request.get('tool_name') != 'grok_cli_video'
            or request.get('scope', {}).get('provider') != 'grok_cli'
            or request.get('media_kind') != 'motion'
            or submitted.get('operation') != 'first_last_frame'
            or prior.get('status') != 'failed' or prior.get('result') != raw
            or any(prior.get(k) is not None for k in ('output', 'preserved_output', 'exception'))
            or raw.get('success') is not False or raw.get('artifacts') != []
            or raw.get('error') != 'Grok CLI invalid_argument error: ' + str(
                FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(cwd)))):
        return False
    native = build_native_video_request(submitted, adapter_version='0.4.0', provider='grok_cli')
    if not {'first_frame', 'last_frame'}.issubset(native['arguments']):
        return False
    receipt = dict(native['receipt'], cli_version=None, session_id=request['attempt_id'],
                   dispatch_status='not_dispatched', submission_evidence='unconfirmed')
    endpoints = {item['role']: {'path': item['path'], 'sha256': item['sha256']}
                 for item in native['input_assets'] if item['role'] in {'first_frame', 'last_frame'}}
    expected = {'provider': 'grok_cli', 'model': PINNED_MODEL, **MODEL_PROVENANCE,
                'cli_version': None, 'error_category': 'invalid_argument',
                'dispatch_status': 'not_dispatched', 'retry_attempted': False,
                'fallback_attempted': False, 'diagnostics': {},
                'session_id': request['attempt_id'], 'dispatch_session_id': request['attempt_id'],
                'conditioning_receipt': receipt,
                'endpoint_conditioning': {'requirement_id': submitted.get('endpoint_requirement_id'),
                    'first_frame': endpoints.get('first_frame'), 'last_frame': endpoints.get('last_frame'),
                    'constraint': 'endpoints_only', 'request_sha256': receipt['request_sha256']}}
    data = raw.get('data', {})
    if isinstance(data, dict) and 'local_setup_evidence' in data:
        expected['local_setup_evidence'] = {'version': '1.0', 'phase': 'cwd_resolution',
            'cli_launch_started': False, 'cwd': str(cwd), 'output_path': str(output)}
    if data != expected:
        return False
    root = directory.parent.parent
    if not retained_continuation and (output.exists() or any(directory.glob('output.*'))
            or any((directory / name).exists() or (directory / name).is_symlink()
                   for name in ('provider_request.json', 'provider_result.json', 'reconciliation.json'))):
        return False
    # Bind native receipt hashes to the actual frozen request and all input bytes.
    _local_grok_inputs(root, request)
    bases = {Path.home() / '.grok/sessions', root / 'sessions'}
    if os.environ.get('GROK_SESSIONS_ROOT'):
        bases.add(Path(os.environ['GROK_SESSIONS_ROOT']).expanduser())
    aid = request['attempt_id']
    if not retained_continuation and any((base / aid).exists() or (base / aid).is_symlink()
           or any(base.glob('*/' + aid)) for base in bases):
        return False
    return True


def _local_grok_failure(directory, request, *, retained_continuation=False):
    """Recognize only retained, exactly bound local pre-CLI defects."""
    prior = _read(directory / 'result.json')
    raw = _read(directory / 'raw_result.json')
    if _local_grok_missing_output_cwd(directory, request, prior, raw,
                                      retained_continuation=retained_continuation):
        return raw
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
    # Only after validating the hash-bound once-only claim may real later
    # continuation session/output evidence coexist with the original refusal.
    _local_grok_failure(directory, request, retained_continuation=True)
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


def _same_original_local_grok_applicability(root, request, *, retained_continuation=False):
    """Private applicability of one proven, never-submitted reserved original.

    Unrelated shot semantics/selections may evolve. This grants neither a new
    reservation nor carried authority; exact own sources, approvals and current
    eligibility remain mandatory. Post-dispatch use first validates the immutable
    once-only claim before treating its later genuine session as attributable.
    """
    from lib.production_provenance import planning_closure
    directory = _inside(Path('production_attempts') / request['attempt_id'], root)
    if _read(directory / 'request.json') != request:
        _fail('local continuation request evidence changed')
    if retained_continuation:
        validate_local_grok_continuation_record(root, request)
    else:
        _local_grok_failure(directory, request)
    marker = _read(root / 'project.json')
    scope = request['scope']
    scopes = _read(root / 'production_scopes.json')['scopes']
    frozen = _read(directory / 'shot_contract.json')
    current = load_shot_contract(root)
    if (request.get('tool_name') != 'grok_cli_video' or scope.get('provider') != 'grok_cli'
            or 'derived_from_policy' in scope or request['scope_id'] != scope.get('id')
            or [s for s in scopes if s.get('id') == request['scope_id']] != [scope]
            or request['project_id'] != marker.get('project_id')
            or request['story_revision'] != marker.get('story_revision')
            or contract_digest(frozen) != request['contract_sha256']
            or approval_plan_digest(frozen) != scope.get('approval_plan_sha256')):
        _fail('local continuation original project/scope/plan authority changed')
    for evidence, parent in ((request['approval_evidence'], directory), (scope['evidence'], root)):
        if file_sha256(_inside(evidence['path'], parent)) != scope['evidence']['sha256']:
            _fail('local continuation approval evidence changed')
    sid = request['shot_id']
    def projection(contract):
        shots = [s for s in contract.get('shots', []) if s.get('id') == sid]
        if len(shots) != 1:
            _fail('local continuation own shot missing or duplicated')
        closure = planning_closure(contract, shots[0])
        plan = copy.deepcopy(contract)
        plan.pop('project_review', None)
        plan['shots'] = [copy.deepcopy(s) if s['id'] == sid else {'id': s['id']}
                         for s in contract['shots']]
        for shot in plan['shots']:
            shot.pop('review', None)
        plan['assets'] = sorted([a for a in contract.get('assets', []) if a['id'] in closure],
                                key=lambda a: a['id'])
        if {a['id'] for a in plan['assets']} != closure:
            _fail('local continuation required asset closure is incomplete')
        return plan, closure, shots[0]
    prior_plan, closure, shot = projection(frozen)
    current_plan, _, _ = projection(current)
    if prior_plan != current_plan:
        _fail('local continuation own sources or contract plan changed')
    relevant = {item['shot_id'] for item in shot.get('upstream', [])}
    for asset in frozen.get('assets', []):
        if asset['id'] in closure and asset.get('upstream_source'):
            relevant.add(asset['upstream_source']['shot_id'])
    def dynamic_dependencies(value):
        if isinstance(value, dict):
            if '$upstream' in value:
                relevant.add(_upstream_binding(value)['shot_id'])
            else:
                for item in value.values():
                    dynamic_dependencies(item)
        elif isinstance(value, list):
            for item in value:
                dynamic_dependencies(item)
    dynamic_dependencies(scope['requests'][sid])
    selected = load_selected_attempts(root)
    frozen_selected = _read(directory / 'selected_attempts.json')
    if any(selected.get(key) != frozen_selected.get(key) for key in relevant):
        _fail('local continuation selected upstream evidence changed')
    restored = _local_grok_inputs(root, request)
    checked = validate_shot_contract(current, project_dir=root, shot_id=sid,
        story_revision=marker['story_revision'], selected_upstream=selected)
    if not checked['eligible']:
        _fail('local continuation current eligibility: ' + '; '.join(checked['errors']))
    _check_motion_inputs(current, sid, _clean(restored), root)
    return True


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
    _same_original_local_grok_applicability(root, request)
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


def _fresh_policy_grok_observation(tool, inputs):
    """Observe Grok CLI compatibility outside locks for a derived Grok policy scope."""
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
    return observation


def execute_governed(tool, inputs, invoke):
    """Fresh Grok observation occurs outside project/ledger locks for policy dispatch."""
    if isinstance(inputs, dict) and isinstance(inputs.get("governance"), dict) and "continue_local_attempt_id" in inputs["governance"]:
        return _execute_local_grok_continuation(tool, inputs, invoke)
    observation = _fresh_policy_grok_observation(tool, inputs)
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
                _check_motion_inputs(active['contract'], active['shot_id'], cleaned, active['root'], policy_composite=True, provider=tool.provider)
            else:
                _check_motion_inputs(active['contract'], active['shot_id'], cleaned, active['root'], provider=tool.provider)
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
    # Retained two-phase check (KTD3): the unlocked preflight rejects invalid
    # requests leaving the project byte-identical (no lock file) and supplies
    # the OpenArt CLI quote read outside the lock; the locked preflight is the
    # authoritative drift recheck of sources, authority and counters.
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
        if not checked.get('governed'):
            _fail('strict project dispatch requires governed authority')
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
                    _check_motion_inputs(checked['contract'], checked['shot_id'], submitted, root, policy_composite=True, provider=tool.provider)
                else:
                    _check_motion_inputs(checked['contract'], checked['shot_id'], submitted, root, provider=tool.provider)
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
            'media_kind':checked['kind'], 'tool_name':tool.name, 'tool_version':getattr(tool, 'version', None),
            'scope_attempt_index':checked['scope_attempt_index'],
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


def _dry_run_preflight(tool, inputs):
    """One operation-local check, including fresh route compatibility evidence.

    Only the dry-run formatter consumes this result. Dispatch always performs
    its own fresh preflights, including the check under the project lock.
    """
    observation = _fresh_policy_grok_observation(tool, inputs)
    token = _GROK_COMPATIBILITY.set(observation or _GROK_COMPATIBILITY.get())
    try:
        return preflight(tool, inputs)
    finally:
        _GROK_COMPATIBILITY.reset(token)


def _dry_run_from_preflight(tool, result):
    """Project immutable dry-run metadata; never admit or dispatch a request."""
    return {'tool':tool.name,'governed':result['governed'], 'would_execute':True,
            'paid_submission':False,'provider_calls':0,'reservations':0,
            'request_sha256':result.get('request_sha256'), 'label':result.get('label', 'governed_preflight')}


def governed_dry_run(tool, inputs):
    return _dry_run_from_preflight(tool, _dry_run_preflight(tool, inputs))


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


def retain_grok_launch(*, session_id, cli_version, grok_path, sessions_root, cwd,
                       native_tool, arguments, adapter_version):
    """Retain actual checked launch metadata before the single native process.

    Called only by the adapter after its real compatibility read. This adds
    attempt evidence without rewriting the frozen approved request.
    """
    import os
    from urllib.parse import quote
    active = _ACTIVE.get()
    if not active or active.get('provider') != 'grok_cli':
        return
    directory = active['directory']
    request = _read(directory / 'request.json')
    if session_id != active['session_id'] or session_id != request['cli_session_id']:
        _fail('Grok launch session differs from reserved original')
    if request['media_kind'] != 'motion':
        return
    expected = _grok_native_arguments(request['submitted_inputs'], request['media_kind'])
    if arguments != expected or native_tool != _grok_native_operation(request['submitted_inputs']):
        _fail('Grok launch controls differ from reserved original')
    # Resolve the actual configured root now; later environment/config changes
    # are not evidence about this launch.
    session_root = Path(sessions_root).expanduser().resolve()
    launch = {'version': '1.0', 'attempt_id': request['attempt_id'],
              'request_sha256': request['request_sha256'],
              'request_record_sha256': file_sha256(directory / 'request.json'),
              'adapter_version': adapter_version, 'cli_version': cli_version,
              'grok_path': str(grok_path), 'sessions_root': str(session_root),
              'cwd': str(cwd), 'session_id': session_id,
              'session_directory': str(session_root / quote(str(cwd), safe='') / session_id),
              'native_tool': native_tool, 'arguments_sha256': _digest(arguments),
              'evidence': 'actual_cli_compatibility_read_before_native_launch'}
    _write_new(directory / 'grok_launch.json', launch)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _retained_grok_launch(directory, request, expected, native_tool, cwd):
    path = directory / 'grok_launch.json'
    if not path.exists():
        return None
    from urllib.parse import quote
    launch = _read(path)
    if (path.is_symlink() or launch.get('version') != '1.0'
            or launch.get('attempt_id') != request['attempt_id']
            or launch.get('request_sha256') != request['request_sha256']
            or launch.get('request_record_sha256') != file_sha256(directory / 'request.json')
            or not isinstance(launch.get('adapter_version'), str) or not launch['adapter_version']
            or launch.get('session_id') != request['cli_session_id']
            or launch.get('cwd') != str(cwd) or launch.get('native_tool') != native_tool
            or launch.get('arguments_sha256') != _digest(expected)
            or not isinstance(launch.get('cli_version'), str) or not launch['cli_version']
            or not isinstance(launch.get('sessions_root'), str)
            or launch.get('evidence') != 'actual_cli_compatibility_read_before_native_launch'
            or launch.get('session_directory') != str(Path(launch['sessions_root']) / quote(str(cwd), safe='') / request['cli_session_id'])):
        _fail('original Grok launch evidence differs from frozen request')
    return launch


def _grok_download_original(root, directory, request, events, expected, native_tool, session, log, log_sha):
    """Bind only the exact original CLI download transport failure, never a new job."""
    import re
    from tools._grok_cli_media import _content_text, _probe_artifact, _validate_media_contract
    original = _read(directory / 'raw_result.json')
    calls = [e for e in events if e.get('type') == 'tool_call']
    updates = [e for e in events if e.get('type') == 'tool_call_update']
    ends = [e for e in events if e.get('type') == 'end']
    failures = [e for e in updates if e.get('status') == 'failed']
    if (len(calls) != 1 or (calls[0].get('toolName') or calls[0].get('title')) != native_tool
            or not isinstance(calls[0].get('toolCallId'), str) or not calls[0]['toolCallId']
            or len(failures) != 1 or any(e.get('toolCallId') != calls[0]['toolCallId']
                or e.get('status') not in {None, 'pending', 'in_progress', 'failed'}
                or e.get('status') != 'failed' and e.get('rawOutput') not in (None, {}) for e in updates)
            or len(ends) != 1 or ends[0].get('sessionId') != request['cli_session_id']
            or ends[0].get('stopReason') != 'end_turn'):
        _fail('original download requires one exact failed native call and terminal session')
    raw = failures[0].get('rawOutput')
    if (not isinstance(raw, dict) or set(raw) != {'error', 'message'}
            or raw['error'] != 'tool_execution_failed' or not isinstance(raw['message'], str)):
        _fail('original download requires exact transport failure output')
    match = re.fullmatch(r'Failed to download video: error sending request for url \((https://vidgen\.x\.ai/xai-vidgen-bucket/xai-video-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.mp4)\)', raw['message'])
    if not match or original.get('error') != 'Grok CLI cli error: ' + _content_text(failures[0]):
        _fail('original download URL or exact returned failure differs')
    binding = {'version': '1.0', 'attempt_id': request['attempt_id'],
        'request_sha256': request['request_sha256'], 'session_id': request['cli_session_id'],
        'native_tool': native_tool, 'arguments_sha256': _digest(expected), 'url': match[1],
        'native_evidence': {'path': str(log), 'sha256': log_sha},
        'original_sha256': {name: file_sha256(directory / name) for name in
            ('request.json', 'raw_result.json', 'result.json', 'grok_launch.json')}}
    proof = {'version': '1.0', 'mode': 'original_download_transport', 'session_id': request['cli_session_id'],
        'session_directory': str(session), 'updates_sha256': log_sha,
        'original_sha256': binding['original_sha256'], 'download_binding': binding}
    receipt_path = directory / 'original_download.json'
    source = directory / 'original_download.mp4'
    if not receipt_path.exists():
        if source.is_symlink() or source.exists() and not source.is_file():
            _fail('unreceipted original download source is not a regular local file')
        # Orphan bytes never qualify by themselves. An interrupted publication
        # repeats only the exact original GET, then checks these bytes against
        # that fresh validated download before retaining the missing receipt.
        return dict(proof, status='download_required'), None
    receipt = _read(receipt_path)
    if (receipt_path.is_symlink() or source.is_symlink() or not source.is_file()
            or set(receipt) != {'binding', 'source', 'media_metadata'} or receipt['binding'] != binding
            or receipt['source'] != {'path': str(source), 'sha256': file_sha256(source)}):
        _fail('original download receipt or source changed')
    metadata = _probe_artifact(source, media_kind='video')
    _validate_media_contract(metadata, tool_name=native_tool, arguments=expected)
    if receipt['media_metadata'] != metadata or file_sha256(source) != receipt['source']['sha256']:
        _fail('original downloaded media differs from retained contract')
    proof.update(source_sha256=receipt['source']['sha256'], media_metadata=metadata,
        download_receipt={'path': str(receipt_path), 'sha256': file_sha256(receipt_path)})
    return proof, source


def _grok_prompt_boundary_original(root, request, *, allow_pending=False, original_download_only=False):
    """Qualify an uncertain original or historical prompt rejection from native logs."""
    from urllib.parse import quote
    from tools._grok_cli_media import (retained_grok_session_stream, _parse_stream,
        _trusted_session_artifact, _sealed_arguments_match, _sealed_prompt_boundary_only,
        _sealed_image_path_alias, _probe_artifact, _validate_media_contract,
        GrokCLIContractError)
    directory = root / 'production_attempts' / request['attempt_id']
    state = _read(directory / 'result.json') if original_download_only else _state(root, request)
    previous = state.get('result') or {}
    data = previous.get('data', {})
    download_failure = (state['status'] == 'failed' and previous.get('success') is False
        and data.get('error_category') == 'cli' and data.get('dispatch_status') == 'failed'
        and 'Failed to download video: error sending request for url (' in str(previous.get('error')))
    boundary = (state['status'] == 'failed' and previous.get('success') is False
        and previous.get('error') == 'Grok CLI protocol error: Grok changed the sealed media-tool arguments; the artifact was rejected'
        and data.get('error_category') == 'protocol' and data.get('dispatch_status') == 'failed')
    uncertain = state['status'] == 'uncertain'
    if (request['scope']['provider'] != 'grok_cli' or request['media_kind'] != 'motion'
            or not (boundary or uncertain or download_failure)):
        _fail('only an uncertain original or exact sealed-prompt boundary rejection can be reconciled')
    if previous and (data.get('session_id') != request['cli_session_id']
            or data.get('dispatch_session_id') != request['cli_session_id']
            or _read(directory / 'raw_result.json') != previous
            or data.get('retry_attempted') is not False or data.get('fallback_attempted') is not False):
        _fail('original returned evidence differs from the exact frozen session')
    submitted = request['submitted_inputs']
    receipt = data.get('conditioning_receipt', {})
    expected = _grok_native_arguments(submitted, request['media_kind'])
    native_tool = _grok_native_operation(submitted, request['media_kind'])
    if receipt and (receipt.get('submitted_arguments') != expected or receipt.get('native_tool') != native_tool
            or receipt.get('provider') != 'grok_cli' or receipt.get('session_id') != request['cli_session_id']):
        _fail('original prompt rejection conditioning differs from frozen request')
    # The original journal chooses the session root, never a caller-supplied source.
    cwd = Path(submitted.get('cwd') or Path(submitted['output_path']).parent).resolve()
    # Returned session diagnostics are retained evidence. For a process crash
    # before a result exists, use only the adapter's explicit configured root.
    launch = _retained_grok_launch(directory, request, expected, native_tool, cwd)
    session_path = (launch or {}).get('session_directory') or data.get('session_directory')
    if launch and data.get('session_directory') and data['session_directory'] != session_path:
        _fail('returned Grok session differs from retained launch')
    if launch and receipt and receipt.get('cli_version') != launch['cli_version']:
        _fail('returned Grok CLI version differs from retained launch')
    if not session_path:
        import os
        sessions_root = os.environ.get('GROK_SESSIONS_ROOT') or str(Path.home() / '.grok' / 'sessions')
        session_path = str(Path(sessions_root) / quote(str(cwd), safe='') / request['cli_session_id'])
    session_directory = Path(session_path).expanduser()
    if (session_directory.name != request['cli_session_id']
            or session_directory.parent.name != quote(str(cwd), safe='')
            or session_directory.is_symlink() or not session_directory.is_dir()):
        _fail('original session directory differs from frozen working directory')
    log = session_directory / ('updates.jsonl' if (session_directory / 'updates.jsonl').exists() else 'events.jsonl')
    if log.is_symlink() or not log.is_file():
        _fail('original recovery requires authoritative native terminal evidence')
    updates_sha256 = file_sha256(log)
    stream = (retained_grok_session_stream(session_directory, request['cli_session_id'])
              if log.name == 'updates.jsonl' else log.read_text())
    events = [json.loads(line) for line in stream.splitlines() if line.strip()]
    if any(not isinstance(event, dict) or not isinstance(event.get('type'), str) for event in events):
        _fail('original recovery native stream is invalid')
    calls = [event for event in events if event.get('type') == 'tool_call']
    if len(calls) != 1:
        _fail('original prompt recovery requires exactly one native call')
    observed = calls[0].get('rawInput')
    image_path_alias = _sealed_image_path_alias(observed, expected)
    prompt_boundary_only = _sealed_prompt_boundary_only(observed, expected)
    if (not isinstance(observed, dict) or boundary and not (prompt_boundary_only or image_path_alias)
            or not _sealed_arguments_match(observed, expected)):
        _fail('original protocol change is not prompt-boundary whitespace or an equivalent sealed image path')
    if download_failure:
        if launch is None:
            _fail('original download recovery requires retained prelaunch evidence')
        for asset in request['input_assets']:
            if file_sha256(_inside(asset['path'], root)) != asset['sha256']:
                _fail('original recovery input bytes changed')
        proof, source = _grok_download_original(root, directory, request, events, expected,
            native_tool, session_directory, log, updates_sha256)
        return proof, source, receipt, expected
    # A concurrent original can have only its exact call and progress updates.
    # Collection must leave that attempt unchanged until its final stream exists;
    # End-bearing and failed records still use the strict final parser below.
    updates = [event for event in events if event.get('type') == 'tool_call_update']
    if (allow_pending and uncertain and not any(event.get('type') == 'end' for event in events)
            and not any(str(event.get('status', '')).lower() == 'failed' for event in updates)):
        call_id = calls[0].get('toolCallId')
        if ((calls[0].get('toolName') or calls[0].get('title')) != native_tool
                or not isinstance(call_id, str) or not call_id
                or any(event.get('toolCallId') != call_id
                       or event.get('status') not in {None, 'pending', 'in_progress', 'completed'}
                       or (event.get('status') != 'completed' and event.get('rawOutput') not in (None, {}))
                       for event in updates)
                or any(event.get('type') not in {'tool_call', 'tool_call_update'} for event in events)):
            _fail('original pending stream differs from the exact native call')
        if any(event.get('status') == 'completed' for event in updates):
            # The native writer appends completion before turn_completed. Only
            # the strict parser's exact missing-end outcome qualifies; duplicate
            # completions, failed output and malformed rawOutput remain errors.
            try:
                _parse_stream(stream, tool_name=native_tool, expected_arguments=expected)
            except GrokCLIContractError as exc:
                if exc.category != 'protocol' or str(exc) != 'expected one terminal end event, observed 0':
                    raise
            else:
                _fail('pending original stream unexpectedly has terminal completion')
        for asset in request['input_assets']:
            if file_sha256(_inside(asset['path'], root)) != asset['sha256']:
                _fail('original recovery input bytes changed')
        return {'status': 'pending', 'attempt_id': request['attempt_id'],
                'request_sha256': request['request_sha256'], 'provider_calls': 0, 'reservations': 0}, None, receipt, expected
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
    if file_sha256(source) != source_sha256 or file_sha256(log) != updates_sha256:
        _fail('original artifact or native session evidence changed during probe')
    if not receipt:
        from tools.video.grok_cli_video import build_native_video_request
        # The launch journal records execution metadata separately from approved
        # controls. Legacy missing metadata stays unknown; current code is no
        # evidence of the version that actually launched an interrupted job.
        version = (launch or {}).get('adapter_version') or (request.get('tool_version') if request.get('tool_name') == 'grok_cli_video' else None)
        native = build_native_video_request(submitted, adapter_version=version, provider='grok_cli')
        receipt = dict(native['receipt'], cli_version=(launch or {}).get('cli_version'), session_id=request['cli_session_id'],
                       dispatch_status='uncertain', submission_evidence='unconfirmed')
    recovery_mode = ('sealed_image_path_equivalence' if boundary and image_path_alias else
                     'prompt_boundary_ascii_whitespace' if boundary else 'uncertain_original_session')
    proof = {'version':'1.0', 'mode':recovery_mode, 'session_id':session_id,
             'updates_sha256':updates_sha256, 'session_directory':str(session_directory),
             'source_sha256':source_sha256, 'media_metadata':metadata,
             'original_sha256':{name:file_sha256(directory / name) for name in ('request.json','raw_result.json','result.json','grok_launch.json') if (directory / name).exists()}}
    return proof, source, receipt, expected


def collect_original_grok_prompt_boundary(project_dir, attempt_id, *, request_sha256, dry_run=True):
    """Collect the exact original; only a bound failed download permits a media GET."""
    from tools._grok_cli_media import _copy_and_validate
    from tools.base_tool import ToolResult
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    with _lock(root):
        request = _read(directory / 'request.json')
        if request_sha256 != request['request_sha256']:
            _fail('original collection request digest differs')
        pending_proof, source, receipt, arguments = _grok_prompt_boundary_original(root, request, allow_pending=True)
        download = pending_proof.get('status') == 'download_required'
        if download and dry_run:
            return {'attempt_id': attempt_id, 'request_sha256': request_sha256,
                'provider_calls': 0, 'reservations': 0, 'would_download_original': True,
                'original_session_recovery': pending_proof}
        if download and Path(request['submitted_inputs']['output_path']).exists():
            _fail('unbound existing output blocks original download')
    if download:
        import os
        import tempfile
        import requests
        from tools._grok_cli_media import _probe_artifact, _validate_media_contract
        staging = None
        try:
            with tempfile.NamedTemporaryFile(prefix='.original-download-', suffix='.mp4', dir=directory, delete=False) as file:
                staging = Path(file.name)
                with requests.get(pending_proof['download_binding']['url'], stream=True,
                        allow_redirects=False, timeout=60) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        _fail('original download requires direct HTTP 200 response')
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        file.write(chunk)
                file.flush(); os.fsync(file.fileno())
            metadata = _probe_artifact(staging, media_kind='video')
            _validate_media_contract(metadata, tool_name=receipt['native_tool'], arguments=arguments)
            digest = file_sha256(staging)
            with _lock(root):
                current, _, _, _ = _grok_prompt_boundary_original(root, request, allow_pending=True)
                if current != pending_proof:
                    _fail('original download evidence changed during transfer')
                source = directory / 'original_download.mp4'
                if source.exists():
                    if source.is_symlink() or not source.is_file() or file_sha256(source) != digest:
                        _fail('orphan original download differs from freshly validated original bytes')
                    source.chmod(0o444)
                else:
                    _preserve_output(staging, source, digest)
                _write_new(directory / 'original_download.json', {'binding': pending_proof['download_binding'],
                    'source': {'path': str(source), 'sha256': digest}, 'media_metadata': metadata})
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
    with _lock(root):
        if _read(directory / 'request.json') != request:
            _fail('original request changed during collection')
        proof, source, original_receipt, arguments = _grok_prompt_boundary_original(root, request, allow_pending=True)
        if proof.get('status') == 'pending':
            return proof
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
        data = copy.deepcopy((_state(root, request).get('result') or {}).get('data', {}))
        from tools._grok_cli_media import PINNED_MODEL, MODEL_PROVENANCE
        data.setdefault('provider', 'grok_cli')
        data.setdefault('model', PINNED_MODEL)
        for key, value in MODEL_PROVENANCE.items():
            data.setdefault(key, value)
        data.setdefault('cli_version', original_receipt.get('cli_version'))
        data.setdefault('conditioning_receipt', copy.deepcopy(original_receipt))
        data.update(session_id=request['cli_session_id'], dispatch_session_id=request['cli_session_id'],
                    retry_attempted=False, fallback_attempted=False)
        data.update(dispatch_status='completed', source_artifact=str(source), output=str(output),
                    reported_session_id=request['cli_session_id'], original_session_recovery=proof, **metadata)
        data.pop('error_category', None)
        data['conditioning_receipt'].update(dispatch_status='completed', submission_evidence='verified_native_call')
        result = ToolResult(success=True, data=data, artifacts=[str(output)], cost_usd=None)
    reconcile_attempt(root, attempt_id, result, request_sha256=request_sha256)
    result.data.update(production_attempt_id=attempt_id, production_request_sha256=request_sha256)
    return result


def _retained_grok_original(root, directory, request, *, request_sha256, dry_run):
    from tools.base_tool import ToolResult
    attempt_id = request['attempt_id']
    retained = _read(directory / 'reconciliation.json')
    if retained.get('status') != 'generated' or not isinstance(retained.get('result'), dict):
        _fail('retained original reconciliation is not collected output')
    result = ToolResult(**retained['result'])
    receipt = result.data.get('conditioning_receipt', {})
    if (not result.success or receipt.get('session_id') != request['cli_session_id']
            or receipt.get('submission_evidence') != 'verified_native_call'
            or receipt.get('submitted_arguments') != _grok_native_arguments(request['submitted_inputs'], 'motion')):
        _fail('retained original reconciliation lacks exact native receipt')
    evidence = _verify_original_grok_motion_success(root, directory, request, result)
    if retained.get('original_session_evidence') != evidence:
        _fail('retained original reconciliation native evidence changed')
    for key in ('output', 'preserved_output'):
        item = retained.get(key, {})
        if not item.get('path') or file_sha256(item['path']) != item.get('sha256'):
            _fail('retained original reconciliation output changed')
    if dry_run:
        return {'attempt_id': attempt_id, 'request_sha256': request_sha256,
                'provider_calls': 0, 'reservations': 0, 'already_collected_original': True}
    result.data.update(production_attempt_id=attempt_id, production_request_sha256=request_sha256)
    return result


def _original_grok_motion_stream(root, directory, request, result):
    """Locate only frozen launch/native evidence, never caller-selected media."""
    from urllib.parse import quote
    from tools._grok_cli_media import retained_grok_session_stream
    submitted = request['submitted_inputs']
    expected = _grok_native_arguments(submitted, 'motion')
    native_tool = _grok_native_operation(submitted, 'motion')
    cwd = Path(submitted.get('cwd') or Path(submitted['output_path']).parent).resolve()
    launch = _retained_grok_launch(directory, request, expected, native_tool, cwd)
    prefix = 'local_continuation_' if (directory / 'local_continuation_claim.json').exists() else ''
    raw_path = directory / (prefix + 'raw_result.json')
    original_path = directory / (prefix + 'result.json')
    if raw_path.exists():
        # Raw native return evidence is authoritative: corrupt bytes fail closed.
        prior = _read(raw_path)
    else:
        try:
            prior = (_read(original_path).get('result') or {}) if original_path.exists() else {}
        except ProductionGovernanceError:
            # Adapter journal parsing may itself have crashed. Preserve these
            # bytes; only exact launch/session/native evidence can recover them.
            prior = {}
    if not isinstance(prior, dict) or not isinstance(prior.get('data', {}), dict):
        _fail('original motion adapter evidence is malformed')
    hints = prior.get('data', {})
    if (any(hints.get(key) is not None and hints[key] != request['cli_session_id']
            for key in ('session_id', 'dispatch_session_id'))
            or launch and hints.get('session_directory') and hints['session_directory'] != launch['session_directory']):
        _fail('original motion adapter evidence contradicts exact frozen launch')
    session_path = ((launch or {}).get('session_directory') or prior.get('data', {}).get('session_directory')
                    or (result.data.get('original_session_recovery') or {}).get('session_directory'))
    if not session_path:
        _fail('original motion reconciliation requires retained exact session evidence')
    session = Path(session_path).expanduser()
    if (session.name != request['cli_session_id'] or session.parent.name != quote(str(cwd), safe='')
            or session.is_symlink() or not session.is_dir()):
        _fail('original motion session differs from frozen launch')
    log = session / ('updates.jsonl' if (session / 'updates.jsonl').exists() else 'events.jsonl')
    if log.is_symlink() or not log.is_file():
        _fail('original motion reconciliation requires retained native terminal evidence')
    log_sha = file_sha256(log)
    stream = (retained_grok_session_stream(session, request['cli_session_id'])
              if log.name == 'updates.jsonl' else log.read_text())
    for asset in request['input_assets']:
        if file_sha256(_inside(asset['path'], root)) != asset['sha256']:
            _fail('original motion reconciliation input bytes changed')
    receipt = result.data.get('conditioning_receipt', {})
    if launch and (receipt.get('cli_version') != launch['cli_version']
                   or receipt.get('adapter_version') != launch['adapter_version']):
        _fail('reconciliation version differs from original launch')
    return submitted, expected, native_tool, session, log, log_sha, stream, launch


def _verify_original_grok_motion_success(root, directory, request, result):
    """Read original native evidence, never caller assertions about canonical media."""
    from tools._grok_cli_media import (_parse_stream, _trusted_session_artifact,
                                     _probe_artifact, _validate_media_contract)
    submitted, expected, native_tool, session, log, log_sha, stream, launch = _original_grok_motion_stream(root, directory, request, result)
    downloaded = result.data.get('original_session_recovery', {}).get('mode') == 'original_download_transport'
    if downloaded:
        proof, source, _, _ = _grok_prompt_boundary_original(root, request, original_download_only=True)
        if source is None or proof != result.data['original_session_recovery']:
            _fail('original downloaded recovery evidence differs')
        session_id = proof['session_id']
    else:
        source_path, _, session_id = _parse_stream(stream, tool_name=native_tool, expected_arguments=expected)
        if session_id != request['cli_session_id']:
            _fail('original motion completion session differs')
        source = _trusted_session_artifact(source_path, session.parent.parent, session_id)
        if not source.is_relative_to(session.resolve()):
            _fail('original motion artifact differs from exact original session')
    digest = file_sha256(source)
    output = _inside(submitted['output_path'], root)
    if output.is_symlink() or not output.is_file() or file_sha256(output) != digest:
        _fail('reconciliation output differs from verified original session bytes')
    if result.data.get('source_artifact') is not None and Path(result.data['source_artifact']).resolve() != source:
        _fail('reconciliation source differs from retained native original')
    for asset in request['input_assets']:
        if file_sha256(_inside(asset['path'], root)) != asset['sha256']:
            _fail('original motion reconciliation input bytes changed')
    metadata = _probe_artifact(source, media_kind='video')
    _validate_media_contract(metadata, tool_name=native_tool, arguments=expected)
    if log_sha != file_sha256(log) or digest != file_sha256(source) or digest != file_sha256(output):
        _fail('original motion evidence changed during reconciliation')
    receipt = result.data.get('conditioning_receipt', {})
    if launch and (receipt.get('cli_version') != launch['cli_version']
                   or receipt.get('adapter_version') != launch['adapter_version']):
        _fail('reconciliation version differs from original launch')
    return {'session_id': session_id, 'session_directory': str(session),
            'native_evidence': {'path': str(log), 'sha256': log_sha},
            'source': {'path': str(source), 'sha256': digest}, 'media_metadata': metadata}


def _verify_original_grok_motion_failure(root, directory, request, result):
    """A caller's failure label cannot erase an original native completion."""
    from tools._grok_cli_media import _sealed_arguments_match, _parse_stream, GrokCLIContractError
    submitted, expected, native_tool, session, log, log_sha, stream, launch = _original_grok_motion_stream(root, directory, request, result)
    try:
        events = [json.loads(line) for line in stream.splitlines() if line.strip()]
    except ValueError:
        _fail('original terminal failure native evidence is invalid')
    if any(not isinstance(event, dict) for event in events):
        _fail('original terminal failure native evidence is invalid')
    calls = [e for e in events if e.get('type') == 'tool_call']
    ends = [e for e in events if e.get('type') == 'end']
    updates = [e for e in events if e.get('type') == 'tool_call_update']
    if (len(calls) != 1 or calls[0].get('toolName') != native_tool
            or not isinstance(calls[0].get('toolCallId'), str) or not calls[0]['toolCallId']
            or not _sealed_arguments_match(calls[0].get('rawInput'), expected)
            or len(ends) != 1 or ends[0].get('sessionId') != request['cli_session_id']
            or ends[0].get('stopReason') != 'end_turn'
            or any(str(e.get('status', '')).lower() == 'completed' for e in updates)):
        _fail('original terminal failure lacks exact failed native completion; completed or uncertain original remains collectable')
    failures = [e for e in updates if e.get('toolCallId') == calls[0]['toolCallId'] and str(e.get('status', '')).lower() == 'failed']
    raw_failure = failures[0].get('rawOutput') if len(failures) == 1 else None
    # The adapter's failed native contract uses error/message-only rawOutput.
    # It is diagnostic evidence; paths/resources/media output are not failures.
    error_only = (raw_failure is None or raw_failure == {} or
                  isinstance(raw_failure, dict) and set(raw_failure) <= {'error', 'message'}
                  and all(isinstance(v, str) for v in raw_failure.values()))
    if (len(failures) != 1 or not error_only
            or any(p.is_file() and p.suffix.lower() in {'.mp4', '.mov', '.webm', '.m4v', '.mkv', '.avi'} for p in session.rglob('*'))
            or Path(submitted['output_path']).exists()):
        _fail('original terminal failure has no unique authoritative failure or retains original media')
    try:
        _parse_stream(stream, tool_name=native_tool, expected_arguments=expected)
    except GrokCLIContractError as exc:
        category = exc.category
    else:
        _fail('original terminal failure native parser observed success')
    if file_sha256(log) != log_sha:
        _fail('original terminal failure evidence changed during verification')
    return {'session_id': request['cli_session_id'], 'session_directory': str(session),
            'native_evidence': {'path': str(log), 'sha256': log_sha},
            'outcome': 'failed', 'error_category': category,
            'failed_native_update_sha256': _digest(failures[0])}


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
        if _state(root, request)['status'] != 'uncertain' or result.data.get('original_session_recovery'):
            proof, source, original_receipt, arguments = _grok_prompt_boundary_original(root, request)
            if (not result.success or result.data.get('original_session_recovery') != proof
                    or result.data.get('source_artifact') != str(source)
                    or file_sha256(Path(request['submitted_inputs']['output_path'])) != proof['source_sha256']):
                _fail('failed reconciliation requires verified original prompt-boundary recovery')
            if any(result.data.get(key) != value for key, value in proof['media_metadata'].items()):
                _fail('original recovery metadata differs from original artifact probe')
            expected_receipt = dict(original_receipt, dispatch_status='completed', submission_evidence='verified_native_call')
            if (result.data.get('conditioning_receipt') != expected_receipt
                    or result.data.get('cli_version') != expected_receipt.get('cli_version')):
                _fail('original recovery receipt differs from retained original launch evidence')
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
        original_motion_evidence = None
        if request['scope']['provider'] == 'grok_cli' and request['media_kind'] == 'motion' and result.success:
            original_motion_evidence = _verify_original_grok_motion_success(root, directory, request, result)
        expected = {(item['path'], item['sha256']) for item in request['input_assets']}
        actual = {(item.get('path'), item.get('sha256')) for item in receipt.get('input_assets', [])}
        if expected != actual:
            _fail('reconciliation input provenance does not match')
        if not result.success:
            if result.data.get('dispatch_status') != 'failed' or receipt.get('dispatch_status') != 'failed':
                _fail('reconciliation outcome is not an authoritative terminal failure')
            record = {'status':'failed', 'result':asdict(result), 'output':None, 'preserved_output':None}
            if request['media_kind'] == 'motion':
                record['original_session_evidence'] = _verify_original_grok_motion_failure(root, directory, request, result)
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
        if original_motion_evidence is not None:
            record['original_session_evidence'] = original_motion_evidence
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
    from lib.shot_contract import selection_digest, UPSTREAM_PREDICATES, provisional_audio_review, required_review_predicates, audio_review_disabled
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
        required = required_review_predicates(root, UPSTREAM_PREDICATES)
        predicates = {item['name']:item for item in review['predicates']}
        if (len(predicates) != len(review['predicates']) or not required.issubset(predicates)
                or any(item.get('severity') == 'cosmetic' for name, item in predicates.items() if name in required)
                or any(item['status'] != 'pass' and not ((audio_provisional and name == 'speaker_source' and item['status'] == 'unknown') or (name == accepted and item['status'] == 'fail'))
                       for name, item in predicates.items() if not (name == 'speaker_source' and audio_review_disabled(root)) and (item.get('severity','critical') == 'critical' or name in required))):
            _fail('selection has missing/failed critical predicates')
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
    """Preserve a named failed review; a rejection never grants new generation."""
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
        from lib.production_provenance import historical_source_provenance
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
            historical_source_provenance(root, attempt_id, shot_id=authority['shot_id'],
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


def resume_original_attempt(project_dir, attempt_id, *, request_sha256, dry_run=False):
    """Resume one exact original; qualified collection never reserves a replacement."""
    root = Path(project_dir).resolve()
    directory = _inside(Path('production_attempts') / attempt_id, root)
    if (directory / 'request.json').exists():
        request = _read(directory / 'request.json')
        if request.get('attempt_id') != attempt_id or request.get('request_sha256') != request_sha256:
            _fail('original resume request/attempt binding differs')
        provider = request['scope']['provider']
        if provider == 'grok_cli' and request.get('media_kind') == 'motion':
            if (directory / 'reconciliation.json').exists():
                with _lock(root):
                    return _retained_grok_original(root, directory, request,
                        request_sha256=request_sha256, dry_run=dry_run)
            return collect_original_grok_prompt_boundary(root, attempt_id,
                request_sha256=request_sha256, dry_run=dry_run)
        if provider == 'openart_cli':
            if dry_run:
                return {'attempt_id': attempt_id, 'request_sha256': request_sha256,
                        'provider_calls': 0, 'reservations': 0, 'would_collect_original': True}
            return collect_openart_attempt(root, attempt_id, request_sha256=request_sha256)
        return {'status': 'blocked', 'kind': 'unsupported_original_collection', 'attempt_id': attempt_id,
            'original_id': request.get('cli_session_id', attempt_id), 'request_sha256': request_sha256,
            'provider': provider, 'counted_original': True, 'provider_calls': 0, 'reservations': 0,
            'reason': 'Strict original collection is not qualified for this provider; retain original and do not resubmit.'}
    from lib import openart_mcp_jobs as jobs
    return jobs.resume_original(root, attempt_id, request_sha256=request_sha256)


ACTION_KINDS = frozenset({'restore_derived_views', 'prepare_board_image', 'begin_native_image',
    'promote_imported_board', 'receive_native_image', 'prepare_motion',
    'collect_grok_original', 'collect_openart_original', 'begin_openart_original',
    'receive_openart_original', 'poll_openart_original', 'download_openart_original',
    'compose_cut', 'complete', 'blocked'})


def _reply_unavailable_action(root, aid, binding, operation):
    return _production_action(root, 'receive_openart_original', subject=aid, binding=binding,
                              inputs=operation, host=True, reason=operation['reason'])


def _recoverable_original_action(root, aid):
    """Exact receive replay for one begun MCP original whose reply is not retained."""
    from lib import openart_mcp_jobs as jobs
    state = jobs.resume_original(root, aid)
    operation = state['next_action']
    if operation['action'] != 'blocked_original_host_reply_unavailable':
        return None
    return _reply_unavailable_action(root, aid, {'attempt_id': aid, 'request_sha256': state['request_sha256'],
                                                 'state_sha256': _digest(state)}, operation)


def _native_original_action(root, call):
    unavailable = bool(call.get('host_exception') and not call.get('host_receipt'))
    inputs = {'call_id': call['call_id'], 'original_request_sha256': call['request_sha256'],
              'supplemental_to': (call.get('host_receipt') or {}).get('sha256')}
    reason = 'Retrieve and retain the exact original host return; the begun call is never emitted again.'
    if call.get('route') == 'openart_mcp' and call['state'] == 'returned' and call.get('host_receipt'):
        from lib import production_images as images
        pending = _read(images._bound_file(root, call['host_receipt'], 'retained generate_image return')).get('result') or {}
        if pending.get('status') in {'PENDING', 'RUNNING'} and isinstance(pending.get('historyId'), str):
            reason = ('OpenArt policy-fallback image is ' + pending['status'] + '; poll creation_get/creation_wait on this '
                      'same historyId, download its one image and call record_openart_image_collection. Never generate again.')
            inputs.update(action='collect_openart_image', history_id=pending['historyId'],
                          collection_helper='production_images.record_openart_image_collection', reason=reason)
    if unavailable:
        reason = 'Consumed native image raised a host exception without a retained reply; retrieve its exact original return and replay receive. Never begin again.'
        inputs.update(action='blocked_original_host_reply_unavailable', slot_id=call['slot_id'],
                      counted_original=True, reason=reason)
    return _production_action(root, 'receive_native_image', subject=call['call_id'],
        binding={'call_id': call['call_id'], 'request_sha256': call['request_sha256'],
                 'state': call['state'], 'scope_sha256': call['scope_sha256'],
                 'host_receipt': call.get('host_receipt'), 'host_exception': call.get('host_exception')},
        inputs=inputs, host=True, reason=reason)


def _recoverable_native_original_action(root, call_id):
    from lib import production_images as images
    call = images.image_call_status(root, call_id)
    if (call['state'] not in images.PENDING or not call.get('host_exception')
            or call.get('host_receipt')):
        return None
    return _native_original_action(root, call)


def _production_action(root, kind, *, subject, binding, inputs=None, reason=None, host=False):
    if kind not in ACTION_KINDS:
        _fail('unsupported canonical production operation')
    marker = _read(root / 'project.json')
    value = {'version': '1.0', 'kind': kind, 'subject': subject,
        'binding': {'project_id': marker['project_id'], 'story_revision': marker.get('story_revision'),
                    **copy.deepcopy(binding)}, 'inputs': copy.deepcopy(inputs or {})}
    if reason is not None:
        value['reason'] = reason
    value['host_required'] = host
    value['action_id'] = _digest(value)
    return value


def _validated_upstream_binding(root, contract, shot_id, upstream_id, selection):
    """Current factual selection plus the existing semantic upstream boundary."""
    from lib.production_provenance import validate_attempt_provenance
    from lib.shot_contract import review_digest
    if not isinstance(selection, dict):
        _fail('upstream binding requires a current accepted selection')
    marker = _read(root / 'project.json')
    if (contract.get('project_id'), contract.get('story_revision')) != (marker['project_id'], marker['story_revision']):
        _fail('upstream contract differs from current project/story')
    validate_attempt_provenance(root, selection['attempt_id'], shot_id=upstream_id,
        story_revision=marker['story_revision'], expected_output=selection['output'])
    for role in ('output', 'outgoing_frame'):
        record = selection[role]
        if file_sha256(_inside(record['path'], root)) != record['sha256']:
            _fail('upstream selection bytes missing or changed: ' + role)
    binding = {'shot_id': upstream_id, 'attempt_id': selection['attempt_id'],
        'output_sha256': selection['output']['sha256'],
        'outgoing_frame_sha256': selection['outgoing_frame']['sha256'],
        'review_sha256': review_digest(selection['review'])}
    candidate = copy.deepcopy(contract)
    shot = next(s for s in candidate['shots'] if s['id'] == shot_id)
    matches = [i for i, item in enumerate(shot['upstream']) if item['shot_id'] == upstream_id]
    if len(matches) != 1:
        _fail('upstream must be one originally declared dependency')
    shot['upstream'][matches[0]] = binding
    from jsonschema import Draft202012Validator
    from schemas.artifacts import load_schema
    if not Draft202012Validator(load_schema('shot_contract')).is_valid(candidate):
        _fail('upstream contract structure is incomplete')
    checked = validate_shot_contract(candidate, project_dir=root, shot_id=shot_id,
        story_revision=marker['story_revision'], selected_upstream={upstream_id: selection})
    label = f'shots.{shot_id}.upstream.{upstream_id}'
    failures = [error for error in checked['errors'] if error.startswith(label + '.') or error.startswith(label + ':')]
    if failures:
        _fail('upstream selection is not eligible: ' + '; '.join(failures))
    return binding


def next_production_action(project_dir):
    """One typed projection of existing canonical state; zero writes or host calls."""
    from lib import production_images as images, production_boards as boards
    from lib.shot_contract import project_review_subject, shot_review_subject, required_asset_ids
    root = Path(project_dir).resolve()
    def action(kind, subject, binding, **kwargs):
        return _production_action(root, kind, subject=subject, binding=binding, **kwargs)
    pack = boards.load_board_pack(root) if (root / boards.ARTIFACT).exists() else None
    board_action = None
    board_actions = {}
    motion_action = None
    ready_original_action = None
    uncertain_cli_actions = []
    recoverable_originals = []
    image_calls = []
    ready_images = []
    def remember_motion(candidate):
        nonlocal motion_action
        # A known pending poll never hides another original's receive/download
        # or uncertain collection work, regardless of attempt iteration order.
        if (motion_action is None or motion_action['kind'] == 'poll_openart_original'
                and candidate['kind'] != 'poll_openart_original'):
            motion_action = candidate
    derived_names = ['script', 'scene_plan', 'asset_manifest']
    if pack and (pack.get('motion_projection', {}).get('contract_digest')
            or pack['specification'].get('motion_binding', {}).get('contract_template', {}).get('planning_mode') == 'shot_local_v1'):
        derived_names.append('shot_contract')
    if pack and pack.get('package_binding') and any(not _artifact_path(root, name + '.json').exists()
            for name in derived_names):
        return action('restore_derived_views', 'package', {'package': pack['package_binding'],
            'specification_sha256': pack['specification_sha256']})
    contract_path = _artifact_path(root, 'shot_contract.json')
    contract = load_shot_contract(root) if contract_path.exists() else None
    motion_contract = pack['specification'].get('motion_binding', {}).get('contract_template') if pack else None
    if pack and contract is not None:
        from jsonschema import Draft202012Validator
        from schemas.artifacts import load_schema
        # Draft/legacy planning must not preempt collection of an exact original.
        # Project optional board floors only from a complete contract or the
        # validated authored template; motion still validates the actual contract.
        if Draft202012Validator(load_schema('shot_contract')).is_valid(contract):
            motion_contract = contract
    motion_roles = {}
    if motion_contract:
        required_assets = set().union(*(required_asset_ids(motion_contract, shot) for shot in motion_contract['shots']))
        motion_roles = {asset['id']: [asset['role']] for asset in motion_contract['assets']
                        if asset['id'] in required_assets}
    if (root / images.ARTIFACT).exists():
        image_calls = images._load(root)['calls']
        for call in image_calls:
            if (pack and call['state'] == 'imported' and call['slot_id'] in pack['prompt_pack']
                    and pack['board_bindings'].get(call['slot_id'], {}).get('call_id') != call['call_id']
                    # Superseded originals and late originals whose successor owns
                    # the lane stay retained/counted; only bindable imports promote.
                    and boards.imported_board_eligibility(root, pack, call['slot_id'], call['call_id'],
                                                          images=images) in {'current', 'restore'}):
                original_action = action('promote_imported_board', call['call_id'],
                    {'call_id': call['call_id'], 'slot_id': call['slot_id'], 'request_sha256': call['request_sha256'],
                     'output': call['output'], 'host_receipt': call.get('host_receipt')})
                board_actions.setdefault(call['slot_id'], original_action)
                board_action = board_action or original_action
                continue
            if call['state'] not in images.PENDING:
                continue
            binding = {'call_id': call['call_id'], 'request_sha256': call['request_sha256'],
                'state': call['state'], 'scope_sha256': call['scope_sha256'],
                'host_receipt': call.get('host_receipt'), 'host_exception': call.get('host_exception')}
            if call['state'] == 'reserved':
                # A retained historical recovery successor is never dispatched.
                if call.get('replaces_host_exception'):
                    continue
                original_action = action('begin_native_image', call['call_id'], binding)
                if pack:
                    board_actions.setdefault(call['slot_id'], original_action)
                board_action = board_action or original_action
                continue
            original_action = _native_original_action(root, call)
            if original_action['inputs'].get('action') == 'blocked_original_host_reply_unavailable':
                recoverable_originals.append(original_action)
                continue
            if pack:
                board_actions.setdefault(call['slot_id'], original_action)
            board_action = board_action or original_action
    if pack:
        # Current bound boards are ready mechanically; semantic observations
        # are disclosed later and never stop or correct the pass.
        counted_slots = {row['slot_id'] for row in images.image_usage(root)['occurrences'] if row['counted']}
        ready_images = [slot for slot in boards.board_schedule(root, images=images)['ready_images']
                        if slot not in counted_slots]
        for slot in ready_images:
            board_actions.setdefault(slot, action('prepare_board_image', slot,
                {'specification_sha256': pack['specification_sha256'],
                 'source_bindings': copy.deepcopy(pack['board_bindings'])}, inputs={'slot_id': slot}))
        if ready_images and board_action is None:
            board_action = board_actions[ready_images[0]]
    selected = load_selected_attempts(root)
    attempted_shots = set()
    for request in _attempts(root):
        if request.get('media_kind') != 'motion':
            continue
        aid = request['attempt_id']; sid = request['shot_id']
        attempted_shots.add(sid)
        if request.get('provider') == 'openart_mcp':
            from lib import openart_mcp_jobs as jobs
            state = jobs.resume_original(root, aid)
            operation = state['next_action']
            kind = {'begin_original': 'begin_openart_original', 'receive_original': 'receive_openart_original',
                'poll_original': 'poll_openart_original', 'download_original': 'download_openart_original'}.get(operation['action'])
            binding = {'attempt_id': aid, 'request_sha256': state['request_sha256'],
                       'state_sha256': _digest(state)}
            if kind:
                candidate = action(kind, aid, binding, inputs=operation,
                    host=kind in {'poll_openart_original'})
                if kind == 'begin_openart_original' and ready_original_action is None:
                    ready_original_action = candidate
                remember_motion(candidate)
                continue
            if operation['action'] == 'blocked_original_host_reply_unavailable':
                # No receipt and no retained reply: the original stays counted
                # and replayable, but cannot hold delivery of settled clips.
                recoverable_originals.append(_reply_unavailable_action(root, aid, binding, operation))
                continue
            observed = _state(root, request)
        else:
            observed = _state(root, request)
        binding = {'attempt_id': aid, 'request_sha256': request['request_sha256'],
            'state_sha256': _digest(observed), 'counted_original': True}
        if observed.get('status') in {'uncertain', 'submitted', 'reserved'}:
            provider = request.get('scope', {}).get('provider')
            kind = {'grok_cli': 'collect_grok_original', 'openart_cli': 'collect_openart_original'}.get(provider, 'blocked')
            candidate = action(kind, aid, binding, inputs={'attempt_id': aid,
                'request_sha256': request['request_sha256'], 'original_id': request.get('cli_session_id', aid),
                'provider': provider}, reason=('Unsupported strict original collection; preserve original ID/count and never replace.' if kind == 'blocked' else None))
            if kind == 'blocked':
                recoverable_originals.append(candidate)
            else:
                if observed.get('status') == 'uncertain':
                    uncertain_cli_actions.append(candidate)
                remember_motion(candidate)
            continue
    if not contract_path.exists():
        if motion_action is not None:
            return motion_action
        if board_action is not None:
            return board_action
        if recoverable_originals:
            # Without planning nothing can be delivered; disclose the retained
            # original's exact ID/count instead of a generic planning block.
            return recoverable_originals[0]
        return action('blocked', 'planning', {}, reason='Canonical authored planning is missing; no trusted derived source is available.')
    # Hand off an already prepared exact request before asking the producer to
    # prepare another shot. begin still revalidates current source and authority
    # and owns the once-only envelope; pending originals retain their ordering.
    if ready_original_action is not None:
        return ready_original_action
    # Ready independent shots dispatch first: a running or uncertain original
    # stays recoverable and is never resubmitted, but cannot hold other clips.
    marker = _read(root / 'project.json')
    blocked_shots = {}
    for shot in contract['shots']:
        sid = shot['id']
        if sid in attempted_shots or sid in selected:
            continue
        checked = validate_shot_contract(contract, project_dir=root, shot_id=sid,
            story_revision=marker['story_revision'], selected_upstream=selected)
        if checked['eligible']:
            # Independent ready shots dispatch while other originals render.
            return action('prepare_motion', sid, {'shot_subject': shot_review_subject(contract, sid)}, host=True,
                inputs={'shot_id': sid}, reason='Producer prepares the one exact registered-route original request for this planned clip; engine admits and dispatches it.')
        blocked_shots[sid] = checked['errors']
    from lib.production_draft import first_cut_candidates, first_cut_media_status, latest_first_cut
    # Save usable footage before waiting on another original. Its unchanged
    # current partial cut does not render again; subsequent advance still
    # collects/polls the exact pending operation, never another generation.
    if board_action is None and motion_action is not None and motion_action in uncertain_cli_actions:
        candidates = first_cut_candidates(root)
        cut = latest_first_cut(root)
        if (any(row['status'] == 'candidate' for row in candidates)
                and (cut is None or first_cut_media_status(root, cut).get('status') != 'current')):
            return action('compose_cut', 'cut', {'selections_sha256': _digest(selected),
                'contract_sha256': contract_digest(contract), 'candidates_sha256': _digest(candidates)},
                inputs={'candidates': candidates, 'blocked_shots': blocked_shots,
                        'recoverable_originals': recoverable_originals + uncertain_cli_actions},
                reason='Save the playable originals as a partial cut, retaining exact pending original collection actions.')
    # Original recovery (receipt/collection/download) precedes board work;
    # board preparation precedes a known pending poll.
    if motion_action is not None and motion_action['kind'] != 'poll_openart_original':
        return motion_action
    if board_action is not None:
        return board_action
    if motion_action is not None:
        return motion_action
    # Declared images whose one original is consumed (failed/uncertain with no
    # bytes) can leave zero submittable clips; that closes out incomplete.
    consumed_images = any(row['counted'] for row in images.image_usage(root)['occurrences'])
    if not attempted_shots and not consumed_images:
        return action('blocked', 'prerequisites', {'contract_sha256': contract_digest(contract)},
            inputs={'blocked_shots': blocked_shots},
            reason='No planned shot is mechanically ready; inspect current source/reference blockers.')
    cut = latest_first_cut(root)
    if cut and cut.get('status') in FIRST_CUT_TERMINAL:
        current = first_cut_media_status(root, cut)
        if current.get('status') == 'current':
            return action('complete', 'cut', {'first_cut': {'path': cut['path'], 'sha256': cut['sha256']}},
                          inputs=dict(current, recoverable_originals=recoverable_originals))
    candidates = first_cut_candidates(root)
    return action('compose_cut', 'cut', {'selections_sha256': _digest(selected),
        'contract_sha256': contract_digest(contract), 'candidates_sha256': _digest(candidates)},
        inputs={'candidates': candidates, 'blocked_shots': blocked_shots, 'recoverable_originals': recoverable_originals},
        reason='Assemble every playable original in planned order; findings, gaps and missing footage are disclosed.')


FIRST_CUT_TERMINAL = frozenset({'complete', 'partial', 'incomplete'})


def _action_status(action):
    return ('ready_to_compose' if action['kind'] == 'compose_cut' else 'complete' if action['kind'] == 'complete'
            else 'blocked' if action['kind'] == 'blocked' else 'host_action' if action['host_required'] else 'mechanical')


def advance(project_dir, *, max_steps=8):
    """Bounded mechanics, then one durable action for the same producer.

    A host envelope is emitted once through its existing intent-before-call
    owner. On interruption its canonical state yields original receipt recovery.
    No timer, second action ledger or creative-review impersonation exists here.
    """
    from lib import production_images as images, production_boards as boards, production_request as preparation
    from lib import openart_mcp_jobs as jobs
    if type(max_steps) is not int or not 1 <= max_steps <= 32:
        _fail('max_steps must be between 1 and 32')
    root = Path(project_dir).resolve()
    # Each mechanical step recomputes the action exactly once after its
    # mutation; that fresh action is carried into the next step.
    action = next_production_action(root)
    for step in range(max_steps):
        kind = action['kind']
        status = _action_status(action)
        if status != 'mechanical':
            return {'status': status, 'action': action, 'steps': step, 'provider_calls': 0}
        try:
            if kind == 'restore_derived_views':
                preparation.prepare_package(root, action['binding']['package'])
            elif kind == 'promote_imported_board':
                boards.bind_imported_board(root, action['binding']['slot_id'], action['subject'], images=images)
            elif kind == 'prepare_board_image':
                if not (root / 'production_scopes.json').exists() or not _read(root / 'production_scopes.json').get('image_envelope'):
                    images.activate_standing_image_envelope(root)
                slot = action['inputs']['slot_id']
                request = boards.prepare_image_request(root, slot, images=images)
                scope = images.derive_native_image_scope(root, request['packet'], slot, request['arguments'], output_path=request['output_path'])
                aid = 'board-' + request['request_sha256'][:24]
                boards.reserve_board_image(root, slot, scope_id=scope, call_id=aid, images=images)
            elif kind == 'begin_native_image':
                envelope = images.begin_native_image(root, action['subject'])
                return {'status': 'host_action', 'action': next_production_action(root),
                        'host_envelope': envelope, 'steps': step + 1, 'provider_calls': 0}
            elif kind in {'collect_grok_original', 'collect_openart_original'}:
                resume_original_attempt(root, action['subject'], request_sha256=action['binding']['request_sha256'])
            elif kind == 'begin_openart_original':
                envelope = jobs.begin(root, action['subject'], authority_fn=prepare_openart_mcp_handoff)
                # The producer returns this begun original's reply through its
                # exact receive action; independent work stays available.
                return {'status': 'host_action', 'action': _recoverable_original_action(root, action['subject'])
                        or next_production_action(root),
                        'host_envelope': envelope, 'steps': step + 1, 'provider_calls': 0}
            elif kind == 'receive_openart_original':
                jobs.resume_original(root, action['subject'], outcome=action['inputs']['outcome'],
                    request_sha256=action['binding']['request_sha256'])
            elif kind == 'download_openart_original':
                receipt = jobs.download_original(root, action['subject'])
                jobs.collect(root, action['subject'], downloaded_path=receipt['downloaded_path'])
            else:
                _fail('unsupported mechanical operation')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return {'status': 'blocked', 'action': action, 'steps': step, 'provider_calls': 0, 'reason': str(exc)}
        updated = next_production_action(root)
        if updated['action_id'] == action['action_id']:
            return {'status': 'blocked', 'action': updated, 'steps': step + 1, 'provider_calls': 0,
                    'reason': 'Original operation is still pending; no replacement or duplicate collection was issued.'}
        action = updated
    return {'status': _action_status(action), 'action': action, 'steps': max_steps, 'provider_calls': 0}


def draft_motion_preparation(project_dir, action, *, output_path=None, compiled_request_id=None,
                             preparation_review_id=None, provider=None):
    """Pure draft of the CURRENT ``prepare_motion`` request from retained inputs.

    Projects the active policy's retained ``planned_request_template`` inputs,
    resolves declared ``$board``/``$upstream`` frame occurrences to their actual
    current bytes, and attaches the existing ``compile_provider_prompt`` preview.
    Grok and OpenArt drafts receive stable administrative IDs. Exact connector
    model/mode inputs infer their approved OpenArt route and account-project and
    upload-authority args.
    It writes nothing, reserves/uploads nothing and makes no review, timing or
    provider judgment. The returned ``inputs``/``provider`` are an ordinary
    draft for ``prep_builder``/``prepare_compiled_request``/``complete_action``;
    admission still validates every field against the retained authority.
    """
    from lib import production_autonomy as autonomy
    from lib.production_request import compile_provider_prompt, _id
    root = Path(project_dir).resolve()
    current = next_production_action(root)
    if not isinstance(action, dict) or action != current or current['kind'] != 'prepare_motion':
        _fail('motion draft requires the exact current prepare_motion action')
    sid = current['inputs'].get('shot_id')
    if sid != current['subject']:
        _fail('motion draft action subject differs from its shot')
    policy, _, _ = autonomy.require_active_policy(root, shot_id=sid)
    retained = autonomy.retained_baselines(root, policy).get(sid)
    if not isinstance(retained, dict):
        _fail('motion draft lacks retained request template for current shot')
    template = retained['planned_request_template']['inputs']
    routes = template.get('allowed_providers') or ([template['preferred_provider']] if template.get('preferred_provider') else [])
    named = template.get('preferred_provider', routes[0] if len(routes) == 1 else None)
    # Connector-native compact inputs name their exact model/mode rather than
    # carrying selector-only provider fields. Infer only that retained route;
    # ambiguous/generic inputs still require the producer's explicit provider.
    if not routes and template.get('model') and template.get('mode'):
        spec = autonomy._provider_spec(policy, 'openart_mcp')
        route = {'model': template['model'], 'mode': template['mode']}
        if (spec is not None and route in spec.get('routes', [])
                and autonomy._intent_allows(policy, sid, 'openart_mcp', template['model'])):
            named, routes = 'openart_mcp', ['openart_mcp']
    if routes and (provider is not None and provider != named):
        _fail('motion draft provider differs from retained request template route')
    provider = named if routes else provider
    if (provider not in autonomy.ROUTE_TOOLS or (routes and routes != [provider])
            or template.get('preferred_tool', autonomy.ROUTE_TOOLS[provider]) != autonomy.ROUTE_TOOLS[provider]
            or autonomy._provider_spec(policy, provider) is None
            or not autonomy._intent_allows(policy, sid, provider, template.get('model'))):
        _fail('retained request template lacks one supported singleton route')
    selected = load_selected_attempts(root)
    frames = []
    def resolve(value, key=None, pointer=''):
        if isinstance(value, dict) and '$board' in value:
            record = resolve_board_input(root, _board_binding(value))
            frames.append({'pointer': pointer, 'binding': copy.deepcopy(value), **record})
            return record['path']
        if isinstance(value, dict) and '$upstream' in value:
            binding = _upstream_binding(value)
            record = selected.get(binding['shot_id'], {}).get(binding['role'], {})
            if not record.get('path') or file_sha256(_inside(record['path'], root)) != record.get('sha256'):
                _fail('dynamic upstream request lacks matching selected bytes')
            frames.append({'pointer': pointer, 'binding': copy.deepcopy(value),
                           'path': str(_inside(record['path'], root)), 'sha256': record['sha256']})
            return str(_inside(record['path'], root))
        if isinstance(value, dict):
            return {k: resolve(v, k, pointer + '/' + k) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, key, pointer + '/' + str(i)) for i, v in enumerate(value)]
        return copy.deepcopy(value)
    inputs = resolve(template)
    authored = compile_provider_prompt(root, sid, provider=provider,
        **({'model': inputs.get('model'), 'input_assets': inputs.get('input_assets'),
            'openart_project_id': inputs.get('openart_project_id',
                autonomy._provider_spec(policy, provider).get('project_id'))} if provider == 'openart_mcp' else {}))
    inputs.update(project_dir=str(root), governance={'shot_id': sid}, prompt=authored['prompt'])
    if provider in ('grok_cli', 'openart_mcp'):
        # Administrative names are deterministic for this exact action. Native
        # upload receipts and preparation judgment remain their existing owners.
        record_id = 'motion-' + current['action_id'][:24]
        inputs.setdefault('output_path', str(_inside('assets/video/' + record_id + '.mp4', root)))
        inputs.setdefault('compiled_request_id', record_id)
        inputs.setdefault('preparation_review_id', record_id)
    if provider == 'openart_mcp':
        spec = autonomy._provider_spec(policy, provider)
        if spec.get('project_id') is not None:
            inputs.setdefault('openart_project_id', spec['project_id'])
    if output_path is not None:
        inputs['output_path'] = str(_inside(output_path, root))
    for name, value in (('compiled_request_id', compiled_request_id), ('preparation_review_id', preparation_review_id)):
        if value is not None:
            inputs[name] = _id(value)
    missing = [k for k in ('output_path', 'compiled_request_id', 'preparation_review_id') if k not in inputs]
    result = {'action_id': current['action_id'], 'shot_id': sid, 'provider': provider, 'inputs': inputs,
            'frames': frames, 'coverage': authored['coverage'], 'source_binding': authored['source_binding'],
            'missing': missing, 'provider_calls': 0}
    if provider == 'openart_mcp' and (frames or inputs.get('input_assets')
            or any(key in inputs for key in ('image_path', 'first_frame', 'reference_image_path', 'last_image_path', 'last_frame'))):
        from lib import openart_mcp_jobs as jobs
        # Exact retained uploads are source evidence, not new transfers. Preserve
        # role/order/template bytes and bind only the ordinary receipt selector.
        # Existing root aliases stay in governed inputs and use their established
        # upload selector; the native projection retains ordinary role ordering.
        files = list(dict.fromkeys(str(_inside(frame['path'], root)) for frame in frames))
        reused = []
        normalized = _openart_controls(inputs)
        for field, selector in (('image_path', 'image_upload_id'), ('last_image_path', 'end_image_upload_id')):
            if field not in normalized:
                continue
            asset = {'source_path': normalized[field]}
            if selector in inputs:
                asset['upload_id'] = inputs[selector]
            bound = jobs.reuse_uploaded_input_asset(root, asset,
                account_binding={'uid_sha256': spec['uid_sha256']},
                openart_project_id=inputs.get('openart_project_id'))
            path = str(_inside(normalized[field], root))
            if bound is None:
                if path not in files:
                    files.append(path)
            else:
                inputs.setdefault(selector, bound['receipt_sha256'])
                if path in files:
                    files.remove(path)
                reused.append({key: bound[key] for key in ('source_path', 'source_sha256', 'receipt_sha256')})
        for asset in inputs.get('input_assets', []):
            path = str(_inside(asset['source_path'], root))
            bound = jobs.reuse_uploaded_input_asset(root, asset,
                account_binding={'uid_sha256': spec['uid_sha256']},
                openart_project_id=inputs.get('openart_project_id'))
            if bound is None:
                if path not in files:
                    files.append(path)
            else:
                if not {'upload_id', 'reference_id'}.intersection(asset):
                    asset['upload_id'] = bound['receipt_sha256']
                if path in files:
                    files.remove(path)
                reused.append({key: bound[key] for key in ('source_path', 'source_sha256', 'receipt_sha256')})
        result['upload_authority_kwargs'] = {'shot_id': sid, 'provider': provider,
            'files': files,
            'account': {'uid_sha256': spec['uid_sha256']},
            'openart_project_id': inputs.get('openart_project_id')}
        result['reused_uploads'] = reused
    return result


def complete_action(project_dir, action, *, outcome):
    """Consume actual producer evidence for one fixed current canonical action."""
    from lib import production_boards as boards, production_images as images, openart_mcp_jobs as jobs
    root = Path(project_dir).resolve()
    valid = (isinstance(action, dict) and action.get('kind') in ACTION_KINDS
        and action.get('action_id') == _digest({k: v for k, v in action.items() if k != 'action_id'}))
    if valid and action.get('kind') == 'receive_native_image' and isinstance(outcome, dict) and 'host_receipt' in outcome:
        call = images.image_call_status(root, action['subject'])
        if call['state'] == 'imported':
            if (action['binding'].get('request_sha256') != call['request_sha256']
                    or action['binding'].get('scope_sha256') != call['scope_sha256']
                    or action['binding'].get('project_id') != _read(root / 'project.json')['project_id']
                    or action['binding'].get('story_revision') != call['story_revision']
                    or set(outcome) - {'host_receipt', 'supplemental_to'}):
                _fail('original image replay binding differs')
            images.import_native_image(root, action['subject'], host_receipt=outcome['host_receipt'], supplemental_to=outcome.get('supplemental_to'))
            if (root / boards.ARTIFACT).exists():
                boards.bind_imported_board(root, call['slot_id'], action['subject'], images=images)
            current = next_production_action(root)
            return {'status': _action_status(current), 'action': current, 'provider_calls': 0, 'replayed': True}
    # A begun original without a retained reply never preempts independent
    # clips, but its exact receive replay stays accepted at any later point.
    if not valid or (action != next_production_action(root) and not (
            action['kind'] == 'receive_openart_original'
            and action == _recoverable_original_action(root, action['subject']) or
            action['kind'] == 'receive_native_image'
            and action == _recoverable_native_original_action(root, action['subject']))):
        _fail('production action is stale, foreign or modified')
    if not isinstance(outcome, dict):
        _fail('actual structured producer outcome required')
    kind = action['kind']; aid = action['subject']
    if kind == 'receive_native_image':
        if set(outcome) - {'host_receipt', 'supplemental_to', 'host_exception'}:
            _fail('unsupported original image outcome')
        if 'host_exception' in outcome:
            images.record_native_image_host_exception(root, aid, host_exception=outcome['host_exception'])
        else:
            imported = images.import_native_image(root, aid, host_receipt=outcome['host_receipt'],
                supplemental_to=outcome.get('supplemental_to'))
            if imported['state'] == 'imported':
                if (root / boards.ARTIFACT).exists():
                    boards.bind_imported_board(root, imported['slot_id'], aid, images=images)
    elif kind == 'poll_openart_original':
        if set(outcome) != {'result'}:
            _fail('original poll requires exact host result')
        jobs.record_status(root, aid, result=outcome['result'])
    elif kind == 'receive_openart_original':
        if set(outcome) != {'result'}:
            _fail('original receive requires exact host result')
        jobs.resume_original(root, aid, outcome=outcome['result'], request_sha256=action['binding']['request_sha256'])
    elif kind == 'prepare_motion':
        if set(outcome) == {'preparation'}:
            # Authored timing and actual image judgment stay with the producer;
            # mechanical binding and the native CLI observation belong here.
            preparation = outcome['preparation']
            if not isinstance(preparation, dict) or set(preparation) != {'timing', 'review'}:
                _fail('motion preparation requires exact authored timing and review')
            draft = draft_motion_preparation(root, action)
            if draft['provider'] != 'grok_cli':
                _fail('authored preparation convenience requires the retained singleton Grok route')
            from tools.tool_registry import registry
            from tools._grok_cli_media import observe_grok_cli_compatibility, DEFAULT_GROK_PATH
            from lib.production_request import prep_builder, prepare_compiled_request, validate_preparation_evidence, source_packet
            import os
            registry.ensure_discovered('tools.video')
            tool = registry.get('grok_cli_video')
            if tool is None:
                _fail('exact registered production tool unavailable')
            inputs = draft['inputs']
            configured = getattr(tool, '_grok_path', None) or os.environ.get('GROK_CLI_PATH', DEFAULT_GROK_PATH)
            observation = observe_grok_cli_compatibility(configured, cwd=inputs.get('cwd') or root)
            native = prep_builder('grok_cli')(inputs, observation)
            profile = {'source': 'real'}
            compiled, reviewed = prepare_compiled_request(inputs, native, profile, coverage=draft['coverage'],
                timing=preparation['timing'], review=preparation['review'])
            packet = source_packet(root, aid, provider='grok_cli', native=native)
            validate_preparation_evidence(compiled, reviewed, inputs, native, profile, packet)
            prepare_compiled_request(inputs, native, profile, coverage=draft['coverage'],
                timing=preparation['timing'], review=preparation['review'], persist=True)
            outcome = {'inputs': inputs, 'provider': 'grok_cli', 'observation': observation}
        if set(outcome) - {'inputs', 'provider', 'observation'} or not {'inputs', 'provider'} <= set(outcome):
            _fail('motion preparation requires exact inputs and approved provider')
        from lib import production_autonomy as autonomy
        provider = outcome['provider']; inputs = copy.deepcopy(outcome['inputs'])
        if provider not in autonomy.ROUTE_TOOLS or inputs.get('governance', {}).get('shot_id') != aid:
            _fail('prepared motion lies outside exact current action')
        scope = autonomy.derive_scope(root, inputs, provider=provider, observation=outcome.get('observation'),
            reuse_undispatched=True)
        inputs['governance'].update(shot_id=aid, scope_id=scope['id'])
        from tools.tool_registry import registry
        registry.ensure_discovered('tools.video')
        tool = registry.get(autonomy.ROUTE_TOOLS[provider])
        if tool is None:
            _fail('exact registered production tool unavailable')
        dry = governed_dry_run(tool, inputs)
        result = tool.execute(inputs)
        return {'status': 'dispatched', 'result': asdict(result), 'dry_run': dry,
                'action': next_production_action(root)}
    else:
        _fail('action does not accept a producer outcome')
    return advance(root)


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
