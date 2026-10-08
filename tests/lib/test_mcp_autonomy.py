"""Connector policy seams, isolated synthetic qualification only; no live calls.

Actual retained policy activation, template/source hashes, scopes, caps and route
history are exercised. The connector profile/preparation proof is an explicit
fake; these tests never publish qualification or claim model readiness.
"""
import copy
import json
import sys
import types

import pytest

from lib import production_autonomy as pa, production_execution as execution
from lib import production_request as preparation
from tests.lib.test_production_autonomy import H, install, make_policy, retained_fixture

MCP = {'id': 'openart_mcp', 'billing': 'unknown_cost_no_ceiling',
       'exposure_acknowledgement': 'no_enforceable_credit_ceiling',
       'uid_sha256': H('mcp account'), 'project_id': 'mcp-project',
       'routes': [{'model': 'h3-turbo', 'mode': 'image2video'}]}


def policy():
    p = make_policy()
    p['providers'].append(copy.deepcopy(MCP))
    p['evidence'] = {'path': f'approvals/autonomy-policy-{H("p")}.json', 'sha256': H('e')}
    return p


@pytest.mark.parametrize('change', [lambda s: s.pop('exposure_acknowledgement'),
    lambda s: s.update(billing='credits', ceiling='40'),
    lambda s: s.update(account_id_sha256=s.pop('uid_sha256')),
    lambda s: s.update(workspace='__unobserved_workspace__'), lambda s: s.update(routes=[])])
def test_mcp_distinct_exact_acceptance_shape(change):
    p = policy()
    assert pa.schema_errors(p) == []
    change(p['providers'][-1])
    assert pa.schema_errors(p)


def test_fresh_three_provider_policy_does_not_migrate_old_hash():
    old = make_policy()
    before = pa.policy_sha256(old, {})
    fresh = policy()
    assert pa.schema_errors(fresh) == []
    assert pa._openart_spec(fresh)['id'] == 'openart_cli'
    assert not pa._provider_spec(old, 'openart_mcp')
    assert pa.policy_sha256(old, {}) == before
    assert pa.policy_sha256(fresh, {}) != before
    fresh['providers'].append(copy.deepcopy(MCP))
    assert pa.schema_errors(fresh) and pa.policy_conflicts(fresh)


@pytest.fixture
def seam(tmp_path, monkeypatch):
    p, sha = install(tmp_path, policy())
    inputs = copy.deepcopy(retained_fixture(tmp_path)['entry']['planned_request_template']['inputs'])
    inputs.update(project_dir=str(tmp_path), model='h3-turbo', mode='image2video',
        duration=8, image_path=str(tmp_path / 'assets/start.svg'),
        preparation_review_id='review', governance={'shot_id': 'entry'})
    profile = {'source': 'fixture', 'account_uid_sha256': MCP['uid_sha256'],
               'profile_sha256': H('fixture profile')}
    native = {'provider': 'openart_mcp', 'model': inputs['model'], 'mode': inputs['mode'],
              'body': {'model': inputs['model'], 'mode': inputs['mode'],
                       'params': {'duration': 8}, 'projectId': MCP['project_id']},
              'account_binding': {'uid_sha256': MCP['uid_sha256']},
              'body_sha256': H('fixture body'), 'source_binding_sha256': H('fixture source role'),
              'profile_sha256': profile['profile_sha256'], 'form_sha256': H('fixture form')}
    calls = []
    def load(model, mode, *, require):
        calls.append((model, mode, require))
        assert require == 'supported'
        return profile
    fake = types.ModuleType('lib.openart_mcp')
    fake.load_profile = load
    fake.prepare_native_request = lambda controls, profile: native
    fake.validate_native_request = lambda *args: None
    monkeypatch.setitem(sys.modules, 'lib.openart_mcp', fake)
    import lib
    monkeypatch.setattr(lib, 'openart_mcp', fake, raising=False)
    monkeypatch.setattr(preparation, 'validate_preparation', lambda *args: {'compiled_sha256': H('fixture compiled')})
    monkeypatch.setattr(pa, 'lock_proof', lambda *args, **kw: H('fixture locks'))
    monkeypatch.setattr(pa, 'cross_provider_block', lambda *args: [])
    monkeypatch.setattr(execution, '_attempts', lambda _: [])
    # Empty credit snapshot is read-only; no ledger is opened or initialized.
    import lib.provider_credit_ledger as ledger
    monkeypatch.setattr(ledger, 'read_existing_snapshot', lambda: {
        'reservations': [], 'unpriced_reservations': [], 'outbox': [], 'unpriced_outbox': []})
    return tmp_path, p, sha, inputs, profile, native, calls


