"""Offline autonomy seams. Synthetic native/profile/lock proofs are not qualification.

Rooted policy, source snapshot and occurrence capture are real; provider helper,
preparation and lock proof are isolated here. Reference transport verification is
covered separately by the native-input credit tests. No CLI/network/spend.
"""
import copy
import json

import pytest

from lib import openart_credit as credit, openart_jobs as jobs
from lib import production_autonomy as pa, production_execution as execution
from lib import production_request as preparation
from tests.lib.test_production_autonomy import H, install, openart_row, retained_fixture
from tests.lib.test_unknown_cost_autonomy import unknown_policy


def referenced_policy(mode='image2video'):
    policy = unknown_policy()
    policy['providers'][0]['routes'] = [{'model': 'pixverseV6', 'mode': mode}]
    return policy


@pytest.mark.parametrize('full,other', [('image2video', 'text2video'), ('text2video', 'image2video')])
@pytest.mark.parametrize('list_key', ['qualification_candidates', 'not_live'])
def test_mode_qualification_independent(full, other, list_key):
    policy = referenced_policy(full)
    row = openart_row(model='pixverseV6', mode=full)
    row[list_key] = [{'model': 'pixverseV6', 'mode': other}]
    assert [r['mode'] for r in pa.eligible_routes(policy, 'entry', [row])[0]] == [full]
    row[list_key][0]['mode'] = full
    assert pa.eligible_routes(policy, 'entry', [row])[0] == []


@pytest.mark.parametrize('mutate', [
    lambda row: row['models'][0].update(mode='text2video'),
    lambda row: row['models'][0].update(production_ready=False),
    lambda row: row['models'][0].update(level='inspected'),
    lambda row: row['models'][0].update(catalog_verified=False),
    lambda row: row['models'][0].update(account_id_sha256=H('other')),
    lambda row: row.update(dispatch_readiness={'ready': False, 'blockers': ['fresh_account_required']}),
])
def test_new_mode_requires_exact_policy_full_catalog_account_readiness(mutate):
    row = openart_row(model='pixverseV6', mode='image2video')
    mutate(row)
    assert pa.eligible_routes(referenced_policy(), 'entry', [row])[0] == []


@pytest.fixture
def seam(tmp_path, monkeypatch):
    policy, sha = install(tmp_path, referenced_policy())
    inputs = copy.deepcopy(retained_fixture(tmp_path)['entry']['planned_request_template']['inputs'])
    inputs.update(project_dir=str(tmp_path), model='pixverseV6', mode='image2video',
                  duration=8, image_path=str(tmp_path / 'assets/start.svg'), image_upload_id='upload-1',
                  preparation_review_id='review', unknown_cost_evidence_id='retained-evidence',
                  governance={'shot_id': 'entry'})
    profile = {'source': 'real', 'account_id_sha256': H('acct')}
    native = {'image_upload': {'upload_id': 'upload-1'}, 'native_body_sha256': H('native'),
              'profile_sha256': H('profile')}
    checks = []
    def reference_digest(actual, actual_profile, *, native):
        assert actual is inputs and actual_profile is profile
        checks.append(copy.deepcopy(native))
        return H('verified source/upload/role') if native.get('image_upload') else None
    monkeypatch.setattr(jobs, 'native_reference_digest', reference_digest, raising=False)
    monkeypatch.setattr(jobs, 'load_qualification', lambda **kw: profile)
    monkeypatch.setattr(jobs, 'prepare_native_request', lambda *a: native)
    monkeypatch.setattr(credit, 'get_retained_unknown_evidence', lambda *a: {
        'account_id_sha256': H('acct'), 'workspace': credit.UNOBSERVED_WORKSPACE})
    monkeypatch.setattr(preparation, 'validate_preparation', lambda *a: {'compiled_sha256': H('compiled')})
    monkeypatch.setattr(pa, 'lock_proof', lambda *a, **kw: H('locks'))
    monkeypatch.setattr(execution, 'load_selected_attempts', lambda _: {'prior': {}})
    from tools import tool_registry
    monkeypatch.setattr(tool_registry, '_openart_route', lambda _: openart_row(model='pixverseV6', mode='image2video'))
    return tmp_path, policy, sha, inputs, native, checks


def test_scope_material_verifies_reference_before_preparation(seam, monkeypatch):
    root, policy, sha, inputs, native, checks = seam
    pa._scope_material(root, inputs, 'openart_cli')
    assert checks == [native]
    def refused(*a, **kw):
        raise ValueError('verified upload/source mismatch')
    monkeypatch.setattr(jobs, 'native_reference_digest', refused)
    monkeypatch.setattr(preparation, 'validate_preparation', lambda *a: pytest.fail('prep reached after invalid reference'))
    with pytest.raises(ValueError, match='upload/source'):
        pa._scope_material(root, inputs, 'openart_cli')


