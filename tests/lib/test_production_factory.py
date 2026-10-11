"""Prospective factory seams with retained synthetic evidence; no provider calls."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib import production_request as request, production_images as images
from lib.shot_contract import project_review_subject, shot_review_subject
from tests.lib.test_production_autonomy import fixture_planning, make_policy, SCRIPT
from tests.lib.test_production_images import board_package, reserve, host_receipt  # noqa: F401
from tests.lib.test_production_request import neutral_package  # noqa: F401
from tests.lib.test_first_cut import p, production  # noqa: F401
from tests.integration.test_first_pass_workflow import attestation


def write(root, name, value):
    path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def creator_fixture(root, name, record):
    """Clearly synthetic fixture of an actual retained creator-message binding."""
    record = copy.deepcopy(record)
    record['authorization'] = write(root, 'notes/' + name + '-authorization.json', {
        'version': '1.0', 'kind': 'creator_authorization', 'origin': 'user_message',
        'identity_id': 'fixture-identity', 'message_id': 'SYNTHETIC-' + name,
        'text': 'SYNTHETIC offline creator authorization fixture; not production authority.',
        'subject_sha256': request.digest(record)})
    return write(root, 'notes/' + name + '.json', record)


def standing_fixture(root):
    contract = fixture_planning(root); contract['planning_mode'] = 'shot_local_v1'
    contract['shots'] = [s for s in contract['shots'] if s['id'] == 'entry']
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    write(root, 'artifacts/shot_contract.json', contract)
    write(root, 'project.json', {'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'identity_id': 'fixture-identity', 'pipeline_type': 'cinematic',
        'governance': {'mode': 'strict', 'version': '1.0'}})
    policy = make_policy(locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
    policy['checkpoint_stages'] = ['proposal', 'script', 'scene_plan', 'assets']
    template = execution.planned_request_template({'duration': 8, 'image_path': str(root / 'assets/start.svg')}, project_dir=root)
    projection = pa.current_projection(root, 'entry')
    baselines = {'entry': {'projection': projection, 'planned_request_template': template,
        'script_snapshot': copy.deepcopy(SCRIPT), 'dialogue_coverage': pa.dialogue_occurrences(projection),
        'approval_plan_sha256': execution.approval_plan_digest(contract, 'entry')}}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': copy.deepcopy(policy['providers']),
        'provider_accounts': {'grok_cli': 'fixture-subscription', 'openart_cli': 'fixture-credit-account'},
        'caps': copy.deepcopy(policy['caps']), 'checkpoint_stages': policy['checkpoint_stages'],
        'flex': copy.deepcopy(policy['flex']), 'approved_source_sha256': [a['sha256'] for a in contract['assets']],
        'required_permissions': [], 'source_transfer': [
            {'provider': p['id'], 'account': 'fixture-subscription' if p['id'] == 'grok_cli' else 'fixture-credit-account',
             'roles': ['identity_reference', 'start_frame', 'end_frame', 'payoff_board'],
             'generated_board_sources': False, 'max_files': 8, 'max_file_bytes': 10000,
             'max_batch_bytes': 80000, 'max_batches': 3} for p in policy['providers']]}
    setup_binding = creator_fixture(root, 'standing', setup)
    command_binding = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'command': 'make_video', 'standing_setup_sha256': setup_binding['sha256']})
    return dict(policy=policy, baselines=baselines, standing_setup=setup_binding,
        run_command=command_binding, identity_id='fixture-identity')


def test_standing_activation_exact_replay_and_current_creator_bytes(tmp_path):
    kwargs = standing_fixture(tmp_path)
    result = pa.activate_standing_run(tmp_path, **kwargs)
    assert result['provider_calls'] == 0 and not result['replayed']
    active, sha, decision, reasons = pa.load_active_policy(tmp_path)
    assert not reasons and sha == result['policy_sha256'] and decision == result['decision_id']
    assert active['activation_basis']['plan_sha256'] == request.digest(kwargs['baselines'])
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert pa.activate_standing_run(tmp_path, **kwargs) == dict(result, replayed=True)
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    (tmp_path / kwargs['run_command']['path']).write_text('{}')
    assert pa.load_active_policy(tmp_path)[0] is None
    assert not list(tmp_path.glob('**/*checkpoint*'))


def standing_stage_fixture(root, stages):
    kwargs = standing_fixture(root)
    kwargs['policy']['checkpoint_stages'] = stages
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization'); setup['checkpoint_stages'] = stages
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization'); command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    return kwargs


def legacy_ungated_standing_activation(root, monkeypatch):
    kwargs = standing_stage_fixture(root, ['proposal', 'script', 'scene_plan', 'assets', 'edit', 'compose'])
    # Reproduce only the old writer defect; all other canonical checks remain real.
    with monkeypatch.context() as patch:
        patch.setattr(pa, '_validate_checkpoint_stages', lambda *args: None, raising=False)
        activation = pa.activate_standing_run(root, **kwargs)
    assert pa.load_active_policy(root)[3] == ['checkpoint stages are not actual gated pipeline stages']
    return kwargs, activation


def file_bytes(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('stages', [['edit'], ['compose'], ['proposal', 'edit', 'compose'], ['research'], ['idea']])
def test_standing_activation_rejects_ungated_stages_before_authority_writes(tmp_path, stages):
    kwargs = standing_stage_fixture(tmp_path, stages)
    with pytest.raises(pa.StandingSetupRequired, match='not actual gated pipeline stages'):
        pa.activate_standing_run(tmp_path, **kwargs)
    assert not (tmp_path / pa.POLICY_ARTIFACT).exists()
    assert not list((tmp_path / 'approvals').glob('*'))
    assert not (tmp_path / 'artifacts/decision_log.json').exists()


def test_reconcile_standing_checkpoint_stages_freezes_original_and_replays_after_progress(tmp_path, monkeypatch):
    kwargs, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    policy_path = tmp_path / pa.POLICY_ARTIFACT
    original_bytes = policy_path.read_bytes()
    old_policy = json.loads(original_bytes)
    evidence_bytes = (tmp_path / old_policy['evidence']['path']).read_bytes()
    old_log = json.loads((tmp_path / 'artifacts/decision_log.json').read_bytes())
    result = pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert result['provider_calls'] == result['reservations'] == 0 and not result['replayed']
    active, sha, decision_id, reasons = pa.load_active_policy(tmp_path, shot_id='entry')
    assert not reasons and (sha, decision_id) == (result['policy_sha256'], result['decision_id'])
    assert active['checkpoint_stages'] == ['proposal', 'script', 'scene_plan', 'assets']
    assert {k: v for k, v in active.items() if k not in {'checkpoint_stages', 'evidence'}} == {
        k: v for k, v in old_policy.items() if k not in {'checkpoint_stages', 'evidence'}}
    assert (tmp_path / old_policy['evidence']['path']).read_bytes() == evidence_bytes
    provenance = json.loads((tmp_path / result['reconciliation']['path']).read_bytes())
    assert (tmp_path / provenance['original_policy']['path']).read_bytes() == original_bytes
    assert provenance['original_policy_sha256'] == original['policy_sha256']
    assert provenance['successor_policy_sha256'] == sha
    assert provenance['removed_checkpoint_stages'] == ['edit', 'compose']
    new_log = json.loads((tmp_path / 'artifacts/decision_log.json').read_bytes())
    assert new_log['decisions'][:-1] == old_log['decisions']
    assert new_log['decisions'][-1]['options_considered'][0]['option_id'] == old_log['decisions'][-1]['selected']
    assert new_log['decisions'][-1]['options_considered'][0]['rejected_because']
    assert pa.root_attempt_counts(tmp_path, active) == pa.root_attempt_counts(tmp_path, old_policy)
    amendment = copy.deepcopy(kwargs)
    amendment['policy']['checkpoint_stages'] = active['checkpoint_stages']
    amendment['policy']['caps']['max_repair_attempts'] = 0
    with pytest.raises(pa.StandingSetupRequired, match='explicit amendment required'):
        pa.activate_standing_run(tmp_path, **amendment)
    # A repeat is a pure no-op even after legitimate production has begun.
    write(tmp_path, 'production_attempts/retained-progress/request.json', {'retained_original': True})
    before = file_bytes(tmp_path)
    assert pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256']) == dict(result, replayed=True)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', ['expected_sha', 'revoked', 'superseded', 'creator', 'source', 'story', 'planning', 'benchmark', 'policy'])
def test_reconcile_standing_checkpoint_stages_rejects_other_invalid_authority_without_writes(tmp_path, monkeypatch, change):
    kwargs, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    expected = original['policy_sha256']
    if change == 'expected_sha': expected = '0' * 64
    elif change in {'revoked', 'superseded'}:
        path = tmp_path / 'artifacts/decision_log.json'; log = json.loads(path.read_text())
        decision = copy.deepcopy(log['decisions'][-1]); decision['decision_id'] = 'later-choice'
        decision['selected'] = 'strict' if change == 'revoked' else 'auto_continue:' + '1' * 64
        log['decisions'].append(decision); path.write_text(json.dumps(log))
    elif change == 'creator': (tmp_path / kwargs['run_command']['path']).write_text('{}')
    elif change == 'source': (tmp_path / 'assets/start.svg').write_bytes(b'changed source')
    elif change == 'story':
        path = tmp_path / 'project.json'; value = json.loads(path.read_text()); value['story_revision'] = 'changed'
        path.write_text(json.dumps(value))
    elif change == 'planning':
        path = tmp_path / 'artifacts/scene_plan.json'; value = json.loads(path.read_text()); value['scenes'][0]['description'] = 'Different authored action'
        path.write_text(json.dumps(value))
    elif change == 'benchmark': (tmp_path / 'provider_benchmark').mkdir()
    elif change == 'policy':
        path = tmp_path / pa.POLICY_ARTIFACT; value = json.loads(path.read_text()); value['caps']['max_total_attempts'] += 1
        path.chmod(0o644)
        path.write_text(json.dumps(value))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=expected)
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('kind', ['motion_journal', 'native_reserved', 'native_uncertain', 'scope', 'image_envelope', 'mcp_attempt', 'mcp_upload', 'cli_upload_authority', 'mcp_upload_authority', 'credit_reservation', 'unpriced_reservation'])
def test_reconcile_standing_checkpoint_stages_refuses_media_or_reservations(tmp_path, monkeypatch, kind):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    if kind == 'motion_journal': write(tmp_path, 'production_attempts/original/request.json', {'attempt_id': 'original'})
    elif kind.startswith('native_'):
        monkeypatch.setattr(images, '_load', lambda _: {'calls': [{'state': kind.removeprefix('native_')}]})
    elif kind == 'scope': write(tmp_path, 'production_scopes.json', {'version': '1.0', 'scopes': [{'id': 'prepared'}]})
    elif kind == 'image_envelope': write(tmp_path, 'production_scopes.json', {'version': '1.0', 'scopes': [], 'image_envelope': {}})
    elif kind == 'mcp_attempt': write(tmp_path, 'openart_mcp/attempts/original/state.json', {'status': 'prepared'})
    elif kind == 'mcp_upload': write(tmp_path, 'openart_mcp/uploads/original.json', {'status': 'consumed_awaiting_original_receipt'})
    elif kind == 'cli_upload_authority': write(tmp_path, 'artifacts/upload_approval-original.json', {})
    elif kind == 'mcp_upload_authority': write(tmp_path, 'artifacts/openart_mcp_upload_authorization-original.json', {})
    else:
        from lib import provider_credit_ledger as ledger
        snapshot = ledger.read_existing_snapshot()
        snapshot['reservations' if kind == 'credit_reservation' else 'unpriced_reservations'] = [
            {'attempt_id': 'private-only', 'binding_json': json.dumps({'project_root': str(tmp_path)})}]
        monkeypatch.setattr(ledger, 'read_existing_snapshot', lambda: snapshot)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='media|reservation|upload|scope'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('change', ['revoked', 'successor', 'provenance', 'original_snapshot', 'original_decision'])
def test_reconcile_standing_checkpoint_stages_replay_cannot_revive_or_amend(tmp_path, monkeypatch, change):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    result = pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    if change == 'successor':
        path = tmp_path / pa.POLICY_ARTIFACT; value = json.loads(path.read_text()); value['caps']['max_total_attempts'] += 1
        path.chmod(0o644)
        path.write_text(json.dumps(value))
    elif change == 'provenance':
        path = tmp_path / result['reconciliation']['path']; path.chmod(0o644); path.write_text('{}')
    elif change == 'original_snapshot':
        record = json.loads((tmp_path / result['reconciliation']['path']).read_text())
        path = tmp_path / record['original_policy']['path']; path.chmod(0o644); path.write_text('{}')
    else:
        path = tmp_path / 'artifacts/decision_log.json'; log = json.loads(path.read_text())
        if change == 'original_decision': log['decisions'][0]['user_approved'] = False
        else:
            decision = copy.deepcopy(log['decisions'][-1]); decision.update(decision_id='revoked', selected='strict')
            log['decisions'].append(decision)
        path.chmod(0o644)
        path.write_text(json.dumps(log))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(tmp_path) == before


def test_reconcile_standing_checkpoint_stages_refuses_stage_absent_from_manifest(tmp_path, monkeypatch):
    kwargs = standing_stage_fixture(tmp_path, ['proposal', 'edit', 'idea'])
    with monkeypatch.context() as patch:
        patch.setattr(pa, '_validate_checkpoint_stages', lambda *args: None)
        activation = pa.activate_standing_run(tmp_path, **kwargs)
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='absent from current pipeline'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=activation['policy_sha256'])
    assert file_bytes(tmp_path) == before


@pytest.mark.parametrize('interrupt', ['evidence', 'temporary_policy', 'temporary_log', 'log_pointer', 'policy_pointer'])
def test_reconcile_standing_checkpoint_stages_resumes_exact_interrupted_publication(tmp_path, monkeypatch, interrupt):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    original_policy = (tmp_path / pa.POLICY_ARTIFACT).read_bytes()
    before_log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
    with monkeypatch.context() as patch:
        if interrupt in {'evidence', 'temporary_policy', 'temporary_log'}:
            write_new = execution._write_new
            def stop_after_evidence(path, value):
                write_new(path, value)
                if (interrupt == 'evidence' and path.name.startswith('autonomy-checkpoint-reconciliation-')
                        or interrupt == 'temporary_policy' and path.name == 'autonomy_policy.json.checkpoints-tmp'
                        or interrupt == 'temporary_log' and path.name == 'decision_log.json.checkpoints-tmp'):
                    raise OSError('synthetic interruption after immutable provenance')
            patch.setattr(execution, '_write_new', stop_after_evidence)
        else:
            replace = Path.replace
            def stop_pointer(path, target):
                if str(target).endswith('decision_log.json' if interrupt == 'log_pointer' else 'autonomy_policy.json'):
                    raise OSError('synthetic pointer publication interruption')
                return replace(path, target)
            patch.setattr(Path, 'replace', stop_pointer)
        with pytest.raises(pa.StandingSetupRequired, match='synthetic'):
            pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert (tmp_path / pa.POLICY_ARTIFACT).read_bytes() == original_policy
    assert pa.load_active_policy(tmp_path)[0] is None
    result = pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert result['replayed'] and result['provider_calls'] == result['reservations'] == 0
    assert pa.load_active_policy(tmp_path)[1:3] == (result['policy_sha256'], result['decision_id'])
    log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text())
    assert log['decisions'][:-1] == before_log['decisions']
    before = file_bytes(tmp_path)
    assert pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256']) == result
    assert file_bytes(tmp_path) == before


def test_reconcile_standing_checkpoint_stages_partial_publication_cannot_revive_revocation(tmp_path, monkeypatch):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    replace = Path.replace
    with monkeypatch.context() as patch:
        def stop_policy(path, target):
            if str(target).endswith('autonomy_policy.json'): raise OSError('synthetic interruption')
            return replace(path, target)
        patch.setattr(Path, 'replace', stop_policy)
        with pytest.raises(pa.StandingSetupRequired):
            pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    path = tmp_path / 'artifacts/decision_log.json'; log = json.loads(path.read_text())
    decision = copy.deepcopy(log['decisions'][-1]); decision.update(decision_id='later-revocation', selected='strict')
    log['decisions'].append(decision); path.chmod(0o644); path.write_text(json.dumps(log))
    before = file_bytes(tmp_path)
    with pytest.raises(pa.StandingSetupRequired, match='revoked or superseded'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(tmp_path) == before


def test_reconcile_standing_checkpoint_stages_preserves_unrelated_shared_source_uploads(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    kwargs, original = legacy_ungated_standing_activation(root, monkeypatch)
    from tools import _openart_cli as cli
    private = tmp_path / 'private-openart'
    write(private, 'uploads/earlier-episode.json', {'source_sha256': kwargs['baselines']['entry']['projection']['assets'][0]['sha256']})
    private.chmod(0o700)
    monkeypatch.setattr(cli, 'state_dir', lambda create=False: private)
    before = file_bytes(private)
    pa.reconcile_standing_checkpoint_stages(root, expected_policy_sha256=original['policy_sha256'])
    assert file_bytes(private) == before


@pytest.mark.parametrize('change', ['policy', 'revocation', 'planning'])
def test_reconcile_standing_checkpoint_stages_racing_change_cannot_be_overwritten(tmp_path, monkeypatch, change):
    _, original = legacy_ungated_standing_activation(tmp_path, monkeypatch)
    write_new = execution._write_new
    changed_path = tmp_path / (pa.POLICY_ARTIFACT if change == 'policy' else
        'artifacts/decision_log.json' if change == 'revocation' else 'artifacts/scene_plan.json')
    def change_after_provenance(path, value):
        write_new(path, value)
        if path.name.startswith('autonomy-checkpoint-reconciliation-'):
            changed = json.loads(changed_path.read_text())
            if change == 'policy': changed['caps']['max_total_attempts'] += 1
            elif change == 'planning': changed['scenes'][0]['description'] = 'Unapproved racing plan'
            else:
                entry = copy.deepcopy(changed['decisions'][-1]); entry.update(decision_id='racing-revocation', selected='strict')
                changed['decisions'].append(entry)
            # Simulate a non-cooperating writer. Atomic replacement preserves the
            # immutable original inode and must never be overwritten by recovery.
            temporary = changed_path.with_suffix('.race.json')
            temporary.write_text(json.dumps(changed)); temporary.replace(changed_path)
    monkeypatch.setattr(execution, '_write_new', change_after_provenance)
    with pytest.raises(pa.StandingSetupRequired, match='changed during reconciliation'):
        pa.reconcile_standing_checkpoint_stages(tmp_path, expected_policy_sha256=original['policy_sha256'])
    assert pa.load_active_policy(tmp_path)[0] is None
    if change == 'policy': assert json.loads(changed_path.read_text())['caps']['max_total_attempts'] == 4
    elif change == 'planning': assert json.loads(changed_path.read_text())['scenes'][0]['description'] == 'Unapproved racing plan'
    else: assert pa.activation_decision(tmp_path)['selected'] == 'strict'


def test_supplied_cinematic_work_passes_proposal_with_actual_standing_scope(tmp_path):
    from lib.checkpoint import write_checkpoint
    root = tmp_path / 'offline-first-pass'; root.mkdir()
    kwargs = standing_fixture(root)
    active = pa.activate_standing_run(root, **kwargs)
    basis = {'kind': 'supplied_script_scenes', 'artifacts': [
        {'path': 'artifacts/' + name + '.json', 'sha256': hashlib.sha256((root / 'artifacts' / (name + '.json')).read_bytes()).hexdigest()}
        for name in ('script', 'scene_plan')]}
    approval = {'kind': 'policy', 'policy_sha256': active['policy_sha256'], 'decision_id': active['decision_id']}
    write_checkpoint(tmp_path, root.name, 'research', 'completed', {},
        pipeline_type='cinematic', metadata={'supplied_basis': basis})
    path = write_checkpoint(tmp_path, root.name, 'proposal', 'completed', {},
        pipeline_type='cinematic', metadata={'supplied_basis': basis}, approval_basis=approval)
    checkpoint = json.loads(path.read_text())
    assert checkpoint['human_approved'] is False
    assert 'proposal_packet' not in checkpoint['artifacts']
    assert checkpoint.get('review', {}) == {}
    script_binding = {'artifact_path': 'artifacts/script.json', 'artifact_sha256': basis['artifacts'][0]['sha256']}
    review_binding = write(root, 'notes/script-review.json', {'version': '1.0', 'stage': 'script',
        **script_binding, 'critical_findings': [], 'findings': []})
    preauth = {**script_binding, 'review_path': review_binding['path'], 'review_sha256': review_binding['sha256'],
        'critical_findings': []}
    path = write_checkpoint(tmp_path, root.name, 'script', 'completed', {'script': SCRIPT},
        pipeline_type='cinematic', approval_basis=approval, review={'preauth': preauth})
    assert json.loads(path.read_text())['human_approved'] is False


@pytest.mark.parametrize('change', ['account', 'source', 'caps', 'creator', 'permissions'])
def test_standing_setup_gaps_fail_before_activation(tmp_path, change):
    kwargs = standing_fixture(tmp_path)
    setup = json.loads((tmp_path / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    if change == 'account': setup['provider_accounts']['grok_cli'] = ''
    elif change == 'source': setup['approved_source_sha256'] = []
    elif change == 'caps': setup['caps']['max_total_attempts'] = 1
    elif change == 'permissions': setup['required_permissions'] = [write(tmp_path, 'notes/permission.json', {'status': 'pending'})]
    kwargs['standing_setup'] = creator_fixture(tmp_path, 'standing', setup)
    command = json.loads((tmp_path / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(tmp_path, 'command', command)
    if change == 'creator':
        evidence = json.loads((tmp_path / kwargs['run_command']['path']).read_text())['authorization']
        (tmp_path / evidence['path']).write_text('{"origin":"creator"}')
    with pytest.raises(pa.StandingSetupRequired): pa.activate_standing_run(tmp_path, **kwargs)
    assert not (tmp_path / 'artifacts/autonomy_policy.json').exists()
    assert not (tmp_path / 'artifacts/decision_log.json').exists()


def test_begun_native_host_action_is_durable_and_never_reemitted(board_package):
    root, *_ = board_package
    call = reserve(board_package, call_id='factory-original')
    first = execution.advance(root)
    assert first['status'] == 'host_action' and first['host_envelope']['call_id'] == call['call_id']
    assert first['action']['kind'] == 'receive_native_image'
    second = execution.advance(root)
    assert 'host_envelope' not in second and second['action'] == first['action']
    assert images.image_usage(root)['total'] == 1
    altered = copy.deepcopy(second['action']); altered['inputs']['call_id'] = 'replacement'
    with pytest.raises(execution.ProductionGovernanceError, match='stale, foreign or modified'):
        execution.complete_action(root, altered, outcome={})
    receipt = host_receipt(root, first['host_envelope'])
    completed = execution.complete_action(root, second['action'], outcome={'host_receipt': receipt})
    assert completed['status'] == 'blocked'  # This image-only fixture has no authored motion plan.
    assert images.image_usage(root)['total'] == 1
    replayed = execution.complete_action(root, second['action'], outcome={'host_receipt': receipt})
    assert replayed['replayed'] and replayed['provider_calls'] == 0
    assert images.image_usage(root)['total'] == 1


def test_unsupported_strict_original_retains_exact_id_and_count(tmp_path):
    write(tmp_path, 'project.json', {'project_id': 'SYNTHETIC-unsupported', 'story_revision': 'r1',
        'governance': {'mode': 'strict', 'version': '1.0'}})
    original = {'attempt_id': 'original-only', 'cli_session_id': 'original-native-id',
        'shot_id': 's1', 'media_kind': 'motion', 'story_revision': 'r1',
        'request_sha256': 'a'*64, 'scope': {'provider': 'unsupported_strict_provider'}}
    binding = write(tmp_path, 'production_attempts/original-only/request.json', original)
    resumed = execution.resume_original_attempt(tmp_path, 'original-only', request_sha256='a'*64)
    assert resumed['status'] == 'blocked' and resumed['original_id'] == 'original-native-id'
    assert resumed['counted_original'] and resumed['provider_calls'] == resumed['reservations'] == 0
    action = execution.next_production_action(tmp_path)
    assert action['kind'] == 'blocked' and action['inputs']['original_id'] == 'original-native-id'
    assert len(execution._attempts(tmp_path)) == 1
    assert hashlib.sha256((tmp_path / binding['path']).read_bytes()).hexdigest() == binding['sha256']


def prospective_package(package):
    package = copy.deepcopy(package); package['planning_mode'] = 'shot_local_v1'
    # Canonical authored shot facts replace duplicated template facts.
    for shot in package['boards']['motion_binding']['contract_template']['shots']:
        shot.pop('prop_body_invariants', None)
    return package


def test_local_package_partial_contract_and_lost_derived_view_restoration(neutral_package, monkeypatch):
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package); binding = bind(package)
    prepared = request.prepare_package(root, binding)
    assert prepared['motion_projection']['pending_assets']
    contract = json.loads((root / 'artifacts/shot_contract.json').read_text())
    assert all(a.get('preparation_pending') and 'sha256' not in a and 'review' not in a for a in contract['assets'])
    original = (root / 'artifacts/script.json').read_bytes()
    (root / 'artifacts/script.json').unlink()
    # Retained same-pack source proves the original authored bytes after usage.
    monkeypatch.setattr(images, 'image_usage', lambda _: {'total': 1})
    assert execution.next_production_action(root)['kind'] == 'restore_derived_views'
    request.prepare_package(root, binding)
    assert (root / 'artifacts/script.json').read_bytes() == original
    assert boards.load_board_pack(root)['package_binding'] == binding


def test_ready_board_loop_and_completed_image_restore_emit_zero_new_calls(neutral_package):
    from lib import production_boards as boards
    from lib.shot_contract import PROJECT_PREDICATES, SHOT_PREDICATES, ASSET_PREDICATES
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['boards']['motion_binding']['contract_template']['shots'][0]['endpoint_completion'] = 'completed'
    binding = bind(package); request.prepare_package(root, binding)
    for slot in ('start', 'end'):
        prepared = boards.prepare_image_request(root, slot, images=images)
        scope = {'id': 'exact-' + slot, 'status': 'approved', 'approved_by': 'SYNTHETIC creator fixture',
            'project_id': package['project_id'], 'story_revision': 'r1', 'phase': 'image', 'provider': 'native_imagegen',
            'evidence': {k: package['boards']['approval'][k] for k in ('path', 'sha256')},
            'image_allowance': 2, 'allowed_image_routes': ['native_imagegen'],
            'requests': {slot: prepared['request_sha256']}, 'attempts_per_shot': {slot: 1}}
        scope_path = root / 'production_scopes.json'
        scopes = json.loads(scope_path.read_text()) if scope_path.exists() else {'version': '1.0', 'scopes': []}
        scopes['scopes'].append(scope); write(root, 'production_scopes.json', scopes)
        call = boards.reserve_board_image(root, slot, scope_id=scope['id'], call_id='original-' + slot, images=images)
        envelope = images.begin_native_image(root, call['call_id'])
        receipt = host_receipt(root, envelope, name=slot)
        images.import_native_image(root, call['call_id'], host_receipt=receipt)
        source = boards.bind_imported_board(root, slot, call['call_id'], images=images)
        review = attestation(source['sha256'], ASSET_PREDICATES, revision='r1')
        review['reviewer'] = 'persistent reviewer'
        boards.record_board_review(root, slot, review, roles=[slot + '_frame'], images=images)
    contract = execution.load_shot_contract(root)
    contract['project_review'] = attestation(project_review_subject(contract), PROJECT_PREDICATES, revision='r1')
    contract['shots'][0]['review'] = attestation(shot_review_subject(contract, 's1'), SHOT_PREDICATES, revision='r1')
    write(root, 'artifacts/shot_contract.json', contract)
    assert boards.board_schedule(root, images=images)['board_ready_shots'] == ['s1']
    assert execution.next_production_action(root)['kind'] == 'prepare_motion'
    retained = {p: p.read_bytes() for p in (root / 'production_images').rglob('*') if p.is_file()}
    expected = {name: (root / 'artifacts' / (name + '.json')).read_bytes() for name in ('script', 'scene_plan', 'asset_manifest')}
    for name in expected: (root / 'artifacts' / (name + '.json')).unlink()
    resumed = execution.advance(root)
    assert resumed['status'] == 'host_action' and resumed['action']['kind'] == 'prepare_motion'
    assert 'host_envelope' not in resumed and images.image_usage(root)['total'] == 2
    assert retained == {p: p.read_bytes() for p in (root / 'production_images').rglob('*') if p.is_file()}
    for name, raw in expected.items(): assert (root / 'artifacts' / (name + '.json')).read_bytes() == raw


def test_local_package_rejects_contradictory_retyped_motion_fact(neutral_package):
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['boards']['motion_binding']['contract_template']['shots'][0]['dominant_action'] = 'Contradictory duplicate'
    with pytest.raises(ValueError, match='duplicated motion fact contradicts'):
        request.compile_package(root, bind(package))
    assert not (root / 'artifacts/board_preparation.json').exists()


@pytest.mark.parametrize('insufficient', [None, 'motion', 'images', 'revoked', 'import_crash', 'import_crash_revoked'])
def test_actual_standing_run_derives_native_image_authority_without_new_creator_gate(neutral_package, insufficient):
    from lib import production_boards as boards
    from lib.shot_contract import ASSET_PREDICATES
    root, package, bind = neutral_package
    package = prospective_package(package); binding = bind(package)
    request.prepare_package(root, binding)
    marker = json.loads((root / 'project.json').read_text()); marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    contract = execution.load_shot_contract(root)
    script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        first = projection['assets'][0]['id']
        template = execution.planned_request_template({'duration': shot['duration_seconds'],
            'image_path': {'$board': {'asset_id': first}}}, project_dir=root)
        baselines[shot['id']] = {'projection': projection, 'planned_request_template': template,
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    policy = make_policy(project_id=package['project_id'], story_revision='r1',
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}},
        flex={'duration_s': [1, 10], 'resolution': [], 'references': {'droppable_roles': [], 'substitutes': []}})
    accounts = {'grok_cli': 'fixture-subscription', 'openart_cli': 'fixture-credit-account', 'native_imagegen': 'fixture-native-account'}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [r['sha256'] for r in package['boards']['references']], 'required_permissions': [],
        'native_images': {'account': accounts['native_imagegen'], 'image_allowance': 4, 'allowed_image_routes': ['native_imagegen']},
        'source_transfer': [{'provider': route, 'account': account,
            'roles': ['identity', 'identity_reference', 'start_frame', 'end_frame', 'payoff_board'],
            'generated_board_sources': True, 'max_files': 8, 'max_file_bytes': 10000,
            'max_batch_bytes': 80000, 'max_batches': 4} for route, account in accounts.items()]}
    if insufficient == 'motion':
        policy['caps']['max_total_attempts'] = 2
    elif insufficient == 'images':
        setup['native_images']['image_allowance'] = 3
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': package['project_id'], 'story_revision': 'r1', 'command': 'make_video',
        'standing_setup_sha256': standing['sha256']})
    kwargs = dict(policy=policy, baselines=baselines, standing_setup=standing, run_command=command, identity_id='fixture-identity')
    if insufficient in {'motion', 'images'}:
        with pytest.raises(pa.StandingSetupRequired, match='fixed .* allowance cannot cover'):
            pa.activate_standing_run(root, **kwargs)
        assert not (root / 'artifacts/autonomy_policy.json').exists()
        assert images.image_usage(root)['total'] == 0
        return
    active = pa.activate_standing_run(root, **kwargs)
    begun = execution.advance(root)
    assert begun['status'] == 'host_action' and begun['action']['kind'] == 'receive_native_image', begun.get('reason', begun)
    assert begun['host_envelope']['call_id']
    slot = images.image_call_status(root, begun['host_envelope']['call_id'])['slot_id']
    if insufficient in {'revoked', 'import_crash_revoked'}:
        log = json.loads((root / 'artifacts/decision_log.json').read_text())
        log['decisions'][0]['user_approved'] = False
        write(root, 'artifacts/decision_log.json', log)
    receipt = host_receipt(root, begun['host_envelope'])
    if insufficient in {'import_crash', 'import_crash_revoked'}:
        # Crash after the canonical original import, before derived pack binding.
        images.import_native_image(root, begun['action']['subject'], host_receipt=receipt)
        assert execution.next_production_action(root)['kind'] == 'promote_imported_board'
        recovered = execution.advance(root)
        assert recovered['action']['kind'] == 'review_board' and 'host_envelope' not in recovered
        assert images.image_usage(root)['total'] == 1
        received = execution.complete_action(root, begun['action'], outcome={'host_receipt': receipt})
        assert received['replayed']
    else:
        received = execution.complete_action(root, begun['action'], outcome={'host_receipt': receipt})
    assert received['action']['kind'] == 'review_board'
    source = boards.load_board_pack(root)['board_bindings'][slot]
    review = attestation(source['sha256'], ASSET_PREDICATES, revision='r1'); review['reviewer'] = 'persistent reviewer'
    continued = execution.complete_action(root, received['action'], outcome={'review': review})
    pack = boards.load_board_pack(root)
    assert boards._observation(pack, slot)['review'] == review
    scopes = json.loads((root / 'production_scopes.json').read_text())
    assert scopes['image_envelope']['activation_basis']['policy_sha256'] == active['policy_sha256']
    assert scopes['image_envelope']['approval']['approved_by'].startswith('standing_media_authority:')
    if insufficient in {'revoked', 'import_crash_revoked'}:
        assert continued['status'] == 'blocked'
        assert images.image_usage(root)['total'] == 1
        with pytest.raises(pa.AutonomyError): images.activate_standing_image_envelope(root)
    else:
        assert pa.activate_standing_run(root, **kwargs) == dict(active, replayed=True)


def test_loop_prefers_ready_independent_motion_over_unrelated_host_recovery(p, monkeypatch):
    p.contract['planning_mode'] = 'shot_local_v1'
    p.contract['project_review']['subject_sha256'] = project_review_subject(p.contract)
    for shot in p.contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(p.contract, shot['id'])
    p.persist_contract()
    # The source closure is real; unrelated submitted host intent cannot hold it.
    (p.root / images.ARTIFACT).write_text('{}')
    monkeypatch.setattr(images, '_load', lambda _: {'calls': [{'call_id': 'later-original', 'state': 'submitted',
        'request_sha256': 'a'*64, 'scope_sha256': 'b'*64}]})
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'prepare_motion' and action['subject'] == 'entry'


def test_loop_discloses_usable_failed_candidate_for_draft_without_fake_selection(p):
    result = p.generate('entry')
    aid = result.data['production_attempt_id']
    output_sha = hashlib.sha256((p.root / result.artifacts[0]).read_bytes()).hexdigest()
    review = attestation(output_sha, ['possession'])
    review.update(status='fail', predicates=[{'name': 'possession', 'status': 'fail', 'evidence': 'SYNTHETIC duplicate bill'}])
    execution.record_rejection(p.root, aid, review)
    # Remaining dependent shots cannot start from a failed upstream selection.
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'compose_cut'
    candidate = action['inputs']['candidates'][0]
    assert candidate['review'] == 'fail' and candidate['strict_selected'] is False
    assert execution.load_selected_attempts(p.root) == {}


def test_completed_original_cannot_be_relabelled_failed_and_status_stays_unknown(p):
    from lib.production_draft import first_cut_candidates
    p.transport.timeout_next = True
    original = p.generate('entry'); aid = original.data['production_attempt_id']
    retained = json.loads((p.root / 'production_attempts' / aid / 'request.json').read_text())
    failed = copy.deepcopy(original)
    failed.data['conditioning_receipt'].update(submission_evidence='verified_native_call', dispatch_status='failed')
    failed.data['dispatch_status'] = 'failed'
    with pytest.raises(execution.ProductionGovernanceError, match='failed native completion'):
        execution.reconcile_attempt(p.root, aid, failed, request_sha256=retained['request_sha256'])
    row = first_cut_candidates(p.root)[0]
    assert row['reason'] == 'original_pending' and row['detail']['status'] == 'uncertain'
    assert len(p.transport.native_requests) == len(execution._attempts(p.root)) == 1


def test_local_packet_and_baseline_survive_unrelated_asset_insert_and_prior_timing(p):
    p.contract['planning_mode'] = 'shot_local_v1'
    next(s for s in p.contract['shots'] if s['id'] == 'interior')['upstream'] = []
    p.contract['project_review']['subject_sha256'] = project_review_subject(p.contract)
    for shot in p.contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(p.contract, shot['id'])
    p.persist_contract()
    scene_path = p.root / 'artifacts/scene_plan.json'; scenes = json.loads(scene_path.read_text())
    sections = []
    for i, scene in enumerate(scenes['scenes']):
        scene['script_section_id'] = 'section-' + scene['id']
        scene.update(start_seconds=i*8, end_seconds=(i+1)*8)
        sections.append({'id': scene['script_section_id'], 'text': 'SYNTHETIC local ' + scene['id'],
            'start_seconds': i*8, 'end_seconds': (i+1)*8})
    write(p.root, 'artifacts/scene_plan.json', scenes)
    write(p.root, 'artifacts/script.json', {'version': '1.0', 'title': 'SYNTHETIC local timing',
        'total_duration_seconds': len(sections)*8, 'sections': sections})
    # Interior follows entry; its local timing changes only in cut placement.
    before = pa.current_projection(p.root, 'interior')
    packet = request.source_packet(p.root, 'interior', provider='grok_cli')
    unrelated = copy.deepcopy(p.contract['assets'][-1]); unrelated['id'] = 'unrelated-insert'
    p.contract['assets'].insert(0, unrelated)
    p.contract['shots'][0]['dominant_action'] = 'Unrelated earlier action'
    p.persist_contract()
    scene_path = p.root / 'artifacts/scene_plan.json'; scenes = json.loads(scene_path.read_text())
    script_path = p.root / 'artifacts/script.json'; script = json.loads(script_path.read_text())
    for scene in scenes['scenes'][1:]:
        scene['start_seconds'] += 1; scene['end_seconds'] += 1
    scenes['scenes'][0]['end_seconds'] += 1
    for section in script['sections'][1:]:
        section['start_seconds'] += 1; section['end_seconds'] += 1
    script['sections'][0]['end_seconds'] += 1; script['total_duration_seconds'] += 1
    write(p.root, 'artifacts/scene_plan.json', scenes); write(p.root, 'artifacts/script.json', script)
    after = pa.current_projection(p.root, 'interior')
    for value in (before, after):
        value.pop('contract_snapshot'); value.pop('scene_snapshot')
    assert before == after
    assert packet == request.source_packet(p.root, 'interior', provider='grok_cli')


def test_prepare_motion_preserves_mcp_stage_at_registered_schema_boundary(p, monkeypatch):
    from jsonschema import Draft202012Validator
    from tools.base_tool import ToolResult
    from tools.video.openart_mcp_video import OpenArtMCPVideo
    from tools.tool_registry import registry
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'prepare_motion'
    inputs = {'project_dir': str(p.root), 'governance': {'scope_id': 'placeholder',
        'shot_id': 'entry', 'stage': 'assets'}, 'model': 'pixverseV6', 'mode': 'text2video', 'prompt': 'Synthetic offline shot'}
    tool = OpenArtMCPVideo(); submitted = []
    monkeypatch.setattr(pa, 'derive_scope', lambda *a, **kw: {'id': 'derived'})
    monkeypatch.setattr(registry, 'ensure_discovered', lambda *a: None)
    monkeypatch.setattr(registry, 'get', lambda name: tool)
    def dry(selected, actual):
        Draft202012Validator(selected.input_schema).validate(actual)
        return {'scope_id': actual['governance']['scope_id']}
    monkeypatch.setattr(execution, 'governed_dry_run', dry)
    monkeypatch.setattr(tool, 'execute', lambda actual: submitted.append(copy.deepcopy(actual)) or ToolResult(success=True))
    execution.complete_action(p.root, action, outcome={'provider': 'openart_mcp', 'inputs': inputs})
    assert submitted[0]['governance'] == {'scope_id': 'derived', 'shot_id': 'entry', 'stage': 'assets'}


def test_motion_scope_crash_reuses_exact_scope_before_one_submission(tmp_path, monkeypatch):
    from tools.base_tool import ToolResult
    from tools.tool_registry import registry
    kwargs = standing_fixture(tmp_path); pa.activate_standing_run(tmp_path, **kwargs)
    policy, sha, decision = pa.require_active_policy(tmp_path)
    inputs = {'governance': {'shot_id': 'entry'}, 'duration': 8}
    material = dict(shot_id='entry', provider='grok_cli',
        request_digest=execution.planned_request_digest(inputs, project_dir=tmp_path),
        approval_plan_sha256=execution.approval_plan_digest(execution.load_shot_contract(tmp_path), 'entry'),
        lock_digest='a'*64, compiled_request_sha256='b'*64, preparation_review_id='fixture-review', resolved_upstream={})
    monkeypatch.setattr(pa, '_scope_material', lambda *a, **kw: (policy, sha, decision, copy.deepcopy(material)))
    action = execution._production_action(tmp_path, 'prepare_motion', subject='entry', binding={}, host=True)
    monkeypatch.setattr(execution, 'next_production_action', lambda _: action)
    class OfflineTool:
        def execute(self, actual):
            submitted.append(copy.deepcopy(actual))
            return ToolResult(success=True)
    submitted = []; tool = OfflineTool()
    monkeypatch.setattr(registry, 'ensure_discovered', lambda *a: None)
    monkeypatch.setattr(registry, 'get', lambda name: tool)
    def crash(*args): raise RuntimeError('synthetic crash after scope derivation before reservation')
    monkeypatch.setattr(execution, 'governed_dry_run', crash)
    with pytest.raises(RuntimeError, match='after scope derivation'):
        execution.complete_action(tmp_path, action, outcome={'provider': 'grok_cli', 'inputs': inputs})
    scopes = json.loads((tmp_path / 'production_scopes.json').read_text())['scopes']
    assert len(scopes) == 1 and submitted == []
    monkeypatch.setattr(execution, 'governed_dry_run', lambda *a: {})
    execution.complete_action(tmp_path, action, outcome={'provider': 'grok_cli', 'inputs': inputs})
    assert json.loads((tmp_path / 'production_scopes.json').read_text())['scopes'] == scopes
    assert len(submitted) == 1 and submitted[0]['governance']['scope_id'] == scopes[0]['id']
    changed = dict(material, request_digest='c'*64)
    monkeypatch.setattr(pa, '_scope_material', lambda *a, **kw: (policy, sha, decision, changed))
    with pytest.raises(pa.AutonomyError, match='undispatched scope differs'):
        pa.derive_scope(tmp_path, inputs, provider='grok_cli', reuse_undispatched=True)
    assert len(submitted) == 1


@pytest.mark.parametrize('provider', ['grok_cli', 'openart_mcp'])
def test_motion_waiting_original_does_not_hold_independent_ready_shot(p, monkeypatch, provider):
    from lib import production_draft as draft, openart_mcp_jobs as jobs
    p.contract['planning_mode'] = 'shot_local_v1'
    next(s for s in p.contract['shots'] if s['id'] == 'interior')['upstream'] = []
    p.contract['project_review']['subject_sha256'] = project_review_subject(p.contract)
    for shot in p.contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(p.contract, shot['id'])
    p.persist_contract()
    original = {'attempt_id': 'synthetic-waiting-original', 'shot_id': 'entry', 'media_kind': 'motion',
        'request_sha256': 'a'*64, 'provider': provider, 'scope': {'provider': provider}}
    monkeypatch.setattr(execution, '_attempts', lambda _: [original])
    monkeypatch.setattr(execution, '_state', lambda *a: {'status': 'uncertain'})
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda _: [])
    monkeypatch.setattr(jobs, 'resume_original', lambda *a: {'request_sha256': 'a'*64,
        'next_action': {'action': 'poll_original', 'attempt_id': original['attempt_id']}})
    action = execution.next_production_action(p.root)
    assert action['kind'] == 'prepare_motion' and action['subject'] == 'interior'


@pytest.mark.parametrize('cut', [
    {'status': 'incomplete', 'technical': {'has_audio': True}, 'missing': ['payoff']},
    {'status': 'complete', 'technical': None, 'missing': []}])
def test_current_first_cut_requires_complete_technical_master(p, monkeypatch, cut):
    from lib import production_draft as draft
    candidates = [{'shot_id': s['id'], 'status': 'candidate', 'review': 'pass'} for s in p.contract['shots']]
    monkeypatch.setattr(draft, 'first_cut_candidates', lambda _: candidates)
    monkeypatch.setattr(draft, 'latest_first_cut', lambda _: dict(cut, path='existing-cut.json', sha256='a'*64))
    monkeypatch.setattr(draft, 'first_cut_status', lambda *a: {'status': 'current'})
    assert execution.advance(p.root)['status'] == 'ready_to_compose'


def upload_standing_fixture(root, *, provider, max_batches=1, uid_sha256=None):
    kwargs = standing_fixture(root)
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    if provider == 'openart_mcp':
        spec = {'id': provider, 'billing': 'unknown_cost_no_ceiling',
            'exposure_acknowledgement': 'no_enforceable_credit_ceiling', 'uid_sha256': uid_sha256,
            'project_id': 'fixture-openart-project', 'routes': [{'model': 'pixverseV6', 'mode': 'image2video'}]}
        kwargs['policy']['providers'].append(spec); setup['providers'].append(copy.deepcopy(spec))
        setup['provider_accounts'][provider] = 'fixture-mcp-account'
        setup['source_transfer'].append(dict(setup['source_transfer'][0], provider=provider, account='fixture-mcp-account'))
    for term in setup['source_transfer']: term['max_batches'] = max_batches
    kwargs['standing_setup'] = creator_fixture(root, 'standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'command', command)
    pa.activate_standing_run(root, **kwargs)
    return pa.require_active_policy(root)[0]


def test_inline_transfer_final_batch_revalidates_same_original_and_zero_sources(tmp_path):
    policy = upload_standing_fixture(tmp_path, provider='grok_cli')
    inputs = {'governance': {'shot_id': 'entry'}, 'image_path': str(tmp_path / 'assets/start.svg')}
    pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli')
    write(tmp_path, 'production_attempts/final-original/request.json', {'attempt_id': 'final-original',
        'scope_id': 'exact-final-scope', 'scope': {'provider': 'grok_cli'}, 'submitted_inputs': inputs})
    # Adapter revalidation validates the current original, not another batch.
    pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli', exclude_scope_id='exact-final-scope')
    with pytest.raises(pa.StandingSetupRequired, match='batch allowance exhausted'):
        pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli')
    pa._validate_standing_transfer(tmp_path, policy, {'governance': {'shot_id': 'entry'}}, 'grok_cli')
    # A zero-source original never consumes source-transfer allowance.
    write(tmp_path, 'production_attempts/no-source/request.json', {'attempt_id': 'no-source',
        'scope_id': 'no-source', 'scope': {'provider': 'grok_cli'}, 'submitted_inputs': {'duration': 8}})
    pa._validate_standing_transfer(tmp_path, policy, inputs, 'grok_cli', exclude_scope_id='exact-final-scope')


def test_cli_standing_upload_exact_final_batch_replay_account_bytes_and_revocation(tmp_path, monkeypatch):
    policy = upload_standing_fixture(tmp_path, provider='openart_cli')
    spec = pa._provider_spec(policy, 'openart_cli')
    account = {'account_id_sha256': spec['account_id_sha256'], 'workspace': spec['workspace']}
    source = str(tmp_path / 'assets/start.svg')
    kwargs = dict(shot_id='entry', provider='openart_cli', files=[source], account=account)
    first = pa.derive_standing_upload_authority(tmp_path, **kwargs)
    assert first['provider_calls'] == 0 and first['authority']['approved_by'].startswith('standing_media_authority:')
    # Already-counted motion using a remote upload is not a fresh upload batch.
    write(tmp_path, 'production_attempts/motion/request.json', {'attempt_id': 'motion', 'scope_id': 'motion',
        'scope': {'provider': 'openart_cli'}, 'submitted_inputs': {'image_path': source}})
    assert pa.derive_standing_upload_authority(tmp_path, **kwargs) == dict(first, replayed=True)
    sha = hashlib.sha256((tmp_path / 'assets/start.svg').read_bytes()).hexdigest()
    assert request.approved_upload_lookup(tmp_path, first['upload_id'], sha)['state'] == 'approved'
    with pytest.raises(pa.StandingSetupRequired, match='upload batch allowance exhausted'):
        pa.derive_standing_upload_authority(tmp_path, **dict(kwargs, files=[str(tmp_path / 'assets/end.svg')]))
    with pytest.raises(pa.StandingSetupRequired, match='account/workspace'):
        pa.derive_standing_upload_authority(tmp_path, **dict(kwargs, account=dict(account, workspace='other')))
    from lib import openart_jobs as cli_jobs
    monkeypatch.setattr(cli_jobs, '_upload_profile', lambda *a: {'mode': 'image2video', 'model': 'kling-3', 'account_id_sha256': 'f'*64})
    monkeypatch.setattr(cli_jobs, 'upload_binding', lambda *a: {})
    monkeypatch.setattr(cli_jobs, '_UPLOAD_APPROVAL_LOOKUP', request.approved_upload_lookup)
    with pytest.raises(cli_jobs.OpenArtCLIError, match='account/model differs'):
        cli_jobs.upload_reference(tmp_path, first['upload_id'], tmp_path / 'assets/start.svg', model='kling-3', mode='image2video')
    raw = (tmp_path / 'assets/start.svg').read_bytes()
    (tmp_path / 'assets/start.svg').write_bytes(raw + b'changed')
    assert request.approved_upload_lookup(tmp_path, first['upload_id'], sha) is None
    (tmp_path / 'assets/start.svg').write_bytes(raw)
    log = json.loads((tmp_path / 'artifacts/decision_log.json').read_text()); log['decisions'][0]['user_approved'] = False
    write(tmp_path, 'artifacts/decision_log.json', log)
    assert request.approved_upload_lookup(tmp_path, first['upload_id'], sha) is None
    assert len(list((tmp_path / 'artifacts').glob('upload_approval-*.json'))) == 1


from tests.integration.test_openart_mcp_governance import fixture_observations  # noqa: F401,E402
from tests.integration.test_openart_mcp_autonomy import lifecycle  # noqa: F401,E402


def test_mcp_standing_upload_final_batch_original_receipt_and_account_revalidation(tmp_path, fixture_observations):
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs
    root = tmp_path / 'standing-mcp'; root.mkdir()
    account = mcp.account_summary()
    upload_standing_fixture(root, provider='openart_mcp', uid_sha256=account['uid_sha256'])
    args = dict(shot_id='entry', provider='openart_mcp', files=[str(root / 'assets/start.svg')],
        account={'uid_sha256': account['uid_sha256']}, openart_project_id='fixture-openart-project')
    authority = pa.derive_standing_upload_authority(root, **args)
    declaration = {'upload_authorization_id': authority['upload_id']}
    begun = jobs.prepare_upload(root, files=args['files'], billing_declaration=declaration,
        openart_project_id=args['openart_project_id'])
    # The canonical consumed batch is revalidated at its original receipt.
    assert pa.derive_standing_upload_authority(root, **args) == dict(authority, replayed=True)
    result = {'projectId': args['openart_project_id'], 'results': [{'fileId': 'fixture-source',
        'status': 'SUCCESS', 'visualReference': {'id': 'fixture-ref', 'type': 'image',
            'url': 'https://fixture.invalid/source.svg', 'metadata': {}}}]}
    with pytest.raises(ValueError, match='different|invalid|exact'):
        jobs._upload_authority(root, authority['authority']['files'], declaration,
            {'uid_sha256': 'f'*64}, args['openart_project_id'])
    # Exact existing owner can still resolve the final already-started batch.
    jobs.record_upload_receipt(root, upload_id=begun['upload_id'], result=result)
    with pytest.raises(pa.StandingSetupRequired, match='upload batch allowance exhausted'):
        pa.derive_standing_upload_authority(root, **dict(args, files=[str(root / 'assets/end.svg')]))
    with pytest.raises(ValueError, match='already consumed'):
        jobs.prepare_upload(root, files=args['files'], billing_declaration=declaration,
            openart_project_id=args['openart_project_id'])


@pytest.mark.parametrize('unrelated', [False, True])
def test_standing_mcp_zero_source_prepare_then_begin_at_final_media_slot(lifecycle, monkeypatch, tmp_path, unrelated):
    from tests.integration.test_openart_alternate_model_repair import _compile
    root, policy, _, inputs, _, _, jobs = lifecycle
    contract = execution.load_shot_contract(root); contract['planning_mode'] = 'shot_local_v1'
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    contract['shots'][0]['review']['subject_sha256'] = shot_review_subject(contract, 'entry')
    if unrelated:
        other = copy.deepcopy(contract['shots'][0]); other['id'] = 'other'; contract['shots'].append(other)
        contract['project_review']['subject_sha256'] = project_review_subject(contract)
        for shot in contract['shots']: shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
        scenes = json.loads((root / 'artifacts/scene_plan.json').read_text())
        scene = copy.deepcopy(scenes['scenes'][0]); scene['id'] = 'other'; scenes['scenes'].append(scene)
        write(root, 'artifacts/scene_plan.json', scenes)
        policy['caps']['max_total_attempts'] = 2
    write(root, 'artifacts/shot_contract.json', contract)
    marker = json.loads((root / 'project.json').read_text()); marker['identity_id'] = 'fixture-identity'
    write(root, 'project.json', marker)
    timing = json.loads((root / 'artifacts/compiled_request-original.json').read_text())['timing']
    inputs, native, _ = _compile(root, inputs, 'standing-zero-source', timing)
    projection = pa.current_projection(root, 'entry')
    baselines = {'entry': {'projection': projection,
        'planned_request_template': execution.planned_request_template(inputs, project_dir=root),
        'script_snapshot': json.loads((root / 'artifacts/script.json').read_text()),
        'dialogue_coverage': pa.dialogue_occurrences(projection),
        'approval_plan_sha256': execution.approval_plan_digest(contract, 'entry')}}
    if unrelated:
        other_projection = pa.current_projection(root, 'other')
        baselines['other'] = dict(copy.deepcopy(baselines['entry']), projection=other_projection,
            dialogue_coverage=pa.dialogue_occurrences(other_projection),
            approval_plan_sha256=execution.approval_plan_digest(contract, 'other'))
    accounts = {p['id']: 'fixture-' + p['id'] for p in policy['providers']}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': accounts, 'caps': policy['caps'],
        'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [], 'required_permissions': [],
        'source_transfer': [{'provider': provider, 'account': account, 'roles': [],
            'generated_board_sources': False, 'max_files': 1, 'max_file_bytes': 1,
            'max_batch_bytes': 1, 'max_batches': 1} for provider, account in accounts.items()]}
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'],
        'command': 'make_video', 'standing_setup_sha256': standing['sha256']})
    # Fixture replaces unspent fixture authority, never a production record.
    (root / 'artifacts/autonomy_policy.json').unlink(); (root / 'artifacts/decision_log.json').unlink()
    write(root, 'production_scopes.json', {'version': '1.0', 'scopes': []})
    activation = pa.activate_standing_run(root, policy=policy, baselines=baselines,
        standing_setup=standing, run_command=command, identity_id='fixture-identity')
    scope = pa.derive_scope(root, inputs, provider='openart_mcp'); inputs['governance']['scope_id'] = scope['id']
    jobs.prepare(root, attempt_id='standing-final-original', generation_inputs=inputs,
        authority_fn=execution.prepare_openart_mcp_handoff)
    begun = jobs.begin(root, 'standing-final-original', authority_fn=execution.prepare_openart_mcp_handoff)
    assert begun['arguments'] == native['body']
    assert jobs.frozen_request(root, 'standing-final-original')['authority']['billing']['policy_sha256'] == activation['policy_sha256']
    assert pa.root_attempt_counts(root, pa.require_active_policy(root)[0])['total'] == 1
    with pytest.raises(ValueError): jobs.begin(root, 'standing-final-original', authority_fn=execution.prepare_openart_mcp_handoff)
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    from tests.integration.test_openart_mcp_governance import install_fake_download
    from lib import production_provenance as provenance
    from lib.shot_contract import file_sha256, selection_digest, UPSTREAM_PREDICATES
    from tests.lib.test_production_images import PNG
    aid = 'standing-final-original'
    jobs.receive(root, aid, outcome={'historyId': 'fixture-standing-original', 'status': 'PENDING'})
    jobs.record_status(root, aid, result={'historyId': 'fixture-standing-original', 'status': 'COMPLETED',
        'resources': [{'id': 'fixture-video', 'mediaType': 'video', 'status': 'COMPLETED',
            'url': 'https://fixture.invalid/standing.mp4'}]})
    media = real_av_clip(tmp_path / 'fixture-standing.mp4', seconds=1)
    install_fake_download(monkeypatch, jobs, media)
    downloaded = jobs.download_original(root, aid); jobs.collect(root, aid, downloaded_path=downloaded['downloaded_path'])
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    output = {'path': inputs['output_path'], 'sha256': file_sha256(inputs['output_path'])}
    validate = lambda: provenance.validate_attempt_provenance(root, aid, shot_id='entry',
        story_revision=contract['story_revision'], expected_output=output)
    assert validate()['result']['status'] == 'generated'
    if unrelated:
        current = execution.load_shot_contract(root)
        current['shots'][1]['dominant_action'] = 'SYNTHETIC unrelated new action'
        write(root, 'artifacts/shot_contract.json', current)
        assert validate()['result']['status'] == 'generated'
    frame = root / 'observed-outgoing.png'; frame.write_bytes(PNG)
    selection = {'attempt_id': aid, 'output': output, 'outgoing_frame': {'path': str(frame), 'sha256': file_sha256(frame)}}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES, revision=contract['story_revision'])
    execution.record_selection(root, 'entry', selection)
    assert execution.load_selected_attempts(root)['entry'] == selection
    current = execution.load_shot_contract(root); current['shots'][0]['dominant_action'] = 'SYNTHETIC changed target action'
    write(root, 'artifacts/shot_contract.json', current)
    with pytest.raises(ValueError): validate()


def grok_final_fixture(tmp_path, **extra):
    """Real activated Grok policy with reviewed compiled request; no provider calls."""
    kwargs = standing_fixture(tmp_path)
    contract = execution.load_shot_contract(tmp_path); shot = contract['shots'][0]
    inputs = {'project_dir': str(tmp_path), 'governance': {'shot_id': 'entry'},
        'prompt': 'Synthetic placeholder before activation', 'operation': 'reference_to_video', 'duration': 8, 'image_path': str(tmp_path / 'assets/start.svg'),
        'output_path': str(tmp_path / 'clip.mp4'), 'compiled_request_id': 'final', 'preparation_review_id': 'final'}
    from lib.shot_contract import required_asset_ids
    consumer = required_asset_ids(contract, shot)
    inputs['reference_image_paths'] = [str(tmp_path / a['path']) for a in contract['assets']
        if a['id'] in consumer and a['id'] != 'start']
    inputs.update(extra)
    kwargs['baselines']['entry']['planned_request_template'] = execution.planned_request_template(inputs, project_dir=tmp_path)
    setup = json.loads((tmp_path / kwargs['standing_setup']['path']).read_text()); setup.pop('authorization')
    next(t for t in setup['source_transfer'] if t['provider'] == 'grok_cli')['max_batches'] = 1
    kwargs['standing_setup'] = creator_fixture(tmp_path, 'standing', setup)
    command = json.loads((tmp_path / kwargs['run_command']['path']).read_text()); command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(tmp_path, 'command', command)
    pa.activate_standing_run(tmp_path, **kwargs)
    observation = {'cli_version': '1.0.34', 'grok_path': '/synthetic-offline/grok'}
    authored = request.compile_provider_prompt(tmp_path, 'entry', provider='grok_cli')
    inputs['prompt'] = authored['prompt']
    native = request.prepare_grok_native(inputs, observation)
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
        'rationale': 'Synthetic offline fixture', 'overlap_policy': 'serial', 'overlap_rationale': 'Synthetic serial windows',
        'segments': [{'dialogue_index': i, 'text_sha256': request.digest(line['text']), 'language': 'en',
            'word_count': 2, 'words_per_minute': 180, 'pause_seconds': 0, 'rationale': 'Synthetic fixture'}
            for i, line in enumerate(shot['dialogue'])],
        'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key, 'value_sha256': request.digest(shot[key]),
            'start_seconds': start, 'end_seconds': end, 'rationale': 'Synthetic fixture'}
            for key, start, end in [('dominant_action', 4.1, 7), ('completed_end_state', 7, 7.4)]]}
    compiled = request.prepare_compiled_request(inputs, native, {'source': 'real'}, coverage=authored['coverage'], timing=timing)
    write(tmp_path, 'artifacts/compiled_request-final.json', compiled)
    write(tmp_path, 'artifacts/preparation_review-final.json', {'version': '1.0', 'review_id': 'final',
        'reviewer': 'SYNTHETIC offline reviewer', 'status': 'pass', 'subject_sha256': request.digest(compiled),
        'evidence_kind': 'reviewed', 'predicates': [{'name': p, 'status': 'pass', 'severity': 'critical',
            'evidence': 'SYNTHETIC software fixture only'} for p in sorted(request.PREDICATES)]})
    return contract, inputs, observation


def test_grok_final_inline_batch_scope_revalidates_after_original_reservation(tmp_path):
    contract, inputs, observation = grok_final_fixture(tmp_path)
    scope = pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    inputs['governance']['scope_id'] = scope['id']
    write(tmp_path, 'production_attempts/final-reservation/request.json', {'attempt_id': 'final-reservation',
        'scope_id': scope['id'], 'scope': scope, 'shot_id': 'entry', 'story_revision': contract['story_revision'],
        'media_kind': 'motion', 'phase': 'first_pass', 'request_sha256': scope['requests']['entry'],
        'submitted_inputs': copy.deepcopy(inputs)})
    assert pa.validate_derived_scope(tmp_path, scope, inputs=inputs, observation=observation)
    with pytest.raises(pa.StandingSetupRequired, match='batch allowance exhausted'):
        pa.derive_scope(tmp_path, inputs, provider='grok_cli', observation=observation)
    assert len(execution._attempts(tmp_path)) == 1


@pytest.mark.parametrize('second_referenced', [False, True])
def test_standing_two_native_boards_zero_source_calls_preserve_transfer_batch(neutral_package, second_referenced):
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package); spec = package['boards']
    keep = {'start', 'other'}
    spec['image_nodes'] = [n for n in spec['image_nodes'] if n['slot_id'] in keep]
    for node in spec['image_nodes']:
        node['reference_ids'] = ['cast'] if second_referenced and node['slot_id'] == 'other' else []
        node['image_dependencies'] = []
    spec['shots'] = [s for s in spec['shots'] if s['shot_id'] in {'s1', 's3'}]
    for shot in spec['shots']: shot['roles'] = [r for r in shot['roles'] if r['asset_id'] in keep or r['asset_id'] == 'cast']
    spec['shared_roles'] = [r for r in spec['shared_roles'] if r['asset_id'] == 'cast']
    template = spec['motion_binding']['contract_template']; template['payoff_asset_id'] = 'other'
    template['assets'] = [a for a in template['assets'] if a['id'] in keep]
    template['shots'] = [s for s in template['shots'] if s['id'] in {'s1', 's3'}]
    for shot in template['shots']: shot['asset_ids'] = [a for a in shot['asset_ids'] if a in keep]
    package['shots'] = [s for s in package['shots'] if s['id'] in {'s1', 's3'}]
    request.prepare_package(root, bind(package))
    marker = json.loads((root / 'project.json').read_text()); marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    contract = execution.load_shot_contract(root); script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        baselines[shot['id']] = {'projection': projection,
            'planned_request_template': execution.planned_request_template({'duration': shot['duration_seconds']}, project_dir=root),
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    policy = make_policy(project_id=contract['project_id'], story_revision=contract['story_revision'],
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
    accounts = {'grok_cli': 'fixture-grok', 'openart_cli': 'fixture-cli', 'native_imagegen': 'fixture-native'}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [r['sha256'] for r in spec['references']], 'required_permissions': [],
        'native_images': {'account': accounts['native_imagegen'], 'image_allowance': 2, 'allowed_image_routes': ['native_imagegen']},
        'source_transfer': [{'provider': provider, 'account': account, 'roles': ['identity', 'start_frame', 'end_frame'],
            'generated_board_sources': True, 'max_files': 8, 'max_file_bytes': 10000,
            'max_batch_bytes': 80000, 'max_batches': 1} for provider, account in accounts.items()]}
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'], 'command': 'make_video',
        'standing_setup_sha256': standing['sha256']})
    pa.activate_standing_run(root, policy=policy, baselines=baselines, standing_setup=standing,
        run_command=command, identity_id='fixture-identity')
    retained = images.activate_standing_image_envelope(root)
    for slot in ('start', 'other'):
        prepared = boards.prepare_image_request(root, slot, images=images)
        assert bool(prepared['packet']['references']) == (second_referenced and slot == 'other')
        scope = images.derive_native_image_scope(root, prepared['packet'], slot, prepared['arguments'], output_path=prepared['output_path'])
        call = boards.reserve_board_image(root, slot, scope_id=scope, call_id='fixture-' + slot, images=images)
        envelope = images.begin_native_image(root, call['call_id'])
        # Exact already-counted original admission excludes itself from batch count.
        images._standing_native_transfer(root, retained, prepared['packet'], exclude_call_id=call['call_id'])
        images.import_native_image(root, call['call_id'], host_receipt=host_receipt(root, envelope, name='receipt-' + slot))
        boards.bind_imported_board(root, slot, call['call_id'], images=images)
    assert images.image_usage(root)['total'] == 2
    calls = images._load(root)['calls']
    assert sum(bool(call['packet']['references']) for call in calls) == int(second_referenced)
    if second_referenced:
        with pytest.raises(pa.StandingSetupRequired, match='batch allowance exhausted'):
            images._standing_native_transfer(root, retained, calls[-1]['packet'])


@pytest.mark.parametrize('tamper', [None, 'handoff', 'reference', 'mixed', 'spec', 'pointer', 'opaque'])
def test_standing_board_materialization_derives_exact_authored_handoff_only(neutral_package, tamper):
    """Promoted reviewed boards may add only the retained package's exact handoff rows."""
    from lib import production_boards as boards
    from lib.shot_contract import ASSET_PREDICATES
    root, package, bind = neutral_package
    package = prospective_package(package)
    package['shots'][0]['pinned_final_frame'] = {'required': True, 'requirement_id': 's1-ending',
                                                 'end_state': package['shots'][0]['end_state']}
    first = 'cast' if tamper in ('mixed', 'opaque') else 'start'
    end = 'end--final' if tamper == 'opaque' else 'end'
    if tamper == 'opaque':
        # Opaque base IDs may contain '--'; only the exact shot alias suffix is special.
        package['boards'] = json.loads(json.dumps(package['boards']).replace('"end"', '"end--final"'))
    if tamper in ('mixed', 'opaque'):
        # Retained reviewed reuse starts the handoff; the same-run board ends it.
        package['boards']['motion_binding']['contract_template']['assets'].append(
            {'id': 'cast', 'role': 'start_frame', 'cast_ids': []})
    package['boards']['motion_binding']['motion_handoffs'] = {first: {'asset_id': first, 'scene_id': 's1',
        'approved': True, 'pinned_final_frame': {'asset_id': end, 'approved': True, 'requirement_id': 's1-ending'}}}
    request.prepare_package(root, bind(package))
    marker = json.loads((root / 'project.json').read_text()); marker.update(identity_id='fixture-identity', pipeline_type='cinematic')
    write(root, 'project.json', marker)
    contract = execution.load_shot_contract(root); script = json.loads((root / 'artifacts/script.json').read_text())
    baselines = {}
    for shot in contract['shots']:
        projection = pa.current_projection(root, shot['id'])
        baselines[shot['id']] = {'projection': projection,
            'planned_request_template': execution.planned_request_template({'duration': shot['duration_seconds']}, project_dir=root),
            'script_snapshot': script, 'dialogue_coverage': pa.dialogue_occurrences(projection),
            'approval_plan_sha256': execution.approval_plan_digest(contract, shot['id'])}
    assert baselines['s1']['projection']['manifest_requirements']['motion_handoffs'] == {}
    policy = make_policy(project_id=contract['project_id'], story_revision=contract['story_revision'],
        locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}})
    accounts = {'grok_cli': 'fixture-grok', 'openart_cli': 'fixture-cli', 'native_imagegen': 'fixture-native'}
    setup = {'version': '1.0', 'identity_id': 'fixture-identity', 'enabled': True,
        'providers': policy['providers'], 'provider_accounts': {k: v for k, v in accounts.items() if k != 'native_imagegen'},
        'caps': policy['caps'], 'checkpoint_stages': policy['checkpoint_stages'], 'flex': policy['flex'],
        'approved_source_sha256': [r['sha256'] for r in package['boards']['references']], 'required_permissions': [],
        'native_images': {'account': accounts['native_imagegen'], 'image_allowance': 4, 'allowed_image_routes': ['native_imagegen']},
        'source_transfer': [{'provider': provider, 'account': account,
            'roles': ['identity', 'start_frame', 'end_frame', 'payoff_board'], 'generated_board_sources': True,
            'max_files': 8, 'max_file_bytes': 10000, 'max_batch_bytes': 80000, 'max_batches': 4}
            for provider, account in accounts.items()]}
    standing = creator_fixture(root, 'standing', setup)
    command = creator_fixture(root, 'command', {'version': '1.0', 'identity_id': 'fixture-identity',
        'project_id': contract['project_id'], 'story_revision': contract['story_revision'], 'command': 'make_video',
        'standing_setup_sha256': standing['sha256']})
    pa.activate_standing_run(root, policy=policy, baselines=baselines, standing_setup=standing,
        run_command=command, identity_id='fixture-identity')
    step = execution.advance(root)
    reviewed = set()
    while {'start', end} - reviewed:
        assert step['status'] == 'host_action' and step['action']['kind'] == 'receive_native_image', step
        slot = images.image_call_status(root, step['host_envelope']['call_id'])['slot_id']
        received = execution.complete_action(root, step['action'],
            outcome={'host_receipt': host_receipt(root, step['host_envelope'], name='receipt-' + slot)})
        source = boards.load_board_pack(root)['board_bindings'][slot]
        review = attestation(source['sha256'], ASSET_PREDICATES, revision='r1'); review['reviewer'] = 'persistent reviewer'
        step = execution.complete_action(root, received['action'], outcome={'review': review})
        reviewed.add(slot)
    manifest_path = root / 'artifacts/asset_manifest.json'
    current = pa.current_projection(root, 's1')['manifest_requirements']
    assert current['motion_handoffs'] == package['boards']['motion_binding']['motion_handoffs']
    assert {a['id'] for a in current['assets']} >= {first, end}
    if tamper in ('mixed', 'opaque'):
        row = next(a for a in current['assets'] if a['id'] == 'cast')
        assert row['source_tool'] == 'provided'
    if tamper in ('handoff', 'reference', 'spec', 'pointer'):
        manifest = json.loads(manifest_path.read_text())
        if tamper in ('handoff', 'spec', 'pointer'):
            manifest['metadata']['motion_handoffs']['start']['pinned_final_frame']['requirement_id'] = 'other-ending'
        if tamper == 'spec':
            # Live pack spec rewritten consistently (digest recomputed) while the
            # hash-bound retained package and activation stay unchanged.
            pack_path = root / boards.ARTIFACT
            pack = json.loads(pack_path.read_text())
            pack['specification']['motion_binding']['motion_handoffs']['start']['pinned_final_frame']['requirement_id'] = 'other-ending'
            pack['specification_sha256'] = boards._digest(pack['specification'])
            pack_path.write_text(json.dumps(pack))
            assert boards.load_board_pack(root)['specification']['motion_binding']['motion_handoffs'] == \
                manifest['metadata']['motion_handoffs']
        if tamper == 'pointer':
            # New hash-bound package with the altered handoff, pack pointer and live
            # spec repointed consistently; the retained image envelope is unchanged.
            altered = copy.deepcopy(package)
            altered['boards']['motion_binding']['motion_handoffs']['start']['pinned_final_frame']['requirement_id'] = 'other-ending'
            data = json.dumps(altered).encode()
            (root / 'notes/package-altered.json').write_bytes(data)
            pack_path = root / boards.ARTIFACT
            pack = json.loads(pack_path.read_text())
            pack['package_binding'] = {'path': 'notes/package-altered.json', 'sha256': hashlib.sha256(data).hexdigest()}
            pack['specification'] = altered['boards']
            pack['specification_sha256'] = boards._digest(pack['specification'])
            pack_path.write_text(json.dumps(pack))
            boards.load_board_pack(root)
        else:
            manifest['metadata'].setdefault('reference_assets', {})['cast'] = {'scene_id': 's1', 'role': 'identity'}
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(pa.AutonomyError, match='changed outside approved flex: /manifest_requirements'):
            pa.require_active_policy(root, shot_id='s1')
        return
    pa.require_active_policy(root, shot_id='s1')
    projection = pa.current_projection(root, 's1')
    assert pa.contract_delta(root, 's1', projection) == (None, [])


