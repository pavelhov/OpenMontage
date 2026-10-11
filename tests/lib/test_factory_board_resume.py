"""Late shot-local corrections through canonical owners; all media is synthetic."""
import base64
import copy
import json
import pytest

from lib import production_autonomy as pa, production_boards as boards
from lib import production_execution as execution, production_images as images
from lib import production_request as request
from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES, UPSTREAM_PREDICATES
from lib.shot_contract import file_sha256, project_review_subject, selection_digest, shot_review_subject
from tests.integration.test_first_pass_workflow import NativeTransport, attestation
from tests.lib.test_production_factory import (creator_fixture, file_bytes, host_receipt,
    prospective_package, retain_factory_board, standing_treatment_fixture, write)
from tests.lib.test_production_images import PNG
from tests.lib.test_production_request import neutral_package  # noqa: F401


def late_board_run(neutral_package, monkeypatch, *, image_allowance=8, native_batches=8,
                   preprepare_target=False, dependent_board=False):
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['shots'] = [s for s in package['shots'] if s['id'] != 's2']
    for shot in package['shots']:
        shot['duration_seconds'] = 5
        shot['script'] = shot['script'].strip()
    spec = package['boards']; template = spec['motion_binding']['contract_template']
    next(n for n in spec['image_nodes'] if n['slot_id'] == 'other')['required_predicates'].append('prop_body_invariants')
    spec['shots'] = [s for s in spec['shots'] if s['shot_id'] != 's2']
    template['shots'] = [s for s in template['shots'] if s['id'] != 's2']
    if dependent_board:
        node = copy.deepcopy(next(n for n in spec['image_nodes'] if n['slot_id'] == 'end'))
        node.update(slot_id='other-end', shot_id='s3', image_dependencies=['other'], output_path='assets/images/other-end.png')
        spec['image_nodes'].append(node)
        spec['shots'][-1]['roles'].append({'asset_id': 'other-end', 'role': 'end_frame'})
        template['assets'].append({'id': 'other-end', 'role': 'end_frame', 'cast_ids': []})
        template['shots'][-1]['asset_ids'].append('other-end')
    next(n for n in spec['image_nodes'] if n['slot_id'] == 'payoff')['shot_id'] = 's1'
    template['reference_mode'] = 'frame_conditioned'
    for shot in template['shots']:
        shot['frame_input'] = {'asset_id': 'start' if shot['id'] == 's1' else 'other', 'role': 'start_frame'}
    request.prepare_package(root, bind(package))
    kwargs = standing_treatment_fixture(root)
    contract = execution.load_shot_contract(root)
    contract['reference_mode'] = 'frame_conditioned'
    write(root, 'artifacts/shot_contract.json', contract)
    for sid, baseline in kwargs['baselines'].items():
        baseline['projection'] = pa.current_projection(root, sid)
        baseline['approval_plan_sha256'] = execution.approval_plan_digest(contract, sid)
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    setup['native_images']['image_allowance'] = image_allowance
    for term in setup['source_transfer']:
        term['max_batches'] = native_batches if term['provider'] == 'native_imagegen' else 8
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    for sid, baseline in kwargs['baselines'].items():
        baseline['planned_request_template'] = execution.planned_request_template({
            'duration': 5, 'image_path': {'$board': {'asset_id': 'start' if sid == 's1' else 'other'}}}, project_dir=root)
    pa.activate_standing_run(root, **kwargs)
    images.activate_standing_image_envelope(root)
    for slot in ('payoff', 'start', 'end', 'other'):
        retain_factory_board(root, slot, predicates={'cast_count': 'pass', 'prop_body_invariants': 'fail'}
            if slot == 'other' and not preprepare_target and not dependent_board else {'cast_count': 'pass'})
    if dependent_board:
        retain_factory_board(root, 'other-end', predicates={'cast_count': 'pass'})
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    contract['shots'][0]['review'] = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    transport = NativeTransport(root)
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', transport)
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda name: '/SYNTHETIC/offline/' + name)
    from tools.video.grok_cli_video import GrokCLIVideo
    tool = GrokCLIVideo(grok_path='/SYNTHETIC/offline/grok', sessions_root=str(root / 'sessions'))
    inputs, native, profile = prepare_motion(root, 's1', 'first-s1')
    observation = {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'}
    scope = pa.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    inputs['governance']['scope_id'] = scope['id']
    generated = tool.execute(inputs)
    assert generated.success, generated.error
    aid = generated.data['production_attempt_id']
    output = execution.load_attempt_result(root, aid)['output']
    frame = root / 'assets/images/SYNTHETIC-outgoing.png'; frame.write_bytes(PNG)
    selection = {'attempt_id': aid, 'output': output,
        'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES, revision='r1')
    execution.record_selection(root, 's1', selection)
    old_target = None
    if preprepare_target:
        planning = execution.advance(root)['action']
        assert (planning['kind'], planning['subject']) == ('review_planning', 's3'), planning
        contract = execution.load_shot_contract(root)
        actual = attestation(shot_review_subject(contract, 's3'), SHOT_PREDICATES, revision='r1')
        execution.complete_action(root, planning, outcome={'shot_review': actual})
        old_target = prepare_motion(root, 's3', 'old-target')
    if preprepare_target or dependent_board:
        pack = boards.load_board_pack(root)
        source = pack['board_bindings']['other']
        failed = attestation(source['sha256'], ['asset_quality', 'cast_identity', 'cast_count', 'prop_body_invariants'], revision='r1')
        failed.update(review_id='SYNTHETIC-new-critical-finding', reviewer='persistent reviewer', status='fail')
        next(p for p in failed['predicates'] if p['name'] == 'prop_body_invariants').update(status='fail', severity='critical')
        boards.record_board_review(root, 'other', failed, roles=['start_frame'], images=images)
    return root, transport, aid, old_target


def prepare_motion(root, sid, record_id):
    contract = execution.load_shot_contract(root)
    shot = next(s for s in contract['shots'] if s['id'] == sid)
    index = contract['shots'].index(shot)
    asset = next(a for a in contract['assets'] if a['id'] == shot['frame_input']['asset_id'])
    authored = request.compile_provider_prompt(root, sid, provider='grok_cli')
    inputs = {'project_dir': str(root), 'governance': {'shot_id': sid}, 'prompt': authored['prompt'],
        'duration': 5, 'operation': 'reference_to_video', 'allow_unknown_cost': True,
        'image_path': str(root / asset['path']), 'output_path': str(root / ('assets/video/' + record_id + '.mp4')),
        'compiled_request_id': record_id, 'preparation_review_id': record_id}
    native = request.prepare_grok_native(inputs, {'cli_version': '1.0.34', 'grok_path': '/SYNTHETIC/offline/grok'})
    profile = {'source': 'real'}
    timing = {'method': 'segmented_estimate', 'duration_seconds': 5, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'SYNTHETIC fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'SYNTHETIC fixture',
        'segments': [], 'action_windows': [{'source_pointer': f'/shot_contract/shots/{index}/' + key,
            'value_sha256': request.digest(shot[key]), 'start_seconds': start, 'end_seconds': end,
            'rationale': 'SYNTHETIC fixture'} for key, start, end in
            [('dominant_action', .1, 3), ('completed_end_state', 3.1, 4.4)]]}
    judgment = {'version': '1.0', 'reviewer': 'SYNTHETIC offline reviewer', 'status': 'pass',
        'evidence_kind': 'reviewed', 'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical',
            'evidence': 'SYNTHETIC fixture only'} for name in sorted(request.PREDICATES)]}
    compiled, review = request.prepare_compiled_request(inputs, native, profile,
        coverage=authored['coverage'], timing=timing, review=judgment)
    write(root, 'artifacts/compiled_request-' + record_id + '.json', compiled)
    write(root, 'artifacts/preparation_review-' + record_id + '.json', review)
    request.validate_preparation(inputs, native, profile)
    return inputs, native, profile


