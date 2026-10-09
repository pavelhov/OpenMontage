"""Pure OpenArt preparation. Agents judge semantics; this module binds evidence.

No CLI, network, ffprobe, transport lock or credit reservation is performed here.
Synthetic fixture reviews cannot qualify a live provider. Compilation is literal;
coverage checks do not claim to understand paraphrases or visual suitability.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from schemas.artifacts import load_schema
from lib.shot_contract import contract_digest, file_sha256, review_digest, validate_shot_contract

METADATA = frozenset({'compiled_request_id', 'preparation_review_id', 'credit_authorization_id', 'credit_quote_id', 'credit_qualification_sha256'})
PREDICATES = frozenset({'coverage', 'reference_suitability', 'native_compatibility', 'continuity', 'feasibility'})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def controls(inputs):
    from lib.production_execution import _openart_controls
    return _openart_controls(inputs)


def _read(root, name):
    from lib.production_execution import _artifact_path
    return json.loads(_artifact_path(root, name).read_text())


def _id(value):
    if not isinstance(value, str) or not value or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        raise ValueError('preparation artifact requires an opaque safe ID')
    return value


def _leaf(value, pointer):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _leaf(child, pointer + '/' + str(key).replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _leaf(child, pointer + '/' + str(index))
    else:
        yield {'source_pointer': pointer, 'value': value, 'value_sha256': digest(value)}



def _check_required_native_controls(scene_plan, manifest, shot_id, *, native=None, project_dir=None):
    """Bind required native ending frames to exact approved canonical handoffs."""
    card = scene_plan.get('metadata', {}).get('visual_development', {}).get('shot_cards', {}).get(shot_id, {})
    for key in ('pinned_initial_frame', 'pinned_start_frame', 'pinned_first_frame'):
        pin = card.get(key)
        if pin is not None:
            if not isinstance(pin, dict) or type(pin.get('required')) is not bool:
                raise ValueError('malformed declared initial frame-pin requirement')
            start_assets = (native or {}).get('input_assets')
            if start_assets is None:
                start_assets = [{'role': 'first_frame'}] if (native or {}).get('image_upload') else []
            if pin['required'] and len([a for a in start_assets if a.get('role') == 'first_frame']) != 1:
                raise ValueError('OpenArt lacks required native initial-frame pin')
    requirement = card.get('pinned_final_frame')
    if requirement is not None and (not isinstance(requirement, dict) or type(requirement.get('required')) is not bool):
        raise ValueError('malformed declared pinned_final_frame requirement')
    assets = {a['id']: a for a in (manifest or {}).get('assets', [])}
    metadata = (manifest or {}).get('metadata', {})
    handoffs = [(key, handoff) for key, handoff in metadata.get('motion_handoffs', {}).items()
                if handoff.get('scene_id', assets.get(key, {}).get('scene_id')) == shot_id
                and handoff.get('pinned_final_frame') is not None]
    refs = [ref for ref in metadata.get('reference_assets', {}).values()
            if ref.get('scene_id') == shot_id and ref.get('temporal_use') == 'last_frame' and ref.get('requirement_id')]
    declared = bool(requirement and requirement['required']) or bool(handoffs) or bool(refs)
    if not declared:
        return
    submitted = (native or {}).get('input_assets', [])
    endings = [a for a in submitted if a.get('role') == 'last_frame']
    starts = [a for a in submitted if a.get('role') == 'first_frame']
    if len(endings) != 1 or len(starts) != 1:
        raise ValueError('OpenArt lacks required native ending-frame pin')
    if project_dir is None or manifest is None or len(handoffs) != 1:
        raise ValueError('ending-frame needs one exact approved starting/ending handoff')
    from lib.pinned_final_frame import pinned_final_frame_params
    start_id, handoff = handoffs[0]
    try:
        bound = pinned_final_frame_params(scene_plan, manifest, scene_id=shot_id,
            keyframe_asset_id=start_id, provider=native.get('provider', 'openart_cli'), model=native.get('model'), project_dir=project_dir)
    except ValueError as exc:
        raise ValueError('ending-frame canonical binding invalid: ' + str(exc)) from None
    if (file_sha256(bound['reference_image_path']) != starts[0].get('source_sha256')
            or file_sha256(bound['last_image_path']) != endings[0].get('source_sha256')):
        raise ValueError('ending-frame native start/target source bytes differ from canonical handoff')
    end_id = handoff['pinned_final_frame']['asset_id']
    for ref in refs:
        if (ref.get('requirement_id') != bound['endpoint_requirement_id']
                or ref.get('asset_id') != end_id or ref.get('approved') is False):
            raise ValueError('ending-frame reference requirement or target differs from approved handoff')



def _manifest_requirement_binding(manifest, shot_id):
    """Bind relevant handoffs/references; publishing an output is not a plan change."""
    if manifest is None:
        return {'assets': [], 'motion_handoffs': {}, 'reference_assets': {}}
    assets = {a['id']: a for a in manifest['assets']}
    metadata = manifest.get('metadata', {})
    handoffs = {key: value for key, value in metadata.get('motion_handoffs', {}).items()
                if value.get('scene_id', assets.get(key, {}).get('scene_id')) == shot_id}
    references = {key: value for key, value in metadata.get('reference_assets', {}).items()
                  if value.get('scene_id', assets.get(key, {}).get('scene_id')) == shot_id}
    ids = set(handoffs) | set(references)
    ids.update(value.get('pinned_final_frame', {}).get('asset_id') for value in handoffs.values())
    return {'assets': [value for key, value in assets.items() if key in ids],
            'motion_handoffs': handoffs, 'reference_assets': references}


def source_packet(project_dir, shot_id, *, provider="openart_cli", native=None, check_native_controls=True):
    """Resolve authoritative closed contract, explicit scene→script mapping and bytes."""
    return _source_packet(project_dir, shot_id, provider=provider, native=native,
        check_native_controls=check_native_controls, historical=False)


def _historical_source_packet(project_dir, shot_id, *, provider="openart_cli", native=None, check_native_controls=True):
    """Reconstruct current original source facts for retained authority replay.

    Callers must independently bind this projection to their trusted retained
    contract/preparation snapshots. No current listening or dispatch authority
    follows from this packet; prospective readers always use ``source_packet``.
    """
    return _source_packet(project_dir, shot_id, provider=provider, native=native,
        check_native_controls=check_native_controls, historical=True)


def _creator_repair_source_packet(project_dir, shot_id, *, native, contract, selected,
                                 script, scene_plan):
    """Source-only projection; provenance must bind these original snapshots.
    Prospective preparation/dispatch never reads caller-supplied source facts.
    """
    return _source_packet(project_dir, shot_id, provider='openart_mcp', native=native,
        check_native_controls=True, historical=True, original_sources=(contract, selected),
        original_artifacts=(script, scene_plan))


def _source_packet(project_dir, shot_id, *, provider, native, check_native_controls, historical,
                   original_sources=None, original_artifacts=None):
    from lib.production_execution import approval_plan_digest, load_selected_attempts
    root = Path(project_dir).resolve()
    contract = _read(root, 'shot_contract.json')
    marker = json.loads((root / 'project.json').read_text())
    if marker.get('governance', {}).get('mode') != 'strict' or marker.get('governance', {}).get('version') != '1.0':
        raise ValueError('strict enrollment required for OpenArt preparation/upload approval')
    selected = load_selected_attempts(root)
    if original_sources is not None:
        contract, selected = original_sources
    if historical:
        from lib.shot_contract import _validate_original_shot_contract
        validate_sources = _validate_original_shot_contract
    else:
        validate_sources = validate_shot_contract
    checked = validate_sources(contract, project_dir=root, shot_id=shot_id,
                               story_revision=marker['story_revision'], selected_upstream=selected)
    if not checked['eligible']:
        raise ValueError('; '.join(checked['errors']))
    if contract['project_id'] != marker['project_id']:
        raise ValueError('contract project mismatch')
    index, shot = next((i, s) for i, s in enumerate(contract['shots']) if s['id'] == shot_id)
    reference_guided = (shot.get('reference_mode', contract.get('reference_mode')) == 'reference_guided')
    if reference_guided and provider != 'openart_mcp':
        raise ValueError('reference-guided contract requires the explicit OpenArt MCP reference route')
    reference_free = (contract.get('reference_mode') == 'reference_free'
                      or shot.get('reference_mode') == 'reference_free')
    if reference_free and provider not in {'openart_cli', 'openart_mcp'}:
        raise ValueError('reference-free contract requires OpenArt text2video')
    rows = []
    if reference_free:
        rows.extend(_leaf(contract['story'], '/shot_contract/story'))
    for key in ('initial_state', 'dominant_action', 'completed_end_state', 'endpoint_completion',
                'cast_ids', 'required_visible_speakers', 'dialogue', 'prop_body_invariants',
                'allowed_transformations', 'transition'):
        rows.extend(_leaf(shot[key], f'/shot_contract/shots/{index}/{key}'))
    refs = []
    payoff_id = contract.get('payoff_asset_id')
    needed_assets = set(shot['asset_ids']) | ({payoff_id} if not reference_free else set())
    needed_cast = (set() if reference_free else
                   set(shot['cast_ids']) | set(contract['late_cast_ids']) | set(contract['payoff_speaker_ids']))
    needed_assets.update(a['id'] for a in contract['assets'] if a['role'] == 'identity_reference' and needed_cast.intersection(a['cast_ids']))
    # All contract review bindings are retained: review prose is outside contract_digest.
    for ai, asset in enumerate(contract['assets']):
        if asset['id'] not in needed_assets:
            continue
        path = (root / asset['path']).resolve()
        if file_sha256(path) != asset['sha256']:
            raise ValueError('reference bytes changed')
        refs.append({'id': asset['id'], 'role': asset['role'], 'cast_ids': asset['cast_ids'],
                     'sha256': asset['sha256'], 'review_sha256': review_digest(asset['review'])})
        if asset['id'] in shot['asset_ids'] or asset['id'] == payoff_id or asset['role'] == 'identity_reference':
            for key in ('id', 'role', 'cast_ids'):
                rows.extend(_leaf(asset[key], f'/shot_contract/assets/{ai}/{key}'))
    scene_plan = original_artifacts[1] if original_artifacts is not None else _read(root, 'scene_plan.json')
    _schema('scene_plan', scene_plan)
    from lib.production_execution import _artifact_path
    manifest_path = _artifact_path(root, 'asset_manifest.json')
    manifest = _read(root, 'asset_manifest.json') if manifest_path.exists() else None
    if manifest is not None:
        _schema('asset_manifest', manifest)
    if provider in {"openart_cli", "openart_mcp"} and check_native_controls:
        _check_required_native_controls(scene_plan, manifest, shot_id, native=native, project_dir=root)
    elif provider not in {"grok_cli", "openart_cli", "openart_mcp"}:
        raise ValueError("unsupported preparation provider")
    if reference_free:
        card = scene_plan.get('metadata', {}).get('visual_development', {}).get('shot_cards', {}).get(shot_id, {})
        for key in ('pinned_initial_frame', 'pinned_start_frame', 'pinned_first_frame', 'pinned_final_frame'):
            requirement = card.get(key)
            if requirement is not None:
                if not isinstance(requirement, dict) or type(requirement.get('required')) is not bool:
                    raise ValueError('malformed declared reference-free frame-pin requirement')
                if requirement['required']:
                    raise ValueError('reference-free contract conflicts with required frame pin')
        obligations = _manifest_requirement_binding(manifest, shot_id)
        if obligations['motion_handoffs'] or obligations['reference_assets']:
            raise ValueError('reference-free contract conflicts with manifest reference/handoff requirement')
    scenes = [s for s in scene_plan['scenes'] if s['id'] == shot_id]
    if len(scenes) != 1 or not scenes[0].get('script_section_id'):
        raise ValueError('explicit scene.script_section_id mapping required')
    script = original_artifacts[0] if original_artifacts is not None else _read(root, 'script.json')
    _schema('script', script)
    ids = [s['id'] for s in script['sections']]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate script section IDs')
    sid = scenes[0]['script_section_id']
    matches = [(i, s) for i, s in enumerate(script['sections']) if s['id'] == sid]
    if len(matches) != 1:
        raise ValueError('mapped script section missing')
    si, section = matches[0]
    rows.extend(_leaf(section['text'], f'/script/sections/{si}/text'))
    for n, row in enumerate(rows):
        row['occurrence_id'] = f'occ-{n:04d}'
    upstream = []
    for expected in shot['upstream']:
        item = selected[expected['shot_id']]
        upstream.append({'shot_id': expected['shot_id'], 'selection_sha256': digest(item),
                         'review_sha256': review_digest(item['review'])})
    binding = {'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
               'shot_id': shot_id, 'contract_sha256': contract_digest(contract),
               'approval_plan_sha256': approval_plan_digest(contract), 'references': refs,
               'upstream': upstream, 'script_sha256': digest(script), 'scene_plan_sha256': digest(scene_plan), 'asset_manifest_sha256': digest(_manifest_requirement_binding(manifest, shot_id)),
               'reviews_sha256': digest([contract['project_review'], shot.get('review')])}
    if reference_free:
        binding['reference_mode'] = 'reference_free'
    elif reference_guided:
        binding['reference_mode'] = 'reference_guided'
    return {'binding': binding, 'occurrences': rows, 'shot': shot}


def compile_prompt(project_dir, shot_id, *, native=None):
    """Return literal prompt and trace map; this does not create a passing review."""
    packet = source_packet(project_dir, shot_id, native=native)
    return _compile_packet(packet)


def _compile_packet(packet, *, native_pointer='/params/prompt'):
    prompt, coverage = '', []
    for row in packet['occurrences']:
        # Strings stay literal (including speech). Repeated lines remain distinct spans.
        fragment = str(row['value']) if isinstance(row['value'], str) else json.dumps(row['value'], ensure_ascii=False)
        parts = row['source_pointer'].split('/')
        if parts[1] == 'script':
            label = 'Script context'
        elif parts[2] == 'story':
            label = 'Story ' + ' '.join(part.replace('_', ' ') for part in parts[3:])
        elif parts[2] == 'shots':
            label = ' '.join(part.replace('_', ' ') for part in parts[4:])
        else:
            label = 'Reference ' + str(int(parts[3]) + 1) + ' ' + ' '.join(part.replace('_', ' ') for part in parts[4:])
        if parts[1] == 'script':
            label = 'Script context (not an additional spoken line)'
        elif 'dialogue' in parts:
            di = parts.index('dialogue')
            label = 'Dialogue ' + str(int(parts[di + 1]) + 1) + ' ' + parts[-1].replace('_', ' ')
        prefix = label.capitalize() + ': '
        start = len(prompt) + len(prefix)
        prompt += prefix + fragment + '\n'
        coverage.append({'occurrence_id': row['occurrence_id'], 'source_pointer': row['source_pointer'],
                         'value_sha256': row['value_sha256'], 'native_pointer': native_pointer,
                         'start': start, 'end': start + len(fragment), 'fragment_sha256': digest(fragment)})
    return {'prompt': prompt, 'coverage': coverage, 'source_binding': packet['binding']}


def build_static_source_packet(project_dir, shot_id, *, provider):
    """Validated provider-neutral source bytes; OpenArt's original guard is preserved."""
    packet = source_packet(project_dir, shot_id, provider=provider,
        check_native_controls=provider != 'openart_mcp')
    if provider == 'grok_cli':
        from lib.production_autonomy import contract_delta, current_projection
        contract_delta(project_dir, shot_id, current_projection(project_dir, shot_id))
    return packet