def test_prepare_motion_actual_grok_dry_run_observes_fresh_outside_lock_and_resumes_scope(tmp_path, monkeypatch):
    """Actual derive, canonical dry run, preflight and registered execute; only CLI boundaries are fake."""
    import fcntl
    from tools import _grok_cli_media as media
    from tools.base_tool import ToolResult
    from tools.video import grok_cli_video
    planning = {'preferred_tool': 'grok_cli_video', 'hosting_provider': 'grok_cli', 'preferred_provider': 'grok_cli',
        'preferred_provider_gap': 0.1, 'allowed_providers': ['grok_cli'], 'task_context': 'synthetic offline plan',
        'target_operation': 'reference_to_video'}
    contract, inputs, observation = grok_final_fixture(tmp_path, allow_unknown_cost=True, **planning)
    lock = tmp_path / '.production-execution.lock'
    observed, submitted = [], []
    def observe(path, *, cwd):
        if lock.exists():
            with lock.open('a+b') as probe:  # raises if production lock is held during observation
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        observed.append(str(cwd)); return dict(observation)
    def transport(**kw):
        submitted.append(copy.deepcopy(kw))
        return ToolResult(success=True, data={'cli_version': observation['cli_version'],
            'session_id': kw['session_id'], 'dispatch_status': 'synthetic_offline'})
    monkeypatch.setattr(media, 'observe_grok_cli_compatibility', observe)
    monkeypatch.setattr(grok_cli_video, 'execute_grok_cli_media', transport)
    monkeypatch.setenv('GROK_CLI_PATH', observation['grok_path'])
    action = execution._production_action(tmp_path, 'prepare_motion', subject='entry', binding={}, host=True)
    monkeypatch.setattr(execution, 'next_production_action', lambda _: action)
    real = execution._execute_governed
    def crash(*a): raise RuntimeError('synthetic crash after canonical dry run before reservation')
    monkeypatch.setattr(execution, '_execute_governed', crash)
    outcome = {'provider': 'grok_cli', 'inputs': copy.deepcopy(inputs), 'observation': dict(observation)}
    with pytest.raises(RuntimeError, match='before reservation'):
        execution.complete_action(tmp_path, action, outcome=copy.deepcopy(outcome))
    scopes = json.loads((tmp_path / 'production_scopes.json').read_text())['scopes']
    assert len(scopes) == 1 and submitted == [] and not execution._attempts(tmp_path)
    assert len(observed) == 2  # dry run and execute each observe afresh
    monkeypatch.setattr(execution, '_execute_governed', real)
    result = execution.complete_action(tmp_path, action, outcome=copy.deepcopy(outcome))
    assert json.loads((tmp_path / 'production_scopes.json').read_text())['scopes'] == scopes
    assert result['status'] == 'dispatched' and result['dry_run']['governed'] and result['dry_run']['provider_calls'] == 0
    assert result['result']['success'] is True, result['result']
    assert len(submitted) == 1 and len(execution._attempts(tmp_path)) == 1 and len(observed) == 4
    # Native transport receives only CLI controls; the durable record keeps the
    # full governed request (planning fields included) bound to its digest.
    assert not set(planning) & set(submitted[0]['arguments'])
    attempt = execution._attempts(tmp_path)[0]
    assert {k: attempt['submitted_inputs'][k] for k in planning} == planning
    assert attempt['request_sha256'] == scopes[0]['requests']['entry']