@pytest.mark.parametrize('preprepare_target', [False, True])
def test_failed_ungenerated_board_corrects_after_selected_motion_and_resumes_original(neutral_package, monkeypatch, preprepare_target):
    root, transport, aid, old_target = late_board_run(neutral_package, monkeypatch, preprepare_target=preprepare_target)
    frozen_attempt = file_bytes(root / 'production_attempts' / aid)
    selected = execution.load_selected_attempts(root)
    original_pack = boards.load_board_pack(root)
    frozen = {name: (root / name).read_bytes() for name in
        ('notes/package.json', 'artifacts/autonomy_policy.json', 'artifacts/decision_log.json')}
    # Bind the completed upstream through the existing mechanics; no board pass is invented.
    ready = execution.advance(root)
    assert (ready['action']['kind'], ready['action']['subject']) == ('prepare_board_correction', 'other'), ready
    corrected = execution.complete_action(root, ready['action'], outcome={
        'changes': 'SYNTHETIC retain the missing prop, preserve story/cast/treatment.',
        'replacement_prompt': 'SYNTHETIC complete corrected composition with the original prop.'})
    envelope = corrected['host_envelope']; original_action = corrected['action']
    assert original_action['kind'] == 'receive_native_image'
    before = file_bytes(root)
    for _ in range(2):
        resumed = execution.advance(root)
        assert resumed['action'] == original_action and 'host_envelope' not in resumed
    assert file_bytes(root) == before
    result = {'image_url': 'data:image/png;base64,' + base64.b64encode(PNG + b'SYNTHETIC correction').decode()}
    receipt = host_receipt(root, envelope, name='late-correction', result=result)
    # Crash after canonical import but before derived board promotion.
    images.import_native_image(root, original_action['subject'], host_receipt=receipt)
    assert execution.next_production_action(root)['kind'] == 'promote_imported_board'
    board_review = execution.advance(root)['action']
    assert (board_review['kind'], board_review['subject']) == ('review_board', 'other')
    assert execution.complete_action(root, original_action, outcome={'host_receipt': receipt})['replayed']
    source = boards.load_board_pack(root)['board_bindings']['other']
    review = attestation(source['sha256'], set(board_review['inputs']['required_predicates']) | {'cast_count'}, revision='r1')
    review.update(review_id='SYNTHETIC-late-correction-review', reviewer='persistent reviewer')
    reviewed = execution.complete_action(root, board_review, outcome={'review': review})
    assert (reviewed['action']['kind'], reviewed['action']['subject']) == ('review_planning', 's3'), '\n'.join(reviewed['action']['inputs'].get('blocked_shots', {}).get('s3', []))
    current = execution.load_shot_contract(root)
    planning = attestation(shot_review_subject(current, 's3'), SHOT_PREDICATES, revision='r1')
    prepared = execution.complete_action(root, reviewed['action'], outcome={'shot_review': planning})
    assert (prepared['action']['kind'], prepared['action']['subject']) == ('prepare_motion', 's3')
    prepare_motion(root, 's3', 'corrected-s3')
    assert len(transport.native_requests) == 1 and images.image_usage(root)['total'] == 5
    assert execution.load_selected_attempts(root) == selected
    assert file_bytes(root / 'production_attempts' / aid) == frozen_attempt
    assert {name: (root / name).read_bytes() for name in frozen} == frozen
    pack = boards.load_board_pack(root)
    assert pack['observations'][:len(original_pack['observations'])] == original_pack['observations']
    assert images.image_call_status(root, 'fixture-other')['state'] == 'imported'
    assert pack['specification'] == original_pack['specification']
    # The retained take still supplies an eligible upstream binding, not just an
    # untouched selection file pointing at unusable history.
    assert execution._validated_upstream_binding(root, current, 's3', 's1', selected['s1']) == current['shots'][1]['upstream'][0]
    if old_target is not None:
        with pytest.raises(ValueError, match='stale|source|binding'):
            request.validate_preparation(*old_target)


