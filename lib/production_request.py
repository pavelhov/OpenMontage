"""OpenArt preparation. Agents judge semantics; this module binds and retains evidence.

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
REFERENCE_GUIDED_MODES = frozenset({'element2video', 'generate-shot-video'})
_GUIDED_ROLE_OPTIONS = {'start_frame': {'reference_image', 'environment_reference', 'first_frame'},
    'end_frame': {'reference_image', 'environment_reference', 'last_frame'},
    'payoff_board': {'reference_image', 'environment_reference'},
    'identity_reference': {'reference_image', 'character_reference'},
    'reference_image': {'reference_image'}, 'reference_video': {'reference_video'},
    'reference_audio': {'reference_audio'}}


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


SHOOTABLE_COMPILER = 'shootable_v1'


def _literal(value):
    return str(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _row(value, pointer, before, after='\n'):
    row = next(_leaf(value, pointer))
    row.update(before=before, after=after)
    return row


def _seconds(value):
    return ('%.3f' % value).rstrip('0').rstrip('.') + 's'


def _shootable_rows(contract, shot, index, reference_free):
    """Prospective shootable projection: each authored direction stays a literal
    span; IDs, completion flags, roles, hashes and reviews stay in bindings."""
    names = contract['cast_names']
    base = f'/shot_contract/shots/{index}'

    def name(cast_id, before, after):
        if cast_id not in names:
            raise ValueError('shootable prompt needs an authored cast_names entry for ' + cast_id)
        return _row(names[cast_id], '/shot_contract/cast_names/' + cast_id.replace('~', '~0').replace('/', '~1'), before, after)

    rows = []
    if reference_free:
        for key in ('desire', 'action', 'consequence', 'payoff'):
            rows.append(_row(contract['story'][key], '/shot_contract/story/' + key, 'Story ' + key + ': '))
    rows.append(_row(shot['initial_state'], base + '/initial_state', 'Opening frame: '))
    rows.append(_row(shot['dominant_action'], base + '/dominant_action', 'Main action: '))
    rows.append(_row(shot['completed_end_state'], base + '/completed_end_state',
                     'End of clip: '))
    cast = shot['cast_ids']
    for n, cast_id in enumerate(cast):
        rows.append(name(cast_id, 'Characters in this shot: ' if n == 0 else ' and ',
                         '.\n' if n == len(cast) - 1 else ''))
    for cast_id in shot['required_visible_speakers']:
        rows.append(name(cast_id, '', ' must be clearly visible on screen while speaking.\n'))
    for n, invariant in enumerate(shot['prop_body_invariants']):
        rows.append(_row(invariant, f'{base}/prop_body_invariants/{n}', 'Keep consistent: '))
    for n, change in enumerate(shot['allowed_transformations']):
        rows.append(_row(change['description'], f'{base}/allowed_transformations/{n}/description',
                         'Allowed change: ', '\n'))
        for m, keep in enumerate(change['preserved_invariants']):
            rows.append(_row(keep, f'{base}/allowed_transformations/{n}/preserved_invariants/{m}',
                             '  During this change keep: '))
    if 'camera' in shot:
        rows.append(_row(shot['camera'], base + '/camera', 'Camera: '))
    rows.append(_row(shot['transition']['rationale'], base + '/transition/rationale',
                     'Continuity from the previous shot: ' if shot['transition']['type'] == 'continuous'
                     else 'Shot opening after the cut: '))
    for n, line in enumerate(shot['dialogue']):
        where = {'visible': 'on screen', 'offscreen': 'off screen, voice only'}.get(line['source'])
        if where is None:
            raise ValueError('shootable prompt cannot render dialogue source ' + repr(line['source']))
        rows.append(name(line['speaker_id'], 'Spoken line %d: ' % (n + 1),
            ' (%s), from %s to %s, says exactly: "' % (where, _seconds(line['start_seconds']),
                                                       _seconds(line['end_seconds']))))
        rows.append(_row(line['text'], f'{base}/dialogue/{n}/text', '', '"\n'))
    if not shot['dialogue']:
        rows[-1]['after'] += 'No spoken lines in this shot.\n'
    if 'sound' in shot:
        rows.append(_row(shot['sound'], base + '/sound', 'Sound: '))
    return rows


def _shootable_scene_rows(scene, si, rows):
    """Authored scene-plan camera language, already bound by scene_plan_sha256."""
    out = []
    for key, label in (('framing', 'Framing: '), ('movement', 'Camera movement: ')):
        if isinstance(scene.get(key), str) and scene[key].strip():
            out.append(_row(scene[key], f'/scene_plan/scenes/{si}/{key}', label))
    language = scene.get('shot_language') or {}
    words = [str(language[k]).replace('_', ' ') + suffix for k, suffix in
             (('shot_size', ' shot'), ('camera_movement', ' camera'), ('lens_mm', 'mm lens')) if k in language]
    if words and (out or rows):
        (out or rows)[-1]['after'] += 'Shot style: ' + ', '.join(words) + '.\n'
    return out


def _compile_shootable(packet, native_pointer):
    prompt, coverage = '', []
    for row in packet['occurrences']:
        fragment = _literal(row['value'])
        prompt += row['before']
        start = len(prompt)
        prompt += fragment + row['after']
        coverage.append({'occurrence_id': row['occurrence_id'], 'source_pointer': row['source_pointer'],
                         'value_sha256': row['value_sha256'], 'native_pointer': native_pointer,
                         'start': start, 'end': start + len(fragment), 'fragment_sha256': digest(fragment)})
    return {'prompt': prompt, 'coverage': coverage, 'source_binding': packet['binding']}


def source_packet(project_dir, shot_id, *, provider="openart_cli", native=None, check_native_controls=True):
    """Resolve authoritative closed contract, explicit scene→script mapping and bytes."""
    return _source_packet(project_dir, shot_id, provider=provider, native=native,
        check_native_controls=check_native_controls, historical=False)


def _historical_source_packet(project_dir, shot_id, *, provider="openart_cli", native=None, check_native_controls=True, reference_bindings=None):
    """Reconstruct current original source facts for retained authority replay.

    Callers must independently bind this projection to their trusted retained
    contract/preparation snapshots. No current listening or dispatch authority
    follows from this packet; prospective readers always use ``source_packet``.
    """
    return _source_packet(project_dir, shot_id, provider=provider, native=native,
        check_native_controls=check_native_controls, historical=True, reference_bindings=reference_bindings)


def _original_source_packet(project_dir, shot_id, *, native, contract, selected,
                                 script, scene_plan, reference_bindings=None):
    """Source-only projection; provenance must bind these original snapshots.
    Prospective preparation/dispatch never reads caller-supplied source facts.
    """
    return _source_packet(project_dir, shot_id, provider='openart_mcp', native=native,
        check_native_controls=True, historical=True, original_sources=(contract, selected),
        original_artifacts=(script, scene_plan), reference_bindings=reference_bindings)


def _source_packet(project_dir, shot_id, *, provider, native, check_native_controls, historical,
                   original_sources=None, original_artifacts=None, reference_bindings=None):
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
    if reference_guided and provider not in {'openart_mcp', 'grok_cli'}:
        raise ValueError('reference-guided contract requires an explicit native reference route')
    frame_conditioned = shot.get('reference_mode', contract.get('reference_mode')) == 'frame_conditioned'
    frame_input = None
    if frame_conditioned:
        from lib.shot_contract import frame_conditioned_start
        frame_input = frame_conditioned_start(contract, shot, root)
        if provider not in {'grok_cli', 'openart_mcp'}:
            raise ValueError('frame-conditioned treatment requires Grok CLI or OpenArt MCP image2video')
    reference_free = (contract.get('reference_mode') == 'reference_free'
                      or shot.get('reference_mode') == 'reference_free')
    if reference_free and provider not in {'openart_cli', 'openart_mcp'}:
        raise ValueError('reference-free contract requires OpenArt text2video')
    shootable = contract.get('prompt_compiler') == SHOOTABLE_COMPILER
    rows = []
    if shootable:
        rows.extend(_shootable_rows(contract, shot, index, reference_free))
    elif reference_free:
        rows.extend(_leaf(contract['story'], '/shot_contract/story'))
    for key in () if shootable else ('initial_state', 'dominant_action', 'completed_end_state', 'endpoint_completion',
                'cast_ids', 'required_visible_speakers', 'dialogue', 'prop_body_invariants',
                'allowed_transformations', 'transition'):
        rows.extend(_leaf(shot[key], f'/shot_contract/shots/{index}/{key}'))
    refs = []
    payoff_id = contract.get('payoff_asset_id')
    from lib.shot_contract import required_asset_ids, shot_local, shot_review_subject
    if shot_local(contract):
        needed_assets = required_asset_ids(contract, shot)
    else:
        # Historical source packets intentionally omit native references for a
        # per-shot free cutaway; the legacy validator still checks global boards.
        needed_assets = set(shot['asset_ids']) | ({payoff_id} if not reference_free else set())
        needed_cast = set() if reference_free else set(shot['cast_ids']) | set(contract['late_cast_ids']) | set(contract['payoff_speaker_ids'])
        needed_assets.update(a['id'] for a in contract['assets'] if a['role'] == 'identity_reference' and needed_cast.intersection(a['cast_ids']))
    # All contract review bindings are retained: review prose is outside contract_digest.
    consumer_assets = (sorted((asset for asset in contract['assets'] if asset['id'] in needed_assets), key=lambda a: a['id'])
                       if shot_local(contract) else contract['assets'])
    native_consumers = None
    compact_source = False
    for ai, asset in enumerate(consumer_assets):
        if asset['id'] not in needed_assets:
            continue
        path = (root / asset['path']).resolve()
        if file_sha256(path) != asset['sha256']:
            raise ValueError('reference bytes changed')
        reference = {'id': asset['id'], 'role': asset['role'], 'cast_ids': asset['cast_ids'],
                     'sha256': asset['sha256'], 'review_sha256': review_digest(asset['review'])}
        if 'time_seconds' in asset and not historical:
            reference['time_seconds'] = asset['time_seconds']
        # Consumer meaning may differ from an immutable board's primary story
        # role (a shared ending becomes the next start). Keep primary semantics
        # in the contract; project exact role/time into this shot's source packet.
        if not historical and shot_local(contract) and (root / 'artifacts/board_preparation.json').exists():
            from lib import production_boards as boards
            # Validate the shared plan once per packet; every asset still gets
            # its own current-byte and review binding above.
            if native_consumers is None:
                pack = boards.load_board_pack(root)
                binding = pack.get('package_binding')
                if binding:
                    from lib import production_images as images
                    authored = json.loads(images._bound_file(root, binding, 'production package').read_text())
                    compact_source = authored.get('authoring_form') == COMPACT_AUTHORING_FORM
                native_consumers = {}
                for row in pack['specification']['shots']:
                    if row['shot_id'] == shot_id:
                        for consumer in row['roles']:
                            if consumer['role'] in NATIVE_BOARD_ROLES:
                                native_consumers.setdefault(consumer['asset_id'], []).append(consumer)
            consumers = native_consumers.get(asset['id'], [])
            if consumers and not (asset['role'] == 'payoff_board' and len(consumers) == 1 and consumers[0]['role'] == 'start_frame'):
                consumer_reference = {key: value for key, value in reference.items() if key != 'time_seconds'}
                for consumer in consumers:
                    refs.append({**consumer_reference, 'role': consumer['role'],
                                 **({'time_seconds': consumer['time_seconds']} if consumer['role'] == 'timed_keyframe' else {}),
                                 **({'native_required': True} if compact_source and consumer['role'] == 'end_frame' else {})})
            else:
                if compact_source and asset['role'] == 'payoff_board' and not consumers:
                    reference['native_required'] = False
                refs.append(reference)
        else:
            refs.append(reference)
        if not shootable and (asset['id'] in shot['asset_ids'] or asset['id'] == payoff_id or asset['role'] == 'identity_reference'):
            for key in ('id', 'role', 'cast_ids'):
                pointer = ('/shot_contract/assets/by_id/' + asset['id'].replace('~', '~0').replace('/', '~1')
                           if shot_local(contract) else f'/shot_contract/assets/{ai}')
                rows.extend(_leaf(asset[key], pointer + '/' + key))
    scene_plan = original_artifacts[1] if original_artifacts is not None else _read(root, 'scene_plan.json')
    _schema('scene_plan', scene_plan)
    if historical and reference_bindings is not None:
        refs = _replay_reference_projection(root, shot_id, scene_plan, refs, reference_bindings, contract)
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
    # An empty context is still bound by script_sha256 below, but cannot have
    # a non-empty literal span in the compiled native prompt.
    if shootable:
        rows.extend(_shootable_scene_rows(scenes[0], scene_plan['scenes'].index(scenes[0]), rows))
    if section['text'] != '':
        rows.extend(_leaf(section['text'], f'/script/sections/{si}/text'))
        if shootable:
            rows[-1].update(before='Story context (do not speak or print): ', after='\n')
    for n, row in enumerate(rows):
        row['occurrence_id'] = f'occ-{n:04d}'
    upstream = []
    from lib.shot_contract import source_upstream_ids
    sources = source_upstream_ids(contract, shot, root)
    for expected in shot['upstream']:
        if expected['shot_id'] not in sources:
            continue  # factory ordering note: no outgoing frame feeds this prompt
        item = selected[expected['shot_id']]
        upstream.append({'shot_id': expected['shot_id'], 'selection_sha256': digest(item),
                         'review_sha256': review_digest(item['review'])})
    binding = {'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
               'shot_id': shot_id, 'contract_sha256': contract_digest(contract),
               'approval_plan_sha256': approval_plan_digest(contract), 'references': refs,
               'upstream': upstream, 'script_sha256': digest(script), 'scene_plan_sha256': digest(scene_plan), 'asset_manifest_sha256': digest(_manifest_requirement_binding(manifest, shot_id)),
               'reviews_sha256': digest([contract['project_review'], shot.get('review')])}
    if shot_local(contract):
        cards = scene_plan.get('metadata', {}).get('visual_development', {}).get('shot_cards', {})
        from lib.production_autonomy import local_timing
        binding.update(contract_sha256=shot_review_subject(contract, shot_id),
            approval_plan_sha256=approval_plan_digest(contract, shot_id),
            script_sha256=digest({'order': ids, 'section': local_timing(section)}),
            scene_plan_sha256=digest({'order': [s['id'] for s in scene_plan['scenes']],
                'scene': local_timing(scenes[0]), 'shot_card': cards.get(shot_id) if isinstance(cards, dict) else cards}))
    if shootable:
        binding['prompt_compiler'] = SHOOTABLE_COMPILER
    if reference_free:
        binding['reference_mode'] = 'reference_free'
    elif reference_guided:
        binding['reference_mode'] = 'reference_guided'
    elif frame_conditioned:
        binding['reference_mode'] = 'frame_conditioned'
        binding['frame_input'] = frame_input
    return {'binding': binding, 'occurrences': rows, 'shot': shot}


def compile_prompt(project_dir, shot_id, *, native=None):
    """Return literal prompt and trace map; this does not create a passing review."""
    packet = source_packet(project_dir, shot_id, native=native)
    return _compile_packet(packet)


def _compile_packet(packet, *, native_pointer='/params/prompt'):
    if packet['binding'].get('prompt_compiler') == SHOOTABLE_COMPILER:
        return _compile_shootable(packet, native_pointer)
    prompt, coverage = '', []
    for row in packet['occurrences']:
        # Strings stay literal (including speech). Repeated lines remain distinct spans.
        fragment = _literal(row['value'])
        parts = row['source_pointer'].split('/')
        if parts[1] == 'script':
            label = 'Script context'
        elif parts[2] == 'story':
            label = 'Story ' + ' '.join(part.replace('_', ' ') for part in parts[3:])
        elif parts[2] == 'shots':
            label = ' '.join(part.replace('_', ' ') for part in parts[4:])
        elif parts[2:4] == ['assets', 'by_id']:
            label = 'Reference ' + parts[4].replace('~1', '/').replace('~0', '~') + ' ' + ' '.join(part.replace('_', ' ') for part in parts[5:])
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


def compile_provider_prompt(project_dir, shot_id, *, provider, model=None,
                            input_assets=None, openart_project_id=None):
    packet = build_static_source_packet(project_dir, shot_id, provider=provider)
    compiled = _compile_provider_packet(packet, provider=provider, model=model, input_assets=input_assets)
    if provider == 'openart_mcp' and input_assets:
        compiled['_mapping_free_prompt'] = compiled['prompt']
        compiled['prompt'] += _ready_mcp_reference_mapping(project_dir, packet,
            input_assets, openart_project_id)
    return compiled


REFERENCE_MAPPING_HEADER = 'Native reference identity mapping (preserve these sources):\n'


def _ready_mcp_reference_mapping(project_dir, packet, input_assets, openart_project_id, *, native=None):
    """Explain actual upload labels using current approved source IDs, in order.

    Upload snapshots may have numeric names. This pure prompt handoff preserves
    their native references and the original receipt; missing uploads add nothing.
    """
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs
    root = Path(project_dir).resolve()
    contract = _read(root, 'shot_contract.json')
    needed = {row['id'] for row in packet['binding']['references']}
    sources = [asset for asset in contract['assets'] if asset['id'] in needed]
    rows = []
    for asset in input_assets:
        if asset.get('role') not in {'reference_image', 'reference_video', 'reference_audio',
                                      'character_reference', 'environment_reference'}:
            continue
        if native is None:
            ready = jobs.reuse_uploaded_input_asset(root, asset,
                account_binding=mcp.account_summary(), openart_project_id=openart_project_id)
        else:
            value = native['body']
            for key in asset['param_path'].strip('/').split('/'):
                value = value[int(key)] if isinstance(value, list) else value[key]
            ready = {**asset, 'visual_reference': value} if isinstance(value, dict) else None
        if ready is None:
            continue
        path = Path(ready['source_path']).resolve()
        matches = [source for source in sources if (root / source['path']).resolve() == path
                   and source['sha256'] == ready['source_sha256']]
        if not matches:
            raise ValueError('ready reference label has no current approved source binding')
        label = ready['visual_reference'].get('label')
        if not isinstance(label, str) or not label.strip():
            raise ValueError('ready native reference lacks its actual upload label')
        description = {'native_label': label, 'source_ids': [source['id'] for source in matches],
            'source_file': path.name, 'cast_ids': sorted({c for source in matches for c in source['cast_ids']})}
        rows.append('Reference ' + str(len(rows) + 1) + ': ' + json.dumps(description, ensure_ascii=False))
    return (REFERENCE_MAPPING_HEADER + '\n'.join(rows) + '\n') if rows else ''


def _h3_projection(packet, provider, model):
    return (provider == 'openart_mcp' and isinstance(model, str) and model.startswith('fal-h3')
            and packet['binding'].get('prompt_compiler') == SHOOTABLE_COMPILER)


def _h3_administrative_row(packet, row):
    parts = row['source_pointer'].split('/')
    return ((len(parts) == 5 and parts[1:3] == ['script', 'sections'] and parts[4] == 'text')
            or (len(parts) == 6 and parts[1:3] == ['shot_contract', 'shots']
                and parts[4:] == ['transition', 'rationale']
                and packet['shot']['transition']['type'] == 'hard_cut'))


def _h3_alignment(packet, input_assets=None):
    # Legacy image_path is the existing single first-frame control projection.
    roles = ([a['role'] for a in input_assets] if input_assets else
             ['first_frame'] if packet['binding'].get('frame_input') else [])
    pins = [role for role in ('first_frame', 'last_frame') if role in roles]
    lines = []
    for index, role in enumerate(pins, 1):
        seconds = 0 if role == 'first_frame' else packet['shot']['duration_seconds']
        target = '(from [Shot 1]) is fully referenced.' if role == 'first_frame' else 'is fully referenced as the final video image.'
        lines.append(f'For the target video, at {seconds:.2f} seconds into the target video, '
                     f'<Picture {index}> {target}')
    return ('\n'.join(lines) + '\n\n') if lines else ''


def _compile_h3_packet(packet, input_assets=None):
    """Literal H3 rendering of the same bound shot; no rewriting or new authority."""
    shot = packet['shot']
    cast_ids = list(dict.fromkeys(line['speaker_id'] for line in shot['dialogue']))
    speakers = {cast_id: 'S' + str(index + 1) for index, cast_id in enumerate(cast_ids)}
    rows = []
    for original in packet['occurrences']:
        if _h3_administrative_row(packet, original):
            continue
        row = copy.deepcopy(original)
        row['after'] = row['after'].replace('No spoken lines in this shot.\n', '')
        pointer = row['source_pointer']
        if '/cast_names/' in pointer:
            cast_id = pointer.rsplit('/', 1)[1].replace('~1', '/').replace('~0', '~')
            tag = '(' + speakers[cast_id] + ')' if cast_id in speakers else ''
            if row['before'].startswith('Spoken line '):
                # The following text occurrence stays separate, even for repeated lines.
                number = int(row['before'].split()[2].rstrip(':')) - 1
                line = shot['dialogue'][number]
                row['before'] = f"From {_seconds(line['start_seconds'])} to {_seconds(line['end_seconds'])}, "
                source = ' (off screen, diegetic speech)' if line['source'] == 'offscreen' else ''
                row['after'] = ' ' + tag + source + ' says: <d>'
            else:
                row['after'] = (' ' + tag if tag else '') + row['after']
        elif '/dialogue/' in pointer and pointer.endswith('/text'):
            row['after'] = '</d>\n'
        elif pointer.endswith('/sound'):
            row['before'] = 'overall_soundscape: '
        rows.append(row)
    compiled = _compile_shootable({**packet, 'occurrences': rows}, '/params/prompt')
    prefix = _h3_alignment(packet, input_assets) + 'integrated_multimodal_description: [Shot 1] '
    compiled['prompt'] = prefix + compiled['prompt']
    for span in compiled['coverage']:
        span['start'] += len(prefix)
        span['end'] += len(prefix)
    if not shot['dialogue']:
        compiled['prompt'] += 'No spoken dialogue.\n'
    if _no_music_default(packet):
        compiled['prompt'] += 'non_diegetic_music: N/A\n'
    return compiled


def _compile_historical_provider_prompt(project_dir, shot_id, *, provider, native):
    """Re-prove original source compilation, without prospective admission.

    Only retained-authority callers use this path after proving their policy and
    frozen preparation. Original source validation still reads current bytes;
    the caller separately checks retained contract deltas and exact native proof.
    """
    packet = _historical_source_packet(project_dir, shot_id, provider=provider, native=native)
    compiled = _compile_provider_packet(packet, provider=provider, model=native.get('model'), historical=True,
        input_assets=native.get('input_assets'))
    original_compilations = compiled.get('_historical_compilations', [
        {key: compiled[key] for key in ('prompt', 'coverage')}])
    originals = compiled.get('_historical_prompt_variants', [compiled['prompt']])
    variants = list(originals)
    compilations = copy.deepcopy(original_compilations)
    if provider == 'openart_mcp':
        mapping = _ready_mcp_reference_mapping(project_dir, packet, native.get('input_assets', []),
            native['body'].get('projectId'), native=native)
        if mapping:
            if _no_music_default(packet):
                compiled['prompt'] += NO_MUSIC_DIRECTION
            compiled['prompt'] += mapping
            variants.extend(value + (NO_MUSIC_DIRECTION if _no_music_default(packet)
                            and 'integrated_multimodal_description: [Shot 1]' not in value else '') + mapping
                            for value in originals)
            compilations.extend({**value,
                'prompt': value['prompt'] + (NO_MUSIC_DIRECTION if _no_music_default(packet)
                    and 'integrated_multimodal_description: [Shot 1]' not in value['prompt'] else '') + mapping}
                for value in original_compilations)
    compiled['_historical_prompt_variants'] = variants
    compiled['_historical_compilations'] = compilations
    return compiled


NO_MUSIC_DIRECTION = 'No background music; keep only the native scene sound.\n'


def _no_music_default(packet):
    """Authored shootable packages without a sound direction ask for no added music."""
    return (packet['binding'].get('prompt_compiler') == SHOOTABLE_COMPILER
            and 'sound' not in packet['shot'])


def historical_prompt_matches(prompt, canonical):
    """Frozen originals compiled before or after the no-music default both stay readable."""
    # Grok's seal strips the trailing newline before the historical canonical is
    # returned, so the appended direction is restored on its own exact line.
    candidates = set()
    for base in canonical.get('_historical_prompt_variants', [canonical['prompt']]):
        body = base.rstrip('\n\r') + '\n'
        candidates.update({base, base.rstrip('\n\r'), body + NO_MUSIC_DIRECTION,
                           (body + NO_MUSIC_DIRECTION).rstrip('\n\r')})
    return prompt in candidates


def historical_preparation_matches(prompt, coverage, canonical):
    """Legacy prompt spelling and span offsets must match the same compilation."""
    return any(coverage == variant['coverage'] and historical_prompt_matches(prompt, variant)
               for variant in canonical.get('_historical_compilations', [canonical]))


def _compile_provider_packet(packet, *, provider, model=None, historical=False, input_assets=None):
    if not historical and _h3_projection(packet, provider, model):
        return _compile_h3_packet(packet, input_assets)
    compiled = _compile_packet(packet, native_pointer='/arguments/prompt' if provider == 'grok_cli' else ('/params/sceneDescription' if provider == 'openart_mcp' and model == 'smart-shot' else '/params/prompt'))
    if not historical and _no_music_default(packet):
        compiled['prompt'] += NO_MUSIC_DIRECTION
    if provider == 'grok_cli':
        from tools._grok_cli_media import validate_prompt
        compiled['prompt'] = validate_prompt(compiled['prompt'])
    if historical and packet['binding'].get('prompt_compiler') == SHOOTABLE_COMPILER:
        compiled['_historical_prompt_variants'] = [compiled['prompt']]
        compiled['_historical_compilations'] = [{key: compiled[key] for key in ('prompt', 'coverage')}]
        # Retained spellings predate the neutral ending label. Compile each
        # exact historical wrapper with its own offsets; never rewrite originals.
        for action_label in ('Main action: ', 'Main action (one continuous beat): '):
            legacy = copy.deepcopy(packet)
            for row in legacy['occurrences']:
                if row['source_pointer'].endswith('/completed_end_state') and row['before'] == 'End of clip: ':
                    row['before'] = 'Ending frame (the action is finished on screen): '
                if row['source_pointer'].endswith('/dominant_action') and row['before'] == 'Main action: ':
                    row['before'] = action_label
            legacy_compiled = _compile_packet(legacy, native_pointer='/arguments/prompt' if provider == 'grok_cli' else
                ('/params/sceneDescription' if provider == 'openart_mcp' and model == 'smart-shot' else '/params/prompt'))
            legacy_prompt = legacy_compiled['prompt']
            if provider == 'grok_cli':
                legacy_prompt = validate_prompt(legacy_prompt)
            compiled['_historical_prompt_variants'].append(legacy_prompt)
            compiled['_historical_compilations'].append({'prompt': legacy_prompt, 'coverage': legacy_compiled['coverage']})
    if historical and _h3_projection(packet, provider, model):
        native_compiled = _compile_h3_packet(packet, input_assets)
        compiled['_historical_prompt_variants'].append(native_compiled['prompt'])
        compiled['_historical_compilations'].append({key: native_compiled[key] for key in ('prompt', 'coverage')})
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
        return _validated_mcp_native_binding(native)
    if native.get('provider') == 'grok_cli':
        return copy.deepcopy(native)
    value = {key: native[key] for key in ('native_controls_sha256', 'native_argv_sha256', 'native_body_sha256',
        'profile_sha256', 'form_sha256', 'form_defaults_sha256', 'cli_version', 'tier', 'account_id_sha256', 'model', 'mode', 'source')}
    if 'native_capabilities_sha256' in native:
        value['native_capabilities_sha256'] = native['native_capabilities_sha256']
    return value


def _validated_mcp_native_binding(native):
    """Pure binding projection after a fresh native rebuild in this boundary."""
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
            speech_seconds = words * 60 / rate + pause
            window_seconds = end - start
            # Decimal interval subtraction can make an exact fit appear too long.
            overrun = speech_seconds > window_seconds and not math.isclose(
                speech_seconds, window_seconds, rel_tol=0, abs_tol=1e-9)
            if not 0 <= start < end <= duration - margin or overrun:
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


def prepare_compiled_request(inputs, native, profile, *, coverage, timing, review=None, persist=False,
                             require_passing_review=False):
    """Build the compiled sidecar after a retained preview; never judge semantics.

    With no ``review``, return the compiled request as before. With a supplied
    reviewer-authored review, return ``(compiled, bound_review)``. Only the exact
    review ID, subject hash and authenticated generated-carrier membership are
    filled when omitted; conflicting supplied bindings reject. ``persist=True``
    retains both validated records with the existing exclusive immutable writer;
    an exact existing record is reused and conflicting records reject before any
    publication. The two writes are individually atomic, not a transaction.
    Reviewer, status, evidence kind and predicates remain supplied judgment,
    including failures or unknowns. ``require_passing_review=True`` checks the
    normal preparation gate against this same current packet before publication.
    Dispatch still performs its own fresh preparation/admission checks.
    """
    from lib.production_execution import planned_request_digest
    if persist and review is None:
        raise ValueError('persisting preparation requires an explicit reviewer-authored review')
    if require_passing_review and review is None:
        raise ValueError('passing preparation requires an explicit reviewer-authored review')
    root = Path(inputs['project_dir']).resolve()
    packet = source_packet(root, inputs['governance']['shot_id'], provider=native.get('provider', 'openart_cli'), native=native)
    # Bind mechanical source digests/counts from the authenticated source packet.
    # Author-supplied intervals, rates, rationale and any conflicting bindings
    # remain untouched and are checked by the normal compiled-request validator.
    timing = copy.deepcopy(timing)
    if isinstance(timing, dict) and timing.get('method') == 'segmented_estimate':
        occurrences = {}
        for row in packet['occurrences']:
            occurrences.setdefault(row['source_pointer'], []).append(row)
        actions = timing.get('action_windows')
        if isinstance(actions, list):
            for window in actions:
                if not isinstance(window, dict) or 'value_sha256' in window:
                    continue
                matches = occurrences.get(window.get('source_pointer'), [])
                if len(matches) != 1:
                    raise ValueError('action window source pointer must identify one exact source occurrence')
                window['value_sha256'] = matches[0]['value_sha256']
        segments = timing.get('segments')
        dialogue = packet['shot']['dialogue']
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                index = segment.get('dialogue_index')
                if type(index) is not int or not 0 <= index < len(dialogue):
                    if 'text_sha256' not in segment or 'word_count' not in segment:
                        raise ValueError('dialogue index must identify an exact dialogue occurrence')
                    continue
                line = dialogue[index]
                if 'text_sha256' not in segment:
                    segment['text_sha256'] = digest(line['text'])
                if 'word_count' not in segment:
                    segment['word_count'] = len(line['text'].split())
    value = {'version': '1.0', 'request_sha256': planned_request_digest(inputs, project_dir=root),
             'source_binding': packet['binding'], 'native_binding': _native_binding(native),
             'coverage': copy.deepcopy(coverage), 'timing': copy.deepcopy(timing)}
    _validate_compiled(value, inputs, native, profile, packet)
    if review is None:
        return value
    bound = _bind_preparation_review(inputs, packet, value, review)
    if require_passing_review:
        validate_preparation_evidence(value, bound, inputs, native, profile, packet, factory=False)
    elif persist:
        from lib.shot_contract import factory_project
        if factory_project(root):
            # Report-only verdicts still need complete truthful, current factual
            # bindings before immutable publication. A bad draft must not strand
            # this factory's deterministic preparation IDs before admission.
            validate_preparation_evidence(value, bound, inputs, native, profile, packet, factory=True)
    if persist:
        from lib.production_execution import _artifact_path, _inside, _write_new
        records = [(_id(inputs.get('compiled_request_id')), 'compiled_request-', value),
                   (_id(inputs.get('preparation_review_id')), 'preparation_review-', bound)]
        targets = []
        for record_id, prefix, record in records:
            path = _inside(_artifact_path(root, prefix + record_id + '.json'), root)
            if path.exists() and json.loads(path.read_text()) != record:
                raise ValueError('immutable preparation record already exists with different evidence')
            targets.append((path, record))
        for path, record in targets:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_new(path, record)
    return value, bound


def _bind_preparation_review(inputs, packet, compiled, review):
    from lib import production_autonomy as autonomy, production_boards as boards
    if not isinstance(review, dict):
        raise ValueError('reviewer-authored preparation review must be an object')
    bound = copy.deepcopy(review)
    expected = {'review_id': _id(inputs.get('preparation_review_id')),
                'subject_sha256': digest(compiled)}
    root = Path(inputs['project_dir']).resolve()
    frame = packet['binding'].get('frame_input')
    if frame is not None:
        pack = boards.load_board_pack(root)
        binding = pack['board_bindings'][frame['asset_id']]
        projection = autonomy.current_projection(root, inputs['governance']['shot_id'])
        if binding.get('origin') == 'canonical_image_import':
            members = []
            for asset in projection['assets']:
                source = binding.get('source_bindings', {}).get(asset['id'])
                if source is None:
                    continue
                if source != {key: asset[key] for key in ('path', 'sha256')}:
                    raise ValueError('generated carrier source binding differs from approved source')
                if asset['cast_ids'] and asset['role'] in {'identity_reference', 'start_frame', 'end_frame', 'payoff_board'}:
                    members.append({key: copy.deepcopy(asset[key]) for key in ('sha256', 'role', 'cast_ids')})
            composite = {'sha256': frame['sha256'], 'role': 'start_frame', 'members': members}
            # Even an empty candidate set must authenticate the actual original
            # carrier. Empty composites are not published into the review schema.
            autonomy._frame_carrier_members(root, frame, projection, [composite])
            expected['composite_boards'] = [composite] if members else []
        elif 'composite_boards' in bound:
            # Provided/reused boards retain explicit reviewer membership; original
            # generated-input facts must never be invented for them.
            autonomy._frame_carrier_members(root, frame, projection, bound['composite_boards'])
    for key, value in expected.items():
        if key in bound and bound[key] != value:
            raise ValueError('supplied preparation review ' + key + ' conflicts with canonical binding')
        bound[key] = value
    _schema('preparation_review', bound)
    return bound


def _schema(name, value):
    try:
        Draft202012Validator(load_schema(name)).validate(value)
    except ValidationError as exc:
        path = '/'.join(map(str, exc.absolute_path)) or '$'
        raise ValueError(f'{name}: invalid {path} ({exc.validator})') from None


def _validate_historical_prompt_override(marker, prompt, packet):
    """Pure reader for a retained historical creator prompt: authenticates the
    frozen marker against the exact submitted prompt and full source occurrences.
    It grants no dispatch; fresh overrides are refused above."""
    if (not isinstance(marker, dict) or set(marker) != {'kind', 'creator_repair', 'prompt_sha256', 'source_occurrences_sha256'}
            or marker['kind'] != 'creator_exact_prompt' or not isinstance(marker['creator_repair'], dict)
            or marker['prompt_sha256'] != digest(prompt)
            or marker['source_occurrences_sha256'] != digest(packet['occurrences'])):
        raise ValueError('historical creator prompt binding or full source occurrences changed')


def _dialogue_rows(packet):
    """Pure retained-history reader: only declared speech text carried a literal
    coverage obligation in a historical exact creator prompt. Full source facts
    remain bound by the authenticated marker; this grants no dispatch."""
    return [row for row in packet['occurrences']
            if '/dialogue/' in row['source_pointer'] and row['source_pointer'].endswith('/text')]


def historical_creator_prompt_coverage(compiled, prompt, packet, *, native_pointer):
    """Authenticate a retained creator prompt and re-derive its ordered literal
    dialogue trace. Read-only; fresh overrides stay refused by every writer path."""
    marker = compiled.get('creator_prompt_override')
    _validate_historical_prompt_override(marker, prompt, packet)
    coverage, cursor = [], 0
    for row in _dialogue_rows(packet):
        fragment = _literal(row['value'])
        start = prompt.find(fragment, cursor)
        if start < 0:
            raise ValueError('historical creator prompt drops or changes ordered dialogue text')
        end = start + len(fragment)
        coverage.append({key: row[key] for key in ('occurrence_id', 'source_pointer', 'value_sha256')})
        coverage[-1].update(native_pointer=native_pointer, start=start, end=end,
                            fragment_sha256=digest(fragment))
        cursor = end
    if compiled['coverage'] != coverage:
        raise ValueError('historical creator prompt coverage changed')
    return coverage


def _validate_compiled(compiled, inputs, native, profile, packet, *, historical=False):
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
        actual_native = prepare_native_request(_openart_mcp_controls(inputs), profile,
            **({'_retained_input_assets': native['input_assets']} if historical else {}))
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
    # MCP was freshly rebuilt and compared above. Project that same validated
    # result; other binding callers still own their full native revalidation.
    binding = _validated_mcp_native_binding(actual_native) if mcp else _native_binding(native)
    if digest(compiled['native_binding']) != digest(binding):
        raise ValueError('stale native body/controls/version/tier/form/defaults')
    body = _body(native)
    prompt_field = 'sceneDescription' if mcp and native['model'] == 'smart-shot' else 'prompt'
    prompt = body['params'][prompt_field]
    override = compiled.get('creator_prompt_override')
    if override is not None:
        if not historical:
            raise ValueError('creator prompt overrides are retired; originals use their authored prompt')
        _validate_historical_prompt_override(override, prompt, packet)
    rows = _dialogue_rows(packet) if override is not None else packet['occurrences']
    if override is None and _h3_projection(packet, native.get('provider'), native.get('model')):
        h3 = _compile_h3_packet(packet, native.get('input_assets'))
        expected = h3['prompt'] + _ready_mcp_reference_mapping(inputs['project_dir'], packet,
            native.get('input_assets', []), native['body'].get('projectId'), native=native)
        # Frozen generic originals retain their original complete coverage.
        if not historical or prompt == expected:
            if prompt != expected:
                raise ValueError('H3 prompt differs from bound native roles or literal projection')
            rows = [row for row in rows if not _h3_administrative_row(packet, row)]
    coverage = compiled['coverage']
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
    elif grok and packet['binding'].get('reference_mode') == 'reference_guided':
        _validate_grok_guided_assets(packet, native)
        from lib.production_execution import _artifact_path
        root = Path(inputs['project_dir']).resolve()
        manifest_path = _artifact_path(root, 'asset_manifest.json')
        _check_frame_hard_inputs(packet, native, _read(root, 'scene_plan.json'),
            _read(root, 'asset_manifest.json') if manifest_path.exists() else None, root)
    elif grok:
        refs = packet['binding']['references']
        frame = packet['binding'].get('frame_input')
        start_refs = [r for r in refs if (r['id'] == frame['asset_id'] and r['role'] in {'start_frame', 'payoff_board'} if frame else r['role'] == 'start_frame') and r['id'] in packet['shot']['asset_ids']]
        submitted = native['request']['input_assets']
        if len(start_refs) != 1 or not any(r['role'] == 'first_frame' and r['sha256'] == start_refs[0]['sha256'] for r in submitted):
            raise ValueError('Grok native request must carry approved start board')
        if frame:
            from lib.production_execution import _artifact_path
            _validate_frame_native(packet, native, submitted=[{'role': a['role'], 'source_sha256': a['sha256'], 'source_path': a['path'], **({'timestamp_s': a['timestamp_s']} if 'timestamp_s' in a else {})} for a in submitted])
            root = Path(inputs['project_dir']).resolve()
            manifest_path = _artifact_path(root, 'asset_manifest.json')
            _check_frame_hard_inputs(packet, native, _read(root, 'scene_plan.json'),
                _read(root, 'asset_manifest.json') if manifest_path.exists() else None, root)
    else:
        validate_openart_asset_roles(packet, native)
        scene = _read(Path(inputs['project_dir']).resolve(), 'scene_plan.json')
        from lib.production_execution import _artifact_path
        root = Path(inputs['project_dir']).resolve()
        manifest = _read(root, 'asset_manifest.json') if _artifact_path(root, 'asset_manifest.json').exists() else None
        _check_required_native_controls(scene, manifest, packet['binding']['shot_id'], native=native, project_dir=root)
        if packet['binding'].get('frame_input'):
            _check_frame_hard_inputs(packet, native, scene, manifest, root)
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


def route_treatment_error(projection, *, provider, model=None, mode=None, operation=None, native_capabilities=None):
    """Pure input-treatment feasibility from authored roles, before media exists.

    This checks explicit guided/frame native-mode and role support. Reviewed
    bytes, uploads, pins and dispatch readiness remain their later owners' gates.
    """
    shot = projection['shot']
    treatment = shot.get('reference_mode', projection['contract_snapshot'].get('reference_mode'))
    if treatment not in {'reference_guided', 'frame_conditioned'}:
        return None
    prefix = f"shot {shot['id']}, provider {provider}, model {model or 'unreported'}, mode {mode or 'unreported'}: "
    if treatment == 'frame_conditioned':
        if provider == 'grok_cli':
            return None
        if provider != 'openart_mcp' or mode != 'image2video':
            return prefix + 'frame-conditioned treatment requires Grok CLI or OpenArt MCP image2video'
        if (native_capabilities or {}).get('roles', {}).get('first_frame', {}).get('supported') is not True:
            return prefix + 'frame-conditioned treatment requires supported native first_frame'
        return None
    if provider == 'grok_cli':
        if operation != 'reference_to_video':
            return prefix + 'reference-guided Grok treatment requires reference_to_video'
        return None
    if provider != 'openart_mcp' or mode not in REFERENCE_GUIDED_MODES:
        return prefix + 'reference-guided contract requires OpenArt MCP element2video or generate-shot-video'
    roles = (native_capabilities or {}).get('roles', {})
    for asset in projection['assets']:
        if not (asset['id'] in shot['asset_ids'] or asset['role'] == 'identity_reference'
                and set(asset.get('cast_ids', [])).intersection(shot.get('cast_ids', []))):
            continue
        # Start/end boards are composition targets unless a separate pin is
        # declared; guided requests need not submit those optional targets.
        if asset['role'] in {'start_frame', 'end_frame'}:
            continue
        options = _GUIDED_ROLE_OPTIONS.get(asset['role'])
        if options and not any(roles.get(role, {}).get('supported') is True for role in options):
            return prefix + f"authored asset {asset['id']} ({asset['role']}) needs supported native role " + ' or '.join(sorted(options))
    return None


def _validate_frame_native(packet, native, *, submitted=None):
    """One starting image carries the exact reviewed composition, without aliases."""
    if native.get('provider') != 'grok_cli' and (native.get('provider') != 'openart_mcp' or native.get('mode') != 'image2video'):
        raise ValueError('frame-conditioned treatment requires Grok CLI or OpenArt MCP image2video')
    refs = packet['binding']['references']
    frame = packet['binding']['frame_input']
    submitted = native.get('input_assets', []) if submitted is None else submitted
    starts = [a for a in submitted if a.get('role') == 'first_frame']
    if (len(starts) != 1 or starts[0].get('source_sha256') != frame['sha256']
            or Path(starts[0].get('source_path', '')).resolve() != Path(frame['path']).resolve()):
        raise ValueError('frame-conditioned request needs exactly one approved starting image')
    mapping = {'end_frame': 'last_frame', 'reference_image': 'reference_image',
               'reference_video': 'reference_video', 'reference_audio': 'reference_audio', 'timed_keyframe': 'timed_keyframe'}
    submitted = [{**a, 'role': {'reference': 'reference_image', 'keyframe': 'timed_keyframe'}.get(a.get('role'), a.get('role')),
                  **({'time_seconds': a['timestamp_s']} if 'timestamp_s' in a else {})} for a in submitted]
    approved = {('first_frame', frame['sha256'], None)} | {(mapping[r['role']], r['sha256'], r.get('time_seconds') if r['role'] == 'timed_keyframe' else None) for r in refs if r['role'] in mapping}
    if any((a.get('role'), a.get('source_sha256'), a.get('time_seconds') if a.get('role') == 'timed_keyframe' else None) not in approved for a in submitted):
        raise ValueError('frame-conditioned native role/source is not an approved consumer asset')
    for ref in refs:
        if (ref['role'] in {'reference_image', 'reference_video', 'reference_audio', 'timed_keyframe'} or ref.get('native_required')) and not any(
                a.get('role') == mapping[ref['role']] and a.get('source_sha256') == ref['sha256']
                and (ref['role'] != 'timed_keyframe' or a.get('time_seconds') == ref.get('time_seconds')) for a in submitted):
            raise ValueError('frame-conditioned request omits separately required native reference ' + ref['role'])


def _validate_grok_guided_assets(packet, native):
    """Guidance images are exact shot-local consumers, never implicit frame pins."""
    if native['request'].get('operation') != 'reference_to_video':
        raise ValueError('reference-guided Grok treatment requires reference_to_video')
    refs = packet['binding']['references']
    approved = {ref['sha256'] for ref in refs if ref['role'] == 'reference_image'}
    submitted = native['request']['input_assets']
    actual = {asset['sha256'] for asset in submitted}
    if not approved or any(asset['role'] != 'reference' for asset in submitted) or actual != approved:
        raise ValueError('Grok guided references must match all approved shot reference images exactly')
    if (any(ref.get('native_required') is True and ref['role'] in {'start_frame', 'end_frame'} for ref in refs)
            or any(ref.get('native_required') is not False and ref['role'] not in {
                'reference_image', 'identity_reference', 'start_frame', 'end_frame'} for ref in refs)):
        raise ValueError('Grok guided request omits a separately required native reference role')


def _check_frame_hard_inputs(packet, native, scene, manifest, root):
    """Keep separately authored native obligations independent of treatment."""
    submitted = native.get('input_assets', [])
    if native.get('provider') == 'grok_cli':
        submitted = [{'role': {'reference': 'reference_image', 'keyframe': 'timed_keyframe'}.get(a['role'], a['role']),
                      'source_sha256': a['sha256']} for a in native['request']['input_assets']]
    view = {**native, 'input_assets': submitted}
    _check_required_native_controls(scene, manifest, packet['binding']['shot_id'], native=view, project_dir=root)
    obligations = _manifest_requirement_binding(manifest, packet['binding']['shot_id'])
    for key, requirement in obligations['reference_assets'].items():
        role = requirement.get('temporal_use') or requirement.get('native_role')
        if role not in {'first_frame', 'last_frame', 'reference_image', 'reference_video', 'reference_audio', 'character_reference', 'environment_reference'}:
            raise ValueError('frame-conditioned treatment cannot omit a separately declared native reference obligation')
        aid = requirement.get('asset_id', key)
        assets = [a for a in (manifest or {}).get('assets', []) if a['id'] == aid]
        if len(assets) != 1 or not assets[0].get('path') or not any(a.get('role') == role and
                a.get('source_sha256') == file_sha256(root / assets[0]['path']) for a in submitted):
            raise ValueError('frame-conditioned request omits separately declared native reference obligation')


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
    if packet['binding'].get('reference_mode') == 'frame_conditioned':
        _validate_frame_native(packet, native, submitted=submitted)
        return
    if packet['binding'].get('reference_mode') == 'reference_guided':
        if native.get('provider') != 'openart_mcp' or native.get('mode') not in REFERENCE_GUIDED_MODES:
            raise ValueError('reference-guided contract requires an explicit native reference-guided mode')
        role_options = _GUIDED_ROLE_OPTIONS
        approved = {(role, ref['sha256']) for ref in refs for role in role_options.get(ref['role'], set())}
        for asset in submitted:
            if (asset.get('role'), asset.get('source_sha256')) not in approved:
                raise ValueError('OpenArt guided native role/source is not an approved shot asset')
        for ref in refs:
            if ref.get('native_required') is False:
                continue
            if ref['role'] == 'identity_reference' and ref['id'] not in packet['shot']['asset_ids'] and packet['binding'].get('reference_mode') == 'reference_guided':
                continue
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


def validate_preparation_evidence(compiled, review, inputs, native, profile, packet, *, historical=False, factory=None):
    """Validate supplied preparation evidence; callers own loading and authority.

    Current and retained historical callers share exact compiled requests,
    source/native bindings and live-evidence requirements. Activated standing factories
    retain semantic unknowns/failures as report-only observations. Historical
    readers use frozen standing authority; generic manifests remain strict.
    """
    _validate_compiled(compiled, inputs, native, profile, packet, historical=historical)
    _schema('preparation_review', review)
    if review['review_id'] != inputs['preparation_review_id']:
        raise ValueError('named review ID differs from selected preparation ID')
    from lib.shot_contract import factory_project, frozen_factory_authority
    if factory is None and historical:
        from lib.production_execution import _read
        path = Path(inputs['project_dir']) / 'production_scopes.json'
        scopes = _read(path).get('scopes', []) if path.exists() else []
        matches = [scope for scope in scopes if scope.get('id') == inputs.get('governance', {}).get('scope_id')]
        report_only = len(matches) == 1 and frozen_factory_authority(inputs['project_dir'], matches[0])
    else:
        report_only = factory_project(inputs['project_dir']) if factory is None else factory
    if review['subject_sha256'] != digest(compiled) or (not report_only and review['status'] != 'pass'):
        raise ValueError('stale or nonpassing preparation review')
    names = [p['name'] for p in review['predicates']]
    if len(names) != len(PREDICATES) or set(names) != PREDICATES or (not report_only and any(p['status'] != 'pass' for p in review['predicates'])):
        raise ValueError('all named critical preparation predicates must pass exactly once')
    if report_only and review['status'] == 'pass' and any(p['status'] != 'pass' for p in review['predicates']):
        raise ValueError('passing preparation verdict contradicts its predicates')
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
        if record['approved_by'].startswith('standing_media_authority:'):
            from lib import production_autonomy as autonomy
            from lib.production_execution import load_shot_contract
            policy, _, decision_id = autonomy.require_active_policy(root, shot_id=record['shot_id'])
            spec = autonomy._provider_spec(policy, 'openart_cli')
            account = {'account_id_sha256': spec['account_id_sha256'], 'workspace': spec['workspace']}
            _, expected_record, _ = autonomy._standing_upload_material(root, shot_id=record['shot_id'], provider='openart_cli',
                files=[next(a['path'] for a in load_shot_contract(root)['assets'] if a['id'] == asset['id'])], account=account)
            if record != expected_record:
                return None
            return {'state': 'approved', 'upload_id': upload_id, 'source_sha256': source_sha256,
                'approval_sha256': digest(record), 'standing_account_id_sha256': spec['account_id_sha256'],
                'standing_models': spec.get('models', [r['model'] for r in spec.get('routes', [])])}
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


def _validate_frozen_preparation(request, frozen, project_dir, *, current_required, original_reviews=False,
                                 historical=False):
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
        current = reconstruct(project_dir, request['shot_id'], native=frozen['native'],
            **({'reference_bindings': packet['binding']['references']} if original_reviews else {}))
        stable = set(packet['binding']) - {'contract_sha256', 'reviews_sha256'}
        if any(current['binding'][key] != packet['binding'][key] for key in stable):
            raise ValueError('stale source/reference/review/upstream bindings')
    from lib.shot_contract import frozen_factory_authority
    validate_preparation_evidence(data['compiled'], data['review'], inputs,
        frozen['native'], frozen['profile'], packet, historical=historical,
        factory=frozen_factory_authority(project_dir, request.get('scope')))
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
        current_required=True, original_reviews=True, historical=True)


def validate_frozen_preparation_history(request, frozen, project_dir):
    """Validate an immutable historical fact, without claiming current eligibility.

    All named evidence, copied asset bytes, compiled content, native/profile proof,
    timing, coverage and preparation predicates remain required.
    """
    return _validate_frozen_preparation(request, frozen, project_dir, current_required=False, historical=True)


# --- Once-authored package preparation (moved from Studio production_session) ---
PACKAGE_PREFLIGHT = ('preparation only; pending boards and current semantic reviews remain '
    'required before the exact ready-video dry-run')


def _encode_record(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _artifact_pointer(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('canonical pointer escapes project')
    return {'path': str(path.relative_to(root)),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}


COMPACT_AUTHORING_FORM = 'shot_local_v1_compact_v1'


def _expand_compact_package(package):
    """Expand administrative repetition only; retain the bound source unchanged."""
    from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES
    top = {'version', 'authoring_form', 'planning_mode', 'project_id', 'story_revision',
           'title', 'shots', 'compact', 'approval_preview', 'prompt_compiler', 'cast_names'}
    if (not top - {'approval_preview', 'prompt_compiler', 'cast_names'} <= set(package) or set(package) - top or 'boards' in package
            or package.get('authoring_form') != COMPACT_AUTHORING_FORM
            or package.get('planning_mode') != 'shot_local_v1'):
        raise ValueError('compact package requires exact opt-in form without full boards')
    header = package.get('compact')
    fields = {'reviewer', 'story', 'approval', 'references', 'story_meaning',
              'late_cast_ids', 'payoff_speaker_ids', 'payoff_asset_id'}
    if (not isinstance(header, dict) or not fields - {'payoff_asset_id'} <= set(header)
            or set(header) - fields):
        raise ValueError('compact package requires explicit bindings and story meaning')
    if not isinstance(header['reviewer'], str) or not header['reviewer'].strip():
        raise ValueError('compact package requires a named reviewer')
    def unknown(subject, predicates):
        return {'review_id': subject + '-unobserved', 'reviewer': header['reviewer'],
            'story_revision': package['story_revision'], 'subject_sha256': '0' * 64,
            'status': 'unknown', 'predicates': [{'name': name, 'severity': 'critical',
                'status': 'unknown', 'evidence': 'UNOBSERVED: review current bytes.'}
                for name in sorted(predicates)]}
    authored = package.get('shots')
    if not isinstance(authored, list) or not authored:
        raise ValueError('compact package needs ordered authored shots')
    nodes, mappings, assets, contracts, shots = [], [], [], [], []
    required_shot = {'id', 'script', 'action', 'duration_seconds', 'end_state', 'cast',
                     'preserve', 'motion', 'boards', 'video_dependencies'}
    required_motion = {'reference_mode', 'frame_input', 'initial_state', 'endpoint_completion',
        'required_visible_speakers', 'dialogue', 'allowed_transformations', 'transition'}
    board_fields = {'slot_id', 'role', 'prompt', 'reference_ids', 'image_dependencies',
                    'required_predicates', 'transparent_background', 'cast_ids'}
    for shot in authored:
        if (not isinstance(shot, dict) or not required_shot <= set(shot)
                or set(shot) - required_shot - {'pinned_initial_frame', 'pinned_final_frame', 'camera', 'sound', 'roles'}):
            raise ValueError('compact shot needs explicit content, motion, boards and dependencies')
        sid = shot['id']
        if (not isinstance(sid, str) or not sid.strip() or Path(sid).name != sid
                or sid in ('.', '..') or '/' in sid or '\\' in sid):
            raise ValueError('unsafe compact shot ID')
        motion = shot['motion']
        if (not isinstance(motion, dict) or set(motion) - required_motion
                or not required_motion - {'frame_input'} <= set(motion)
                or motion['reference_mode'] not in {'frame_conditioned', 'reference_guided'}
                or motion['reference_mode'] == 'reference_guided' and 'frame_input' in motion):
            raise ValueError('compact motion needs explicit frame_conditioned treatment and all semantics')
        if not isinstance(shot['boards'], list) or (not shot['boards'] and not shot.get('roles')):
            raise ValueError('compact shot needs authored boards')
        motion = copy.deepcopy(motion)
        if motion['reference_mode'] == 'frame_conditioned' and 'frame_input' not in motion:
            declared = shot.get('roles', [{'asset_id': b.get('slot_id'), 'role': b.get('role')}
                                          for b in shot['boards'] if isinstance(b, dict)])
            _validate_consumer_roles(declared)
            starts = [role for role in declared if role['role'] == 'start_frame']
            if len(starts) != 1:
                raise ValueError(sid + '/motion/frame_input needs one explicit start_frame role')
            motion['frame_input'] = copy.deepcopy(starts[0])
        deps = shot['video_dependencies']
        if (not isinstance(deps, list) or any(not isinstance(dep, str) or not dep.strip() for dep in deps)
                or len(set(deps)) != len(deps)):
            raise ValueError('compact video dependencies need unique explicit shot IDs')
        roles = []
        for board in shot['boards']:
            if (isinstance(board, dict) and isinstance(board.get('upstream_source'), dict)
                    and 'prompt' not in board):
                board = copy.deepcopy(board)
                board['prompt'] = 'Reuse ' + str(board['upstream_source'].get('shot_id')) + ' outgoing frame as start_frame.'
            if (not isinstance(board, dict) or not board_fields <= set(board)
                    or set(board) - board_fields - {'upstream_source', 'time_seconds'}):
                raise ValueError('compact board requires exact authored prompt, role and source declarations')
            if 'upstream_source' in board:
                source = board['upstream_source']
                if (not isinstance(source, dict) or set(source) != {'shot_id', 'role'}
                        or source['role'] != 'outgoing_frame' or source['shot_id'] not in deps
                        or motion['frame_input'] != {'asset_id': board['slot_id'], 'role': 'start_frame'}
                        or board['role'] != 'start_frame' or board['reference_ids'] or board['image_dependencies']
                        or board['transparent_background'] is not False
                        or sum(isinstance(b, dict) and 'upstream_source' in b for b in shot['boards']) != 1):
                    raise ValueError('compact upstream_source needs one start_frame board reusing a declared '
                                     'video dependency outgoing_frame with no image sources')
            for key in ('reference_ids', 'image_dependencies', 'required_predicates', 'cast_ids'):
                values = board[key]
                if (not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values)
                        or len(set(values)) != len(values)):
                    raise ValueError('compact board ' + key + ' needs unique explicit IDs/names')
            slot = board['slot_id']
            if (not isinstance(slot, str) or not slot.strip() or Path(slot).name != slot
                    or slot in ('.', '..') or '/' in slot or '\\' in slot):
                raise ValueError('unsafe compact board slot ID')
            nodes.append({k: copy.deepcopy(v) for k, v in board.items() if k not in {'cast_ids', 'time_seconds'}})
            nodes[-1].update(shot_id=sid, output_path='assets/images/candidates/' + slot + '.png')
            roles.append({'asset_id': slot, 'role': ('start_frame'
                if motion.get('frame_input') == {'asset_id': slot, 'role': 'start_frame'} else board['role']),
                **({'time_seconds': board['time_seconds']} if 'time_seconds' in board else {})})
            assets.append({'id': slot, 'role': board['role'], 'cast_ids': copy.deepcopy(board['cast_ids']),
                **({'time_seconds': board['time_seconds']} if 'time_seconds' in board else {})})
            if 'upstream_source' in board:
                assets[-1]['upstream_source'] = copy.deepcopy(board['upstream_source'])
        if 'roles' in shot:
            roles = copy.deepcopy(shot['roles'])
        _validate_consumer_roles(roles)
        mappings.append({'shot_id': sid, 'roles': roles,
                         'video_dependencies': copy.deepcopy(shot['video_dependencies'])})
        contracts.append({'id': sid, **copy.deepcopy(motion),
            'asset_ids': list(dict.fromkeys(r['asset_id'] for r in roles)),
            'upstream': [{'shot_id': dep} for dep in shot['video_dependencies']],
            'review': unknown(sid, SHOT_PREDICATES)})
        clean = {k: copy.deepcopy(v) for k, v in shot.items() if k not in {'boards', 'video_dependencies', 'roles'}}
        clean['motion'] = {k: v for k, v in clean['motion'].items() if k not in {'reference_mode', 'frame_input'}}
        shots.append(clean)
    for asset in assets:
        if asset['role'] == 'timed_keyframe' and 'time_seconds' not in asset:
            consumers = [r for mapping in mappings for r in mapping['roles']
                         if r['asset_id'] == asset['id'] and r['role'] == 'timed_keyframe']
            if not consumers:
                raise ValueError('timed board needs exact consumer time_seconds')
            asset['time_seconds'] = consumers[0]['time_seconds']
    shared_roles = []
    if not isinstance(header['references'], list):
        raise ValueError('compact references must be an explicit list')
    for reference in header['references']:
        if not isinstance(reference, dict):
            raise ValueError('compact reference must be an exact source object')
        role = reference.get('role')
        if role not in {'identity_reference', 'payoff_board'}:
            if 'cast_ids' in reference:
                raise ValueError('compact reference cast_ids require explicit identity_reference or payoff_board role')
            continue
        cast = reference.get('cast_ids')
        if (not isinstance(cast, list) or (role == 'identity_reference' and not cast)
                or any(not isinstance(c, str) or not c.strip() for c in cast) or len(set(cast)) != len(cast)):
            raise ValueError('compact ' + role + ' reference requires unique explicit cast_ids')
        # The supplied board keeps its semantic role; native consumption stays
        # in the authored shot roles and retains ordinary source/review guards.
        assets.append({'id': reference['id'], 'role': role, 'cast_ids': copy.deepcopy(cast)})
        if role == 'identity_reference':
            shared_roles.append({'asset_id': reference['id'], 'role': role})
    identity_cast = {c for a in assets if a['role'] == 'identity_reference' for c in a['cast_ids']}
    for shot in authored:
        if isinstance(shot['cast'], list) and any(c not in identity_cast for c in shot['cast']):
            raise ValueError('compact cast requires explicit identity_reference source metadata')
    if 'payoff_asset_id' in header:
        payoff = next((a for a in assets if a['id'] == header['payoff_asset_id']), None)
        if payoff is None or payoff['role'] != 'payoff_board':
            raise ValueError('compact payoff asset must name an explicitly authored or supplied payoff_board')
    template = {'version': '1.0', 'planning_mode': 'shot_local_v1',
        'reference_mode': 'frame_conditioned', 'story': copy.deepcopy(header['story_meaning']),
        **{k: copy.deepcopy(header[k]) for k in ('late_cast_ids', 'payoff_speaker_ids', 'payoff_asset_id') if k in header},
        'assets': assets, 'shots': contracts, 'project_review': unknown('project', PROJECT_PREDICATES)}
    result = {k: copy.deepcopy(v) for k, v in package.items() if k not in {'authoring_form', 'compact'}}
    result['shots'] = shots
    result['boards'] = {'version': '1.1', **{k: copy.deepcopy(header[k])
        for k in ('reviewer', 'story', 'approval')},
        'references': [{k: copy.deepcopy(v) for k, v in reference.items() if k != 'cast_ids'}
            for reference in header['references']],
        'image_nodes': nodes, 'shared_roles': shared_roles, 'shots': mappings,
        'motion_binding': {'contract_template': template, 'motion_handoffs': {}}}
    return result


def _apply_prompt_compiler(package, template):
    """Explicit prospective opt-in; packages without it keep legacy prompt bytes."""
    if 'prompt_compiler' not in package:
        if 'cast_names' in package or 'prompt_compiler' in template or 'cast_names' in template:
            raise ValueError('cast_names require explicit package prompt_compiler shootable_v1')
        return
    if package['prompt_compiler'] != SHOOTABLE_COMPILER:
        raise ValueError('unsupported prompt compiler')
    names = package.get('cast_names')
    cast = set()
    for shot in package['shots']:
        motion = shot.get('motion', {}) if isinstance(shot.get('motion', {}), dict) else {}
        cast |= set(shot.get('cast', [])) | set(motion.get('required_visible_speakers', []))
        cast |= {line.get('speaker_id') for line in motion.get('dialogue', []) if isinstance(line, dict)}
    if (not isinstance(names, dict)
            or any(not isinstance(v, str) or not v.strip() for v in names.values())
            or not cast <= set(names)):
        raise ValueError('shootable prompts need a readable authored cast_names entry for every cast member and speaker')
    for key, value in (('prompt_compiler', SHOOTABLE_COMPILER), ('cast_names', names)):
        if key in template and template[key] != value:
            raise ValueError('duplicated prompt compiler fact contradicts package ' + key)
        template[key] = copy.deepcopy(value)


def _compile_package_targets(root, package_binding):
    """Validate retained sources and the shared structural projection without writes."""
    from lib import production_images as images
    package_path = images._bound_file(root, package_binding, 'production package')
    result = _compile_package_value(root, json.loads(package_path.read_text()), package_binding)
    spec = result[0]
    if spec['image_nodes']:
        images.build_preboard_packet(root, story=spec['story'], approval=spec['approval'],
            references=spec['references'], board_slots=[{'id': node['slot_id'],
                'shot_id': node['shot_id'], 'role': node['role'], 'status': 'unresolved'}
                for node in spec['image_nodes']])
    else:
        for label in ('story', 'approval'):
            images._bound_file(root, spec[label], label)
    return result


def _compile_package_value(root, package, package_binding):
    """Shared structural core, including current reference bytes and output conflicts.

    Prospective story/approval wrappers are checked from retained bytes by the
    publishing path; this projection alone grants no generation authority.
    """
    from lib import production_boards as boards, production_images as images
    from lib.episode_production_controls import episode_production_status
    package_path = boards._inside(root, package_binding['path'])
    if 'authoring_form' in package or 'compact' in package:
        package = _expand_compact_package(package)
    marker = json.loads((root / 'project.json').read_text())
    if (set(package) - {'version', 'project_id', 'story_revision', 'title', 'shots', 'boards', 'approval_preview', 'planning_mode',
                        'prompt_compiler', 'cast_names'}
            or not {'version', 'project_id', 'story_revision', 'title', 'shots', 'boards'} <= set(package)
            or package['version'] != '1.0'
            or any(package[k] != marker[k] for k in ('project_id', 'story_revision'))
            or not isinstance(package['title'], str) or not package['title'].strip()):
        raise ValueError('package requires exact current project/story binding and title')
    spec = copy.deepcopy(package['boards'])
    if spec.get('version') != '1.1' or not spec.get('motion_binding'):
        raise ValueError('new production package requires boards version 1.1 and an explicit motion_binding')
    if 'planning_mode' in package:
        if package['planning_mode'] != 'shot_local_v1':
            raise ValueError('unsupported prospective planning mode')
        template = spec['motion_binding']['contract_template']
        template['planning_mode'] = 'shot_local_v1'
        _apply_prompt_compiler(package, template)
        authored = package['shots']
        originals = {shot['id']: shot for shot in template.get('shots', [])}
        derived = []
        for shot in authored:
            sid = shot['id']
            mapping = next((item for item in spec['shots'] if item['shot_id'] == sid), None)
            if mapping is None:
                raise ValueError('authored shot has no exact board mapping')
            motion = shot.get('motion', {})
            allowed = {'initial_state', 'endpoint_completion', 'required_visible_speakers',
                       'dialogue', 'allowed_transformations', 'transition'}
            if not isinstance(motion, dict) or set(motion) - allowed:
                raise ValueError('motion contains unsupported authored semantic fields')
            values = {'id': sid, 'dominant_action': shot['action'],
                'duration_seconds': shot['duration_seconds'],
                **({'completed_end_state': shot['end_state']} if 'end_state' in shot else {}),
                **({'cast_ids': shot['cast']} if 'cast' in shot else {}),
                **({'prop_body_invariants': shot['preserve']} if 'preserve' in shot else {}),
                **{k: shot[k] for k in ('camera', 'sound') if k in shot},
                **motion}
            original = copy.deepcopy(originals.get(sid, {}))
            for key, value in values.items():
                if key in original and original[key] != value:
                    raise ValueError('duplicated motion fact contradicts authored shot ' + sid + '/' + key)
            original.update(values)
            original.setdefault('asset_ids', [r['asset_id'] for r in mapping['roles']])
            original.setdefault('upstream', [{'shot_id': dep} for dep in mapping.get('video_dependencies', [])])
            derived.append(original)
        template['shots'] = derived
    nodes = boards._validate_spec(root, spec)
    source_paths = {boards._inside(root, b['path']) for b in
        [package_binding, spec['story'], spec['approval'], *spec['references']]}
    reserved_paths = {boards._inside(root, directory + name + '.json')
        for directory in ('', 'artifacts/') for name in ('script', 'scene_plan', 'asset_manifest')}
    existing_pack = boards.load_board_pack(root) if (root / boards.ARTIFACT).exists() else None
    same_pack = existing_pack and existing_pack['specification_sha256'] == boards._digest(spec)
    scopes_path = root / 'production_scopes.json'
    scopes = json.loads(scopes_path.read_text()).get('scopes', []) if scopes_path.exists() else []
    if not same_pack and any(scope.get('phase') != 'image' for scope in scopes):
        raise ValueError('new package cannot rewrite motion planning frozen by retained scopes')
    outputs = [boards._inside(root, n['output_path']) for n in nodes.values()]
    for output in outputs:
        if output.relative_to(root).parts[0] not in ('assets', 'renders'):
            raise ValueError('board output collides with administrative or artifact paths')
        if output.exists() and not same_pack:
            raise ValueError('board output path already exists before package preparation')
    if len(set(outputs)) != len(outputs) or set(outputs) & (source_paths | reserved_paths):
        raise ValueError('board output paths duplicate or overwrite retained package inputs/artifacts')
    shots = package['shots']
    if not isinstance(shots, list) or not shots:
        raise ValueError('package needs ordered authored shots')
    ids = []
    sections, scenes, cards = [], [], {}
    clock = 0
    for shot in shots:
        if set(shot) - {'id', 'script', 'action', 'duration_seconds', 'end_state', 'preserve',
                'pinned_initial_frame', 'pinned_final_frame', 'cast', 'motion', 'camera', 'sound'}:
            raise ValueError('unsupported authored shot field')
        if ({'camera', 'sound'} & set(shot)) and package.get('prompt_compiler') != SHOOTABLE_COMPILER:
            raise ValueError('authored camera/sound directions require prompt_compiler shootable_v1')
        if 'motion' in shot and package.get('planning_mode') != 'shot_local_v1':
            raise ValueError('authored motion fields require prospective planning mode')
        sid = shot['id']
        if (not isinstance(sid, str) or not sid.strip() or Path(sid).name != sid
                or sid in ('.', '..') or sid in ids):
            raise ValueError('duplicate or unsafe authored shot ID')
        if any(not isinstance(shot[k], str) for k in ('script', 'action')) or not shot['action'].strip():
            raise ValueError('shot script/action must be exact authored text')
        duration = shot['duration_seconds']
        if (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
            raise ValueError('shot duration must be finite and positive')
        card = {k: shot[k] for k in ('end_state', 'preserve') if k in shot}
        if ('end_state' in card and (not isinstance(card['end_state'], str) or not card['end_state'].strip())
                or 'preserve' in card and (not isinstance(card['preserve'], list)
                    or any(not isinstance(v, str) or not v.strip() for v in card['preserve']))):
            raise ValueError('end_state/preserve need explicit authored text')
        for key in ('pinned_initial_frame', 'pinned_final_frame'):
            if key not in shot:
                continue
            pin = shot[key]
            if (not isinstance(pin, dict) or set(pin) != {'required', 'requirement_id', 'end_state'}
                    or type(pin['required']) is not bool
                    or any(not isinstance(pin[k], str) or not pin[k].strip() for k in ('requirement_id', 'end_state'))):
                raise ValueError('malformed authored frame-pin requirement')
            card[key] = pin
        if 'cast' in shot:
            if (not isinstance(shot['cast'], list) or any(not isinstance(v, str) or not v.strip() for v in shot['cast'])
                    or len(set(shot['cast'])) != len(shot['cast'])):
                raise ValueError('cast requires unique authored character IDs')
            card['cast'] = shot['cast']
        cards[sid] = card
        ids.append(sid)
        end = clock + duration
        if not math.isfinite(end):
            raise ValueError('cumulative duration must be finite')
        sections.append({'id': sid, 'text': shot['script'], 'start_seconds': clock, 'end_seconds': end})
        scenes.append({'id': sid, 'type': 'generated', 'description': shot['action'],
            'script_section_id': sid, 'start_seconds': clock, 'end_seconds': end})
        clock = end
    if ids != [s['shot_id'] for s in spec['shots']]:
        raise ValueError('authored shots and ordered board shot mappings differ')
    if not spec['approval'].get('approved_by'):
        raise ValueError('story needs explicit approval provenance')
    assets, bindings, reference_aliases = [], {}, {}
    for ref in spec['references']:
        aid = ref['id']
        roles = [ref['role']] + [r['role'] for r in spec.get('shared_roles', []) if r['asset_id'] == aid]
        shared = any(r['asset_id'] == aid for r in spec.get('shared_roles', []))
        users = [s['shot_id'] for s in spec['shots'] if shared or any(r['asset_id'] == aid for r in s['roles'])]
        roles += [r['role'] for s in spec['shots'] for r in s['roles'] if r['asset_id'] == aid]
        # Every provided reference needs its current declared role review, even
        # when used only as an image-generation source rather than a shot role.
        for role in dict.fromkeys(roles or [ref['role']]):
            images.validate_image_reuse(root, ref, role=role)
        path = boards._inside(root, ref['path'])
        for index, scene_id in enumerate(users or [ids[0]]):
            asset_id = boards.motion_reference_alias(spec, aid, scene_id)
            if asset_id in bindings or (index and asset_id in ({r['id'] for r in spec['references']} | set(nodes))):
                raise ValueError('provided reference scene alias collides with asset ID')
            assets.append({'id': asset_id, 'type': 'image', 'path': str(path.relative_to(root)),
                'source_tool': 'provided', 'scene_id': scene_id})
            bindings[asset_id] = {k: ref[k] for k in ('path', 'sha256', 'review', 'applicability')}
            reference_aliases[asset_id] = aid
    metadata = {'project_id': marker['project_id'], 'story_revision': marker['story_revision'],
        'package': {'path': str(package_path.relative_to(root)), 'sha256': package_binding['sha256']},
        'story': spec['story'], 'approval': spec['approval']}
    records = {
        'script': {'version': '1.0', 'title': package['title'], 'total_duration_seconds': clock,
            'sections': sections, 'metadata': metadata},
        'scene_plan': {'version': '1.0', 'scenes': scenes,
            'metadata': {**metadata, 'visual_development': {'shot_cards': cards}}},
        'asset_manifest': {'version': '1.0', 'assets': assets,
            'metadata': {**metadata, 'source_bindings': bindings}}}
    boards._motion_template_for_plan(spec, records['scene_plan'])
    try:
        for name, record in records.items():
            Draft202012Validator(load_schema(name)).validate(record)
    except ValidationError as exc:
        raise ValueError('package artifact schema: ' + exc.message) from exc
    targets = {boards._inside(root, 'artifacts/' + name + '.json'): _encode_record(record)
               for name, record in records.items()}

    def compatible(path, payload):
        if path.read_bytes() == payload:
            return True
        if path.name != 'asset_manifest.json' or not same_pack:
            return False
        actual, initial = json.loads(path.read_text()), json.loads(payload)
        try:
            Draft202012Validator(load_schema('asset_manifest')).validate(actual)
        except ValidationError:
            return False
        actual_assets = {a['id']: a for a in actual['assets']}
        if len(actual_assets) != len(actual['assets']):
            return False
        if any(actual_assets.get(a['id']) != a for a in initial['assets']):
            return False
        current = actual.get('metadata', {})
        source_bindings = current.get('source_bindings', {})
        if not isinstance(source_bindings, dict):
            return False

        def preserves_binding(key, seed):
            binding = source_bindings.get(key)
            if not isinstance(binding, dict):
                return False
            for field, value in seed.items():
                if binding.get(field) == value:
                    continue
                # A newer genuine role observation can supersede the seeded
                # reuse review while preserving the same retained source bytes.
                if (field != 'review' or not any(o['asset_id'] == reference_aliases[key]
                        and o['review'] == binding.get('review')
                        and o['review']['subject_sha256'] == seed['sha256']
                        for o in existing_pack['observations'])):
                    return False
            return True
        return (actual.get('version') == initial['version']
            and all(current.get(k) == v for k, v in initial['metadata'].items() if k != 'source_bindings')
            and all(preserves_binding(k, v) for k, v in initial['metadata']['source_bindings'].items()))

    # Check every conflict before any directory, lock or artifact write.
    for path, payload in targets.items():
        legacy = boards._inside(root, path.name)
        if legacy.exists() and not compatible(legacy, payload):
            raise ValueError('legacy canonical package artifact conflict: ' + path.name)
        if path.exists() and not compatible(path, payload):
            raise ValueError('canonical package artifact conflict: ' + path.name)
    if (root / boards.ARTIFACT).exists():
        if boards.load_board_pack(root)['specification_sha256'] != boards._digest(spec):
            raise ValueError('current board pack already binds another specification')
    if any(not path.exists() for path in targets):
        if not same_pack and (images.image_usage(root)['total'] or episode_production_status(root)['usage']['total']):
            raise ValueError('cannot materialize missing package artifacts after dispatch')
    return spec, records, targets


def preflight_package(project_dir, package, *, package_binding):
    """Project prospective authored wrappers through the normal structural core.

    Reads actual references and existing conflicts but writes no wrappers, locks,
    attempts or authority. Publication rechecks the bound sources normally.
    """
    root = Path(project_dir).expanduser().resolve()
    spec, records, _ = _compile_package_value(root, package, package_binding)
    return {'spec': spec, 'records': records}


def compile_package(project_dir, package_binding):
    """Validate a bound once-authored package and return its projections; writes nothing.

    Input binding is {path, sha256}. The package JSON has version "1.0", the
    current project_id/story_revision, a title, ordered shots [{id, script,
    action, duration_seconds, optional end_state, preserve, pinned frames, cast}]
    and boards (an exact board specification version 1.1 with motion_binding).
    Explicit authoring_form shot_local_v1_compact_v1 instead expands compact
    declarations into that same specification; its original binding is retained.
    Script, scene plan and asset manifest are all derived from this one package.
    Existing different canonical artifacts fail closed.
    """
    root = Path(project_dir).expanduser().resolve()
    spec, records, _ = _compile_package_targets(root, package_binding)
    return {'spec': spec, 'records': records}


def prepare_package(project_dir, package_binding):
    """Materialize the compiled records and board pack; zero provider calls or reservations.

    Replay of the same package is byte-identical. An interrupted run may leave
    only an identical subset, which the next invocation fills in.
    """
    import os
    import tempfile
    from lib import production_boards as boards, production_images as images
    root = Path(project_dir).expanduser().resolve()
    _compile_package_targets(root, package_binding)  # Invalid input leaves the project unchanged.

    @boards._board_transaction
    def materialize(root):
        spec, _, targets = _compile_package_targets(root, package_binding)
        for path, payload in targets.items():
            if path.exists():
                continue
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.link(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        return spec

    spec = materialize(root)
    pack = boards.prepare_board_pack(root, spec, images=images)
    # The existing board pack indexes the immutable authored source, so missing
    # derived views can be rebuilt without reconstructing authority or receipts.
    retained_binding = {'path': str(images._bound_file(root, package_binding, 'production package').relative_to(root)),
                        'sha256': package_binding['sha256']}
    @boards._board_transaction
    def retain_package(root):
        current = boards.load_board_pack(root)
        if current.get('package_binding') != retained_binding:
            if current.get('package_binding') is not None:
                raise ValueError('board pack already indexes a different retained package')
            current['package_binding'] = retained_binding
            boards._save(root, current)
        return current
    pack = retain_package(root)
    projection = pack.get('motion_projection')
    if projection is None:
        raise ValueError('package motion projection incomplete; replay preparation to reconcile')
    return {'status': 'prepared', 'provider_calls': 0, 'reservations': 0,
        'artifacts': [_artifact_pointer(root, 'artifacts/' + name + '.json')
            for name in ('script', 'scene_plan', 'asset_manifest')],
        'motion_projection': projection,
        'motion_preflight': PACKAGE_PREFLIGHT}


NATIVE_BOARD_ROLES = {'start_frame', 'end_frame', 'timed_keyframe', 'reference_image'}


def _validate_consumer_roles(roles, *, require_timing=True):
    if not isinstance(roles, list):
        raise ValueError('consumer roles must be an ordered list')
    seen = set()
    for row in roles:
        if (not isinstance(row, dict) or set(row) - {'asset_id', 'role', 'time_seconds'}
                or not {'asset_id', 'role'} <= set(row) or not isinstance(row['asset_id'], str)
                or not row['asset_id'].strip() or not isinstance(row['role'], str) or not row['role'].strip()):
            raise ValueError('consumer requires exact asset_id, role and optional time_seconds')
        key = (row['asset_id'], row['role'])
        if key in seen:
            raise ValueError('duplicate consumer asset/role')
        seen.add(key)
        if ('time_seconds' in row and row['role'] != 'timed_keyframe') or (require_timing and row['role'] == 'timed_keyframe' and 'time_seconds' not in row):
            raise ValueError('time_seconds belongs to every timed_keyframe consumer only')
        if 'time_seconds' in row:
            _number(row['time_seconds'], 'consumer time_seconds')


def build_video_request_templates(package, settings):
    """Derive exact native inputs from existing board consumers, before image jobs.

    Returns plain per-shot inputs with canonical $board sources. This pure
    authoring projection grants no account, source-transfer or attempt authority.
    Existing production preparation resolves/hashes/uploads those same sources.
    """
    from tools.tool_registry import registry
    if not isinstance(settings, dict) or set(settings) - {'provider', 'preferred_provider', 'allowed_providers', 'model', 'mode', 'operation', 'resolution', 'aspect_ratio', 'native_params', 'allow_unknown_cost', 'timeout_seconds', 'openart_project_id'}:
        raise ValueError('unsupported selected video setting')
    package = _expand_compact_package(package) if 'authoring_form' in package or 'compact' in package else copy.deepcopy(package)
    spec = package['boards']
    nodes = {n['slot_id']: n for n in spec['image_nodes']}
    references = {r['id']: r for r in spec['references']}
    if len(nodes) != len(spec['image_nodes']) or len(references) != len(spec['references']) or nodes.keys() & references.keys():
        raise ValueError('duplicate declared board source')
    provider = settings.get('provider', settings.get('preferred_provider'))
    if settings.get('preferred_provider', provider) != provider or settings.get('allowed_providers', [provider]) != [provider]:
        raise ValueError('selected provider constraints conflict')
    caps = registry.describe_video_inputs(provider, model=settings.get('model'),
        mode=settings.get('mode'), operation=settings.get('operation'))
    if caps['status'] != 'available':
        raise ValueError('selected video inputs unavailable: ' + str(caps['blockers']))
    mappings = {s['shot_id']: s for s in spec['shots']}
    if len(mappings) != len(spec['shots']) or [s['id'] for s in package['shots']] != list(mappings):
        raise ValueError('authored shots and native consumer mappings differ')
    result = {}
    for shot in package['shots']:
        roles = copy.deepcopy(mappings[shot['id']]['roles'])
        _validate_consumer_roles(roles)
        if any(row['asset_id'] not in nodes and row['asset_id'] not in references for row in roles):
            raise ValueError('unknown board role source')
        if any(row['role'] not in NATIVE_BOARD_ROLES | {'identity_reference', 'identity', 'payoff_board', 'observed_outgoing_frame', 'voice_reference', 'reference_video', 'reference_audio'} for row in roles):
            raise ValueError('unknown authored consumer role')
        declared = [r for r in roles if r['role'] in NATIVE_BOARD_ROLES]
        if any(r['role'] in {'reference_video', 'reference_audio'} for r in roles):
            raise ValueError('selected image compiler cannot silently omit declared native media roles')
        if provider == 'grok_cli' and not declared:
            raise ValueError('Grok native operation requires at least one declared image consumer')
        branches = caps['branches'] or [{'roles': caps['roles'], 'settings': caps['settings']}]
        fits = []
        for branch in branches:
            valid = True
            for role_name, capability in branch['roles'].items():
                count = sum(r['role'] == role_name for r in declared)
                if count and not capability['supported'] or capability['required'] and count < max(1, capability['minimum']) or count and count < capability['minimum'] or capability['maximum'] is not None and count > capability['maximum']:
                    valid = False
            if valid:
                fits.append(branch)
        if not fits:
            raise ValueError('declared consumer roles are unsupported or violate exact native limits/branch')
        duration = shot['duration_seconds']
        for key, value in [('duration', duration), *[(k, settings[k]) for k in ('resolution', 'aspect_ratio') if k in settings]]:
            schema = caps['settings'].get(key)
            if schema is None:
                schemas = [branch['settings'].get(key) for branch in fits]
                if not any(schema is not None and Draft202012Validator(schema).is_valid(value) for schema in schemas):
                    raise ValueError('unsupported selected native setting: ' + key)
            elif not Draft202012Validator(schema).is_valid(value):
                raise ValueError('unsupported selected native setting: ' + key)
        timed = [r for r in declared if r['role'] == 'timed_keyframe']
        timing = caps['roles']['timed_keyframe'].get('timing') or {}
        last = None
        for row in timed:
            timestamp = row['time_seconds']
            if not 0 < timestamp < duration or last is not None and timestamp - last < timing.get('minimum_spacing_seconds', 0):
                raise ValueError('timed keyframe must be inside duration, increasing and correctly spaced')
            last = timestamp
        native = {k: copy.deepcopy(v) for k, v in settings.items() if k != 'provider' and not (provider == 'openart_mcp' and k in {'preferred_provider', 'allowed_providers'})}
        native_duration = native.get('native_params', {}).get('duration') if provider == 'openart_mcp' else None
        if native_duration is not None:
            if type(native_duration) not in (int, float) or native_duration != duration:
                raise ValueError('native duration differs from authored shot duration')
        else:
            native['duration'] = duration
        native['operation'] = caps['operation']
        def source(row):
            return {'$board': {'asset_id': row['asset_id']}}
        if caps['provider'] == 'grok_cli':
            for role_name, field in (('start_frame', 'image_path'), ('end_frame', 'last_image_path')):
                consumers = [r for r in declared if r['role'] == role_name]
                if consumers:
                    native[field] = source(consumers[0])
            if timed:
                native['keyframes'] = [{'image': source(row), 'timestamp_s': row['time_seconds']} for row in timed]
            refs = [source(r) for r in declared if r['role'] == 'reference_image']
            if refs:
                native['reference_image_paths'] = refs
        else:
            from lib import openart_mcp as mcp
            mapping = {'start_frame': 'first_frame', 'end_frame': 'last_frame', 'reference_image': 'reference_image'}
            if len(declared) == 1 and declared[0]['role'] == 'start_frame':
                native['image_path'] = source(declared[0])  # preserve the legacy start-only input layout
            else:
                native['input_assets'] = [{'role': mapping[row['role']], 'source_path': source(row)} for row in declared]
            if native['operation'] == 'first_last_frame' and not {'start_frame', 'end_frame'} <= {r['role'] for r in declared}:
                raise ValueError('first_last_frame requires explicit start/end consumers')
            root = caps['root_schema']; props = mcp._properties(root)
            params = copy.deepcopy(settings.get('native_params', {}))
            if not isinstance(params, dict) or set(params) & {field for field, _ in mcp.ROLES.values()}:
                raise ValueError('native media requires declared source-bound roles')
            for key, field in (('duration', 'videoDuration' if 'videoDuration' in props else 'duration'),
                               ('resolution', 'videoResolution' if 'videoResolution' in props else 'resolution'),
                               ('aspect_ratio', 'videoAspectRatio' if 'videoAspectRatio' in props else 'aspectRatio')):
                if key == 'duration' or key in settings:
                    value = duration if key == 'duration' else settings[key]
                    if field in params and params[field] != value:
                        raise ValueError('conflicting native/public setting ' + key)
                    params[field] = value
            params['sceneDescription' if 'sceneDescription' in props else 'prompt'] = shot['action']
            for row in declared:
                field = caps['roles'][row['role']]['native_field']
                schema = props[field]
                ref = mcp._project_reference({'id': 'planned-source', 'type': 'image', 'url': 'https://planned.invalid/source.png', 'label': 'planned source'}, schema.get('items', schema))
                if field == 'visualReferences':
                    params.setdefault(field, []).append(ref)
                else:
                    params[field] = ref
            errors = list(mcp._validator(root).iter_errors(params))
            if errors:
                raise ValueError('native settings/roles violate exact selected form: ' + errors[0].message)
            if 'videoCount' in params and (type(params['videoCount']) is not int or params['videoCount'] != 1):
                raise ValueError('one governed shot requires videoCount exactly one')
        result[shot['id']] = native
    return result


def _replay_reference_projection(root, shot_id, scene_plan, references, retained, contract):
    """Reconstruct saved consumer metadata from exact original package semantics.

    The retained row selects the old/new representation only. Source bytes,
    cast and review hashes are independently rebuilt; roles/times must be in
    the original hash-bound package. Older packets remain byte-identical.
    """
    original = {row['id']: row for row in references}
    if {row['id'] for row in retained} != set(original):
        raise ValueError('retained source reference IDs differ from canonical original')
    binding = scene_plan.get('metadata', {}).get('package')
    consumers, compact = [], False
    if binding:
        from lib import production_images as images
        package = json.loads(images._bound_file(root, binding, 'original production package').read_text())
        compact = package.get('authoring_form') == COMPACT_AUTHORING_FORM
        spec = _expand_compact_package(package)['boards'] if compact else package['boards']
        consumers = [role for shot in spec['shots'] if shot['shot_id'] == shot_id for role in shot['roles']]
    assets = {asset['id']: asset for asset in contract['assets']}
    output = []
    for saved in retained:
        base = original[saved['id']]
        if any(saved.get(key) != base[key] for key in ('cast_ids', 'sha256', 'review_sha256')):
            raise ValueError('retained source reference bytes/cast/review differ')
        declared = [role for role in consumers if role['asset_id'] == saved['id'] and role['role'] == saved['role']]
        if saved['role'] != base['role'] and not declared:
            raise ValueError('retained native role lacks exact original consumer declaration')
        row = {**base, 'role': saved['role']}
        if 'time_seconds' in saved:
            timestamp = saved['time_seconds']
            declared_times = [role['time_seconds'] for role in declared if 'time_seconds' in role]
            if (saved['role'] != 'timed_keyframe' or (timestamp not in declared_times if declared_times
                    else assets[saved['id']].get('time_seconds') != timestamp)):
                raise ValueError('retained keyframe time differs from original declaration')
            row['time_seconds'] = timestamp
        if 'native_required' in saved:
            required = saved['native_required']
            native_consumers = [role for role in consumers if role['asset_id'] == saved['id'] and role['role'] in NATIVE_BOARD_ROLES]
            if (not compact or type(required) is not bool
                    or required and (saved['role'] != 'end_frame' or not declared)
                    or not required and (saved['role'] != 'payoff_board' or native_consumers)):
                raise ValueError('retained native requirement differs from original compact consumer')
            row['native_required'] = required
        if set(saved) != set(row):
            raise ValueError('retained reference fields differ from supported original projection')
        output.append(row)
    return output
