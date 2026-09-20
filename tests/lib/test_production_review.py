"""Synthetic declarations prove gate behavior, never audiovisual quality."""
import copy
import json
from pathlib import Path

import pytest

from lib.checkpoint import CheckpointValidationError, read_checkpoint, write_checkpoint
from lib.shot_contract import file_sha256, selection_digest
from schemas.artifacts import validate_artifact


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o644)
    path.write_text(json.dumps(value))


@pytest.fixture
def production(tmp_path, monkeypatch):
    # Real shared dispatch + Grok adapter. Only transport/probe boundaries are
    # mocked by the existing cross-layer harness; no hand-written success journal.
    from tests.integration import test_first_pass_workflow as workflow
    from lib.checkpoint import init_project
    from lib.production_execution import planned_request_digest, record_selection
    from lib.shot_contract import UPSTREAM_PREDICATES
    original_read = workflow.read
    def one_shot_story(path):
        value = original_read(path)
        if path.name == 'story-package.json':
            value['shots'] = value['shots'][:1]
        return value
    monkeypatch.setattr(workflow, 'read', one_shot_story)
    root = init_project('offline-first-pass', title='Synthetic final-review fixture',
        pipeline_type='cinematic', pipeline_dir=tmp_path, governance='strict',
        story_revision='courier-story-1')
    dispatched = workflow.Production(root, monkeypatch)
    dispatched.inputs['entry']['output_path'] = str(root / 'shot.bin')
    dispatched.scope['requests']['entry'] = planned_request_digest(dispatched.inputs['entry'], project_dir=root)
    dispatched.persist_scope()
    generated = dispatched.generate('entry')
    assert generated.success, generated.error
    (root / 'outgoing.bin').write_bytes(b'synthetic outgoing frame')
    (root / 'master.bin').write_bytes(b'synthetic composed master')
    selection = {
        'attempt_id': generated.data['production_attempt_id'],
        'output': {'path': str(root / 'shot.bin'), 'sha256': file_sha256(root / 'shot.bin')},
        'outgoing_frame': {'path': 'outgoing.bin', 'sha256': file_sha256(root / 'outgoing.bin')},
    }
    selection['review'] = workflow.attestation(selection_digest(selection), UPSTREAM_PREDICATES)
    record_selection(root, 'entry', selection)
    review = dispatched.full_review(root / 'master.bin')
    review['duration_seconds'] = 8
    review['av_review']['end_seconds'] = 8
    # Checkpoint behavior is the test subject below; this single-shot fixture
    # deliberately supplies no pipeline name so stage prerequisites are separate.
    marker = original_read(root / 'project.json')
    marker.pop('pipeline_type', None)
    save(root / 'project.json', marker)
    yield root, review, dispatched.contract, selection
    dispatched.network.assert_not_called()


def check(production):
    from lib.production_review import validate_final_review
    root, review, _, _ = production
    return validate_final_review(root, review, probe=lambda _: {'duration_seconds': 8})


def test_technical_pass_without_full_av_never_final(production):
    del production[1]['av_review']
    result = check(production)
    assert not result['eligible'] and result['release_status'] == 'draft'


def test_valid_current_review_and_schema(production):
    validate_artifact('final_review', production[1])
    assert check(production)['eligible']


@pytest.mark.parametrize('dimension', ['transport', 'technical', 'visual', 'audio', 'story'])
def test_dimensions_fail_independently(production, dimension):
    production[1]['dimensions'][dimension]['status'] = 'unknown'
    production[1]['metadata'] = {'review_round': 100, 'remaining_budget': 0}
    assert not check(production)['eligible']