def compile_provider_prompt(project_dir, shot_id, *, provider, model=None):
    packet = build_static_source_packet(project_dir, shot_id, provider=provider)
    compiled = _compile_packet(packet, native_pointer='/arguments/prompt' if provider == 'grok_cli' else ('/params/sceneDescription' if provider == 'openart_mcp' and model == 'smart-shot' else '/params/prompt'))
    if provider == 'grok_cli':
        from tools._grok_cli_media import validate_prompt
        compiled['prompt'] = validate_prompt(compiled['prompt'])
    return compiled


def _body(native):
    if native.get('provider') == 'grok_cli':
        return {'params': copy.deepcopy(native['request']['arguments'])}
    if native.get('provider') == 'openart_mcp':
        from lib.openart_mcp import digest as mcp_digest
        body = copy.deepcopy(native['body'])
        if mcp_digest(body) != native['body_sha256']:
            raise ValueError('MCP native body binding changed')
        return body
    from lib import openart_jobs as jobs
    evidence = native['dry_run']
    receipt = jobs._load_receipt(evidence['receipt_id'], evidence['receipt_sha256'], 'native_preview_invalid')
    parsed = jobs.cli.dry_run_request(receipt['parsed'])
    if parsed['body_sha256'] != native['native_body_sha256']:
        raise ValueError('native body binding changed')
    return parsed['body']


