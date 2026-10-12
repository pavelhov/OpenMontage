"""Canonical delivery status reads existing selection evidence without successor writes."""
from lib.episode_production_controls import episode_production_status
from lib.production_execution import load_selected_attempts, record_selection
from lib.production_review import certify_final
from lib.shot_contract import resolve_selection_review
from tests.integration.test_first_pass_workflow import production  # noqa: F401


def test_canonical_episode_status_transitions(production):
    p = production
    for shot in p.contract['shots']:
        sid = shot['id']
        if shot['upstream']:
            p.bind_upstream(sid)
        result = p.generate(sid)
        assert result.success, result.error
        p.select(sid, result)
    native_calls = len(p.transport.native_requests)
    selected = load_selected_attempts(p.root)
    before = {sid: row['output']['sha256'] for sid, row in selected.items()}
    for sid, row in selected.items():
        assert resolve_selection_review(p.root, sid, row) == row['review']
    status = episode_production_status(p.root)
    assert all(value in {'ready', 'ready_with_warnings'} for value in status['shot_readiness'].values())
    assert status['delivery'] == 'shots_ready_final_pending'
    master, _ = p.draft()
    final = p.full_review(master)
    certify_final(p.root, final)
    assert episode_production_status(p.root)['delivery'] == 'certified_final'
    path = p.root / final['output_path']
    original = path.read_bytes()
    path.write_bytes(original + b'changed master bytes')
    try:
        status = episode_production_status(p.root)
        assert not status['final_review']['eligible']
        assert status['delivery'] == 'shots_ready_final_pending'
        assert len(p.transport.native_requests) == native_calls
        assert {sid: row['output']['sha256'] for sid, row in load_selected_attempts(p.root).items()} == before
    finally:
        path.write_bytes(original)
