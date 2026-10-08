"""R18 dispatch integration: retained scopes and offline private originals."""
import copy
import contextlib
import json

import pytest

from lib import production_execution as execution, openart_dispatch as dispatch
from tests.lib.test_production_execution import project, ImageTool, MotionTool, write_scopes
from tests.lib.test_production_video_guard import journal, crash_after_reservation
from tests.integration.test_openart_dispatch_recovery import governed, _qualified_billing_origin
from tests.lib.test_local_render_provenance import local
from tools.video.openart_cli_video import OpenArtCLIVideo


def approve(root, inputs, scope, tool, *, phase=None, replaces=None):
    scope = copy.deepcopy(scope)
    scope['provider'] = tool.provider
    if phase:
        scope['phase'] = phase
    if replaces:
        scope['replaces_attempt_ids'] = replaces
    scope['requests'][inputs['governance']['shot_id']] = execution.planned_request_digest(inputs, project_dir=root)
    write_scopes(root, scope)
    return scope


class GrokMotion(MotionTool):
    provider = 'grok_cli'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENMONTAGE_OPENART_STATE_DIR', str(tmp_path / 'absent-private'))
    return tmp_path


def test_running_cross_provider_repair_is_blocked(isolated):
    inputs, scope, _ = project(isolated, motion=True)
    journal(isolated, provider='openart_cli', status='running')
    tool = GrokMotion()
    approve(isolated, inputs, scope, tool, phase='repair', replaces=['original'])
    with pytest.raises(execution.ProductionGovernanceError, match='unresolved'):
        execution.preflight(tool, inputs)
    assert tool.calls == 0


def invoke(tool, inputs, direct):
    if direct:
        return execution.execute_governed(tool, inputs,
            lambda submitted: ImageTool.execute.__wrapped__.__wrapped__(tool, submitted))
    return tool.execute(inputs)


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('status', ['running', 'uncertain'])
def test_same_shot_dispatch_blocks_without_provider_or_new_attempt(isolated, direct, status):
    inputs, scope, _ = project(isolated, motion=True)
    journal(isolated, provider='openart_cli', status=status)
    tool = GrokMotion()
    approve(isolated, inputs, scope, tool, phase='repair', replaces=['original'])
    with pytest.raises(execution.ProductionGovernanceError, match='unresolved'):
        invoke(tool, inputs, direct)
    assert tool.calls == 0
    assert len(list((isolated / 'production_attempts').iterdir())) == 1


@pytest.mark.parametrize('direct', [False, True])
def test_authoritative_duplicate_recheck_is_under_existing_lock(isolated, monkeypatch, direct):
    inputs, scope, _ = project(isolated, motion=True)
    original = journal(isolated, provider='openart_cli', status='failed')
    tool = GrokMotion()
    approve(isolated, inputs, scope, tool, phase='repair', replaces=['original'])
    real_lock = execution._lock
    real_reader = __import__('lib.production_video_guard', fromlist=['read_video_duplicate_blocks']).read_video_duplicate_blocks
    locked = []
    reads = []
    @contextlib.contextmanager
    def lock(root):
        with real_lock(root):
            locked.append(True)
            (original / 'result.json').write_text(json.dumps({'status':'running'}))
            try:
                yield
            finally:
                locked.pop()
    def reader(root, shot):
        reads.append(bool(locked))
        return real_reader(root, shot)
    monkeypatch.setattr(execution, '_lock', lock)
    monkeypatch.setattr('lib.production_video_guard.read_video_duplicate_blocks', reader)
    with pytest.raises(execution.ProductionGovernanceError, match='unresolved'):
        invoke(tool, inputs, direct)
    assert reads == [False, True]
    assert tool.calls == 0
    assert len(list((isolated / 'production_attempts').iterdir())) == 1


@pytest.mark.parametrize('prior_kind', ['image', 'audio', 'local_render'])
def test_uncertain_other_phase_does_not_consume_motion_first_pass(isolated, prior_kind):
    inputs, scope, _ = project(isolated, motion=True)
    journal(isolated, kind=prior_kind, status='uncertain')
    assert MotionTool().execute(inputs).success


@pytest.mark.parametrize('phase', ['image', 'audio'])
def test_explicit_same_shot_nonvideo_dispatch_survives_uncertain_motion(isolated, phase):
    inputs, scope, _ = project(isolated)
    journal(isolated, kind='motion', provider='openart_cli', status='uncertain')
    class AudioTool(ImageTool):
        capability = 'tts'
    tool = ImageTool() if phase == 'image' else AudioTool()
    approve(isolated, inputs, scope, tool, phase=phase)
    assert tool.execute(inputs).success
    assert tool.calls == 1


def test_real_local_render_survives_same_shot_uncertain_motion(local):
    root, inputs, scope, tool, calls = local
    journal(root, provider='openart_cli', status='uncertain')
    assert tool.execute(inputs).success
    assert any(args[0] == 'render' for args, _ in calls)


def test_other_shot_motion_dispatch_survives_uncertain_video(isolated):
    inputs, _, _ = project(isolated, motion=True)
    journal(isolated, shot='other', provider='openart_cli', status='uncertain')
    assert MotionTool().execute(inputs).success


@pytest.mark.parametrize('motion', [False, True])
@pytest.mark.parametrize('kind', [None, 'unknown'])
def test_unknown_prior_kind_fails_closed_in_every_phase(isolated, motion, kind):
    inputs, _, _ = project(isolated, motion=motion)
    journal(isolated, kind=kind, status='failed')
    tool = MotionTool() if motion else ImageTool()
    with pytest.raises(execution.ProductionGovernanceError, match='unclassifiable'):
        tool.execute(inputs)
    assert tool.calls == 0