def test_real_scope_capture_and_rooted_begin_terms(seam):
    root, p, sha, inputs, profile, native, calls = seam
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    inputs['governance']['scope_id'] = scope['id']
    terms = pa.rooted_policy_mcp_authority(root, inputs, scope, native, profile)
    assert terms['policy_sha256'] == sha
    assert terms['uid_sha256'] == MCP['uid_sha256']
    assert terms['body_sha256'] == native['body_sha256']
    assert terms['source_binding_sha256'] == native['source_binding_sha256']
    assert terms['enforceable_credit_ceiling'] is False
    assert terms['exposure_acknowledgement'] == 'no_enforceable_credit_ceiling'
    assert terms['sha256'] == scope['unknown_cost_authorization_sha256']
    assert all(c[2] == 'supported' for c in calls)
    assert 'credit_terms' not in scope
    assert 'unknown_cost_authorization_id' not in inputs
    assert not list((root / 'artifacts').glob('unknown_cost_authorization-*.json'))
    assert pa.validate_policy_mcp_attempt(root, scope, inputs=inputs, native=native, profile=profile) == sha


@pytest.mark.parametrize('change', [lambda n: n['account_binding'].update(uid_sha256=H('other')),
    lambda n: n['body'].update(projectId='other'), lambda n: n['body'].update(mode='text2video'),
    lambda n: n.update(body_sha256=H('other')), lambda n: n.update(source_binding_sha256=H('other'))])
def test_prepared_native_or_account_drift_blocks_begin(seam, change):
    root, _, _, inputs, profile, native, _ = seam
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    frozen = copy.deepcopy(native)
    change(native)
    with pytest.raises((pa.AutonomyError, ValueError)):
        pa.rooted_policy_mcp_authority(root, inputs, scope, frozen, profile)


@pytest.mark.parametrize('field', ['billing_authority', 'credit_quote_id', 'unknown_cost_evidence_id', 'unknown_cost_authorization_id'])
def test_caller_billing_authority_never_activates_mcp(seam, field):
    root, _, _, inputs, _, _, calls = seam
    inputs[field] = 'caller authority'
    with pytest.raises(pa.AutonomyError):
        pa.derive_scope(root, inputs, provider='openart_mcp')
    assert calls == [] and not (root / 'production_scopes.json').exists()


def test_revocation_between_capture_and_begin_is_strict(seam):
    root, _, _, inputs, profile, native, _ = seam
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    path = root / 'artifacts/decision_log.json'
    log = json.loads(path.read_text())
    log['decisions'][0]['user_approved'] = False
    path.write_text(json.dumps(log))
    with pytest.raises(pa.AutonomyError, match='Strict'):
        pa.rooted_policy_mcp_authority(root, inputs, scope, native, profile)


def test_source_bytes_drift_blocks_begin(seam):
    root, _, _, inputs, profile, native, _ = seam
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    (root / 'assets/start.svg').write_bytes(b'different retained source')
    with pytest.raises((pa.AutonomyError, execution.ProductionGovernanceError, ValueError)):
        pa.rooted_policy_mcp_authority(root, inputs, scope, native, profile)


def test_original_pending_and_caps_block_new_scope(seam, monkeypatch):
    root, _, _, inputs, _, _, _ = seam
    pa.derive_scope(root, inputs, provider='openart_mcp')
    with pytest.raises(pa.AutonomyError, match='undispatched'):
        pa.derive_scope(root, inputs, provider='openart_mcp')
    monkeypatch.setattr(pa, 'cross_provider_block', lambda *a: ['original MCP pending'])
    with pytest.raises(pa.AutonomyError, match='original MCP pending'):
        pa.derive_scope(root, inputs, provider='openart_mcp')


def test_exact_mode_and_named_model_only(seam):
    root, _, _, inputs, _, _, calls = seam
    inputs['mode'] = 'text2video'
    with pytest.raises(pa.AutonomyError, match='outside freshly approved'):
        pa.derive_scope(root, inputs, provider='openart_mcp')
    assert not calls