def _native_binding(native):
    if native.get('provider') == 'openart_mcp':
        from lib.openart_mcp import validate_native_request
        validate_native_request(native)
        return {'provider': 'openart_mcp', 'transport': native['transport'],
                'native_controls_sha256': digest(native['body']['params']),
                'native_body_sha256': native['body_sha256'],
                'profile_sha256': native['profile_sha256'],
                'form_sha256': native['form_sha256'],
                'schema_observation_sha256': native['schema_observation_sha256'],
                'account_id_sha256': native['account_binding']['uid_sha256'],
                'account_observation_sha256': native['account_binding']['account_observation_sha256'],
                'references_sha256': native['source_binding_sha256'],
                'model': native['model'], 'mode': native['mode'], 'source': native['source']}
    if native.get('provider') == 'grok_cli':
        return copy.deepcopy(native)
    value = {key: native[key] for key in ('native_controls_sha256', 'native_argv_sha256', 'native_body_sha256',
        'profile_sha256', 'form_sha256', 'form_defaults_sha256', 'cli_version', 'tier', 'account_id_sha256', 'model', 'mode', 'source')}
    if 'native_capabilities_sha256' in native:
        value['native_capabilities_sha256'] = native['native_capabilities_sha256']
    return value


def mcp_native_settings(native):
    """Exact ordinary form controls; opaque reference IDs bind separately by hash.

    Retained connector media objects can contain signed URLs. They remain in the
    private native body and its hash instead of a public qualification packet.
    """
    reserved = {'prompt', 'sceneDescription', 'startFrame', 'endFrame', 'visualReferences',
                'characterReferenceImageUrls', 'environmentReferenceImageUrl'}
    for asset in native.get('input_assets', []):
        parts = str(asset.get('param_path', '')).strip('/').split('/')
        if parts and parts[0] == 'params':
            parts = parts[1:]
        if parts:
            reserved.add(parts[0])
    return {key: copy.deepcopy(value) for key, value in native['body']['params'].items() if key not in reserved}