@pytest.mark.parametrize('denied', ['image_cap', 'source_batches', 'revoked', 'modified_action', 'expanded_outcome'])
def test_late_correction_denials_preserve_originals_and_cumulative_counts(neutral_package, monkeypatch, denied):
    root, transport, aid, _ = late_board_run(neutral_package, monkeypatch,
        image_allowance=4 if denied == 'image_cap' else 8,
        native_batches=4 if denied == 'source_batches' else 8)
    action = execution.advance(root)['action']
    assert action['kind'] == 'prepare_board_correction'
    frozen_attempt = file_bytes(root / 'production_attempts' / aid)
    selected = execution.load_selected_attempts(root)
    calls = copy.deepcopy(images._load(root))
    outcome = {'changes': 'SYNTHETIC exact missing-prop correction'}
    if denied == 'revoked':
        log = json.loads((root / 'artifacts/decision_log.json').read_text())
        log['decisions'].append(dict(pa.activation_decision(root), decision_id='SYNTHETIC-revoked', selected='strict'))
        write(root, 'artifacts/decision_log.json', log)
    elif denied == 'modified_action':
        action['inputs']['slot_id'] = 'start'
    elif denied == 'expanded_outcome':
        outcome['frame_input'] = {'asset_id': 'start', 'role': 'start_frame'}
    for _ in range(2):
        with pytest.raises((ValueError, execution.ProductionGovernanceError)):
            execution.complete_action(root, action, outcome=outcome)
        assert images._load(root) == calls and images.image_usage(root)['total'] == 4
        assert execution.load_selected_attempts(root) == selected
        assert file_bytes(root / 'production_attempts' / aid) == frozen_attempt
        assert len(transport.native_requests) == 1