def test_shared_identity_reference_first_consumed_by_later_shot_keeps_stable_aliases(neutral_package):
    # Shared identity_reference actually consumed only by s2: compile, validation
    # and local motion promotion must agree on one alias per consumer shot.
    from lib import production_boards as boards
    root, package, bind = neutral_package
    package = prospective_package(package)
    spec = package['boards']
    reference = spec['references'][0]
    reference['role'] = 'identity_reference'
    reference['applicability']['roles'] = ['identity_reference', 'start_frame', 'end_frame']
    spec['shared_roles'][0]['role'] = 'identity_reference'
    template = spec['motion_binding']['contract_template']
    template['assets'].append({'id': 'cast', 'role': 'identity_reference', 'cast_ids': ['actor']})
    for shot in template['shots']:
        shot['cast_ids'] = ['actor'] if shot['id'] == 's2' else []
    binding = bind(package)
    assert request.prepare_package(root, binding)['status'] == 'prepared'
    manifest = json.loads((root / 'artifacts/asset_manifest.json').read_text())
    pairs = [(a['id'], a['scene_id']) for a in manifest['assets'] if a['id'].startswith('cast')]
    assert pairs == [('cast', 's1'), ('cast--s2', 's2'), ('cast--s3', 's3')]
    assert all(boards.motion_reference_alias(spec, 'cast', sid) == aid for aid, sid in pairs)
    first = (root / 'artifacts/asset_manifest.json').read_bytes()
    request.prepare_package(root, binding)
    assert (root / 'artifacts/asset_manifest.json').read_bytes() == first
    assert images.image_usage(root)['total'] == 0