def _number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f'{label}: finite number required')
    return value


def validate_timing(timing, packet, project_dir, body):
    """Check declared intervals and explicit estimates, never infer performance quality."""
    shot = packet['shot']
    duration_key = 'videoDuration' if body.get('model') == 'smart-shot' else 'duration'
    duration = _number(body['params'][duration_key], 'native duration', 0.001)
    if duration != shot['duration_seconds'] or timing['duration_seconds'] != duration:
        raise ValueError('timing duration differs from actual native duration')
    margin = _number(timing['margin_seconds'], 'margin')
    if not timing['language'].strip() or not timing['rationale'].strip() or not timing['overlap_rationale'].strip():
        raise ValueError('explicit language, timing and overlap rationale required')
    required_actions = {r['source_pointer']: r['value_sha256'] for r in packet['occurrences']
                        if r['source_pointer'].endswith(('/dominant_action', '/completed_end_state'))}
    seen_actions = set()
    windows = []
    for window in timing['action_windows']:
        if required_actions.get(window['source_pointer']) != window['value_sha256']:
            raise ValueError('action window lacks approved action/completion source binding')
        seen_actions.add(window['source_pointer'])
        start = _number(window['start_seconds'], 'action start')
        end = _number(window['end_seconds'], 'action end')
        if not start < end <= duration - margin:
            raise ValueError('action window exceeds native duration/margin')
        windows.append((start, end))
    if seen_actions != set(required_actions):
        raise ValueError('action and completed endpoint windows required')
    # Even measured audio must fit every approved speaker/source interval.
    for line in shot['dialogue']:
        start = _number(line['start_seconds'], 'dialogue start')
        end = _number(line['end_seconds'], 'dialogue end')
        if not 0 <= start < end <= duration - margin:
            raise ValueError('approved dialogue window exceeds native duration/margin')
    if timing['method'] == 'measured_audio':
        record = timing['audio']
        path = (Path(project_dir) / record['path']).resolve()
        if not path.is_relative_to(Path(project_dir).resolve()) or file_sha256(path) != record['sha256']:
            raise ValueError('approved audio bytes changed')
        measurement = record['measurement']
        if measurement['audio_sha256'] != record['sha256'] or not measurement['measurer'].strip():
            raise ValueError('audio measurement binding required')
        speech_end = _number(measurement['duration_seconds'], 'measured audio duration', 0.001)
        root = Path(project_dir).resolve()
        approval_path = (root / record['approval_path']).resolve()
        if not approval_path.is_relative_to(root) or record['approval_sha256'] != file_sha256(approval_path):
            raise ValueError('audio approval evidence changed or escapes project')
        approval = json.loads(approval_path.read_text())
        required = {'version', 'state', 'approved_by', 'audio_sha256', 'measurement_sha256',
                    'dialogue_sha256', 'contract_sha256', 'language', 'evidence'}
        if set(approval) != required or approval['version'] != '1.0' or approval['state'] != 'approved' \
                or not isinstance(approval['approved_by'], str) or not approval['approved_by'].strip() \
                or approval['audio_sha256'] != record['sha256'] or approval['measurement_sha256'] != digest(measurement) \
                or approval['dialogue_sha256'] != digest(shot['dialogue']) \
                or approval['contract_sha256'] != packet['binding']['contract_sha256'] \
                or approval['language'] != timing['language']:
            raise ValueError('approved audio must bind measurement and every dialogue occurrence/speaker/source')
        evidence = approval['evidence']
        if not isinstance(evidence, dict) or set(evidence) != {'path','sha256'}:
            raise ValueError('audio approval requires retained evidence')
        evidence_path = (root / evidence['path']).resolve()
        if not evidence_path.is_relative_to(root) or file_sha256(evidence_path) != evidence['sha256']:
            raise ValueError('audio approval evidence bytes changed')
        if speech_end + margin > duration:
            raise ValueError('measured speech exceeds native duration')
        if not shot['dialogue'] or speech_end > max(line['end_seconds'] for line in shot['dialogue']):
            raise ValueError('measured speech exceeds approved dialogue windows')
        speech_windows = [(line['start_seconds'], line['end_seconds']) for line in shot['dialogue']]
    else:
        segments = timing['segments']
        if len(segments) != len(shot['dialogue']):
            raise ValueError('timing needs every dialogue occurrence')
        speech_windows = []
        for i, (segment, line) in enumerate(zip(segments, shot['dialogue'])):
            if segment['dialogue_index'] != i or segment['text_sha256'] != digest(line['text']):
                raise ValueError('timing dialogue occurrence mismatch')
            if not segment['language'].strip() or not segment['rationale'].strip():
                raise ValueError('explicit segment language/rate rationale required')
            words = segment['word_count']
            if type(words) is not int or words != len(line['text'].split()):
                raise ValueError('explicit word count differs from utterance')
            rate = _number(segment['words_per_minute'], 'speech rate', 0.001)
            pause = _number(segment['pause_seconds'], 'pause')
            start, end = line['start_seconds'], line['end_seconds']
            if not 0 <= start < end <= duration - margin or words * 60 / rate + pause > end - start:
                raise ValueError('impossible dialogue timing')
            speech_windows.append((start, end))
    intervals = speech_windows + windows
    overlaps = any(max(a[0], b[0]) < min(a[1], b[1]) for i, a in enumerate(intervals) for b in intervals[i+1:])
    if overlaps and timing['overlap_policy'] != 'reviewed_parallel':
        raise ValueError('overlapping action/speech requires explicit parallel review')