def test_late_correction_invalidates_only_actual_dependent_board(neutral_package, monkeypatch):
    root, transport, aid, _ = late_board_run(neutral_package, monkeypatch, dependent_board=True)
    before = boards.load_board_pack(root)
    child = before['board_bindings']['other-end']
    selected = execution.load_selected_attempts(root)
    frozen_attempt = file_bytes(root / 'production_attempts' / aid)
    action = execution.advance(root)['action']
    assert (action['kind'], action['subject']) == ('prepare_board_correction', 'other')
    step = execution.complete_action(root, action, outcome={'changes': 'SYNTHETIC exact missing prop correction'})
    result = {'image_url': 'data:image/png;base64,' + base64.b64encode(PNG + b'SYNTHETIC new parent').decode()}
    receipt = host_receipt(root, step['host_envelope'], name='corrected-parent', result=result)
    reviewed = execution.complete_action(root, step['action'], outcome={'host_receipt': receipt})
    review_action = reviewed['action']
    assert (review_action['kind'], review_action['subject']) == ('review_board', 'other')
    pack = boards.load_board_pack(root)
    review = attestation(pack['board_bindings']['other']['sha256'],
        set(review_action['inputs']['required_predicates']) | {'cast_count'}, revision='r1')
    review.update(review_id='SYNTHETIC-corrected-parent-review', reviewer='persistent reviewer')
    next_step = execution.complete_action(root, review_action, outcome={'review': review})
    assert (next_step['action']['kind'], next_step['action']['subject']) == ('prepare_board_image', 'other-end')
    current = boards.load_board_pack(root)
    assert current['board_bindings']['other-end'] == child
    assert not boards._binding_current(root, current, 'other-end')
    assert file_sha256(root / child['path']) == child['sha256']
    assert images.image_call_status(root, child['call_id'])['state'] == 'imported'
    for slot in ('start', 'end', 'payoff'):
        assert current['board_bindings'][slot] == before['board_bindings'][slot]
        assert boards._binding_current(root, current, slot)
    assert execution.load_selected_attempts(root) == selected
    assert file_bytes(root / 'production_attempts' / aid) == frozen_attempt
    assert len(transport.native_requests) == 1 and images.image_usage(root)['total'] == 6


def test_authorized_source_batch_successor_resumes_late_correction_without_reset(neutral_package, monkeypatch):
    root, transport, aid, _ = late_board_run(neutral_package, monkeypatch, native_batches=4)
    action = execution.advance(root)['action']
    assert (action['kind'], action['subject']) == ('prepare_board_correction', 'other')
    changes = {'changes': 'SYNTHETIC exact missing prop correction'}
    with pytest.raises(ValueError, match='source.*batch|batch.*allowance|batch.*exhaust'):
        execution.complete_action(root, action, outcome=changes)
    policy, policy_sha, _ = pa.require_active_policy(root)
    original_setup = policy['activation_basis']['standing_setup']
    setup = json.loads((root / original_setup['path']).read_text()); setup.pop('authorization')
    next(t for t in setup['source_transfer'] if t['provider'] == 'native_imagegen')['max_batches'] = 8
    candidate = creator_fixture(root, 'source-batches-successor', setup)
    frozen = {name: (root / name).read_bytes() for name in
        ('artifacts/autonomy_policy.json', 'artifacts/decision_log.json', original_setup['path'])}
    frozen_attempt = file_bytes(root / 'production_attempts' / aid)
    selected = execution.load_selected_attempts(root)
    scopes = json.loads((root / 'production_scopes.json').read_text())
    counts = pa.root_attempt_counts(root, policy)
    receipt = pa.amend_standing_source_max_batches(root, expected_policy_sha256=policy_sha,
        expected_setup_sha256=original_setup['sha256'], standing_setup=candidate)
    assert receipt['provider_calls'] == 0 and images.image_usage(root)['total'] == 4
    assert pa.root_attempt_counts(root, policy) == counts
    assert {name: (root / name).read_bytes() for name in frozen} == frozen
    retained = json.loads((root / 'production_scopes.json').read_text())
    assert retained['scopes'] == scopes['scopes'] and retained['image_envelope'] == scopes['image_envelope']
    step = execution.complete_action(root, execution.next_production_action(root), outcome=changes)
    assert step['action']['kind'] == 'receive_native_image' and step['host_envelope']
    resumed = execution.advance(root)
    assert resumed['action'] == step['action'] and 'host_envelope' not in resumed
    assert execution.load_selected_attempts(root) == selected
    assert file_bytes(root / 'production_attempts' / aid) == frozen_attempt
    assert images.image_usage(root)['total'] == 5 and len(transport.native_requests) == 1