@pytest.mark.parametrize('change', ['master', 'shot', 'frame', 'selection', 'scene', 'revision', 'journal', 'predicate', 'duration', 'tail', 'reviewer'])
def test_stale_or_incomplete_evidence_cannot_certify(production, change):
    root, review, contract, selection = production
    if change in {'master', 'shot'}:
        (root / f'{change}.bin').write_bytes(b'changed bytes')
    elif change == 'frame':
        (root / 'outgoing.bin').write_bytes(b'changed frame')
    elif change == 'selection':
        selection['attempt_id'] = 'replacement'
        save(root / 'artifacts/selected_attempts.json', {'entry': selection})
    elif change == 'scene':
        review['scenes'] = []
    elif change == 'revision':
        contract['story_revision'] = 'story-2'
        save(root / 'artifacts/shot_contract.json', contract)
    elif change == 'journal':
        save(root / 'production_attempts' / selection['attempt_id'] / 'result.json', {'status': 'uncertain'})
    elif change == 'predicate':
        review['predicates'][0].update(status='fail', severity='cosmetic')
    elif change == 'duration':
        review['duration_seconds'] = 7
    elif change == 'tail':
        review['av_review']['end_seconds'] = 7
    else:
        review['reviewer']['id'] = ''
    assert not check(production)['eligible'], change


def test_cosmetic_warnings_do_not_block(production):
    production[1]['predicates'].append({'name': 'lighting_polish', 'status': 'fail', 'severity': 'cosmetic', 'evidence': 'Slight lighting difference'})
    result = check(production)
    assert result['eligible'] and result['warnings']


def test_legacy_schema_readable_but_not_current(production):
    legacy = {'version': '1.0', 'output_path': 'master.bin', 'status': 'pass', 'checks': {key: {} for key in ['technical_probe', 'visual_spotcheck', 'audio_spotcheck', 'promise_preservation', 'subtitle_check']}}
    validate_artifact('final_review', legacy)
    production[1].clear()
    production[1].update(legacy)
    assert not check(production)['eligible']


def report():
    return {'version': '1.0', 'outputs': [{'path': 'master.bin', 'format': 'mp4', 'resolution': '720x1280', 'duration_seconds': 8}]}


def test_checkpoint_compose_draft_label_and_legacy_read(tmp_path):
    path = write_checkpoint(tmp_path, 'legacy', 'compose', 'completed', {'render_report': report()})
    assert json.loads(path.read_text())['metadata']['release_status'] == 'draft'
    assert read_checkpoint(tmp_path, 'legacy', 'compose')['status'] == 'completed'


def test_checkpoint_final_claim_cannot_bypass_gate(production):
    root, review, _, _ = production
    del review['av_review']
    with pytest.raises(CheckpointValidationError, match='FINAL CERTIFICATION'):
        write_checkpoint(root.parent, root.name, 'compose', 'completed', {'render_report': report(), 'final_review': review})


def test_checkpoint_valid_final_uses_gate_and_current_bytes(production, monkeypatch):
    import lib.production_review as module
    monkeypatch.setattr(module, 'probe_master', lambda _: {'duration_seconds': 8})
    root, review, _, _ = production
    path = write_checkpoint(root.parent, root.name, 'compose', 'completed', {'render_report': report(), 'final_review': review})
    assert json.loads(path.read_text())['metadata']['release_status'] == 'final'
    (root / 'master.bin').write_bytes(b'changed')
    with pytest.raises(CheckpointValidationError, match='FINAL CERTIFICATION'):
        write_checkpoint(root.parent, root.name, 'compose', 'completed', {'render_report': report(), 'final_review': review})


def test_canonical_certify_writer_rejects_and_preserves_existing(production):
    from lib.production_review import certify_final, FinalCertificationError
    root, review, _, _ = production
    path = root / 'artifacts/final_review.json'
    path.write_text('{"prior":"keep"}')
    review['dimensions']['story']['status'] = 'fail'
    with pytest.raises(FinalCertificationError):
        certify_final(root, review, probe=lambda _: {'duration_seconds': 8})
    assert json.loads(path.read_text()) == {'prior': 'keep'}


def test_existing_checkpoint_cannot_claim_final_without_any_review(tmp_path):
    with pytest.raises(CheckpointValidationError, match='FINAL CERTIFICATION'):
        write_checkpoint(tmp_path, 'legacy', 'compose', 'completed', {'render_report': report()}, metadata={'release_status': 'final'})