def private_grok(root, inputs, *, repair=None):
    inputs = copy.deepcopy(inputs)
    for key in ('compiled_request_id', 'preparation_review_id', 'credit_authorization_id',
                'credit_quote_id', 'credit_qualification_sha256'):
        inputs.pop(key, None)
    inputs['output_path'] = str(root / 'assets/grok.mp4')
    inputs['preferred_provider'] = 'grok_cli'
    inputs['allowed_providers'] = ['grok_cli']
    scope = json.loads((root / 'production_scopes.json').read_text())['scopes'][0]
    scope['id'] = 'grok-repair' if repair else 'grok-first-pass'
    inputs['governance']['scope_id'] = scope['id']
    tool = GrokMotion()
    approve(root, inputs, scope, tool, phase='repair' if repair else 'first_pass', replaces=[repair] if repair else None)
    return inputs, tool


@pytest.mark.parametrize('direct', [False, True])
def test_real_private_outbox_before_journal_blocks_grok(governed, monkeypatch, direct):
    root, original_inputs, _, _ = governed
    root, tmp, attempt = crash_after_reservation(governed)
    inputs, tool = private_grok(root, original_inputs)
    before = (tmp / 'calls').read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('duplicate check called CLI or initialized ledger')
    monkeypatch.setattr(dispatch, 'ledger', forbidden)
    monkeypatch.setattr(dispatch.cli, 'run_readonly', forbidden)
    with pytest.raises(execution.ProductionGovernanceError, match='prepared'):
        invoke(tool, inputs, direct)
    assert tool.calls == 0
    assert not (root / 'production_attempts' / attempt / 'request.json').exists()
    assert (tmp / 'calls').read_bytes() == before
    assert not (tmp / 'paid').exists()


def test_valid_original_terminal_settlement_permits_exact_grok_repair(governed, monkeypatch):
    root, original_inputs, _, tmp, attempt, binding, _ = _qualified_billing_origin(governed, monkeypatch)
    dispatch.resolve_attempt(root, attempt, binding.request_sha256)
    inputs, tool = private_grok(root, original_inputs, repair=attempt)
    before = (tmp / 'calls').read_bytes()
    assert tool.execute(inputs).success
    assert tool.calls == 1
    assert (tmp / 'calls').read_bytes() == before


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('public_status', ['running', 'uncertain'])
def test_real_private_submitted_original_blocks_cross_provider(governed, monkeypatch, direct, public_status):
    root, original_inputs, _, tmp = governed
    result = OpenArtCLIVideo().execute(original_inputs)
    attempt = result.data['production_attempt_id']
    # The private submit/launch receipts remain authoritative even when the
    # current host journal reports the provider's running state.
    path = root / 'production_attempts' / attempt / 'result.json'
    record = json.loads(path.read_text())
    record['status'] = public_status
    path.chmod(0o600)
    path.write_text(json.dumps(record))
    inputs, tool = private_grok(root, original_inputs, repair=attempt)
    before = (tmp / 'calls').read_bytes()
    requests = list((root / 'production_attempts').glob('*/request.json'))
    def forbidden(*args, **kwargs):
        raise AssertionError('guard contacted original provider')
    monkeypatch.setattr(dispatch.cli, 'run_readonly', forbidden)
    with pytest.raises(execution.ProductionGovernanceError, match='unresolved private job acceptance'):
        invoke(tool, inputs, direct)
    assert tool.calls == 0
    assert list((root / 'production_attempts').glob('*/request.json')) == requests
    assert (tmp / 'calls').read_bytes() == before
    assert len((tmp / 'paid').read_text().splitlines()) == 1


def test_scope_counts_remain_exact_across_phase_isolation(isolated):
    inputs, scope, _ = project(isolated, motion=True)
    journal(isolated, kind='image', status='failed')
    path = isolated / 'production_attempts/original/request.json'
    request = json.loads(path.read_text())
    request['scope_id'] = scope['id']
    path.write_text(json.dumps(request))
    scope['attempts_per_shot']['entry'] = 1
    write_scopes(isolated, scope)
    with pytest.raises(execution.ProductionGovernanceError, match='allowance exhausted'):
        MotionTool().execute(inputs)


def test_repair_cannot_replace_only_another_phase(isolated):
    inputs, scope, _ = project(isolated, motion=True)
    journal(isolated, kind='image', status='failed')
    approve(isolated, inputs, scope, MotionTool(), phase='repair', replaces=['original'])
    with pytest.raises(execution.ProductionGovernanceError, match='exact attempts'):
        MotionTool().execute(inputs)


def test_no_policy_pure_preflight_and_dry_run_leave_project_bytes_unchanged(isolated):
    inputs, _, _ = project(isolated, motion=True)
    before = {str(path.relative_to(isolated)):path.read_bytes() for path in isolated.rglob('*') if path.is_file()}
    assert execution.preflight(MotionTool(), inputs)['governed']
    assert MotionTool().dry_run(inputs)['provider_calls'] == 0
    after = {str(path.relative_to(isolated)):path.read_bytes() for path in isolated.rglob('*') if path.is_file()}
    assert after == before
    assert not (isolated / '.production-execution.lock').exists()
    assert not (isolated / 'absent-private').exists()