def prepare_grok_native(inputs, observation):
    """Pure genuine native builder; caller observation is refreshed at dispatch outside locks."""
    from lib.production_execution import _clean
    from tools.video.grok_cli_video import build_native_video_request, GrokCLIVideo
    from tools._grok_cli_media import check_cli_feature_gates
    if set(observation) != {'cli_version', 'grok_path'} or not all(isinstance(v, str) and v.strip() for v in observation.values()):
        raise ValueError('closed fresh Grok CLI observation required')
    cleaned = _clean(inputs)
    # Match the selector's native dispatch projection without changing the
    # governed request digest: these are route/planning fields, not CLI controls.
    for key in ('preferred_tool', 'hosting_provider', 'preferred_provider', 'preferred_provider_gap',
                'allowed_providers', 'task_context', 'target_operation'):
        cleaned.pop(key, None)
    native = build_native_video_request(cleaned, adapter_version=GrokCLIVideo.version)
    check_cli_feature_gates(observation['cli_version'], native['arguments'])
    return {'provider': 'grok_cli', 'cli_version': observation['cli_version'],
            'grok_path': observation['grok_path'], 'request': native}


def prep_builder(provider):
    if provider == 'grok_cli':
        return prepare_grok_native
    if provider == 'openart_cli':
        from lib.openart_jobs import prepare_native_request
        return prepare_native_request
    if provider == 'openart_mcp':
        from lib.openart_mcp import prepare_native_request
        from lib.production_execution import _openart_mcp_controls
        return lambda inputs, profile: prepare_native_request(_openart_mcp_controls(inputs), profile)
    raise ValueError('provider outside Auto-continue')


def prepare_compiled_request(inputs, native, profile, *, coverage, timing):
    """Build sidecar after a retained preview. Persist and obtain named review separately."""
    from lib.production_execution import planned_request_digest
    root = Path(inputs['project_dir']).resolve()
    packet = source_packet(root, inputs['governance']['shot_id'], provider=native.get('provider', 'openart_cli'), native=native)
    value = {'version': '1.0', 'request_sha256': planned_request_digest(inputs, project_dir=root),
             'source_binding': packet['binding'], 'native_binding': _native_binding(native),
             'coverage': copy.deepcopy(coverage), 'timing': copy.deepcopy(timing)}
    _validate_compiled(value, inputs, native, profile, packet)
    return value


def _schema(name, value):
    try:
        Draft202012Validator(load_schema(name)).validate(value)
    except ValidationError as exc:
        path = '/'.join(map(str, exc.absolute_path)) or '$'
        raise ValueError(f'{name}: invalid {path} ({exc.validator})') from None