@pytest.mark.parametrize('mutation', ['no_listening', 'spotcheck', 'av_nan', 'predicate_missing', 'predicate_duplicate', 'scene_gap', 'scene_extra', 'selected_fail', 'selected_missing', 'unenrolled'])
def test_additional_fail_closed_review_paths(production, mutation):
    root, review, _, selection = production
    if mutation == 'no_listening':
        review['av_review']['listened_full'] = False
    elif mutation == 'spotcheck':
        review['av_review']['mode'] = 'spotcheck'
    elif mutation == 'av_nan':
        review['av_review']['end_seconds'] = float('nan')
    elif mutation == 'predicate_missing':
        review['predicates'].pop()
    elif mutation == 'predicate_duplicate':
        review['predicates'].append(copy.deepcopy(review['predicates'][0]))
    elif mutation == 'scene_gap':
        review['scenes'][0]['start_seconds'] = 1
    elif mutation == 'scene_extra':
        review['scenes'].append(copy.deepcopy(review['scenes'][0]))
    elif mutation == 'selected_fail':
        selection['review']['status'] = 'fail'
        save(root / 'artifacts/selected_attempts.json', {'entry': selection})
    elif mutation == 'selected_missing':
        selection['review']['predicates'].pop()
        save(root / 'artifacts/selected_attempts.json', {'entry': selection})
    else:
        save(root / 'project.json', {'project_id': root.name, 'story_revision': 'story-1'})
    assert not check(production)['eligible']


def test_render_report_cannot_substitute_unreviewed_master(production):
    from lib.production_review import validate_final_review
    root, review, _, _ = production
    assert not validate_final_review(root, review, probe=lambda _: {'duration_seconds': 8}, output_paths=['other-master.bin'])['eligible']


def test_checkpoint_publish_requires_review_even_without_explicit_final_claim(tmp_path):
    save(tmp_path / 'legacy/project.json', {'project_id': 'legacy', 'governance': {'version': '1.0', 'mode': 'strict'}})
    with pytest.raises(CheckpointValidationError, match='FINAL CERTIFICATION'):
        write_checkpoint(tmp_path, 'legacy', 'publish', 'completed', {'publish_log': {'version': '1.0', 'entries': []}})


def test_existing_v1_final_checkpoint_is_structurally_readable(tmp_path):
    legacy = {'version': '1.0', 'output_path': 'master.bin', 'status': 'pass', 'checks': {key: {} for key in ['technical_probe', 'visual_spotcheck', 'audio_spotcheck', 'promise_preservation', 'subtitle_check']}}
    save(tmp_path / 'old/checkpoint_compose.json', {
        'version': '1.0', 'project_id': 'old', 'pipeline_type': 'unknown', 'stage': 'compose',
        'status': 'completed', 'timestamp': '2026-01-01T00:00:00Z', 'checkpoint_policy': 'guided',
        'human_approval_required': False, 'human_approved': False,
        'artifacts': {'render_report': report(), 'final_review': legacy},
    })
    assert read_checkpoint(tmp_path, 'old', 'compose')['artifacts']['final_review']['version'] == '1.0'


def test_legacy_ungoverned_publish_remains_available_without_current_certification(tmp_path):
    path = write_checkpoint(tmp_path, 'legacy', 'publish', 'completed', {'publish_log': {'version': '1.0', 'entries': []}})
    assert json.loads(path.read_text())['metadata']['release_status'] == 'uncertified_legacy'


def test_reconciled_original_attempt_can_certify(production):
    root, _, _, selection = production
    directory = root / 'production_attempts' / selection['attempt_id']
    retained = json.loads((directory / 'result.json').read_text())
    save(directory / 'result.json', {'status': 'uncertain', 'result': retained['result']})
    save(directory / 'reconciliation.json', retained)
    assert check(production)['eligible']


def test_shared_canonical_loader_rejects_conflicting_legacy_copy(production):
    root, _, contract, _ = production
    contract['story_revision'] = 'conflicting-old-story'
    save(root / 'shot_contract.json', contract)
    assert not check(production)['eligible']


