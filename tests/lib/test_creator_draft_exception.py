"""Synthetic offline proof of one creator-accepted duplicate-bill draft boundary."""
import copy
from pathlib import Path
import pytest

from lib import production_execution as execution
from lib.production_draft import append_creator_draft_exception
from lib.production_review import validate_final_review
from lib.shot_contract import file_sha256, UPSTREAM_PREDICATES, selection_digest
from tests.integration.test_first_pass_workflow import production, attestation, save, read  # noqa: F401


def bound(path):
    return {'path': str(path), 'sha256': file_sha256(path)}


@pytest.fixture(params=["prop_body_invariants", "transformation"])
def accepted(production, request):
    p = production
    result = p.generate('entry')
    assert result.success
    aid = result.data['production_attempt_id']
    state = execution.load_attempt_result(p.root, aid)
    frame = p.root / 'assets/images/clean-outgoing.bin'
    frame.write_bytes(b'SYNTHETIC independently reviewed clean single bill endpoint')
    selection = {'attempt_id': aid, 'output': state['output'], 'outgoing_frame': bound(frame)}
    failure = attestation(state['output']['sha256'], UPSTREAM_PREDICATES | {'prop_body_invariants'})
    failure['status'] = 'fail'
    predicate = next(x for x in failure['predicates'] if x['name'] == request.param)
    predicate.update(status='fail', severity='critical', evidence='SYNTHETIC duplicate bill during clip; endpoint clean.')
    execution.record_rejection(p.root, aid, failure)
    failure_path = next((p.root/'production_attempts'/aid/'rejections').glob('*.json'))
    snapshot = p.root / 'accepted-contract.json'; save(snapshot, p.contract)
    evidence = p.root / 'creator-acceptance.txt'
    evidence.write_text('SYNTHETIC creator accepts exact existing entry duplicate bill for draft only.')
    outgoing = attestation(file_sha256(frame), UPSTREAM_PREDICATES | {'prop_body_invariants'})
    record = {'version':'1.0', 'mode':'creator_accepted_existing_footage_draft',
              'project_id':p.root.name, 'story_revision':p.story['story_revision'],
              'shot_id':'entry', 'attempt_id':aid, 'contract':bound(snapshot),
              'output':state['output'], 'outgoing_frame':bound(frame), 'failed_review':bound(failure_path),
              'failed_predicate':request.param, 'defect':'duplicate_bill',
              'accepted_by':'synthetic creator', 'acceptance_evidence':bound(evidence), 'outgoing_review':outgoing}
    frozen = {name:(p.root/'production_attempts'/aid/name).read_bytes()
              for name in ('request.json','result.json','shot_contract.json')}
    scopes = (p.root/'production_scopes.json').read_bytes()
    return p, record, selection, failure, frozen, scopes


def authorize(accepted):
    p, record, selection, failure, _, _ = accepted
    reference = append_creator_draft_exception(p.root, record)
    selection['review'] = copy.deepcopy(failure)
    selection['review'].update(review_id='synthetic-draft-selection', status='provisional',
                              subject_sha256=selection_digest(selection), creator_draft_exception=reference)
    return reference


def test_existing_failure_selected_downstream_frozen_replay_and_final_blocked(accepted):
    p, record, selection, failure, frozen, scopes = accepted
    authorize(accepted)
    execution.record_selection(p.root, 'entry', selection)
    for sid in ('interior', 'payoff'):
        p.bind_upstream(sid)
        result = p.generate(sid)
        assert result.success, result.error
        p.select(sid, result)
    master, _ = p.draft()
    final = p.full_review(master)
    checked = validate_final_review(p.root, final)
    assert not checked['eligible'] and checked['release_status'] == 'draft'
    assert any('failed/stale selection review' in error or 'prop_body_invariants' in error for error in checked['errors'])
    assert len(execution._attempts(p.root)) == len(p.transport.native_requests) == 3
    assert (p.root/'production_scopes.json').read_bytes() == scopes
    for name, raw in frozen.items():
        assert (p.root/'production_attempts'/record['attempt_id']/name).read_bytes() == raw
    assert read(Path(record['failed_review']['path'])) == failure
    assert execution.load_selected_attempts(p.root)['entry']['review']['status'] == 'provisional'


@pytest.mark.parametrize('change', ['shot','output','outgoing','failure','approval','story','target_plan',
                                  'outgoing_fail','missing_outgoing','other_failure','predicate','contract'])
def test_invalid_acceptance_does_not_change_counters_or_call_provider(accepted, change):
    p, record, selection, _, _, _ = accepted
    original_calls = len(p.transport.native_requests)
    if change == 'shot': record['shot_id'] = 'interior'
    elif change in {'output','outgoing'}: record['outgoing_frame' if change=='outgoing' else 'output']['sha256'] = 'a'*64
    elif change in {'failure','approval'}:
        key = 'failed_review' if change=='failure' else 'acceptance_evidence'
        path = Path(record[key]['path']); path.chmod(0o644); path.write_text('tampered')
    elif change == 'story': record['story_revision'] = 'stale'
    elif change == 'target_plan':
        p.contract['shots'][0]['dominant_action'] += ' changed'; p.persist_contract()
    elif change == 'outgoing_fail': record['outgoing_review']['predicates'][0]['status'] = 'fail'
    elif change == 'missing_outgoing': record['outgoing_review']['predicates'].pop()
    elif change == 'predicate': record['failed_predicate'] = 'cast_identity'
    elif change == 'contract': record['contract']['sha256'] = 'b'*64
    else:
        authorize(accepted)
        selection['review']['predicates'][0]['status'] = 'unknown'
        with pytest.raises(execution.ProductionGovernanceError): execution.record_selection(p.root,'entry',selection)
        assert len(p.transport.native_requests) == original_calls
        return
    with pytest.raises((ValueError, execution.ProductionGovernanceError)):
        append_creator_draft_exception(p.root, record)
    assert len(execution._attempts(p.root)) == len(p.transport.native_requests) == original_calls