def test_corrected_board_never_reschedules_superseded_imported_original(neutral_package, original_host_exception=False):
    # Original critically rejected, exact corrected board passes: the retained
    # original stays counted/imported but is never promoted again.
    from lib import production_boards as boards
    from lib.shot_contract import ASSET_PREDICATES
    root, package, bind = neutral_package
    package = prospective_package(package); request.prepare_package(root, bind(package))
    def run(call_id, correction=None, fail=False):
        prepared = boards.prepare_image_request(root, 'start', correction=correction, images=images)
        scope = {'id': 'scope-' + call_id, 'status': 'approved', 'approved_by': 'SYNTHETIC creator fixture',
            'project_id': package['project_id'], 'story_revision': 'r1', 'phase': 'image', 'provider': 'native_imagegen',
            'evidence': {k: package['boards']['approval'][k] for k in ('path', 'sha256')},
            'image_allowance': 2, 'allowed_image_routes': ['native_imagegen'],
            'requests': {'start': prepared['request_sha256']}, 'attempts_per_shot': {'start': 1}}
        path = root / 'production_scopes.json'
        scopes = json.loads(path.read_text()) if path.exists() else {'version': '1.0', 'scopes': []}
        scopes['scopes'].append(scope); write(root, 'production_scopes.json', scopes)
        boards.reserve_board_image(root, 'start', scope_id=scope['id'], call_id=call_id, correction=correction, images=images)
        envelope = images.begin_native_image(root, call_id)
        if original_host_exception and call_id == 'original-start':
            from tests.lib.test_production_images import file_sha256
            write(root, 'receipts/raised.json', {'call_id': call_id, 'tool_name': envelope['tool_name'],
                'arguments': envelope['arguments'], 'exception': {'message': 'SYNTHETIC raised host exception'}})
            raised = root / 'receipts/raised.json'
            images.record_native_image_host_exception(root, call_id,
                host_exception={'path': str(raised), 'sha256': file_sha256(raised)})
        images.import_native_image(root, call_id, host_receipt=host_receipt(root, envelope, name=call_id))
        source = boards.bind_imported_board(root, 'start', call_id, images=images)
        review = attestation(source['sha256'], ASSET_PREDICATES, revision='r1')
        review.update(reviewer='persistent reviewer', review_id='review-' + call_id)
        if fail:
            review['status'] = 'fail'; review['predicates'][0].update(status='fail', severity='critical')
        boards.record_board_review(root, 'start', review, roles=['start_frame'], images=images)
    run('original-start', fail=True)
    run('corrected-start', correction=boards.board_correction_intent(root, 'start', changes='SYNTHETIC exact fix'))
    pack = boards.load_board_pack(root)
    assert pack['board_bindings']['start']['call_id'] == 'corrected-start'
    assert boards.imported_board_eligibility(root, pack, 'start', 'original-start', images=images) is None
    with pytest.raises(ValueError, match='differs from the current prepared board request'):
        boards.bind_imported_board(root, 'start', 'original-start', images=images)
    action = execution.next_production_action(root)
    assert action['kind'] != 'promote_imported_board', action
    stepped = execution.advance(root)
    assert stepped.get('action', {}).get('kind') != 'promote_imported_board', stepped
    assert boards.load_board_pack(root)['board_bindings']['start']['call_id'] == 'corrected-start'
    assert images.image_usage(root)['total'] == 2
    assert images.image_call_status(root, 'original-start')['state'] == 'imported'