def test_mcp_report_uses_immutable_original_and_unknown_exposure(seam, monkeypatch):
    root, p, sha, inputs, profile, native, _ = seam
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    billing = pa.rooted_policy_mcp_authority(root, inputs, scope, native, profile)
    authority = {'scope': scope, 'scope_id': scope['id'], 'scope_sha256': pa.digest(scope),
                 'shot_id': 'entry', 'request_sha256': scope['requests']['entry'], 'billing': billing}
    fake = types.ModuleType('lib.openart_mcp_jobs')
    fake.list_attempts = lambda root: [{'attempt_id': 'synthetic-original', 'status': 'uncertain'}]
    fake.frozen_request = lambda root, aid: {'generation_inputs': inputs, 'native': native, 'authority': authority}
    fake.attempt_state = lambda root, aid: {'status': 'uncertain'}
    monkeypatch.setattr(execution, 'load_attempt_result', lambda root, aid: {'status': 'uncertain'})
    monkeypatch.setitem(sys.modules, 'lib.openart_mcp_jobs', fake)
    import lib
    monkeypatch.setattr(lib, 'openart_mcp_jobs', fake, raising=False)
    from lib.production_autonomy_report import _mcp_policy_rows
    rows, exposure, snapshot = _mcp_policy_rows(root, p, sha)
    assert len(rows) == 1 and rows[0]['status'] == 'uncertain'
    assert rows[0]['credits']['requested_charge'] == 'unknown'
    assert exposure['attempts'] == 1 and exposure['enforceable_credit_ceiling'] is False
    assert snapshot['frozen_sha256']['synthetic-original']
    authority['scope_sha256'] = H('tamper')
    with pytest.raises(pa.AutonomyError, match='scope binding changed'):
        _mcp_policy_rows(root, p, sha)


@pytest.mark.parametrize('params', [
    {'duration': 8, 'resolution': '720p', 'aspectRatio': '16:9'},
    {'videoDuration': 8, 'videoResolution': '720p', 'videoAspectRatio': '16:9'},
])
def test_mcp_report_exact_ordinary_and_smartshot_aliases(params):
    from lib.production_autonomy_report import _mcp_dimensions
    assert _mcp_dimensions(params) == {'duration': 8, 'resolution': '720p', 'aspect_ratio': '16:9'}
    assert _mcp_dimensions({}) == {'duration': None, 'resolution': None, 'aspect_ratio': None}


def test_mcp_report_canonical_native_aspect_has_no_false_baseline_change(seam, monkeypatch):
    root, p, sha, inputs, profile, native, _ = seam
    # Reapprove the actual canonical native field in the retained template.
    from tests.lib.test_production_autonomy import install_existing
    inputs['native_params'] = {'aspectRatio': '16:9'}
    native['body']['params']['aspectRatio'] = '16:9'
    p, sha = install_existing(root, p, templates={'entry': execution.planned_request_template(inputs, project_dir=root)})
    scope = pa.derive_scope(root, inputs, provider='openart_mcp')
    billing = pa.rooted_policy_mcp_authority(root, inputs, scope, native, profile)
    authority = {'scope': scope, 'scope_id': scope['id'], 'scope_sha256': pa.digest(scope),
                 'shot_id': 'entry', 'request_sha256': scope['requests']['entry'], 'billing': billing}
    fake = types.ModuleType('lib.openart_mcp_jobs')
    fake.list_attempts = lambda root: [{'attempt_id': 'synthetic-original', 'status': 'uncertain'}]
    fake.frozen_request = lambda root, aid: {'generation_inputs': inputs, 'native': native, 'authority': authority}
    monkeypatch.setitem(sys.modules, 'lib.openart_mcp_jobs', fake)
    import lib
    monkeypatch.setattr(lib, 'openart_mcp_jobs', fake, raising=False)
    monkeypatch.setattr(execution, 'load_attempt_result', lambda root, aid: {'status': 'uncertain'})
    from lib.production_autonomy_report import _mcp_policy_rows
    rows, _, _ = _mcp_policy_rows(root, p, sha)
    assert rows[0]['settings']['aspect_ratio'] == '16:9'
    assert 'aspect_ratio' not in rows[0]['baseline_delta']['settings_changes']