@pytest.mark.parametrize('change', ['acceptance','output','outgoing','rejection','record','target_plan','wrong_shot'])
def test_selected_exception_tamper_blocks_downstream_zero_calls(accepted, change):
    p, record, selection, _, _, _ = accepted
    reference = authorize(accepted)
    execution.record_selection(p.root, 'entry', selection)
    p.bind_upstream('interior')
    if change == 'target_plan':
        p.contract['shots'][0]['prop_body_invariants'] = ['changed invariant']; p.persist_contract()
    elif change == 'wrong_shot':
        with pytest.raises(execution.ProductionGovernanceError): execution.record_selection(p.root,'interior',selection)
        return
    else:
        key = {'acceptance':'acceptance_evidence','output':'output','outgoing':'outgoing_frame','rejection':'failed_review'}.get(change)
        path = Path(record[key]['path'] if key else reference['path'])
        path.chmod(0o644); path.write_bytes(b'tampered')
    with pytest.raises(execution.ProductionGovernanceError): p.generate('interior')
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


@pytest.mark.parametrize('audio_authority', ['absent', 'valid', 'tampered'])
def test_visual_acceptance_never_grants_audio_authority(accepted, audio_authority):
    from tests.lib.test_draft_audio_review import authorize as authorize_audio
    p, _, selection, _, _, _ = accepted
    authorize(accepted)
    speaker = next(x for x in selection['review']['predicates'] if x['name'] == 'speaker_source')
    speaker.update(status='unknown', evidence='SYNTHETIC audio unavailable; not passing.')
    if audio_authority != 'absent':
        selection['review']['draft_policy_sha256'] = authorize_audio(p)
        if audio_authority == 'tampered':
            (p.root/'draft-approval.txt').write_text('tampered authority')
    if audio_authority == 'valid':
        execution.record_selection(p.root, 'entry', selection)
        p.bind_upstream('interior')
        result = p.generate('interior')
        assert result.success, result.error
        p.select('interior', result)  # Includes frozen upstream provenance replay.
        assert len(p.transport.native_requests) == 2
    else:
        with pytest.raises(execution.ProductionGovernanceError):
            execution.record_selection(p.root,'entry',selection)
        assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


@pytest.mark.parametrize('mode', ['two_in_original', 'separate_rejection', 'unknown_in_other', 'changed_named_finding'])
def test_exception_cannot_hide_other_original_critical_findings(accepted, mode):
    p, record, _, failure, _, _ = accepted
    other = copy.deepcopy(failure)
    other['review_id'] = 'synthetic-other-rejection'
    if mode == 'changed_named_finding':
        next(x for x in other['predicates'] if x['name'] == record['failed_predicate'])['evidence'] = 'Different unaccepted critical finding'
    else:
        next(x for x in other['predicates'] if x['name'] == 'cast_identity').update(
            status='unknown' if mode == 'unknown_in_other' else 'fail', severity='critical',
            evidence='SYNTHETIC other critical cast problem; creator did not accept.')
    execution.record_rejection(p.root, record['attempt_id'], other)
    if mode == 'two_in_original':
        path = next(path for path in (p.root/'production_attempts'/record['attempt_id']/'rejections').glob('*.json')
                    if read(path)['review_id'] == other['review_id'])
        record['failed_review'] = bound(path)
    with pytest.raises(ValueError, match='another retained critical finding'):
        append_creator_draft_exception(p.root, record)
    assert len(execution._attempts(p.root)) == len(p.transport.native_requests) == 1


@pytest.mark.parametrize('audio_authority', [False, True])
def test_retained_speaker_unknown_requires_original_audio_authority(accepted, audio_authority):
    from tests.lib.test_draft_audio_review import authorize as authorize_audio
    p, record, selection, failure, _, _ = accepted
    next(x for x in failure['predicates'] if x['name']=='speaker_source').update(
        status='unknown', evidence='SYNTHETIC originally unavailable auditory evidence.')
    execution.record_rejection(p.root, record['attempt_id'], failure)
    path = next(path for path in (p.root/'production_attempts'/record['attempt_id']/'rejections').glob('*.json')
                if read(path) == failure)
    record['failed_review'] = bound(path)
    if audio_authority:
        audio = authorize_audio(p)
        authorize(accepted)
        selection['review']['draft_policy_sha256'] = audio
        execution.record_selection(p.root, 'entry', selection)
        p.bind_upstream('interior')
        result = p.generate('interior')
        assert result.success, result.error
        p.select('interior', result)
    else:
        with pytest.raises(ValueError, match='another retained critical finding'):
            authorize(accepted)
        assert len(execution._attempts(p.root)) == len(p.transport.native_requests) == 1
