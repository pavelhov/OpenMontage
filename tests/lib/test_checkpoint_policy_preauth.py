"""Offline policy gates exercise retained approval and real checkpoint writes."""
import hashlib
import json
import copy

import pytest

from lib.checkpoint import CheckpointValidationError, _prepare_policy_decision_log, write_checkpoint
from lib.production_autonomy import ACTIVATION_CATEGORY, ACTIVATION_SUBJECT
from lib import production_autonomy as pa
from tests.contracts.test_phase0_contracts import sample_artifact
from tests.lib.test_production_autonomy import install, make_policy

PROJECT = 'offline-first-pass'


def snapshot(root):
    """Capture directories as well as bytes: failures cannot create empty history."""
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob('*')}


def setup_gate(tmp_path, *, stage='script', stages=None):
    root = tmp_path / PROJECT
    root.mkdir()
    marker = {'project_id': PROJECT, 'story_revision': 'story-1',
              'pipeline_type': 'framework-smoke', 'governance': {'mode': 'strict', 'version': '1.0'}}
    policy, sha = install(root, make_policy(checkpoint_stages=['script'] if stages is None else stages), marker=marker)
    if policy['checkpoint_stages'] and set(policy['checkpoint_stages']) <= {'research', 'script'}:
        # Negative checkpoint tests begin from demonstrably valid authority,
        # so an unrelated broken baseline cannot make them falsely pass.
        pa.require_active_policy(root)
    if stage == 'script':
        write_checkpoint(tmp_path, PROJECT, 'research', 'completed',
                         {'research_brief': sample_artifact('research_brief')}, human_approved=True)
    name = {'research': 'research_brief', 'script': 'script', 'scene_plan': 'scene_plan',
            'publish': 'publish_log', 'compose': 'render_report'}[stage]
    art_path = root / 'artifacts' / f'{name}.json'
    # A retained policy binds the approved script bytes. Never replace them
    # with a generic checkpoint sample after installing the baseline.
    if stage == 'script':
        artifact = json.loads(art_path.read_text())
    else:
        artifact = sample_artifact(name)
        art_path.write_text(json.dumps(artifact))
    art_sha = hashlib.sha256(art_path.read_bytes()).hexdigest()
    retained = {'version': '1.0', 'stage': stage, 'findings': [],
                'artifact_path': f'artifacts/{name}.json', 'artifact_sha256': art_sha, 'critical_findings': []}
    review_path = root / 'artifacts' / f'{stage}_review.json'
    review_path.write_text(json.dumps(retained))
    pre = {'artifact_path': retained['artifact_path'], 'artifact_sha256': art_sha,
           'review_path': f'artifacts/{stage}_review.json',
           'review_sha256': hashlib.sha256(review_path.read_bytes()).hexdigest(), 'critical_findings': []}
    basis = {'kind': 'policy', 'decision_id': 'd-act', 'policy_sha256': sha}
    return root, policy, artifact, retained, pre, basis


def complete(tmp_path, artifact, pre, basis, **over):
    kwargs = {'review': {'preauth': pre}, 'approval_basis': basis}
    kwargs.update(over)
    return write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': artifact}, **kwargs)


def new_decision_log():
    return {'version': '1.0', 'project_id': PROJECT, 'decisions': [{
        'decision_id': 'new-choice', 'stage': 'script', 'category': 'provider_selection',
        'subject': 'Video route', 'selected': 'approved', 'reason': 'Retained policy route',
        'options_considered': [{'option_id': 'approved', 'label': 'Approved', 'score': 1, 'reason': 'Fits'}],
    }]}


@pytest.mark.parametrize('case', ['existing_collision', 'repeated_identical', 'repeated_conflicting'])
def test_policy_log_preparation_rejects_collisions_without_any_write(tmp_path, case):
    root = tmp_path / PROJECT
    (root / 'artifacts').mkdir(parents=True)
    original = new_decision_log()
    (root / 'artifacts/decision_log.json').write_text(json.dumps(original))
    candidate = copy.deepcopy(original)
    if case == 'existing_collision':
        candidate['decisions'][0]['reason'] = 'Contradictory replacement of retained history'
    else:
        candidate['decisions'][0]['decision_id'] = 'fresh-choice'
        duplicate = copy.deepcopy(candidate['decisions'][0])
        if case == 'repeated_conflicting':
            duplicate['reason'] = 'Contradictory second entry'
        candidate['decisions'].append(duplicate)
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        _prepare_policy_decision_log(root, PROJECT, candidate)
    assert snapshot(root) == before