def test_corrected_board_never_repromotes_rejected_host_exception_original(neutral_package):
    # The original retained an actual raised host exception, later imported real
    # bytes, failed review, and a normal exact correction passed. No successor
    # recovery moved the lane, so the original is superseded, never restored.
    test_corrected_board_never_reschedules_superseded_imported_original(neutral_package, True)


def test_selected_exact_repair_supersedes_rejected_original_review_until_replacement_stale(p):
    # Original critically rejected; exact repair generated and selected. The old
    # original keeps its count/evidence but no longer demands review_selection.
    # Invalidating the replacement bytes makes the stale verdict hide nothing.
    from tests.integration.test_first_pass_workflow import save
    from lib.production_execution import record_rejection, planned_request_digest
    first = p.generate('entry'); assert first.success, first.error
    original = first.data['production_attempt_id']
    rejection = attestation(file_sha256_of(first.artifacts[0]), ['possession'])
    rejection.update(status='fail', predicates=[{'name': 'possession', 'status': 'fail',
        'severity': 'critical', 'evidence': 'SYNTHETIC critical defect'}])
    record_rejection(p.root, original, rejection)
    repair = copy.deepcopy(p.scope)
    repair.update(id='entry-repair', phase='repair', replaces_attempt_ids=[original], attempts_per_shot={'entry': 1})
    repair_inputs = copy.deepcopy(p.inputs['entry'])
    repair_inputs['governance']['scope_id'] = 'entry-repair'
    repair_inputs['output_path'] = str(p.root / 'assets/video/entry-repair.mp4')
    repair['requests'] = {'entry': planned_request_digest(repair_inputs, project_dir=p.root)}
    save(p.root / 'production_scopes.json', {'version': '1.0', 'scopes': [p.scope, repair]})
    second = p.selector.execute(repair_inputs); assert second.success, second.error
    p.select('entry', second)
    action = execution.next_production_action(p.root)
    assert not (action['kind'] == 'review_selection' and action['subject'] == original), action
    assert len(p.transport.native_requests) == 2
    assert any(a['attempt_id'] == original for a in execution._attempts(p.root))
    replacement = p.root / 'assets/video/entry-repair.mp4'
    replacement.write_bytes(replacement.read_bytes() + b'SYNTHETIC drift')
    # The stale selection no longer supersedes: the rejected original is again the
    # disclosed current candidate (never strict), and the cut is not complete.
    from lib import production_draft as draft
    entry = next(r for r in draft.first_cut_candidates(p.root) if r['shot_id'] == 'entry')
    assert (entry['attempt_id'], entry['review'], entry['strict_selected']) == (original, 'fail', False)
    assert [f['name'] for f in entry['findings']] == ['possession']
    stale = execution.next_production_action(p.root)
    assert stale['kind'] not in {'complete', 'deliver'}, stale
    assert len(p.transport.native_requests) == 2