def _validate_compiled(compiled, inputs, native, profile, packet):
    from lib.production_execution import planned_request_digest
    _schema('compiled_request', compiled)
    grok = native.get('provider') == 'grok_cli'
    reference_free = packet['binding'].get('reference_mode') == 'reference_free'
    if reference_free and (grok or native.get('mode') != 'text2video'):
        raise ValueError('reference-free contract requires OpenArt text2video')
    mcp = native.get('provider') == 'openart_mcp'
    if mcp:
        from lib.openart_mcp import prepare_native_request
        from lib.production_execution import _openart_mcp_controls
        actual_native = prepare_native_request(_openart_mcp_controls(inputs), profile)
    elif grok:
        actual_native = prepare_grok_native(inputs, {'cli_version': native['cli_version'], 'grok_path': native['grok_path']})
    else:
        from lib import openart_jobs as jobs
        try:
            actual_native = jobs.prepare_native_request(controls(inputs), profile)
        except jobs.OpenArtCLIError as exc:
            raise ValueError('native preparation invalid: ' + exc.kind) from None
    if digest(actual_native) != digest(native):
        raise ValueError('stale native body/controls/version/tier/form/defaults')
    if digest(compiled['source_binding']) != digest(packet['binding']):
        raise ValueError('stale source/reference/review/upstream bindings')
    if compiled['request_sha256'] != planned_request_digest(inputs, project_dir=inputs['project_dir']):
        raise ValueError('compiled request changed')
    if digest(compiled['native_binding']) != digest(_native_binding(native)):
        raise ValueError('stale native body/controls/version/tier/form/defaults')
    body = _body(native)
    prompt_field = 'sceneDescription' if mcp and native['model'] == 'smart-shot' else 'prompt'
    prompt = body['params'][prompt_field]
    rows, coverage = packet['occurrences'], compiled['coverage']
    if len(rows) != len(coverage):
        raise ValueError('coverage omits required occurrence')
    spans = []
    for row, mapped in zip(rows, coverage):
        if any(mapped[key] != row[key] for key in ('occurrence_id', 'source_pointer', 'value_sha256')):
            raise ValueError('coverage occurrence/source mismatch')
        start, end = mapped['start'], mapped['end']
        expected_pointer = '/arguments/prompt' if grok else '/params/' + prompt_field
        if mapped['native_pointer'] != expected_pointer or not 0 <= start < end <= len(prompt):
            raise ValueError('coverage must target actual native prompt fragment')
        fragment = prompt[start:end]
        if mapped['fragment_sha256'] != digest(fragment):
            raise ValueError('actual native fragment changed')
        expected = str(row['value']) if isinstance(row['value'], str) else json.dumps(row['value'], ensure_ascii=False)
        if fragment != expected:
            raise ValueError('literal source occurrence dropped/changed in native prompt')
        if any(max(start, a) < min(end, b) for a, b in spans):
            raise ValueError('duplicate coverage reuses a native fragment')
        spans.append((start, end))
    if reference_free:
        if packet['binding']['references'] or packet['binding']['upstream'] or native.get('image_upload') is not None:
            raise ValueError('reference-free native request contains image/reference obligations')
        media_fields = ('startFrame', 'endFrame', 'visualReferences', 'characterReferenceImageUrls', 'environmentReferenceImageUrl') if mcp else ('image', 'images', 'first_frame', 'last_frame', 'keyframes', 'references', 'voices', 'audio')
        if (mcp and (native.get('input_assets') or any(key in body['params'] and body['params'][key] not in ([], None) for key in media_fields))) or (not mcp and any(key in body['params'] for key in media_fields)):
            raise ValueError('reference-free native request contains image/reference controls')
    elif grok:
        refs = packet['binding']['references']
        start_refs = [r for r in refs if r['role'] == 'start_frame' and r['id'] in packet['shot']['asset_ids']]
        submitted = native['request']['input_assets']
        if len(start_refs) != 1 or not any(r['role'] == 'first_frame' and r['sha256'] == start_refs[0]['sha256'] for r in submitted):
            raise ValueError('Grok native request must carry approved start board')
    else:
        validate_openart_asset_roles(packet, native)
        scene = _read(Path(inputs['project_dir']).resolve(), 'scene_plan.json')
        from lib.production_execution import _artifact_path
        root = Path(inputs['project_dir']).resolve()
        manifest = _read(root, 'asset_manifest.json') if _artifact_path(root, 'asset_manifest.json').exists() else None
        _check_required_native_controls(scene, manifest, packet['binding']['shot_id'], native=native, project_dir=root)
    if not grok and not mcp and profile['source'] == 'real':
        from lib import openart_jobs as jobs
        from tools import _openart_cli as cli
        entry = next(e for e in profile['captured_receipts'] if e['kind'] == 'form')
        form = jobs._receipt_parsed(entry['receipt_id'], entry['receipt_sha256'])
        try:
            root_schema = cli.form_root_schema(form, model=profile['model'], mode=profile['mode'])
            form_schema = ({root_schema['union']: root_schema['branches']} if root_schema['union']
                           else root_schema['branches'][0])
            Draft202012Validator(form_schema).validate(body['params'])
        except cli.OpenArtCLIError:
            raise ValueError('native form schema or metadata is unsupported') from None
        except ValidationError as exc:
            raise ValueError('native form rejects field ' + '/'.join(map(str, exc.absolute_path))) from None
    validate_timing(compiled['timing'], packet, inputs['project_dir'], body)


def validate_openart_asset_roles(packet, native):
    """Bind actual native media roles to reviewed shot bytes, never prompt mentions.

    Native builders verify retained upload proofs; this checks their immutable
    role/source results against the authoritative contract. Legacy single-image
    profiles retain their original representation.
    """
    submitted = native.get('input_assets')
    if submitted is None:
        upload = native.get('image_upload')
        submitted = ([{'role': 'first_frame', 'source_sha256': upload['source_sha256']}]
                     if upload else [])
    if not isinstance(submitted, list):
        raise ValueError('OpenArt native asset roles missing')
    refs = [r for r in packet['binding']['references'] if r['id'] in packet['shot']['asset_ids']
            or r['role'] == 'identity_reference' and set(r.get('cast_ids', [])).intersection(packet['shot'].get('cast_ids', []))]
    if packet['binding'].get('reference_mode') == 'reference_guided':
        if native.get('provider') != 'openart_mcp' or native.get('mode') not in {'element2video', 'generate-shot-video'}:
            raise ValueError('reference-guided contract requires an explicit native reference-guided mode')
        role_options = {'start_frame': {'reference_image', 'environment_reference', 'first_frame'},
            'end_frame': {'reference_image', 'environment_reference', 'last_frame'},
            'identity_reference': {'reference_image', 'character_reference'},
            'reference_image': {'reference_image'}, 'reference_video': {'reference_video'},
            'reference_audio': {'reference_audio'}}
        approved = {(role, ref['sha256']) for ref in refs for role in role_options.get(ref['role'], set())}
        for asset in submitted:
            if (asset.get('role'), asset.get('source_sha256')) not in approved:
                raise ValueError('OpenArt guided native role/source is not an approved shot asset')
        for ref in refs:
            if ref['role'] in {'start_frame', 'end_frame'}:
                continue  # reviewed composition targets, no implicit temporal pin
            if ref['role'] in role_options and not any(asset.get('role') in role_options[ref['role']]
                    and asset.get('source_sha256') == ref['sha256'] for asset in submitted):
                raise ValueError('OpenArt guided native request omits approved reference role ' + ref['role'])
        return
    starts = [r for r in refs if r['role'] == 'start_frame']
    if len(starts) != 1 or not any(a.get('role') == 'first_frame' and a.get('source_sha256') == starts[0]['sha256'] for a in submitted):
        raise ValueError('OpenArt closed shot contract needs approved start board')
    mapping = {'start_frame': 'first_frame', 'end_frame': 'last_frame',
               'identity_reference': 'reference_image', 'reference_image': 'reference_image',
               'reference_video': 'reference_video',
               'reference_audio': 'reference_audio'}
    approved = {(mapping.get(r['role']), r['sha256']) for r in refs}
    for asset in submitted:
        if (asset.get('role'), asset.get('source_sha256')) not in approved:
            raise ValueError('OpenArt native role/source is not an approved shot asset')
    # The shot contract always retains a reviewed ending target. It becomes a
    # native pin only through the canonical scene/handoff requirement checked
    # separately; an optional target remains bound in source/semantic review.
    # Identity references may be carried by the reviewed composite starting board
    # in image2video. Other modes must preserve separately declared references.
    required = [r for r in refs if r['role'] in mapping
                and r['role'] != 'end_frame'
                and not (r['role'] == 'identity_reference' and native.get('mode') == 'image2video')]
    for ref in required:
        if not any(a.get('role') == mapping[ref['role']] and a.get('source_sha256') == ref['sha256'] for a in submitted):
            raise ValueError('OpenArt native request omits approved reference role ' + ref['role'])