def test_policy_log_preparation_allows_identical_existing_entry_replay(tmp_path):
    root = tmp_path / PROJECT
    (root / 'artifacts').mkdir(parents=True)
    original = new_decision_log()
    path = root / 'artifacts/decision_log.json'
    path.write_text(json.dumps(original))
    before = snapshot(root)
    prepared_path, projected = _prepare_policy_decision_log(root, PROJECT, copy.deepcopy(original))
    assert prepared_path == path
    assert projected == original
    assert snapshot(root) == before


def test_human_approved_path_preserves_legacy_existing_id_deduplication(tmp_path):
    root = tmp_path / PROJECT
    root.mkdir()
    original = new_decision_log()
    path = root / 'decision_log.json'
    path.write_text(json.dumps(original))
    candidate = copy.deepcopy(original)
    candidate['decisions'][0]['reason'] = 'Legacy same-ID payload'
    checkpoint = write_checkpoint(
        tmp_path, PROJECT, 'research', 'completed',
        {'research_brief': sample_artifact('research_brief'), 'decision_log': candidate},
        pipeline_type='framework-smoke', human_approved=True,
    )
    assert json.loads(checkpoint.read_text())['human_approved'] is True
    assert json.loads(path.read_text()) == original


def test_real_retained_policy_gate_records_truthful_human_false(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    path = complete(tmp_path, artifact, pre, basis)
    checkpoint = json.loads(path.read_text())
    assert checkpoint['human_approved'] is False
    assert checkpoint['human_approval_required'] is True
    assert checkpoint['metadata']['approval_basis'] == basis
    assert checkpoint['review']['preauth'] == pre
    assert not (root / 'history').exists()


@pytest.mark.parametrize('basis', [None, {'kind': 'invalid caller policy'}])
def test_explicit_human_approval_keeps_existing_path(tmp_path, basis):
    root, _, artifact, _, _, _ = setup_gate(tmp_path)
    path = write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': artifact},
                            human_approved=True, approval_basis=basis)
    result = json.loads(path.read_text())
    assert result['human_approved'] is True
    assert 'approval_basis' not in result.get('metadata', {})


@pytest.mark.parametrize('case', ['missing_policy', 'unapproved', 'stale', 'revoked', 'benchmark', 'unlisted',
                                  'artifact_hash', 'review_hash', 'critical', 'missing_critical', 'caller_critical',
                                  'contradictory_findings', 'wrong_path', 'wrong_review_artifact', 'extra_basis',
                                  'missing_binding', 'checkpoint_payload', 'evidence_changed', 'extra_preauth',
                                  'malformed_hash', 'malformed_policy_hash', 'wrong_decision_id',
                                  'extra_review', 'wrong_review_hash_binding', 'missing_findings',
                                  'empty_stages', 'ungated_policy_stage'])
def test_invalid_policy_gate_writes_nothing_even_with_decision_log(tmp_path, case):
    stages = {'unlisted': ['research'], 'empty_stages': [], 'ungated_policy_stage': ['compose']}.get(case)
    root, policy, artifact, retained, pre, basis = setup_gate(tmp_path, stages=stages)
    log_path = root / 'artifacts' / 'decision_log.json'
    log = json.loads(log_path.read_text())
    if case == 'missing_policy':
        (root / 'artifacts' / 'autonomy_policy.json').unlink()
    elif case == 'unapproved':
        log['decisions'][-1]['user_approved'] = False
    elif case == 'stale':
        basis['policy_sha256'] = '1' * 64
    elif case == 'revoked':
        log['decisions'].append(dict(log['decisions'][-1], decision_id='revoked', selected='strict'))
    elif case == 'benchmark':
        (root / 'provider_benchmark').mkdir()
    elif case == 'artifact_hash':
        (root / pre['artifact_path']).write_text('{}')
    elif case == 'review_hash':
        (root / pre['review_path']).write_text('{}')
    elif case == 'critical':
        retained['critical_findings'] = ['Missing speaker']
    elif case == 'missing_critical':
        del retained['critical_findings']
    elif case == 'caller_critical':
        pre['critical_findings'] = ['Caller issue']
    elif case == 'contradictory_findings':
        retained['findings'] = [{'id': 'f1', 'severity': 'critical', 'description': 'Wrong speaker'}]
    elif case == 'wrong_path':
        pre['artifact_path'] = 'artifacts/other.json'
    elif case == 'wrong_review_artifact':
        retained['artifact_path'] = 'artifacts/other.json'
    elif case == 'wrong_review_hash_binding':
        retained['artifact_sha256'] = '1' * 64
    elif case == 'extra_review':
        retained['caller_approved'] = True
    elif case == 'missing_findings':
        del retained['findings']
    elif case == 'extra_basis':
        basis['caller_approved'] = True
    elif case == 'extra_preauth':
        pre['caller_approved'] = True
    elif case == 'malformed_hash':
        pre['artifact_sha256'] = 'NOT_A_HASH'
    elif case == 'malformed_policy_hash':
        basis['policy_sha256'] = 'NOT_A_HASH'
    elif case == 'wrong_decision_id':
        basis['decision_id'] = 'not-the-activation'
    elif case == 'missing_binding':
        del pre['review_sha256']
    elif case == 'checkpoint_payload':
        artifact['title'] = 'Unreviewed title'
    elif case == 'evidence_changed':
        (root / policy['evidence']['path']).write_text('{}')
    if case in {'unapproved', 'revoked'}:
        log_path.write_text(json.dumps(log))
    if case in {'critical', 'missing_critical', 'contradictory_findings', 'wrong_review_artifact',
                'wrong_review_hash_binding', 'extra_review', 'missing_findings'}:
        review_path = root / pre['review_path']
        review_path.write_text(json.dumps(retained))
        pre['review_sha256'] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    artifacts = {'script': artifact, 'decision_log': {'version': '1.0', 'decisions': [{'decision_id': 'never-merged'}]}}
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, 'script', 'completed', artifacts,
                         review={'preauth': pre}, approval_basis=basis)
    assert snapshot(root) == before
    assert artifacts['script'] == artifact
    assert not (root / 'decision_log.json').exists()
    assert not (root / 'history').exists()


