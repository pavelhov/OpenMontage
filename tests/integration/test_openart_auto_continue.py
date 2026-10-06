"""Offline Auto-continue regression boundaries; no production qualification claims."""
import copy
import json

import pytest

from lib import production_request as request
from lib.shot_contract import file_sha256
from tests.lib.test_production_request import package  # noqa: F401


def test_historical_preparation_preserves_facts_after_current_planning_change(package):
    root, inputs, native, profile, _, _ = package
    proof = request.freeze_preparation('historical', inputs, native, profile)
    saved = {'attempt_id': 'historical', 'scope_id': 'approved', 'shot_id': 'entry',
             'input_assets': [{'role': 'image_path', 'path': inputs['image_path'],
                              'original_path': inputs['image_path'], 'sha256': file_sha256(inputs['image_path'])}],
             'openart': {'preparation_snapshot': proof}}
    frozen = {'inputs': inputs, 'native': native, 'profile': profile}
    assert request.validate_frozen_preparation_history(saved, frozen, root) == proof
    script_path = root / 'artifacts/script.json'
    script = json.loads(script_path.read_text())
    script['title'] = 'Current changed planning'
    script_path.write_text(json.dumps(script))
    with pytest.raises(ValueError, match='stale source'):
        request.validate_frozen_preparation(saved, frozen, root)
    assert request.validate_frozen_preparation_history(saved, frozen, root) == proof
    corrupt = copy.deepcopy(frozen)
    corrupt['native']['native_body_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        request.validate_frozen_preparation_history(saved, corrupt, root)


@pytest.mark.parametrize('actor_voice', [False, True], ids=['noncast-flex', 'actor-reference-lock'])
def test_grok_preparation_uses_real_builder_and_closed_native_arm(package, monkeypatch, actor_voice):
    from lib import production_execution as execution, production_autonomy as autonomy
    from tests.lib.test_production_autonomy import make_policy, install_existing
    from PIL import Image
    root, _, _, _, _, _ = package
    contract_path = root / 'artifacts/shot_contract.json'
    contract = json.loads(contract_path.read_text())
    for asset in contract['assets']:
        target = root / ('assets/' + asset['id'] + '.png')
        Image.new('RGB', (8, 8), 'white').save(target)
        asset['path'] = str(target.relative_to(root))
        asset['sha256'] = file_sha256(target)
        asset['review']['subject_sha256'] = asset['sha256']
    for ident, color in [('voice-a', 'red'), ('voice-b', 'blue')]:
        target = root / f'assets/{ident}.png'
        Image.new('RGB', (8, 8), color).save(target)
        asset = copy.deepcopy(contract['assets'][0])
        asset.update(id=ident, role='voice_reference', cast_ids=['patient'] if actor_voice and ident == 'voice-a' else [], path=str(target.relative_to(root)), sha256=file_sha256(target))
        asset['review']['subject_sha256'] = asset['sha256']
        contract['assets'].append(asset)
        contract['shots'][0]['asset_ids'].append(ident)
    from lib.shot_contract import contract_digest
    sha = contract_digest(contract)
    contract['project_review']['subject_sha256'] = sha
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = sha
    second = copy.deepcopy(contract['shots'][0])
    second['id'] = 'other'
    contract['shots'].append(second)
    sha = contract_digest(contract)
    contract['project_review']['subject_sha256'] = sha
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = sha
    contract_path.write_text(json.dumps(contract))
    scene_path = root / 'artifacts/scene_plan.json'
    scene_plan = json.loads(scene_path.read_text())
    scene_plan['scenes'].append(dict(scene_plan['scenes'][0], id='other', script_section_id='s2', start_seconds=8, end_seconds=16))
    scene_path.write_text(json.dumps(scene_plan))
    script_path = root / 'artifacts/script.json'
    script = json.loads(script_path.read_text())
    script['sections'].append(dict(script['sections'][0], id='s2', start_seconds=8, end_seconds=16))
    script['total_duration_seconds'] = 16
    script_path.write_text(json.dumps(script))
    inputs = {'project_dir': str(root), 'governance': {'scope_id': 'grok-approved', 'shot_id': 'entry'},
              'operation': 'first_last_frame', 'first_frame': str(root / 'assets/start.png'),
              'last_frame': str(root / 'assets/end.png'), 'duration': 8, 'resolution': '720p',
              'reference_image_paths': [str(root / f'assets/{ident}.png') for ident in ('voice-a', 'voice-b')],
              'output_path': str(root / 'grok.mp4'), 'compiled_request_id': 'g1',
              'preparation_review_id': 'gr1', 'allow_unknown_cost': True}
    policy = make_policy(checkpoint_stages=[], locked={'cast': {}, 'dialogue': {}, 'sources': [],
                                                      'story_predicates': [], 'controls': {}})
    policy['flex']['references']['droppable_roles'] = [] if actor_voice else ['voice_reference']
    template = execution.planned_request_template(inputs, project_dir=root)
    other_inputs = copy.deepcopy(inputs)
    other_inputs.update(output_path=str(root / 'other.mp4'), compiled_request_id='g2', preparation_review_id='gr2')
    other_inputs['governance']['shot_id'] = 'other'
    install_existing(root, policy, templates={'entry': template, 'other': execution.planned_request_template(other_inputs, project_dir=root)})
    assert autonomy.require_active_policy(root)
    prompt = request.compile_provider_prompt(root, 'entry', provider='grok_cli')
    inputs['prompt'] = prompt['prompt']
    native = request.prep_builder('grok_cli')(inputs, {'cli_version': '1.0.34', 'grok_path': '/offline/grok'})
    assert native['request']['arguments']['duration'] == 8
    assert native['request']['receipt']['media_model'] is None
    assert 'form_sha256' not in native
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
              'rationale': 'Synthetic offline feasibility evidence', 'overlap_policy': 'serial',
              'overlap_rationale': 'Synthetic separate windows',
              'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key,
                  'value_sha256': request.digest(contract['shots'][0][key]),
                  'start_seconds': start, 'end_seconds': end, 'rationale': 'Synthetic action'}
                  for key, start, end in [('dominant_action', 4.1, 7), ('completed_end_state', 7, 7.4)]],
              'segments': [{'dialogue_index': i, 'text_sha256': request.digest(line['text']), 'language': 'en',
                  'word_count': 2, 'words_per_minute': 180, 'pause_seconds': 0, 'rationale': 'Synthetic rate'}
                  for i, line in enumerate(contract['shots'][0]['dialogue'])]}
    compiled = request.prepare_compiled_request(inputs, native, {'source': 'fixture'}, coverage=prompt['coverage'], timing=timing)
    assert compiled['native_binding'] == native
    bad = copy.deepcopy(compiled)
    bad['native_binding']['model'] = 'invented-media-model'
    with pytest.raises(ValueError):
        request._validate_compiled(bad, inputs, native, {'source': 'fixture'},
                                   request.source_packet(root, 'entry', provider='grok_cli'))
    (root / 'artifacts/compiled_request-g1.json').write_text(json.dumps(compiled))
    board = {'sha256': contract['assets'][0]['sha256'], 'role': 'start_frame',
             'members': [{'sha256': a['sha256'], 'role': a['role'], 'cast_ids': a['cast_ids']}
                         for a in contract['assets'] if a['role'] in autonomy.CAST_ROLES]}
    review = {'version': '1.0', 'review_id': 'gr1', 'reviewer': 'Synthetic offline fixture author',
              'subject_sha256': request.digest(compiled), 'status': 'pass', 'evidence_kind': 'reviewed',
              'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical',
                              'evidence': 'Synthetic offline declared pass'} for name in sorted(request.PREDICATES)],
              'composite_boards': [board]}
    review_path = root / 'artifacts/preparation_review-gr1.json'
    review_path.write_text(json.dumps(review))
    assert len(autonomy.lock_proof(root, 'entry', inputs=inputs, native=native, profile={'source': 'real'})) == 64
    review['composite_boards'][0]['members'] = []
    review_path.write_text(json.dumps(review))
    with pytest.raises(ValueError):
        autonomy.lock_proof(root, 'entry', inputs=inputs, native=native, profile={'source': 'real'})
    review['composite_boards'] = [board]
    board['members'] = [{'sha256': a['sha256'], 'role': a['role'], 'cast_ids': a['cast_ids']}
                        for a in contract['assets'] if a['role'] in autonomy.CAST_ROLES]
    review_path.write_text(json.dumps(review))
    if actor_voice:
        candidate = copy.deepcopy(inputs)
        candidate['reference_image_paths'] = candidate['reference_image_paths'][1:]
        candidate_native = request.prepare_grok_native(candidate, {'cli_version': '1.0.34', 'grok_path': '/offline/grok'})
        candidate_compiled = request.prepare_compiled_request(candidate, candidate_native, {'source': 'real'},
                                                             coverage=prompt['coverage'], timing=timing)
        candidate_review = copy.deepcopy(review)
        candidate_review['subject_sha256'] = request.digest(candidate_compiled)
        (root / 'artifacts/compiled_request-g1.json').write_text(json.dumps(candidate_compiled))
        review_path.write_text(json.dumps(candidate_review))
        request.validate_preparation(candidate, candidate_native, {'source': 'real'})
        with pytest.raises(ValueError, match='implicit locked'):
            autonomy.lock_proof(root, 'entry', inputs=candidate, native=candidate_native, profile={'source': 'real'})
        return
    # Each extra declared lock is independently proved against actual native,
    # canonical dialogue/story and the reviewed composite board, even when the
    # implicit-only policy has already passed genuine preparation.
    original_authority = {p: p.read_bytes() for p in (root / 'artifacts/autonomy_policy.json', root / 'artifacts/decision_log.json')}
    for key, value in [
        ('cast', {'patient': [{'sha256': 'f' * 64, 'role': 'start_frame'}]}),
        ('dialogue', {'0': {'text_sha256': 'f' * 64, 'speaker_id': 'patient'}}),
        ('sources', ['f' * 64]),
        ('story_predicates', [{'id': 'missing-story-fact', 'sha256': 'f' * 64}]),
        ('controls', {'duration': {'value': 7}}),
        ('controls', {'start_frame': {'value_sha256': request.digest(contract['assets'][0]['sha256'])}}),
    ]:
        invalid_policy = copy.deepcopy(policy)
        invalid_policy['locked'][key] = value
        if key == 'controls' and 'duration' in value:
            invalid_policy['flex']['duration_s'] = []
        install_existing(root, invalid_policy, templates={'entry': template, 'other': execution.planned_request_template(other_inputs, project_dir=root)})
        with pytest.raises(ValueError):
            autonomy.lock_proof(root, 'entry', inputs=inputs, native=native, profile={'source': 'real'})
        for path, raw in original_authority.items():
            path.write_bytes(raw)
    assert len(autonomy.lock_proof(root, 'entry', inputs=inputs, native=native, profile={'source': 'real'})) == 64
    observation = {'cli_version': '1.0.34', 'grok_path': '/offline/grok'}
    scope = autonomy.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    inputs['governance']['scope_id'] = scope['id']
    assert autonomy.validate_derived_scope(root, scope, inputs=inputs, observation=observation)
    with pytest.raises(autonomy.AutonomyError, match='undispatched'):
        autonomy.derive_scope(root, inputs, provider='grok_cli', observation=observation)
    forged = copy.deepcopy(scope)
    forged['attempts_per_shot']['entry'] = 2
    with pytest.raises(autonomy.AutonomyError):
        autonomy.validate_derived_scope(root, forged, inputs=inputs, observation=observation)

    from tests.integration.test_first_pass_workflow import NativeTransport
    from tools.video.grok_cli_video import GrokCLIVideo
    from tools.tool_registry import ToolRegistry
    transport = NativeTransport(root)
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', transport)
    monkeypatch.setattr('tools._grok_cli_media.shutil.which', lambda name: '/offline/' + name)
    tool = GrokCLIVideo(grok_path='grok', sessions_root=str(root / 'sessions'))
    registry = ToolRegistry()
    registry.register(tool)
    result = registry.get('grok_cli_video').execute(inputs)
    assert result.success, result.error
    assert len(transport.native_requests) == 1
    assert result.data['production_attempt_id']

    aid = result.data['production_attempt_id']
    retained_result = execution.load_attempt_result(root, aid)
    from lib.production_provenance import validate_attempt_provenance
    verified = validate_attempt_provenance(root, aid, shot_id='entry', story_revision=contract['story_revision'],
                                          expected_output=retained_result['output'])
    assert verified['request']['scope_id'] == scope['id']

    from tests.integration.test_first_pass_workflow import attestation, PIXEL
    from lib.shot_contract import UPSTREAM_PREDICATES, selection_digest, review_digest, PROJECT_PREDICATES, SHOT_PREDICATES
    def select(shot_id, generated):
        attempt_id = generated.data['production_attempt_id']
        state = execution.load_attempt_result(root, attempt_id)
        frame = root / 'assets' / (shot_id + '-observed.png')
        frame.write_bytes(PIXEL)
        chosen = {'attempt_id': attempt_id, 'output': state['output'],
                  'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
        chosen['review'] = attestation(selection_digest(chosen), UPSTREAM_PREDICATES, revision=contract['story_revision'])
        execution.record_selection(root, shot_id, chosen)
        return chosen
    first_selection = select('entry', result)
    from lib.production_retime import retime_planning
    updated = retime_planning(contract, scene_plan, script, {'other': 6})
    current_contract = updated['contract']
    new_sha = contract_digest(current_contract)
    current_contract['project_review'] = attestation(new_sha, PROJECT_PREDICATES, revision=contract['story_revision'])
    for shot in current_contract['shots']:
        shot['review'] = attestation(new_sha, SHOT_PREDICATES, revision=contract['story_revision'])
    contract_path.write_text(json.dumps(current_contract))
    scene_path.write_text(json.dumps(updated['scene_plan']))
    script_path.write_text(json.dumps(updated['script']))
    assert autonomy.require_active_policy(root)
    assert updated['script']['total_duration_seconds'] == 14
    assert validate_attempt_provenance(root, aid, shot_id='entry', story_revision=contract['story_revision'],
                                       expected_output=retained_result['output'])
    other_inputs['duration'] = 6
    other_inputs['reference_image_paths'] = other_inputs['reference_image_paths'][1:]
    # Legal partial drop retains the immutable original asset pool and requires
    # a newly compiled actual native request and fresh named preparation review.
    other_prompt = request.compile_provider_prompt(root, 'other', provider='grok_cli')
    other_inputs['prompt'] = other_prompt['prompt']
    other_native = request.prepare_grok_native(other_inputs, observation)
    other_timing = copy.deepcopy(timing)
    other_timing['duration_seconds'] = 6
    other_timing['margin_seconds'] = .3
    for window in other_timing['action_windows']:
        window['source_pointer'] = window['source_pointer'].replace('/shots/0/', '/shots/1/')
        window['start_seconds'] *= .75
        window['end_seconds'] *= .75
    stale_prepared_inputs = dict(other_inputs, compiled_request_id='g1', preparation_review_id='gr1')
    with pytest.raises(ValueError):
        request.validate_preparation(stale_prepared_inputs, other_native, {'source': 'real'})
    other_compiled = request.prepare_compiled_request(other_inputs, other_native, {'source': 'real'},
                                                       coverage=other_prompt['coverage'], timing=other_timing)
    (root / 'artifacts/compiled_request-g2.json').write_text(json.dumps(other_compiled))
    other_review = copy.deepcopy(review)
    other_review.update(review_id='gr2', subject_sha256=request.digest(other_compiled))
    (root / 'artifacts/preparation_review-gr2.json').write_text(json.dumps(other_review))
    second_scope = autonomy.derive_scope(root, other_inputs, provider='grok_cli', observation=observation)
    other_inputs['governance']['scope_id'] = second_scope['id']
    second_result = registry.get('grok_cli_video').execute(other_inputs)
    assert second_result.success, second_result.error
    second_selection = select('other', second_result)
    assert len(transport.native_requests) == 2
    master = root / 'master.mp4'
    master.write_bytes(b'Synthetic master assembled from actual offline native result files')
    from lib.production_review import certify_final
    monkeypatch.setattr('lib.production_review.probe_master', lambda path: {'duration_seconds': 14})
    final = {'version': '2.0', 'project_id': current_contract['project_id'],
             'story_revision': current_contract['story_revision'], 'contract_sha256': contract_digest(current_contract),
             'output_path': str(master), 'output_sha256': file_sha256(master), 'duration_seconds': 14,
             'release_status': 'final', 'reviewer': {'id': 'synthetic-author', 'kind': 'agent',
                'method': 'Synthetic offline AV attestation, no production quality claim', 'reviewed_at': '2026-10-06T00:00:00Z'},
             'dimensions': {name: {'status': 'pass', 'evidence': 'Synthetic offline evidence'}
                 for name in ('transport', 'technical', 'visual', 'audio', 'story')},
             'av_review': {'status': 'pass', 'mode': 'synchronized_av', 'watched_full': True,
                 'listened_full': True, 'start_seconds': 0, 'end_seconds': 14, 'evidence': 'Synthetic offline evidence'},
             'scenes': [{'scene_id': sid, 'attempt_id': chosen['attempt_id'], 'output_sha256': chosen['output']['sha256'],
                 'selection_sha256': selection_digest(chosen), 'review_sha256': review_digest(chosen['review']),
                 'start_seconds': start, 'end_seconds': end}
                 for sid, chosen, start, end in [('entry', first_selection, 0, 8), ('other', second_selection, 8, 14)]],
             'predicates': attestation('0' * 64, PROJECT_PREDICATES, revision=contract['story_revision'])['predicates']}
    certify_final(root, final)

    report_path = autonomy.completion_report(root, autonomy.require_active_policy(root)[1])
    report = json.loads(report_path.read_bytes())
    assert len(report['attempts']) == 2
    assert {row['status'] for row in report['attempts']} == {'generated'}

    def reject_provenance_mutation(path, transform):
        raw = path.read_bytes()
        path.chmod(0o600)
        value = json.loads(raw)
        transform(value)
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            validate_attempt_provenance(root, aid, shot_id='entry', story_revision=contract['story_revision'],
                                        expected_output=retained_result['output'])
        path.write_bytes(raw)
    reject_provenance_mutation(contract_path, lambda c: c['shots'][0].update(purpose='Unapproved own-shot story drift'))
    reject_provenance_mutation(root / 'artifacts/decision_log.json',
                              lambda log: log['decisions'][0].update(selected='strict'))
    reject_provenance_mutation(root / 'production_attempts' / aid / 'autonomy_preparation.json',
                              lambda p: p['native']['request']['arguments'].update(duration=6))
    reject_provenance_mutation(root / 'production_attempts' / aid / 'autonomy_preparation.json',
                              lambda p: p['review'].update(status='fail'))
    reject_provenance_mutation(root / 'production_attempts' / aid / 'request.json',
                              lambda r: r['scope'].update(derived_from_policy=None))
    assert validate_attempt_provenance(root, aid, shot_id='entry', story_revision=contract['story_revision'],
                                       expected_output=retained_result['output'])
    counts = autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])
    assert counts == {'total': 2, 'per_shot': {'entry': 1, 'other': 1}, 'repair': 0, 'unmapped_outbox': []}
    capped = copy.deepcopy(autonomy.require_active_policy(root)[0])
    capped['caps']['max_total_attempts'] = 2
    with pytest.raises(ValueError, match='total attempts'):
        autonomy.check_caps(capped, 'other', counts, 'repair')
    journal = root / 'production_attempts' / aid / 'request.json'
    original_journal = journal.read_bytes()
    journal.chmod(0o600)
    bad = json.loads(original_journal)
    bad['media_kind'] = None
    journal.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match='unknown production kind'):
        autonomy.root_attempt_counts(root, capped)
    journal.write_bytes(original_journal)
    # No public status can erase an already consumed attempt slot.
    result_path = root / 'production_attempts' / aid / 'result.json'
    original_result = result_path.read_bytes()
    result_path.chmod(0o600)
    for status in ('pending', 'uncertain', 'failed', 'generated', 'terminal_unselected'):
        result_path.write_text(json.dumps({'status': status}))
        assert autonomy.root_attempt_counts(root, capped)['total'] == 2
    result_path.write_bytes(original_result)
    with pytest.raises(ValueError):
        autonomy._validate_repair_evidence(root, 'entry', [aid, aid])
    with pytest.raises(ValueError):
        autonomy._validate_repair_evidence(root, 'other', [aid])
    with pytest.raises(ValueError):
        autonomy._validate_repair_evidence(root, 'entry', [aid])
    from lib.production_execution import record_rejection
    failed_review = copy.deepcopy(first_selection['review'])
    failed_review['status'] = 'fail'
    failed_review['subject_sha256'] = retained_result['output']['sha256']
    record_rejection(root, aid, failed_review)
    autonomy._validate_repair_evidence(root, 'entry', [aid])
    repair_inputs = copy.deepcopy(inputs)
    repair_inputs.update(output_path=str(root / 'repair.mp4'), compiled_request_id='g3', preparation_review_id='gr3')
    repair_prompt = request.compile_provider_prompt(root, 'entry', provider='grok_cli')
    repair_inputs['prompt'] = repair_prompt['prompt']
    repair_native = request.prepare_grok_native(repair_inputs, observation)
    repair_compiled = request.prepare_compiled_request(repair_inputs, repair_native, {'source': 'real'},
                                                       coverage=repair_prompt['coverage'], timing=timing)
    repair_review = copy.deepcopy(review)
    repair_review.update(review_id='gr3', subject_sha256=request.digest(repair_compiled))
    (root / 'artifacts/compiled_request-g3.json').write_text(json.dumps(repair_compiled))
    (root / 'artifacts/preparation_review-gr3.json').write_text(json.dumps(repair_review))
    repair_scope = autonomy.derive_scope(root, repair_inputs, provider='grok_cli', observation=observation,
                                         phase='repair', replaces_attempt_ids=[aid])
    repair_inputs['governance']['scope_id'] = repair_scope['id']
    repair_result = registry.get('grok_cli_video').execute(repair_inputs)
    assert repair_result.success, repair_result.error
    assert autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])['repair'] == 1
    repair_journal = root / 'production_attempts' / repair_result.data['production_attempt_id'] / 'request.json'
    raw_repair_journal = repair_journal.read_bytes()
    repair_journal.chmod(0o600)
    corrupted = json.loads(raw_repair_journal)
    corrupted['phase'] = 'first_pass'
    repair_journal.write_text(json.dumps(corrupted))
    with pytest.raises(ValueError, match='phase'):
        autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])
    repair_journal.write_bytes(raw_repair_journal)
    valid_counts = autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])
    repair_cap_policy = copy.deepcopy(autonomy.require_active_policy(root)[0])
    repair_cap_policy['caps'].update(max_total_attempts=99, max_attempts_per_shot=99)
    with pytest.raises(ValueError, match='repair attempts'):
        autonomy.check_caps(repair_cap_policy, 'entry', valid_counts, 'repair')
    for mutate in (
        lambda r: r.update(phase=None),
        lambda r: (r.update(phase='first_pass'), r['scope'].update(phase='first_pass')),
        lambda r: r.update(scope_id='missing-retained-scope'),
        lambda r: r.update(request_sha256='f' * 64),
    ):
        corrupted = json.loads(raw_repair_journal)
        mutate(corrupted)
        repair_journal.write_text(json.dumps(corrupted))
        with pytest.raises(ValueError):
            autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])
        repair_journal.write_bytes(raw_repair_journal)
    scopes_path = root / 'production_scopes.json'
    raw_scopes = scopes_path.read_bytes()
    scopes = json.loads(raw_scopes)
    scopes['scopes'].append(copy.deepcopy(repair_scope))
    scopes_path.write_text(json.dumps(scopes))
    with pytest.raises(ValueError, match='duplicate scope'):
        autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])
    scopes_path.write_bytes(raw_scopes)
    rejection_path = next((root / 'production_attempts' / aid / 'rejections').glob('*.json'))
    failed_review['subject_sha256'] = 'f' * 64
    rejection_path.chmod(0o600)
    rejection_path.write_text(json.dumps(failed_review))
    with pytest.raises(ValueError):
        autonomy._validate_repair_evidence(root, 'entry', [aid])