def file_sha256_of(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


@pytest.mark.parametrize('successor_emitted', [True, False])
def test_late_host_exception_original_respects_successor_lane(neutral_package, successor_emitted):
    # A late authentic original stays counted/collectable. A submitted linked
    # successor owns the lane; an un-emitted reservation lets the original restore.
    from lib import production_boards as boards
    from tests.lib.test_production_images import file_sha256
    root, package, bind = neutral_package
    package = prospective_package(package); request.prepare_package(root, bind(package))
    prepared = boards.prepare_image_request(root, 'start', images=images)
    scope = {'id': 'scope-start', 'status': 'approved', 'approved_by': 'SYNTHETIC creator fixture',
        'project_id': package['project_id'], 'story_revision': 'r1', 'phase': 'image', 'provider': 'native_imagegen',
        'evidence': {k: package['boards']['approval'][k] for k in ('path', 'sha256')},
        'image_allowance': 2, 'allowed_image_routes': ['native_imagegen'],
        'requests': {'start': prepared['request_sha256']}, 'attempts_per_shot': {'start': 1}}
    write(root, 'production_scopes.json', {'version': '1.0', 'scopes': [scope]})
    boards.reserve_board_image(root, 'start', scope_id='scope-start', call_id='original-start', images=images)
    envelope = images.begin_native_image(root, 'original-start')
    path = root / 'receipts/exception.json'
    write(root, 'receipts/exception.json', {'call_id': 'original-start', 'tool_name': envelope['tool_name'],
        'arguments': envelope['arguments'], 'exception': {'message': 'SYNTHETIC raised host exception'}})
    images.record_native_image_host_exception(root, 'original-start',
        host_exception={'path': str(path), 'sha256': file_sha256(path)})
    child = boards.prepare_image_request(root, 'start', replace_exception_call_id='original-start', images=images)
    child_scope = 'scope-successor'
    successor = dict(scope, id=child_scope, requests={'start': child['request_sha256']})
    write(root, 'production_scopes.json', {'version': '1.0', 'scopes': [scope, successor]})
    boards.reserve_board_image(root, 'start', scope_id=child_scope, call_id='successor-start',
        replace_exception_call_id='original-start', images=images)
    if successor_emitted:
        images.begin_native_image(root, 'successor-start')
    images.import_native_image(root, 'original-start', host_receipt=host_receipt(root, envelope, name='late-original'))
    pack = boards.load_board_pack(root)
    kind = boards.imported_board_eligibility(root, pack, 'start', 'original-start', images=images)
    action = execution.next_production_action(root)
    if successor_emitted:
        assert kind == 'successor'
        assert not (action['kind'] == 'promote_imported_board' and action['subject'] == 'original-start'), action
        assert boards.bind_imported_board(root, 'start', 'original-start', images=images) is None
        assert 'start' not in boards.load_board_pack(root)['board_bindings']
    else:
        assert kind == 'restore'
        assert action['kind'] == 'promote_imported_board' and action['subject'] == 'original-start', action
        bound = boards.bind_imported_board(root, 'start', 'original-start', images=images)
        assert bound['call_id'] == 'original-start'
        assert boards.load_board_pack(root)['image_packets']['start']['request_sha256'] == prepared['request_sha256']
        # The never-emitted successor stays reserved/counted but is not scheduled:
        # it can no longer qualify for recovery once the original returned bytes.
        after = execution.next_production_action(root)
        assert not (after['kind'] == 'begin_native_image' and after['subject'] == 'successor-start'), after
        stepped = execution.advance(root)
        assert stepped['action']['subject'] != 'successor-start', stepped
        assert images.image_call_status(root, 'successor-start')['state'] == 'reserved'
    assert images.image_usage(root)['total'] == 2
    assert images.image_call_status(root, 'original-start')['state'] == 'imported'
