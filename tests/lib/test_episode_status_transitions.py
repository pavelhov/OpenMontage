"""Canonical episode delivery transitions through real offline artifacts."""
from lib.episode_production_controls import episode_production_status
from lib.production_execution import load_selected_attempts, record_selection
from lib.production_review import certify_final
from lib.production_review_successors import record_review_successor, resolve_selection_review
from lib.shot_contract import review_digest
from tests.lib.test_draft_audio_review import production, authorize, candidate
from tests.lib.test_production_review_successors import upgrade


def test_canonical_episode_status_transitions(production):
    p = production
    policy = authorize(p)
    for shot in p.contract['shots']:
        sid = shot['id']
        if shot['upstream']:
            p.bind_upstream(sid)
        result = p.generate(sid)
        assert result.success, result.error
        selection = candidate(p, sid, result, policy)
        if sid == 'entry':
            assert next(x for x in selection['review']['predicates']
                        if x['name'] == 'speaker_source')['status'] == 'unknown'
            selection['review']['predicates'].append({
                'name': 'cosmetic_detail', 'status': 'fail', 'severity': 'cosmetic',
                'evidence': 'Synthetic minor nonessential detail',
            })
        record_selection(p.root, sid, selection)

    native_calls = len(p.transport.native_requests)
    before_outputs = {sid: selection['output']['sha256']
                      for sid, selection in load_selected_attempts(p.root).items()}
    status = episode_production_status(p.root)
    assert status['shot_readiness']['entry'] == 'pending'
    assert status['delivery'] == 'draft_only'

    selected = load_selected_attempts(p.root)
    review, evidence = upgrade(p, 'entry')
    record_review_successor(p.root, 'entry', review, evidence=evidence)
    status = episode_production_status(p.root)
    assert status['shot_readiness']['entry'] == 'ready_with_warnings'
    assert status['selections']['entry']['warnings'] == ['cosmetic_detail']
    assert status['selections']['entry']['critical'] == []
    assert status['delivery'] == 'draft_only'
    assert len(p.transport.native_requests) == native_calls
    assert {sid: s['output']['sha256'] for sid, s in load_selected_attempts(p.root).items()} == before_outputs

    for sid in selected:
        if sid == 'entry':
            continue
        successor, evidence = upgrade(p, sid)
        record_review_successor(p.root, sid, successor, evidence=evidence)
    status = episode_production_status(p.root)
    assert status['shot_readiness']['entry'] == 'ready_with_warnings'
    assert all(value in {'ready', 'ready_with_warnings'}
               for value in status['shot_readiness'].values())
    assert status['delivery'] == 'shots_ready_final_pending'

    selected = load_selected_attempts(p.root)
    master, _ = p.draft()
    final = p.full_review(master)
    for scene in final['scenes']:
        current_review = resolve_selection_review(p.root, scene['scene_id'], selected[scene['scene_id']])
        scene['review_sha256'] = review_digest(current_review)
    certify_final(p.root, final)
    status = episode_production_status(p.root)
    assert status['final_review']['eligible']
    assert status['delivery'] == 'certified_final'
    assert len(p.transport.native_requests) == native_calls
    assert {sid: s['output']['sha256'] for sid, s in load_selected_attempts(p.root).items()} == before_outputs

    master_path = p.root / final['output_path']
    original = master_path.read_bytes()
    master_path.write_bytes(original + b'changed master bytes')
    try:
        status = episode_production_status(p.root)
        assert not status['final_review']['eligible']
        assert status['delivery'] == 'shots_ready_final_pending'
        assert all(value in {'ready', 'ready_with_warnings'}
                   for value in status['shot_readiness'].values())
        assert len(p.transport.native_requests) == native_calls
    finally:
        master_path.write_bytes(original)