def test_retained_source_byte_drift_blocks_reference_scope(seam):
    root, policy, sha, inputs, native, checks = seam
    (root / 'assets/start.svg').write_bytes(b'changed current source')
    with pytest.raises((pa.AutonomyError, execution.ProductionGovernanceError, ValueError)):
        pa._scope_material(root, inputs, 'openart_cli')
    assert checks == []


def capture(seam):
    root, policy, sha, inputs, native, checks = seam
    scope = {'id': 'derived-one', 'requests': {'entry': H('request')}}
    pa._capture_policy_unknown_cost(root, policy, sha, scope, inputs)
    return json.loads((root / 'artifacts/unknown_cost_authorization-derived-one.json').read_text())


def test_occurrence_emits_only_verified_reference_digest(seam):
    auth = capture(seam)
    assert auth['occurrences'] == [{'id': 'derived-one-0', 'index': 0,
        'request_sha256': H('request'), 'native_sha256': H('native'),
        'profile_sha256': H('profile'), 'references_sha256': H('verified source/upload/role')}]
    assert auth['approved_by'] == f'policy:{seam[2]}'
    assert seam[3]['unknown_cost_authorization_id'] == 'derived-one'


def test_ref_free_capture_legacy_occurrence_unchanged(seam):
    seam[4]['image_upload'] = None
    auth = capture(seam)
    assert 'references_sha256' not in auth['occurrences'][0]


def test_capture_bad_native_reference_writes_no_authorization(seam, monkeypatch):
    def refused(*a, **kw):
        raise ValueError('native role/source/upload mismatch')
    monkeypatch.setattr(jobs, 'native_reference_digest', refused)
    with pytest.raises(ValueError, match='role/source/upload'):
        capture(seam)
    assert not (seam[0] / 'artifacts/unknown_cost_authorization-derived-one.json').exists()


def test_gated_mode_never_reaches_capture_or_reference_verification(seam, monkeypatch):
    root, policy, sha, inputs, native, checks = seam
    from tools import tool_registry
    row = openart_row(model='pixverseV6', mode='image2video', level='pre_submit')
    monkeypatch.setattr(tool_registry, '_openart_route', lambda _: row)
    with pytest.raises(pa.AutonomyError, match='not production-ready'):
        pa.derive_scope(root, inputs, provider='openart_cli')
    assert checks == []
    assert not (root / 'production_scopes.json').exists()
    assert not list((root / 'artifacts').glob('unknown_cost_authorization-*.json'))


def test_role_bound_assets_do_not_need_legacy_image_upload_field(seam, monkeypatch):
    del seam[4]['image_upload']
    seam[4]['input_assets'] = [{'role': 'first_frame', 'source_sha256': H('source')}]
    monkeypatch.setattr(jobs, 'native_reference_digest', lambda *a, **kw: H('role-bound verified reference'))
    auth = capture(seam)
    assert auth['occurrences'][0]['references_sha256'] == H('role-bound verified reference')


@pytest.mark.parametrize('authority', ['unknown_cost_authorization', 'unknown_cost_authorization_sha256',
                                     'unknown_cost_authorization_id'])
def test_referenced_scope_refuses_caller_authority_before_helpers(seam, authority):
    root, policy, sha, inputs, native, checks = seam
    inputs[authority] = 'caller-authority'
    with pytest.raises(pa.AutonomyError, match='unavailable'):
        pa.derive_scope(root, inputs, provider='openart_cli')
    assert checks == []
    assert not (root / 'production_scopes.json').exists()


def test_new_policy_emits_single_bounded_reference_occurrence(seam, monkeypatch):
    root, policy, sha, inputs, native, checks = seam
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(root / 'isolated-private'))
    scope = pa.derive_scope(root, inputs, provider='openart_cli')
    assert scope['attempts_per_shot'] == {'entry': 1}
    assert scope['approved_by'] == f'policy:{sha}'
    assert 'credit_terms' not in scope
    auth = json.loads((root / f'artifacts/unknown_cost_authorization-{scope["id"]}.json').read_text())
    assert auth['count'] == 1 and auth['purpose'] == 'generation'
    assert auth['occurrences'][0]['references_sha256'] == H('verified source/upload/role')
    assert scope['unknown_cost_authorization_sha256'] == credit.unknown_cost_authorization_digest(auth)
