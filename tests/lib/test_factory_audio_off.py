"""Exact creator-bound native audio-off; all authority/media are synthetic."""
import copy
import json

import pytest

from lib import production_autonomy as pa, openart_mcp as mcp
from tests.lib.test_factory_board_resume import late_board_run
from tests.lib.test_factory_frame_alias import alternate_inputs
from tests.lib.test_production_factory import creator_fixture, file_bytes, write
from tests.lib.test_openart_mcp_native import observations as guided_observations  # noqa: F401
from tests.lib.test_production_request import neutral_package  # noqa: F401


def retain_audio_off(root, policy, sha, profile, *, shots=None, change=None):
    record = {'version': '1.0', 'kind': 'native_audio_off_approval',
        'project_id': policy['project_id'], 'story_revision': policy['story_revision'],
        'policy_sha256': sha, 'shots': shots or ['s1'],
        'route': {'provider': 'openart_mcp', 'model': profile['model'], 'mode': profile['mode']},
        'controls': {'generateAudio': False}, 'form_sha256': profile['form_sha256'],
        'approved_packet': write(root, 'notes/SYNTHETIC-approved-false-packet.json',
            {'SYNTHETIC': 'offline approved packet', 'native_params': {'generateAudio': False}})}
    if change:
        change(record)
    return creator_fixture(root, 'SYNTHETIC-audio-off', record)


@pytest.fixture
def audio_run(neutral_package, monkeypatch, guided_observations):
    root, transport, _, _ = late_board_run(neutral_package, monkeypatch)
    monkeypatch.setattr(mcp, '_ALLOW_FIXTURE_PRODUCTION', True)
    policy, sha, _ = pa.require_active_policy(root)
    profile = mcp.load_profile('byte-plus-seedance-2-mini', 'image2video', require='supported')
    inputs = alternate_inputs(root)
    inputs['native_params'] = {'generateAudio': False}
    return root, transport, policy, sha, profile, inputs


def test_exact_audio_off_adoption_replay_and_delta_preserve_originals(audio_run):
    root, transport, policy, sha, profile, inputs = audio_run
    approval = retain_audio_off(root, policy, sha, profile)
    before = file_bytes(root)
    counts = pa.root_attempt_counts(root, policy)
    result = pa.adopt_native_audio_off_approval(root, expected_policy_sha256=sha, approval=approval)
    assert not result['replayed'] and result['provider_calls'] == 0
    assert all(file_bytes(root)[p] == b for p, b in before.items() if str(p) != 'production_scopes.json')
    stable = file_bytes(root)
    assert pa.adopt_native_audio_off_approval(root, expected_policy_sha256=sha, approval=approval)['replayed']
    pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp')
    assert file_bytes(root) == stable
    assert pa.root_attempt_counts(root, policy) == counts and len(transport.native_requests) == 1


@pytest.mark.parametrize('defect', ['policy', 'project', 'story', 'shot', 'duplicate_shots',
    'model', 'mode', 'provider', 'true', 'zero', 'extra_control', 'form', 'subject',
    'missing_packet', 'approval_hash', 'expected_policy'])
def test_audio_off_adoption_refuses_inexact_creator_route_control_or_packet(audio_run, defect):
    root, _, policy, sha, profile, _ = audio_run
    def change(record):
        if defect in {'policy', 'project', 'story'}:
            record[{'policy': 'policy_sha256', 'project': 'project_id', 'story': 'story_revision'}[defect]] = 'wrong'
        elif defect == 'shot': record['shots'] = ['foreign']
        elif defect == 'duplicate_shots': record['shots'] = ['s1', 's1']
        elif defect in {'model', 'mode', 'provider'}: record['route'][defect] = 'foreign'
        elif defect == 'true': record['controls']['generateAudio'] = True
        elif defect == 'zero': record['controls']['generateAudio'] = 0
        elif defect == 'extra_control': record['controls']['seed'] = -1
        elif defect == 'form': record['form_sha256'] = '0' * 64
    approval = retain_audio_off(root, policy, sha, profile, change=change)
    record = json.loads((root / approval['path']).read_text())
    if defect == 'subject':
        auth = json.loads((root / record['authorization']['path']).read_text())
        auth['subject_sha256'] = '0' * 64
        record['authorization'] = write(root, record['authorization']['path'], auth)
        approval = write(root, approval['path'], record)
    elif defect == 'missing_packet': (root / record['approved_packet']['path']).unlink()
    elif defect == 'approval_hash': approval['sha256'] = '0' * 64
    before = file_bytes(root)
    with pytest.raises(pa.AutonomyError):
        pa.adopt_native_audio_off_approval(root,
            expected_policy_sha256='0' * 64 if defect == 'expected_policy' else sha, approval=approval)
    assert file_bytes(root) == before