def validate_preparation(inputs, native, profile):
    """Dispatcher entry: pure, authoritative current sidecars; returns bound evidence."""
    root = Path(inputs['project_dir']).resolve()
    compiled = _read(root, 'compiled_request-' + _id(inputs.get('compiled_request_id')) + '.json')
    review = _read(root, 'preparation_review-' + _id(inputs.get('preparation_review_id')) + '.json')
    packet = source_packet(root, inputs['governance']['shot_id'], provider=native.get('provider', 'openart_cli'), native=native)
    return validate_preparation_evidence(compiled, review, inputs, native, profile, packet)


def validate_preparation_evidence(compiled, review, inputs, native, profile, packet):
    """Validate supplied preparation evidence; callers own loading and authority.

    Current and retained historical callers share these exact compiled/native,
    semantic-review and live-evidence requirements. This does not choose a source
    packet, relax current planning checks, or grant dispatch authority.
    """
    _validate_compiled(compiled, inputs, native, profile, packet)
    _schema('preparation_review', review)
    if review['review_id'] != inputs['preparation_review_id']:
        raise ValueError('named review ID differs from selected preparation ID')
    if review['subject_sha256'] != digest(compiled) or review['status'] != 'pass':
        raise ValueError('stale or nonpassing preparation review')
    names = [p['name'] for p in review['predicates']]
    if len(names) != len(PREDICATES) or set(names) != PREDICATES or any(p['status'] != 'pass' for p in review['predicates']):
        raise ValueError('all named critical preparation predicates must pass exactly once')
    if profile['source'] == 'real' and review['evidence_kind'] != 'reviewed':
        raise ValueError('fixture-only preparation review cannot qualify live provider')
    return {'compiled_sha256': digest(compiled), 'review_sha256': digest(review)}


def approved_upload_lookup(project_root, upload_id, source_sha256):
    """Pre-upload nonspend approval; intentionally independent of preview/compilation."""
    try:
        root = Path(project_root).resolve()
        record = _read(root, 'upload_approval-' + _id(upload_id) + '.json')
        expected = {'version', 'upload_id', 'asset_id', 'shot_id', 'source_sha256', 'source_binding',
                    'approved_by', 'evidence_path', 'evidence_sha256'}
        if set(record) != expected or record['version'] != '1.0' or not record['approved_by'].strip():
            return None
        packet = source_packet(root, record['shot_id'], check_native_controls=False)
        if record['upload_id'] != upload_id or record['source_sha256'] != source_sha256 or record['source_binding'] != packet['binding']:
            return None
        asset = next(a for a in packet['binding']['references'] if a['id'] == record['asset_id'])
        if asset['role'] not in {'start_frame', 'end_frame', 'identity_reference', 'reference_image', 'reference_video', 'reference_audio'} or asset['sha256'] != source_sha256 or not (asset['id'] in packet['shot']['asset_ids'] or asset['role'] == 'identity_reference'
                    and set(asset['cast_ids']).intersection(packet['shot']['cast_ids'])):
            return None
        evidence = (root / record['evidence_path']).resolve()
        if not evidence.is_relative_to(root) or file_sha256(evidence) != record['evidence_sha256']:
            return None
        return {'state': 'approved', 'upload_id': upload_id, 'source_sha256': source_sha256, 'approval_sha256': digest(record)}
    except (ValueError, OSError, KeyError, TypeError, StopIteration, ValidationError):
        return None


