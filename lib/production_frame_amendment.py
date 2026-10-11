"""One exact pre-motion reference-guided -> frame-conditioned amendment.

Pure planning and an explicit producer writer retain original authority/media
bytes. This operation never reviews pixels, invokes providers, resets usage or
amends any other planning, transport, account or allowance term.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import uuid
from pathlib import Path

from lib import production_autonomy as pa, production_boards as boards
from lib import production_execution as execution, production_images as images
from lib.production_request import digest
from lib.shot_contract import file_sha256, frame_conditioned_start

POINTER = 'artifacts/frame_input_amendment.json'
KIND = 'pre_motion_frame_input_amendment'


def _raw(root, path):
    return execution._inside(path, root).read_bytes()


def _json(root, path):
    return json.loads(_raw(root, path))


def _binding(root, path):
    path = execution._inside(path, root)
    return {'path': str(path.relative_to(root)), 'sha256': file_sha256(path)}


def _bound(root, binding):
    return images._bound_file(root, binding, 'frame amendment evidence')


def _authority(root, proposal, authority):
    _bound(root, proposal)
    source = json.loads(_bound(root, authority).read_text())
    marker = _json(root, 'project.json')
    human = source.get('human_delegation', {})
    if (source.get('version') != '1.0' or source.get('kind') != 'delegated_authorization_source'
            or any(source.get(k) != marker[k] for k in ('project_id', 'story_revision'))
            or source.get('proposal_sha256') != proposal['sha256']
            or any(not isinstance(source.get(k), str) or not source[k].strip()
                   for k in ('source_thread_id', 'source_turn_id', 'relay_message_id', 'relay_text'))
            or any(not isinstance(human.get(k), str) or not human[k].strip() for k in ('message_id', 'text'))
            or source.get('revoked') is True):
        raise pa.StandingSetupRequired('exact retained correction proposal and delegated human authority required')


def _no_motion(root, policy):
    counts = pa.root_attempt_counts(root, policy)
    if counts['total'] != 0:
        raise pa.StandingSetupRequired('frame amendment requires zero motion usage/reservations')
    scopes = _json(root, 'production_scopes.json')
    if any(scope.get('phase') != 'image' for scope in scopes['scopes']):
        raise pa.StandingSetupRequired('frame amendment cannot replace retained motion scopes')
    if any(path.is_file() for name in ('production_attempts', 'openart_mcp/attempts') for path in (root / name).rglob('*')):
        raise pa.StandingSetupRequired('frame amendment requires zero prepared/submitted motion originals')
    if any(list((root / 'artifacts').glob(pattern)) for pattern in ('compiled_request-*.json', 'preparation_review-*.json')):
        raise pa.StandingSetupRequired('frame amendment requires zero retained motion preparations')
    from lib.provider_credit_ledger import read_existing_snapshot
    snapshot = read_existing_snapshot()
    for key in ('reservations', 'unpriced_reservations'):
        for row in snapshot[key]:
            binding = json.loads(row['binding_json'])
            origin = binding.get('project_root')
            if not isinstance(origin, str) or not Path(origin).is_absolute():
                raise pa.StandingSetupRequired('private reservation origin cannot be validated')
            if Path(origin).resolve() == root:
                raise pa.StandingSetupRequired('frame amendment requires zero private motion reservations')
    reserved = {row['attempt_id'] for key in ('reservations', 'unpriced_reservations') for row in snapshot[key]}
    if any(row['attempt_id'] not in reserved for key in ('outbox', 'unpriced_outbox', 'claims', 'unpriced_claims') for row in snapshot[key]):
        raise pa.StandingSetupRequired('private outbox/reservation origin is missing')
    usage = images.image_usage(root)
    setup = pa._bound_document(root, policy['activation_basis']['standing_setup'], 'standing setup')
    if usage['total'] > setup['native_images']['image_allowance']:
        raise pa.StandingSetupRequired('retained cumulative image usage exceeds original allowance')
    return {'images': usage, 'motion': counts}


def expected_original(project_dir):
    """Pure complete expected-original guard for the single pre-motion operation."""
    root = Path(project_dir).resolve()
    policy, sha, decision = pa.require_active_policy(root)
    pack = boards.load_board_pack(root)
    paths = {'project.json', pa.POLICY_ARTIFACT, str(execution._artifact_path(root, 'decision_log.json').relative_to(root)),
        boards.ARTIFACT, pack['package_binding']['path'], 'production_scopes.json',
        *[str(execution._artifact_path(root, name + '.json').relative_to(root)) for name in
          ('script', 'scene_plan', 'asset_manifest', 'shot_contract')], policy['evidence']['path']}
    if (root / images.ARTIFACT).exists():
        paths.add(str(images.ARTIFACT))
    for source in [pack['specification']['story'], pack['specification']['approval'], *pack['specification']['references']]:
        paths.add(source['path'])
    for asset in execution.load_shot_contract(root)['assets']:
        if asset.get('path'):
            paths.add(asset['path'])
    for name in ('standing_setup', 'run_command'):
        binding = policy['activation_basis'][name]
        document = pa._bound_document(root, binding, name)
        paths.add(binding['path']); paths.add(document['authorization']['path'])
    return {'policy_sha256': sha, 'decision_id': decision,
        'files': {path: file_sha256(execution._inside(path, root)) for path in sorted(paths)},
        'usage': _no_motion(root, policy)}


def _delta(contract, specification):
    result = copy.deepcopy(contract)
    if result.get('planning_mode') != 'shot_local_v1' or result.get('reference_mode') != 'reference_guided':
        raise pa.StandingSetupRequired('amendment only replaces explicit shot-local reference_guided treatment')
    result['reference_mode'] = 'frame_conditioned'
    mappings = {s['shot_id']: s['roles'] for s in specification['shots']}
    for shot in result['shots']:
        if shot.get('reference_mode', 'reference_guided') != 'reference_guided' or 'frame_input' in shot:
            raise pa.StandingSetupRequired('amendment cannot change mixed/previous frame treatments')
        starts = [r for r in mappings[shot['id']] if r['role'] == 'start_frame']
        if len(starts) != 1 or starts[0]['asset_id'] not in shot['asset_ids']:
            raise pa.StandingSetupRequired('each consumer needs exactly one existing authored starting composition')
        if 'reference_mode' in shot:
            shot['reference_mode'] = 'frame_conditioned'
        shot['frame_input'] = copy.deepcopy(starts[0])
    return result


def _successor_decision(original_sha, successor_sha, proposal, authority):
    decision_id = 'standing-frame-input-' + successor_sha[:24]
    return {'decision_id': decision_id, 'stage': 'proposal', 'category': pa.ACTIVATION_CATEGORY,
        'subject': pa.ACTIVATION_SUBJECT, 'selected': 'auto_continue:' + successor_sha, 'user_approved': True,
        'reason': 'Exact delegated correction of native input treatment; retained story/routes/accounts/allowances and cumulative usage. '
            + 'Authority ' + authority['path'] + ' sha256:' + authority['sha256'] + '; proposal ' + proposal['path'] + ' sha256:' + proposal['sha256'] + '.',
        'options_considered': [{'option_id': 'auto_continue:' + original_sha, 'label': 'Original reference-guided activation',
            'score': 0, 'reason': 'Original authority retained.', 'rejected_because': 'Approved routes carry composite starting frames.'},
            {'option_id': 'auto_continue:' + successor_sha, 'label': 'Linked frame-conditioned activation', 'score': 1,
             'reason': 'Existing exact declared and observed starting compositions.'}]}


def plan_frame_input_amendment(project_dir, *, expected, proposal, authority):
    """Pure receipt; no lock files, upload, admission or provider observation."""
    root = Path(project_dir).resolve()
    if (root / POINTER).exists():
        raise pa.StandingSetupRequired('one linked frame-input amendment is already retained; use exact apply replay')
    _authority(root, proposal, authority)
    actual = expected_original(root)
    if actual != expected:
        raise pa.StandingSetupRequired('stale expected original package/authority/usage bytes')
    policy, _, _ = pa.require_active_policy(root)
    pack = boards.load_board_pack(root)
    retained_envelope = _json(root, 'production_scopes.json')['image_envelope']
    if images._image_envelope(root, _json(root, 'production_scopes.json')) != retained_envelope:
        raise pa.StandingSetupRequired('original image envelope is not current retained authority')
    original_contract = execution.load_shot_contract(root)
    successor_contract = _delta(original_contract, pack['specification'])
    # Evidence is actual and current before amendment; changed plan reviews stay
    # byte-identical and become stale through the changed subject hashes.
    for shot in successor_contract['shots']:
        frame_conditioned_start(successor_contract, shot, root)
    original_package = _json(root, pack['package_binding']['path'])
    successor_package = copy.deepcopy(original_package)
    successor_package['boards']['motion_binding']['contract_template'] = _delta(
        {**successor_package['boards']['motion_binding']['contract_template'], 'planning_mode': 'shot_local_v1'},
        pack['specification'])
    spec = copy.deepcopy(pack['specification'])
    spec['motion_binding']['contract_template'] = _delta(spec['motion_binding']['contract_template'], spec)
    boards._validate_spec(root, spec)
    old_baselines = pa.retained_baselines(root, policy)
    script = _json(root, 'artifacts/script.json'); scene = _json(root, 'artifacts/scene_plan.json')
    manifest = _json(root, 'artifacts/asset_manifest.json')
    baselines = {}
    for shot in successor_contract['shots']:
        sid = shot['id']
        projection = pa.static_planning_projection(successor_contract, scene, script, sid, manifest)
        baselines[sid] = {'projection': projection,
            'planned_request_template': copy.deepcopy(old_baselines[sid]['planned_request_template']),
            'script_snapshot': copy.deepcopy(script), 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(successor_contract, sid)}
    successor_policy = copy.deepcopy(policy)
    successor_policy['shots'] = {sid: pa.baseline_descriptor(value, root) for sid, value in baselines.items()}
    successor_policy['activation_basis']['plan_sha256'] = digest(baselines)
    policy_sha = pa.policy_sha256(successor_policy, baselines)
    content = pa.evidence_content(successor_policy, baselines)
    evidence_raw = json.dumps(content, indent=2, allow_nan=False).encode()
    successor_policy['evidence'] = {'path': f'approvals/autonomy-policy-{policy_sha}.json',
        'sha256': hashlib.sha256(evidence_raw).hexdigest()}
    decision_id = 'standing-frame-input-' + policy_sha[:24]
    log_path = str(execution._artifact_path(root, 'decision_log.json').relative_to(root))
    log = _json(root, log_path)
    decision = _successor_decision(expected['policy_sha256'], policy_sha, proposal, authority)
    log['decisions'].append(decision)
    # The id depends solely on exact original/approval bytes, never a timestamp.
    amendment_id = digest({'expected': expected, 'proposal': proposal, 'authority': authority})
    directory = 'artifacts/history/frame-input-' + amendment_id
    package_path = directory + '/successor-package.json'
    package_raw = json.dumps(successor_package, indent=2, allow_nan=False).encode()
    successor_pack = copy.deepcopy(pack)
    successor_pack.update(specification=spec, specification_sha256=boards._digest(spec),
        package_binding={'path': package_path, 'sha256': hashlib.sha256(package_raw).hexdigest()})
    successor_pack['events'].append({'kind': KIND, 'observed_at': 'retained_authority',
        'details': {'amendment_id': amendment_id, 'original_package': pack['package_binding']}})
    changes = {boards.ARTIFACT: successor_pack, 'artifacts/shot_contract.json': successor_contract,
        pa.POLICY_ARTIFACT: successor_policy, log_path: log}
    retained_new = {package_path: successor_package, successor_policy['evidence']['path']: content}
    result = {'version': '1.0', 'kind': KIND, 'amendment_id': amendment_id, 'expected': copy.deepcopy(expected),
        'proposal': copy.deepcopy(proposal), 'authority': copy.deepcopy(authority),
        'successor_policy_sha256': policy_sha, 'successor_decision_id': decision_id,
        'changed_paths': sorted(changes), 'provider_calls': 0, 'reservations': 0,
        'changes': changes, 'retained_new': retained_new}
    result['plan_sha256'] = digest(result)
    return result


def _publish(path, raw):
    temporary = path.with_name('.' + path.name + '.frame-input-tmp')
    with temporary.open('wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _save_raw(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise pa.StandingSetupRequired('immutable frame amendment history differs')
        return
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.frame-history-tmp')
    with temporary.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    temporary.chmod(0o444)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_record_subjects(root, record):
    """Recompute the closed transformation from trusted retained originals."""
    plan = record['plan']; originals = record['originals']
    if set(originals) != set(plan['expected']['files']):
        raise pa.StandingSetupRequired('frame amendment original inventory differs')
    def old(path):
        return json.loads(_bound(root, originals[path]).read_text())
    pack = old(boards.ARTIFACT); policy = old(pa.POLICY_ARTIFACT)
    baselines = pa.retained_baselines(root, policy)
    if pa.policy_sha256(policy, baselines) != plan['expected']['policy_sha256']:
        raise pa.StandingSetupRequired('frame amendment original policy/evidence differs')
    log_path = next(path for path in originals if Path(path).name == 'decision_log.json')
    original_log = old(log_path)
    original_decision = next((d for d in reversed(original_log['decisions']) if d.get('category') == pa.ACTIVATION_CATEGORY
        and d.get('subject') == pa.ACTIVATION_SUBJECT), None)
    if (not original_decision or original_decision.get('decision_id') != plan['expected']['decision_id']
            or original_decision.get('selected') != 'auto_continue:' + plan['expected']['policy_sha256']
            or original_decision.get('user_approved') is not True):
        raise pa.StandingSetupRequired('frame amendment original latest approval is invalid')
    if pack['specification_sha256'] != boards._digest(pack['specification']) or pack['prompt_pack'] != {
            n['slot_id']: n for n in pack['specification']['image_nodes']}:
        raise pa.StandingSetupRequired('frame amendment original board prompt pack differs')
    marker = old('project.json'); current_marker = _json(root, 'project.json')
    if any(marker.get(k) != current_marker.get(k) for k in ('project_id', 'story_revision', 'identity_id')):
        raise pa.StandingSetupRequired('frame amendment original belongs to a different project/story/identity')
    for path in plan['expected']['files']:
        if path not in plan['changed_paths'] and path not in {str(images.ARTIFACT), 'production_scopes.json', 'project.json',
                'artifacts/script.json', 'artifacts/scene_plan.json', 'artifacts/asset_manifest.json'}:
            if file_sha256(execution._inside(path, root)) != plan['expected']['files'][path]:
                raise pa.StandingSetupRequired('frame amendment unchanged original source/authority bytes differ')
    package_binding = pack['package_binding']
    if package_binding['sha256'] != originals[package_binding['path']]['sha256']:
        raise pa.StandingSetupRequired('frame amendment original package binding differs')
    # Original package and authority remain at their canonical retained paths.
    _bound(root, package_binding)
    for name in ('standing_setup', 'run_command'):
        document = pa._bound_document(root, policy['activation_basis'][name], name)
        pa._bound_authorization(root, document, policy['activation_basis']['identity_id'])
    contract = _delta(old('artifacts/shot_contract.json'), pack['specification'])
    if plan['changes']['artifacts/shot_contract.json'] != contract:
        raise pa.StandingSetupRequired('frame amendment contains unexpected semantic delta')
    package = old(package_binding['path'])
    package['boards']['motion_binding']['contract_template'] = _delta(
        {**package['boards']['motion_binding']['contract_template'], 'planning_mode': 'shot_local_v1'}, pack['specification'])
    package_path = 'artifacts/history/frame-input-' + plan['amendment_id'] + '/successor-package.json'
    if plan['retained_new'].get(package_path) != package:
        raise pa.StandingSetupRequired('frame amendment successor package changed unrelated terms')
    spec = copy.deepcopy(pack['specification'])
    spec['motion_binding']['contract_template'] = _delta(spec['motion_binding']['contract_template'], spec)
    expected_pack = copy.deepcopy(pack)
    expected_pack.update(specification=spec, specification_sha256=boards._digest(spec),
        package_binding={'path': package_path, 'sha256': hashlib.sha256(json.dumps(package, indent=2, allow_nan=False).encode()).hexdigest()})
    expected_pack['events'].append({'kind': KIND, 'observed_at': 'retained_authority',
        'details': {'amendment_id': plan['amendment_id'], 'original_package': pack['package_binding']}})
    if plan['changes'][boards.ARTIFACT] != expected_pack:
        raise pa.StandingSetupRequired('frame amendment successor board pack changed unrelated facts')
    script = old('artifacts/script.json'); scene = old('artifacts/scene_plan.json'); manifest = old('artifacts/asset_manifest.json')
    successor_baselines = {}
    for shot in contract['shots']:
        sid = shot['id']; projection = pa.static_planning_projection(contract, scene, script, sid, manifest)
        successor_baselines[sid] = {'projection': projection,
            'planned_request_template': copy.deepcopy(baselines[sid]['planned_request_template']),
            'script_snapshot': copy.deepcopy(script), 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, sid)}
    candidate = copy.deepcopy(policy)
    candidate['shots'] = {sid: pa.baseline_descriptor(value, root) for sid, value in successor_baselines.items()}
    candidate['activation_basis']['plan_sha256'] = digest(successor_baselines)
    sha = pa.policy_sha256(candidate, successor_baselines); content = pa.evidence_content(candidate, successor_baselines)
    candidate['evidence'] = {'path': f'approvals/autonomy-policy-{sha}.json',
        'sha256': hashlib.sha256(json.dumps(content, indent=2, allow_nan=False).encode()).hexdigest()}
    if (plan['changes'][pa.POLICY_ARTIFACT] != candidate or plan['successor_policy_sha256'] != sha
            or plan['retained_new'].get(candidate['evidence']['path']) != content
            or plan['successor_decision_id'] != 'standing-frame-input-' + sha[:24]):
        raise pa.StandingSetupRequired('frame amendment successor policy changed unrelated authority')
    expected_log = copy.deepcopy(original_log)
    expected_log['decisions'].append(_successor_decision(plan['expected']['policy_sha256'], sha, plan['proposal'], plan['authority']))
    if (set(plan['changes']) != {boards.ARTIFACT, pa.POLICY_ARTIFACT, 'artifacts/shot_contract.json', log_path}
            or plan['changed_paths'] != sorted(plan['changes'])
            or set(plan['retained_new']) != {package_path, candidate['evidence']['path']}
            or plan['changes'][log_path] != expected_log
            or plan.get('provider_calls') != 0 or plan.get('reservations') != 0):
        raise pa.StandingSetupRequired('frame amendment successor decision/inventory changed unrelated terms')
    successor_log = plan['changes'][log_path]
    if (successor_log['decisions'][:-1] != original_log['decisions'] or
            _json(root, log_path)['decisions'][:len(original_log['decisions'])] != original_log['decisions']):
        raise pa.StandingSetupRequired('frame amendment lost or changed original approval history')
    if plan['amendment_id'] != digest({'expected': plan['expected'], 'proposal': plan['proposal'], 'authority': plan['authority']}):
        raise pa.StandingSetupRequired('frame amendment deterministic identity changed')
    for path in plan['retained_new']:
        if _json(root, path) != plan['retained_new'][path]:
            raise pa.StandingSetupRequired('retained frame amendment successor evidence changed')


def _completed_record(root, *, allow_publishing=False):
    pointer = _json(root, POINTER)
    if pointer.get('status') != 'complete' and not (allow_publishing and pointer.get('status') == 'publishing'):
        raise pa.StandingSetupRequired('frame amendment publication is incomplete; explicit producer replay required')
    record = json.loads(_bound(root, pointer['record']).read_text())
    plan = record['plan']
    if (record.get('kind') != KIND or plan.get('kind') != KIND
            or plan['plan_sha256'] != digest({k: v for k, v in plan.items() if k != 'plan_sha256'})
            or pointer.get('plan_sha256') != plan['plan_sha256']):
        raise pa.StandingSetupRequired('frame amendment link/plan bytes differ')
    _authority(root, plan['proposal'], plan['authority'])
    for original, binding in record['originals'].items():
        if binding['sha256'] != plan['expected']['files'][original]:
            raise pa.StandingSetupRequired('frame amendment original snapshot differs from expected authority')
        _bound(root, binding)
    _validate_record_subjects(root, record)
    return record


def validate_successor(project_dir, policy):
    """Read-only current authority link check; historical context grants no spend."""
    root = Path(project_dir).resolve()
    if not (root / POINTER).exists():
        return
    record = _completed_record(root)
    plan = record['plan']
    if policy != plan['changes'][pa.POLICY_ARTIFACT]:
        raise pa.StandingSetupRequired('current policy differs from linked frame-input successor')
    decision = pa.activation_decision(root)
    if (not decision or decision.get('decision_id') != plan['successor_decision_id']
            or decision.get('selected') != 'auto_continue:' + plan['successor_policy_sha256'] or decision.get('user_approved') is not True):
        raise pa.StandingSetupRequired('frame-input successor authority is revoked or superseded')
    original = json.loads(_bound(root, record['originals'][pa.POLICY_ARTIFACT]).read_text())
    original_baselines = pa.retained_baselines(root, original)
    if pa.policy_sha256(original, original_baselines) != plan['expected']['policy_sha256']:
        raise pa.StandingSetupRequired('trusted original frame amendment policy differs')
    immutable = {k: v for k, v in original.items() if k not in {'shots', 'evidence', 'activation_basis'}}
    if immutable != {k: v for k, v in policy.items() if k not in {'shots', 'evidence', 'activation_basis'}}:
        raise pa.StandingSetupRequired('frame amendment expanded original policy terms')
    if {k: v for k, v in original['activation_basis'].items() if k != 'plan_sha256'} != {
            k: v for k, v in policy['activation_basis'].items() if k != 'plan_sha256'}:
        raise pa.StandingSetupRequired('frame amendment changed original activation authority')
    old_pack = json.loads(_bound(root, record['originals'][boards.ARTIFACT]).read_text())
    old_contract = json.loads(_bound(root, record['originals']['artifacts/shot_contract.json']).read_text())
    if plan['changes']['artifacts/shot_contract.json'] != _delta(old_contract, old_pack['specification']):
        raise pa.StandingSetupRequired('frame amendment contains unexpected semantic delta')
    current_pack = boards.load_board_pack(root)
    expected_pack = plan['changes'][boards.ARTIFACT]
    for key in ('specification', 'specification_sha256', 'prompt_pack', 'package_binding'):
        if current_pack[key] != expected_pack[key]:
            raise pa.StandingSetupRequired('frame amendment board package binding differs')
    if current_pack['observations'][:len(old_pack['observations'])] != old_pack['observations']:
        raise pa.StandingSetupRequired('frame amendment lost retained original observations')
    old_scopes = json.loads(_bound(root, record['originals']['production_scopes.json']).read_text())
    current_scopes = _json(root, 'production_scopes.json')
    if (current_scopes['image_envelope'] != old_scopes['image_envelope']
            or any(scope not in current_scopes['scopes'] for scope in old_scopes['scopes'])):
        raise pa.StandingSetupRequired('frame amendment changed original image envelope/scopes')


def historical_image_envelope(project_dir, retained):
    """Authenticate original envelope using retained original authority, collection only."""
    root = Path(project_dir).resolve()
    if not (root / POINTER).exists():
        return False
    record = _completed_record(root)
    plan = record['plan']
    original_scopes = json.loads(_bound(root, record['originals']['production_scopes.json']).read_text())
    if retained != original_scopes['image_envelope']:
        raise pa.StandingSetupRequired('historical frame amendment envelope differs from retained original')
    policy = json.loads(_bound(root, record['originals'][pa.POLICY_ARTIFACT]).read_text())
    baselines = pa.retained_baselines(root, policy)
    sha = pa.policy_sha256(policy, baselines)
    old_log_path = next(path for path in record['originals'] if Path(path).name == 'decision_log.json')
    old_log = json.loads(_bound(root, record['originals'][old_log_path]).read_text())
    decision = next((d for d in reversed(old_log['decisions']) if d.get('category') == pa.ACTIVATION_CATEGORY and d.get('subject') == pa.ACTIVATION_SUBJECT), None)
    expected = {'kind': 'standing_run', 'policy_sha256': sha, 'decision_id': plan['expected']['decision_id'],
        'run_command': policy['activation_basis']['run_command']}
    if (sha != plan['expected']['policy_sha256'] or not decision or decision.get('decision_id') != expected['decision_id']
            or decision.get('selected') != 'auto_continue:' + sha or decision.get('user_approved') is not True
            or retained['activation_basis'] != expected
            or retained['approval'] != {**policy['evidence'], 'approved_by': 'standing_media_authority:' + expected['decision_id']}):
        raise pa.StandingSetupRequired('historical frame amendment activation differs from trusted original')
    # Rebuild the exact original definition using the trusted original pack,
    # never the successor package or caller-supplied snapshots.
    pack = json.loads(_bound(root, record['originals'][boards.ARTIFACT]).read_text())
    definition = images._standing_image_definition(root, policy, check_first_pass_capacity=False, retained_pack=pack)
    if retained['definition'] != definition:
        raise pa.StandingSetupRequired('historical frame amendment definition differs from exact original board package')
    return True


def apply_frame_input_amendment(project_dir, *, expected_plan_sha256, expected, proposal, authority):
    """Producer-only explicit writer; deterministic interrupted replay stays closed."""
    root = Path(project_dir).resolve()
    with execution._lock(root):
        lock = root / 'artifacts/.board_preparation.lock'
        with lock.open('a') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                if (root / POINTER).exists():
                    record = _completed_record(root, allow_publishing=True)
                    plan = record['plan']
                    if (plan['plan_sha256'] != expected_plan_sha256 or plan['expected'] != expected
                            or plan['proposal'] != proposal or plan['authority'] != authority):
                        raise pa.StandingSetupRequired('frame amendment replay differs from exact approved plan')
                    if _json(root, POINTER)['status'] == 'complete':
                        pa.require_active_policy(root)
                        return {k: plan[k] for k in ('amendment_id', 'successor_policy_sha256', 'successor_decision_id', 'provider_calls', 'reservations')} | {'replayed': True}
                    original_policy = json.loads(_bound(root, record['originals'][pa.POLICY_ARTIFACT]).read_text())
                    _no_motion(root, original_policy)
                else:
                    plan = plan_frame_input_amendment(root, expected=expected, proposal=proposal, authority=authority)
                    if plan['plan_sha256'] != expected_plan_sha256:
                        raise pa.StandingSetupRequired('frame amendment plan differs from expected exact receipt')
                    history = 'artifacts/history/frame-input-' + plan['amendment_id']
                    originals = {}
                    for index, (path, sha) in enumerate(expected['files'].items()):
                        raw = _raw(root, path)
                        if hashlib.sha256(raw).hexdigest() != sha:
                            raise pa.StandingSetupRequired('original bytes changed during frame amendment')
                        saved = history + '/originals/' + str(index) + '-' + Path(path).name
                        _save_raw(root / saved, raw); originals[path] = {'path': saved, 'sha256': sha}
                    for path, value in plan['retained_new'].items():
                        _save_raw(root / path, json.dumps(value, indent=2, allow_nan=False).encode())
                    record = {'version': '1.0', 'kind': KIND, 'plan': plan, 'originals': originals}
                    record_path = history + '/amendment.json'
                    _save_raw(root / record_path, json.dumps(record, indent=2, allow_nan=False).encode())
                    if expected_original(root) != expected:
                        raise pa.StandingSetupRequired('authority/usage changed before frame amendment publication')
                    pointer = {'version': '1.0', 'status': 'publishing', 'plan_sha256': expected_plan_sha256,
                        'record': _binding(root, record_path)}
                    _publish(root / POINTER, json.dumps(pointer, indent=2).encode())
                for path, value in plan['changes'].items():
                    raw = _raw(root, path)
                    successor = json.dumps(value, indent=2, allow_nan=False).encode()
                    if raw != successor and hashlib.sha256(raw).hexdigest() != expected['files'][path]:
                        raise pa.StandingSetupRequired('partial frame amendment pointer changed or authority revoked')
                _authority(root, proposal, authority)
                for path, value in plan['changes'].items():
                    _publish(root / path, json.dumps(value, indent=2, allow_nan=False).encode())
                pointer = _json(root, POINTER); pointer['status'] = 'complete'
                _publish(root / POINTER, json.dumps(pointer, indent=2).encode())
                pa.require_active_policy(root)
                return {k: plan[k] for k in ('amendment_id', 'successor_policy_sha256', 'successor_decision_id', 'provider_calls', 'reservations')} | {'replayed': False}
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