@pytest.mark.parametrize('defect', ['missing', 'revoked', 'wrong_shot', 'wrong_model',
    'true', 'zero', 'count', 'seed', 'conflicting_lock', 'explicit_baseline', 'schema'])
def test_audio_off_admission_keeps_all_other_native_controls_and_authority_exact(audio_run, monkeypatch, defect):
    root, _, policy, sha, profile, inputs = audio_run
    approval = retain_audio_off(root, policy, sha, profile)
    if defect != 'missing':
        pa.adopt_native_audio_off_approval(root, expected_policy_sha256=sha, approval=approval)
    if defect == 'revoked':
        record = json.loads((root / approval['path']).read_text())
        (root / record['authorization']['path']).write_text('SYNTHETIC authority revoked')
    elif defect == 'wrong_shot': inputs['governance']['shot_id'] = 's3'
    elif defect == 'wrong_model': inputs['model'] = 'foreign'
    elif defect == 'true': inputs['native_params']['generateAudio'] = True
    elif defect == 'zero': inputs['native_params']['generateAudio'] = 0
    elif defect == 'count': inputs['native_params']['videoCount'] = 2
    elif defect == 'seed': inputs['native_params']['seed'] = 7
    elif defect == 'conflicting_lock': policy['locked']['controls']['generateAudio'] = {'value': True}
    elif defect == 'explicit_baseline':
        baselines = copy.deepcopy(pa.retained_baselines(root, policy))
        baselines['s1']['planned_request_template']['inputs']['native_params'] = {'generateAudio': True}
        monkeypatch.setattr(pa, 'retained_baselines', lambda *a: baselines)
    elif defect == 'schema':
        altered = copy.deepcopy(profile)
        altered['form']['jsonSchema']['properties']['generateAudio'] = {'const': True}
        monkeypatch.setattr(mcp, 'load_profile', lambda *a, **kw: altered)
    before = file_bytes(root)
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(root, policy, inputs['governance']['shot_id'], inputs, 'openart_mcp',
            policy_context=(sha, 'SYNTHETIC'))
    assert file_bytes(root) == before


def test_original_audio_off_exact_proof_replays_after_authority_revocation(audio_run):
    root, _, policy, sha, profile, inputs = audio_run
    approval = retain_audio_off(root, policy, sha, profile)
    pa.adopt_native_audio_off_approval(root, expected_policy_sha256=sha, approval=approval)
    proof = pa._native_audio_off_proof(root, policy, sha, 's1', inputs, 'openart_mcp')
    record = json.loads((root / approval['path']).read_text())
    (root / record['authorization']['path']).write_text('SYNTHETIC authority revoked')
    before = file_bytes(root)
    pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp',
        policy_context=(sha, 'SYNTHETIC'), native_audio_off_approval=proof, historical=True)
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp')
    wrong = copy.deepcopy(inputs); wrong['native_params']['generateAudio'] = True
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(root, policy, 's1', wrong, 'openart_mcp',
            policy_context=(sha, 'SYNTHETIC'), native_audio_off_approval=proof, historical=True)
    tampered = copy.deepcopy(proof); tampered['authorization']['content'] += 'tampered'
    with pytest.raises(pa.AutonomyError):
        pa._validate_request_delta(root, policy, 's1', inputs, 'openart_mcp',
            policy_context=(sha, 'SYNTHETIC'), native_audio_off_approval=tampered, historical=True)
    assert file_bytes(root) == before