def test_no_policy_basis_remains_strict(tmp_path):
    root, _, artifact, _, _, _ = setup_gate(tmp_path)
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': artifact})
    assert snapshot(root) == before


def test_failed_policy_rewrite_cannot_archive_or_replace_existing_checkpoint(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    complete(tmp_path, artifact, pre, basis)
    before = snapshot(root)
    basis['policy_sha256'] = '1' * 64
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        complete(tmp_path, artifact, pre, basis)
    assert snapshot(root) == before
    assert not (root / 'history').exists()


def test_invalid_policy_cannot_create_project_output_directories(tmp_path):
    before = snapshot(tmp_path)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(
            tmp_path, PROJECT, 'research', 'completed',
            {'research_brief': sample_artifact('research_brief')},
            pipeline_type='framework-smoke',
            approval_basis={'kind': 'policy', 'decision_id': 'absent', 'policy_sha256': '1' * 64},
            review={'preauth': {}},
        )
    assert snapshot(tmp_path) == before
    assert not (tmp_path / PROJECT).exists()


@pytest.mark.parametrize('change', ['artifact', 'review', 'revoked'])
def test_saved_policy_predecessor_revalidated_on_advance(tmp_path, change):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path, stage='research', stages=['research'])
    predecessor = write_checkpoint(tmp_path, PROJECT, 'research', 'completed', {'research_brief': artifact},
                                   review={'preauth': pre}, approval_basis=basis)
    assert json.loads(predecessor.read_text())['human_approved'] is False
    script = sample_artifact('script')
    write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': script}, human_approved=True)
    if change in {'artifact', 'review'}:
        (root / pre[f'{change}_path']).write_text('{}')
    else:
        log_path = root / 'artifacts' / 'decision_log.json'
        log = json.loads(log_path.read_text())
        log['decisions'].append(dict(log['decisions'][-1], decision_id='revoked', selected='strict'))
        log_path.write_text(json.dumps(log))
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': script}, human_approved=True)
    assert snapshot(root) == before


def test_caller_cannot_select_another_project_manifest_for_policy_gate(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        complete(tmp_path, artifact, pre, basis, pipeline_type='cinematic')
    assert snapshot(root) == before


def test_unknown_marker_manifest_is_a_gate_violation_without_writes(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    marker = json.loads((root / 'project.json').read_text())
    marker['pipeline_type'] = 'does-not-exist'
    (root / 'project.json').write_text(json.dumps(marker))
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        complete(tmp_path, artifact, pre, basis)
    assert snapshot(root) == before


def test_caller_gate_flag_cannot_replace_an_actual_manifest_gate(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path, stage='research', stages=['research'])
    marker = json.loads((root / 'project.json').read_text())
    marker['pipeline_type'] = 'animated-explainer'  # research is ungated here
    (root / 'project.json').write_text(json.dumps(marker))
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, 'research', 'completed', {'research_brief': artifact},
                         review={'preauth': pre}, approval_basis=basis, human_approval_required=True)
    assert snapshot(root) == before


def test_metadata_presence_alone_cannot_approve_a_gate(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': artifact},
                         review={'preauth': pre}, metadata={'approval_basis': basis})
    assert snapshot(root) == before


@pytest.mark.parametrize('case', ['invalid_log', 'revokes', 'replaces', 'duplicate_activation',
                                  'wrong_log_project', 'invalid_metadata', 'unserializable_metadata'])
def test_valid_policy_invalid_caller_payload_cannot_write_a_decision_log(tmp_path, case):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    log = new_decision_log()
    metadata = None
    if case == 'invalid_log':
        log = {'version': '1.0', 'decisions': [{'decision_id': 'never-merged'}]}
    elif case in {'revokes', 'replaces'}:
        log['decisions'][0].update(category=ACTIVATION_CATEGORY, subject=ACTIVATION_SUBJECT,
                                   selected='strict' if case == 'revokes' else 'auto_continue:' + '1' * 64)
    elif case == 'duplicate_activation':
        log['decisions'] = [json.loads((root / 'artifacts/decision_log.json').read_text())['decisions'][-1]]
    elif case == 'wrong_log_project':
        log['project_id'] = 'another-project'
    elif case == 'invalid_metadata':
        metadata = 'invalid metadata'
    else:
        metadata = {'caller_object': b'not JSON'}
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': artifact, 'decision_log': log},
                         review={'preauth': pre}, approval_basis=basis, metadata=metadata)
    assert snapshot(root) == before
    assert not (root / 'decision_log.json').exists()