def freeze_approval(attempt_id, scope, evidence_bytes):
    """Retain complete approval privately, publishing opaque hash-bound counterparts."""
    from tools import _openart_cli as cli
    aid = _id(attempt_id)
    parent = cli.state_dir() / 'preparation' / aid
    raw = json.dumps(scope, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    cli.write_private(parent / 'scope.json', raw)
    cli.write_private(parent / 'approval.bin', evidence_bytes)
    if hashlib.sha256(evidence_bytes).hexdigest() != scope['evidence']['sha256']:
        raise ValueError('approval evidence changed during private snapshot')
    return {'snapshot_id': aid, 'scope_sha256': hashlib.sha256(raw).hexdigest(),
            'evidence_sha256': hashlib.sha256(evidence_bytes).hexdigest()}


def public_scope(scope):
    """Keep control/provenance structure; private prompts and URLs remain opaque."""
    from tools import _openart_cli as cli
    def hide(value, key=None):
        if key == 'prompt':
            return '<private OpenArt prompt>'
        if isinstance(value, dict):
            return {k: hide(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [hide(v) for v in value]
        return cli.redact(value)
    return hide(scope)


def load_private_approval(request):
    """Verify immutable private scope and original evidence before provenance replay."""
    from tools import _openart_cli as cli
    binding = request['openart']['approval_snapshot']
    aid = _id(request['attempt_id'])
    if binding['snapshot_id'] != aid:
        raise ValueError('private approval snapshot identity differs')
    parent = cli.state_dir() / 'preparation' / aid
    scope_path, evidence = parent / 'scope.json', parent / 'approval.bin'
    for path in (scope_path, evidence):
        cli._check_private(path, False)
    if file_sha256(scope_path) != binding['scope_sha256'] or file_sha256(evidence) != binding['evidence_sha256']:
        raise ValueError('private approval snapshot changed')
    scope = json.loads(scope_path.read_text())
    if public_scope(scope) != request['scope'] or scope['evidence']['sha256'] != binding['evidence_sha256']:
        raise ValueError('public/private approval scope differs')
    public_evidence = request['approval_evidence']
    if public_evidence != {'snapshot_id': aid, 'sha256': binding['evidence_sha256']}:
        raise ValueError('public/private approval evidence differs')
    return {'scope': scope, 'approval_evidence': {'path': str(evidence), 'sha256': binding['evidence_sha256']},
            'private_parent': parent}


def freeze_preparation(attempt_id, inputs, native, profile):
    """Recheck current preparation and freeze complete named evidence privately."""
    from tools import _openart_cli as cli
    proof = validate_preparation(inputs, native, profile)
    root = Path(inputs['project_dir']).resolve()
    compiled = _read(root, 'compiled_request-' + _id(inputs['compiled_request_id']) + '.json')
    review = _read(root, 'preparation_review-' + _id(inputs['preparation_review_id']) + '.json')
    if proof != {'compiled_sha256': digest(compiled), 'review_sha256': digest(review)}:
        raise ValueError('preparation changed while snapshotting')
    packet = source_packet(root, inputs['governance']['shot_id'], provider=native.get('provider', 'openart_cli'), native=native)
    if packet['binding'] != compiled['source_binding']:
        raise ValueError('source packet changed while snapshotting')
    payload = {'compiled': compiled, 'review': review, 'source_packet': packet}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    aid = _id(attempt_id)
    cli.write_private(cli.state_dir() / 'preparation' / aid / 'review.json', raw)
    return {'snapshot_id': aid, 'snapshot_sha256': hashlib.sha256(raw).hexdigest(), **proof}


def _validate_frozen_preparation(request, frozen, project_dir, *, current_required, original_reviews=False):
    """Replay the frozen preparation against current source and frozen native body."""
    from tools import _openart_cli as cli
    proof = request['openart']['preparation_snapshot']
    aid = _id(request['attempt_id'])
    if proof['snapshot_id'] != aid:
        raise ValueError('preparation snapshot identity differs')
    path = cli.state_dir() / 'preparation' / aid / 'review.json'
    cli._check_private(path, False)
    if file_sha256(path) != proof['snapshot_sha256']:
        raise ValueError('preparation snapshot bytes changed')
    data = json.loads(path.read_text())
    if set(data) != {'compiled', 'review', 'source_packet'} or digest(data['compiled']) != proof['compiled_sha256'] or digest(data['review']) != proof['review_sha256']:
        raise ValueError('preparation snapshot binding differs')
    inputs = copy.deepcopy(frozen['inputs'])
    inputs.pop('cli_session_id', None)
    inputs['project_dir'] = str(Path(project_dir).resolve())
    inputs['governance'] = {'scope_id': request['scope_id'], 'shot_id': request['shot_id']}
    # Frozen paths bind copied bytes; planning digest restores their approved locations.
    from lib.production_execution import _paths
    records = iter(request['input_assets'])
    def restore(role, path):
        record = next(records)
        if record['role'] != role or record['path'] != str(path) or file_sha256(path) != record['sha256']:
            raise ValueError('preparation input snapshot differs')
        return record['original_path']
    inputs = _paths(inputs, Path(project_dir).resolve(), restore)
    packet = data['source_packet']
    if current_required:
        reconstruct = _historical_source_packet if original_reviews else source_packet
        current = reconstruct(project_dir, request['shot_id'], native=frozen['native'])
        stable = set(packet['binding']) - {'contract_sha256', 'reviews_sha256'}
        if any(current['binding'][key] != packet['binding'][key] for key in stable):
            raise ValueError('stale source/reference/review/upstream bindings')
    validate_preparation_evidence(data['compiled'], data['review'], inputs,
        frozen['native'], frozen['profile'], packet)
    return proof


def validate_frozen_preparation(request, frozen, project_dir):
    """Certify preparation against current planning and immutable native bytes."""
    return _validate_frozen_preparation(request, frozen, project_dir, current_required=True)


def _validate_frozen_preparation_original(request, frozen, project_dir):
    """Re-prove retained generation with current source bytes/original reviews.

    Current stable planning/reference/selected subjects still have to match the
    frozen packet. Separate current eligibility readers validate audio successors.
    """
    return _validate_frozen_preparation(request, frozen, project_dir,
        current_required=True, original_reviews=True)


def validate_frozen_preparation_history(request, frozen, project_dir):
    """Validate an immutable historical fact, without claiming current eligibility.

    All named evidence, copied asset bytes, compiled content, native/profile proof,
    timing, coverage and preparation predicates remain required.
    """
    return _validate_frozen_preparation(request, frozen, project_dir, current_required=False)