def test_named_board_end_reference_does_not_satisfy_native_end_pin(package):
    from lib import production_autonomy as autonomy, production_execution as execution
    from tests.lib.test_production_autonomy import make_policy, install_existing
    root, inputs, native, profile, compiled, review = package
    projection = autonomy.current_projection(root, 'entry')
    actor_ids = set(projection['implicit_cast_ids'])
    members = [{'sha256': a['sha256'], 'role': a['role'], 'cast_ids': a['cast_ids']}
               for a in projection['assets'] if actor_ids.intersection(a['cast_ids'])]
    assert any(member['role'] == 'end_frame' for member in members)
    board = {'sha256': file_sha256(inputs['image_path']), 'role': 'start_frame', 'members': members}
    review['composite_boards'] = [board]
    request._schema('preparation_review', review)
    (root / 'artifacts/preparation_review-r1.json').write_text(json.dumps(review))
    policy = make_policy(checkpoint_stages=[], locked={'cast': {}, 'dialogue': {}, 'sources': [],
                                                      'story_predicates': [], 'controls': {}})
    template = execution.planned_request_template(inputs, project_dir=root)
    install_existing(root, policy, templates={'entry': template})
    assert len(autonomy.lock_proof(root, 'entry', inputs=inputs, native=native, profile=profile)) == 64
    end_sha = next(member['sha256'] for member in members if member['role'] == 'end_frame')
    policy['locked']['controls'] = {'end_frame': {'value_sha256': end_sha}}
    install_existing(root, policy, templates={'entry': template})
    with pytest.raises(ValueError, match='native locked control absent: end_frame'):
        autonomy.lock_proof(root, 'entry', inputs=inputs, native=native, profile=profile)
    unsupported = copy.deepcopy(review)
    unsupported['composite_boards'][0]['members'].append({'sha256': end_sha, 'role': 'voice_reference', 'cast_ids': ['patient']})
    with pytest.raises(ValueError):
        request._schema('preparation_review', unsupported)