def test_policy_nonactivation_append_preserves_canonical_authority_and_history(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    path = root / 'artifacts/decision_log.json'
    original = json.loads(path.read_text())['decisions']
    log = new_decision_log()
    write_checkpoint(tmp_path, PROJECT, 'script', 'completed', {'script': artifact, 'decision_log': log},
                     review={'preauth': pre}, approval_basis=basis)
    assert not (root / 'decision_log.json').exists()
    current = json.loads(path.read_text())['decisions']
    assert current == original + log['decisions']
    assert pa.validate_preauth(root, 'script', basis, pre) == basis['policy_sha256']
    complete(tmp_path, artifact, pre, basis)


def test_policy_append_with_two_equal_log_aliases_fails_without_writes(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    (root / 'decision_log.json').write_bytes((root / 'artifacts/decision_log.json').read_bytes())
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, 'script', 'completed',
                         {'script': artifact, 'decision_log': new_decision_log()},
                         review={'preauth': pre}, approval_basis=basis)
    assert snapshot(root) == before


def test_policy_append_with_conflicting_log_aliases_fails_without_writes(tmp_path):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path)
    (root / 'decision_log.json').write_text(json.dumps(new_decision_log()))
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        complete(tmp_path, artifact, pre, basis)
    assert snapshot(root) == before


def test_noncritical_structured_review_can_preauthorize(tmp_path):
    root, _, artifact, retained, pre, basis = setup_gate(tmp_path)
    retained['findings'] = [
        {'id': 'suggestion', 'severity': 'suggestion', 'description': 'Optional wording refinement'},
        {'id': 'nitpick', 'severity': 'nitpick', 'description': 'Optional punctuation refinement'},
    ]
    path = root / pre['review_path']
    path.write_text(json.dumps(retained))
    pre['review_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = json.loads(complete(tmp_path, artifact, pre, basis).read_text())
    assert result['human_approved'] is False


def test_fresh_review_cannot_authorize_drift_from_retained_full_script_snapshot(tmp_path):
    root, _, artifact, retained, pre, basis = setup_gate(tmp_path)
    artifact['title'] = 'Unapproved change outside selected script section'
    artifact_path = root / pre['artifact_path']
    artifact_path.write_text(json.dumps(artifact))
    current_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    retained['artifact_sha256'] = current_sha
    pre['artifact_sha256'] = current_sha
    review_path = root / pre['review_path']
    review_path.write_text(json.dumps(retained))
    pre['review_sha256'] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    before = snapshot(root)
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        complete(tmp_path, artifact, pre, basis)
    assert snapshot(root) == before


@pytest.mark.parametrize('stage', ['publish', 'compose'])
def test_publish_or_unlisted_ungated_compose_cannot_gain_policy_approval(tmp_path, stage):
    root, _, artifact, _, pre, basis = setup_gate(tmp_path, stage=stage, stages=['script'])
    marker = json.loads((root / 'project.json').read_text())
    marker['pipeline_type'] = 'animated-explainer'
    (root / 'project.json').write_text(json.dumps(marker))
    before = snapshot(root)
    name = 'publish_log' if stage == 'publish' else 'render_report'
    with pytest.raises(CheckpointValidationError, match='GATE VIOLATION:'):
        write_checkpoint(tmp_path, PROJECT, stage, 'completed', {name: artifact},
                         review={'preauth': pre}, approval_basis=basis, metadata={'release_status': 'final'})
    assert snapshot(root) == before
