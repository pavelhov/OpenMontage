"""Actual native compilation -> fresh mixed policy -> prepare -> original begin.

Every account, qualification profile, approval and connector outcome is synthetic
and isolated. No connector call, upload, download or live qualification occurs.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib import production_request as preparation
from tests.lib.test_production_autonomy import make_policy, install_existing


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    from lib import openart_mcp as mcp, openart_mcp_jobs as jobs
    from tests.integration.test_openart_mcp_governance import prepare_project, FIXTURE
    discovery = tmp_path / 'discovery'
    discovery.mkdir(mode=0o700)
    raw = (Path(__file__).parents[1] / 'fixtures/openart/mcp_integration_forms.json').read_bytes()
    account = {'version': '1.0', 'transport': 'agent_mediated_connector',
        'classification': 'synthetic_fixture', 'observed_at': '2026-10-07T00:00:00Z',
        'tool': 'openart_me', 'arguments': {}, 'data': {'user': {'uid': 'synthetic-mcp-user'}, 'credits': 1000}}
    for kind, payload in [('schema', raw), ('account', json.dumps(account).encode())]:
        path = discovery / f'2026-10-07-{kind}-observation.json'
        path.write_bytes(payload)
        path.chmod(0o600)
        monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_EXPECTED_SHA256_' + kind.upper(), hashlib.sha256(payload).hexdigest())
    monkeypatch.setenv('OPENMONTAGE_OPENART_MCP_DISCOVERY_DIR', str(discovery))
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'private'))
    root = tmp_path / 'mixed'
    inputs, _ = prepare_project(root)
    marker = json.loads((root / 'project.json').read_text())
    marker['pipeline_type'] = 'cinematic'
    (root / 'project.json').write_text(json.dumps(marker))
    # Supported native route: no prior generated result or qualification exists.
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    assert jobs.qualified_profile(inputs['model'], inputs['mode'],
        mcp.load_profile(inputs['model'], inputs['mode'], require='candidate')) is None
    inputs.pop('unknown_cost_authorization_id')
    inputs['openart_project_id'] = 'fixture-openart-project'
    inputs['governance'] = {'shot_id': 'entry', 'stage': 'generate'}
    profile = mcp.load_profile(inputs['model'], inputs['mode'], require='supported')
    assert profile['readiness']['empirical_result']['status'] == 'not_tested'
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
    old_compiled = json.loads((root / 'artifacts/compiled_request-original.json').read_text())
    authored = preparation.compile_provider_prompt(root, 'entry', provider='openart_mcp', model=inputs['model'])
    compiled = preparation.prepare_compiled_request(inputs, native, profile,
        coverage=authored['coverage'], timing=old_compiled['timing'])
    (root / 'artifacts/compiled_request-original.json').write_text(json.dumps(compiled))
    review_path = root / 'artifacts/preparation_review-original.json'
    review = json.loads(review_path.read_text())
    review['subject_sha256'] = preparation.digest(compiled)
    review_path.write_text(json.dumps(review))
    p = make_policy()
    p['providers'] = [copy.deepcopy(p['providers'][1]), {'id': 'openart_mcp',
        'billing': 'unknown_cost_no_ceiling', 'exposure_acknowledgement': 'no_enforceable_credit_ceiling',
        'uid_sha256': native['account_binding']['uid_sha256'], 'project_id': 'fixture-openart-project',
        'routes': [{'model': inputs['model'], 'mode': inputs['mode']}]}]
    p['caps'] = {'max_total_attempts': 1, 'max_attempts_per_shot': 1, 'max_repair_attempts': 0}
    p['locked'] = {'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}}
    p['flex'] = {'duration_s': [], 'resolution': [], 'references': {'droppable_roles': [], 'substitutes': []}}
    p['checkpoint_stages'] = []
    p['model_selection_intents'] = {'entry': {'mode': 'auto', 'approved_pool': [
        {'provider': 'grok_cli', 'tool': 'grok_cli_video'},
        {'provider': 'openart_mcp', 'model': inputs['model'], 'tool': 'openart_mcp_video'}]}}
    template = execution.planned_request_template(inputs, project_dir=root)
    policy, sha = install_existing(root, p, templates={'entry': template})
    (root / 'production_scopes.json').write_text(json.dumps({'version': '1.0', 'scopes': []}))
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    inputs['governance']['scope_id'] = scope['id']
    return root, policy, sha, inputs, native, profile, jobs


def test_actual_compile_policy_prepare_begin_at_single_attempt_cap(lifecycle):
    root, policy, sha, inputs, native, profile, jobs = lifecycle
    jobs.prepare(root, attempt_id='policy-original', generation_inputs=inputs,
                 authority_fn=execution.prepare_openart_mcp_handoff)
    envelope = jobs.begin(root, 'policy-original', authority_fn=execution.prepare_openart_mcp_handoff)
    assert envelope['arguments'] == native['body']
    frozen = jobs.frozen_request(root, 'policy-original')
    assert frozen['authority']['billing']['policy_sha256'] == sha
    assert frozen['authority']['billing']['enforceable_credit_ceiling'] is False
    assert pa.root_attempt_counts(root, policy)['total'] == 1
    with pytest.raises(ValueError):
        jobs.begin(root, 'policy-original', authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.receive(root, 'policy-original', outcome={'historyId': 'fixture-original', 'status': 'PENDING'})
    assert jobs.poll_args(root, 'policy-original')['arguments'] == {'historyId': 'fixture-original'}
    with pytest.raises(ValueError):
        pa.derive_scope(root, inputs, provider='openart_mcp')


@pytest.mark.parametrize('change', ['revoke', 'native_type', 'body_hash'])
def test_actual_policy_begin_rechecks_revoke_and_native_types(lifecycle, change):
    root, _, _, inputs, _, _, jobs = lifecycle
    jobs.prepare(root, attempt_id='policy-original', generation_inputs=inputs,
                 authority_fn=execution.prepare_openart_mcp_handoff)
    if change == 'revoke':
        path = root / 'artifacts/decision_log.json'
        data = json.loads(path.read_text())
        data['decisions'][0]['user_approved'] = False
        path.write_text(json.dumps(data))
    elif change == 'native_type':
        path = root / 'artifacts/compiled_request-original.json'
        data = json.loads(path.read_text())
        data['native_binding']['native_body_sha256'] = '0' * 64
        path.write_text(json.dumps(data))
    else:
        path = root / 'production_scopes.json'
        data = json.loads(path.read_text())
        data['scopes'][0]['unknown_cost_authorization_sha256'] = '0' * 64
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        jobs.begin(root, 'policy-original', authority_fn=execution.prepare_openart_mcp_handoff)

@pytest.mark.parametrize('value', [True, '1', 1.0])
def test_actual_native_json_type_drift_does_not_recompile_approval(lifecycle, value):
    root, _, _, inputs, _, _, jobs = lifecycle
    inputs['native_params']['duration'] = value
    with pytest.raises(ValueError):
        jobs.prepare(root, attempt_id='typed-original', generation_inputs=inputs,
                     authority_fn=execution.prepare_openart_mcp_handoff)


def test_actual_policy_original_collection_provenance_and_unknown_cost_report(lifecycle, monkeypatch, tmp_path):
    root, policy, sha, inputs, _, _, jobs = lifecycle
    jobs.prepare(root, attempt_id='policy-original', generation_inputs=inputs,
                 authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'policy-original', authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.receive(root, 'policy-original', outcome={'historyId': 'fixture-original', 'status': 'PENDING'})
    jobs.record_status(root, 'policy-original', result={'historyId': 'fixture-original', 'status': 'COMPLETED',
        'resources': [{'id': 'fixture-original-resource', 'mediaType': 'video', 'status': 'COMPLETED', 'url': 'https://fixture.invalid/policy.mp4'}],
        'creditsCharged': 1})
    from tests.integration.test_openart_first_pass_workflow import real_av_clip
    downloaded = real_av_clip(tmp_path / 'synthetic-original.mp4', seconds=1)
    from tests.integration.test_openart_mcp_governance import install_fake_download
    install_fake_download(monkeypatch, jobs, downloaded)
    receipt = jobs.download_original(root, 'policy-original')
    jobs.collect(root, 'policy-original', downloaded_path=receipt['downloaded_path'])
    from lib import production_provenance as provenance
    monkeypatch.setattr(provenance, '_ALLOW_OPENART_FIXTURE_PROVENANCE', True)
    expected = {'path': inputs['output_path'], 'sha256': hashlib.sha256(downloaded.read_bytes()).hexdigest()}
    result = provenance.validate_attempt_provenance(root, 'policy-original', shot_id='entry',
        story_revision=policy['story_revision'], expected_output=expected)
    assert result['result']['status'] == 'generated'
    from lib.production_autonomy_report import _mcp_policy_rows
    rows, exposure, snapshot = _mcp_policy_rows(root, policy, sha)
    assert rows[0]['status'] == 'generated'
    assert rows[0]['credits']['requested_charge'] == 'unknown'
    assert exposure['attempts'] == 1 and exposure['enforceable_credit_ceiling'] is False
    assert snapshot['frozen_sha256']['policy-original']


def test_actual_original_failure_review_allows_one_bounded_repair(lifecycle):
    root, old_policy, _, inputs, _, _, jobs = lifecycle
    p = copy.deepcopy(old_policy)
    p['caps'] = {'max_total_attempts': 2, 'max_attempts_per_shot': 2, 'max_repair_attempts': 1}
    policy, _ = install_existing(root, p, templates={'entry': execution.planned_request_template(inputs, project_dir=root)})
    (root / 'production_scopes.json').write_text(json.dumps({'version': '1.0', 'scopes': []}))
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    inputs['governance']['scope_id'] = scope['id']
    jobs.prepare(root, attempt_id='policy-original', generation_inputs=inputs,
                 authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'policy-original', authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.receive(root, 'policy-original', outcome={'historyId': 'fixture-original', 'status': 'PENDING'})
    with pytest.raises(ValueError):
        pa.derive_scope(root, inputs, provider='openart_mcp', phase='repair', replaces_attempt_ids=['policy-original'])
    jobs.record_status(root, 'policy-original', result={'historyId': 'fixture-original', 'status': 'FAILED'})
    terminal = jobs.terminal_failure_record(root, 'policy-original')
    rejection = root / 'openart_mcp/attempts/policy-original/rejection.json'
    rejection.write_text(json.dumps({'status': 'fail', 'kind': 'generation_terminal_failure',
        'attempt_id': 'policy-original', 'terminal_failure_sha256': terminal['terminal_failure_sha256'],
        'reviewer': 'Synthetic offline engineering rejection'}))
    inputs['output_path'] = str(root / 'repair.mp4')
    inputs['compiled_request_id'] = 'repair'
    inputs['preparation_review_id'] = 'repair'
    from lib import openart_mcp as mcp
    profile = mcp.load_profile(inputs['model'], inputs['mode'], require='supported')
    assert profile['readiness']['empirical_result']['status'] == 'not_tested'
    native = mcp.prepare_native_request(execution._openart_mcp_controls(inputs), profile)
    original = json.loads((root / 'artifacts/compiled_request-original.json').read_text())
    authored = preparation.compile_provider_prompt(root, 'entry', provider='openart_mcp', model=inputs['model'])
    compiled = preparation.prepare_compiled_request(inputs, native, profile,
        coverage=authored['coverage'], timing=original['timing'])
    (root / 'artifacts/compiled_request-repair.json').write_text(json.dumps(compiled))
    review = json.loads((root / 'artifacts/preparation_review-original.json').read_text())
    review.update(review_id='repair', subject_sha256=preparation.digest(compiled))
    (root / 'artifacts/preparation_review-repair.json').write_text(json.dumps(review))
    repair = pa.derive_scope(root, inputs, provider='openart_mcp', phase='repair', replaces_attempt_ids=['policy-original'])
    inputs['governance']['scope_id'] = repair['id']
    jobs.prepare(root, attempt_id='policy-repair', generation_inputs=inputs,
                 authority_fn=execution.prepare_openart_mcp_handoff)
    jobs.begin(root, 'policy-repair', authority_fn=execution.prepare_openart_mcp_handoff)
    counts = pa.root_attempt_counts(root, policy)
    assert counts['total'] == 2 and counts['repair'] == 1
    with pytest.raises(ValueError):
        pa.derive_scope(root, inputs, provider='openart_mcp', phase='repair', replaces_attempt_ids=['policy-original'])