def test_shared_legacy_sidecar_paths_still_read(production):
    root, _, _, _ = production
    for name in ['shot_contract.json', 'selected_attempts.json']:
        (root / 'artifacts' / name).rename(root / name)
    assert check(production)['eligible']


def test_complete_reviews_do_not_certify_skeletal_attempt_journal(production):
    root, _, _, selection = production
    path = root / 'production_attempts' / selection['attempt_id'] / 'request.json'
    current = json.loads(path.read_text())
    save(path, {key: current[key] for key in ['attempt_id', 'project_id', 'story_revision', 'shot_id']})
    save(path.parent / 'result.json', {'status': 'generated', 'result': {'success': True}, 'output': selection['output']})
    assert not check(production)['eligible']
    from lib.production_execution import ProductionGovernanceError, record_selection
    from lib.production_review import FinalCertificationError, certify_final
    with pytest.raises(ProductionGovernanceError, match='provenance'):
        record_selection(root, 'entry', selection)
    with pytest.raises(FinalCertificationError, match='provenance'):
        certify_final(root, production[1], probe=lambda _: {'duration_seconds': 8})
    with pytest.raises(CheckpointValidationError, match='provenance'):
        write_checkpoint(root.parent, root.name, 'compose', 'completed',
            {'render_report': report(), 'final_review': production[1]})


def corrupt_original_result(production):
    """Retain an actual mocked native return, then simulate interrupted journal IO."""
    from tools.base_tool import ToolResult
    root, _, _, selection = production
    directory = root / 'production_attempts' / selection['attempt_id']
    retained = json.loads((directory / 'result.json').read_text())
    request = json.loads((directory / 'request.json').read_text())
    (directory / 'raw_result.json').unlink()
    original = directory / 'result.json'
    original.chmod(0o644)
    original.write_bytes(b'{"status": "uncert')
    return directory, request, ToolResult(**retained['result'])


def test_corrupt_original_reconciles_then_selects_and_certifies(production, monkeypatch):
    from lib.production_execution import reconcile_attempt, record_selection
    from lib.production_review import certify_final
    root, review, _, selection = production
    directory, request, recovered = corrupt_original_result(production)
    original_bytes = (directory / 'result.json').read_bytes()
    state = reconcile_attempt(root, selection['attempt_id'], recovered,
        request_sha256=request['request_sha256'])
    assert state['status'] == 'generated'
    record_selection(root, 'entry', selection)
    assert check(production)['eligible']
    certify_final(root, review, probe=lambda _: {'duration_seconds': 8})
    monkeypatch.setattr('lib.production_review.probe_master', lambda _: {'duration_seconds': 8})
    checkpoint = write_checkpoint(root.parent, root.name, 'compose', 'completed',
        {'render_report': report(), 'final_review': review})
    assert json.loads(checkpoint.read_text())['metadata']['release_status'] == 'final'
    assert (directory / 'result.json').read_bytes() == original_bytes


@pytest.mark.parametrize('recovery', ['none', 'missing_receipt', 'unconfirmed'])
def test_corrupt_original_without_qualified_recovery_stays_blocked(production, recovery):
    from lib.production_execution import ProductionGovernanceError, reconcile_attempt, record_selection
    root, _, _, selection = production
    directory, request, recovered = corrupt_original_result(production)
    if recovery == 'missing_receipt':
        del recovered.data['conditioning_receipt']
        with pytest.raises(ProductionGovernanceError):
            reconcile_attempt(root, selection['attempt_id'], recovered, request_sha256=request['request_sha256'])
    elif recovery == 'unconfirmed':
        reconcile_attempt(root, selection['attempt_id'], recovered, request_sha256=request['request_sha256'])
        record = json.loads((directory / 'reconciliation.json').read_text())
        record['result']['data']['conditioning_receipt']['submission_evidence'] = 'unconfirmed'
        save(directory / 'reconciliation.json', record)
    assert not check(production)['eligible']
    with pytest.raises(ProductionGovernanceError):
        record_selection(root, 'entry', selection)
