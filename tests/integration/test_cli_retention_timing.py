"""Observed recovery publications, using original offline transport fixtures only."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import socket

import pytest

from lib import production_execution as execution
from lib.events import read_events
from lib.shot_contract import file_sha256
from tools.base_tool import ToolResult
from tools._grok_cli_media import GrokCLIContractError
from tools.video.openart_cli_video import OpenArtCLIVideo
from tests.integration.test_first_pass_workflow import production, read  # noqa: F401
from tests.integration.test_grok_prompt_boundary_recovery import rejected, collect  # noqa: F401
from tests.integration.test_local_grok_continuation import reserved, continue_attempt  # noqa: F401
from tests.tools.test_openart_cli_video import synthetic_openart_project


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('retention timing fixture attempted a network connection')
    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def observations(root, aid):
    return [row for row in read_events(root) if row.get('attempt_id') == aid
            and row.get('event') == 'governed-result-retained' and row.get('output_sha256')]


def assert_observed(root, request, before):
    rows = observations(root, request['attempt_id'])
    assert len(rows) == 1
    row = rows[0]
    assert before <= datetime.fromisoformat(row['ts']) <= datetime.now(timezone.utc)
    for key in ('attempt_id', 'request_sha256', 'project_id', 'story_revision', 'shot_id', 'scope_id', 'media_kind'):
        assert row[key] == request[key]
    assert row['tool'] == request['tool_name'] and row['status'] == 'generated'
    retained = execution.load_attempt_result(root, request['attempt_id'])
    assert row['output_sha256'] == retained['output']['sha256'] == file_sha256(retained['preserved_output']['path'])
    return row


def recovered_original(p):
    p.transport.timeout_next = True
    original = p.generate('entry')
    assert not original.success
    aid = original.data['production_attempt_id']
    bound = read(p.root / 'production_attempts' / aid / 'request.json')
    from tools._grok_cli_media import _parse_stream
    session = p.transport.original_sessions[aid]
    _, _, sid = _parse_stream(session['stream'], tool_name='reference_to_video', expected_arguments=session['arguments'])
    assert sid == aid
    result = copy.deepcopy(original)
    result.success, result.error, result.artifacts = True, None, [bound['submitted_inputs']['output_path']]
    result.data['dispatch_status'] = 'completed'
    result.data['conditioning_receipt'].update(dispatch_status='completed', submission_evidence='verified_native_call')
    return aid, bound, result


def recovery_case(kind, request, tmp_path, monkeypatch):
    """Prepare a genuinely uncertain/failed original; return its canonical recovery."""
    if kind == 'local':
        fixture = request.getfixturevalue('reserved')
        p, aid, _, bound, _ = fixture
        return p.root, aid, bound, lambda: continue_attempt(fixture, dry_run=False)
    if kind == 'original_prompt':
        fixture = request.getfixturevalue('rejected')
        p, aid, _, bound, _ = fixture
        return p.root, aid, bound, lambda: collect(fixture, dry_run=False)
    if kind == 'reconciliation':
        p = request.getfixturevalue('production')
        aid, bound, result = recovered_original(p)
        return p.root, aid, bound, lambda: execution.reconcile_attempt(p.root, aid, result, request_sha256=bound['request_sha256'])
    inputs, _, state = synthetic_openart_project(tmp_path, monkeypatch)
    original = OpenArtCLIVideo().execute(inputs)
    aid = original.data['production_attempt_id']
    bound = read(tmp_path / 'production_attempts' / aid / 'request.json')
    assert state['submits'] == 1 and state['collections'] == 0
    return tmp_path, aid, bound, lambda: execution.collect_openart_attempt(tmp_path, aid, request_sha256=bound['request_sha256'])


@pytest.mark.parametrize('kind', ['local', 'reconciliation', 'original_prompt', 'openart'])
def test_successful_original_recovery_observes_exact_new_publication_once(kind, request, tmp_path, monkeypatch):
    root, aid, bound, recover = recovery_case(kind, request, tmp_path, monkeypatch)
    count = len(execution._attempts(root))
    scope_bytes = (root / 'production_scopes.json').read_bytes()
    assert not observations(root, aid)
    before = datetime.now(timezone.utc)
    recover()
    assert_observed(root, bound, before)
    events = (root / 'events.jsonl').read_bytes()
    if kind == 'openart':
        assert recover()['status'] == 'generated'
    else:
        with pytest.raises((execution.ProductionGovernanceError, FileExistsError)):
            recover()
    assert (root / 'events.jsonl').read_bytes() == events
    assert len(execution._attempts(root)) == count
    assert (root / 'production_scopes.json').read_bytes() == scope_bytes


@pytest.mark.parametrize('kind', ['local', 'reconciliation', 'original_prompt', 'openart'])
def test_recovery_retention_reaches_studio_with_unknown_backend_time(kind, request, tmp_path, monkeypatch):
    timing = pytest.importorskip('production_timing')
    root, aid, bound, recover = recovery_case(kind, request, tmp_path, monkeypatch)
    assert timing.summarize_project(root)['first_video_retained'] is None
    before = datetime.now(timezone.utc)
    recover()
    row = assert_observed(root, bound, before)
    report = timing.summarize_project(root)
    assert report['first_video_retained']['attempt_id'] == aid
    assert report['first_video_retained']['ts'] == row['ts']
    assert report['first_video_retained']['output_sha256'] == row['output_sha256']
    assert report['backend_render_seconds'] is None


@pytest.mark.parametrize('kind', ['local', 'reconciliation', 'original_prompt', 'openart'])
def test_failed_publication_has_no_success_clock(kind, request, tmp_path, monkeypatch):
    root, aid, _, recover = recovery_case(kind, request, tmp_path, monkeypatch)
    publish = execution._write_new
    def fail_generated(path, value):
        if Path(path).name in {'local_continuation_result.json', 'reconciliation.json'} and value.get('status') == 'generated':
            raise OSError('synthetic publication failure')
        return publish(path, value)
    monkeypatch.setattr(execution, '_write_new', fail_generated)
    with pytest.raises(OSError, match='synthetic publication failure'):
        recover()
    assert not observations(root, aid)


@pytest.mark.parametrize('kind', ['local', 'reconciliation', 'original_prompt', 'openart'])
def test_timing_failure_does_not_block_retained_original(kind, request, tmp_path, monkeypatch):
    root, aid, _, recover = recovery_case(kind, request, tmp_path, monkeypatch)
    def fail_event(*args, **kwargs):
        raise OSError('synthetic observation failure')
    with monkeypatch.context() as dropped:
        dropped.setattr('lib.events.emit_event', fail_event)
        recover()
    assert execution.load_attempt_result(root, aid)['status'] == 'generated'
    assert not observations(root, aid)
    # Replaying evidence without its original clock cannot claim a new retention.
    if kind == 'openart':
        assert recover()['status'] == 'generated'
    else:
        with pytest.raises((execution.ProductionGovernanceError, FileExistsError)):
            recover()
    assert not observations(root, aid)


@pytest.mark.parametrize('success', [False, True])
def test_local_failed_or_missing_output_has_no_retention_clock(reserved, monkeypatch, success):
    p, aid, _, _, _ = reserved
    def no_output(**kwargs):
        return ToolResult(success=success, data={'session_id': aid, 'dispatch_status': 'failed' if not success else 'completed'})
    monkeypatch.setattr('tools.video.grok_cli_video.execute_grok_cli_media', no_output)
    continue_attempt(reserved, dry_run=False)
    assert execution.load_attempt_result(p.root, aid)['status'] == ('failed' if not success else 'uncertain')
    assert not observations(p.root, aid)


@pytest.mark.parametrize('outcome', ['failed', 'no_output', 'wrong_request'])
def test_reconciliation_no_output_or_invalid_original_has_no_success_clock(production, outcome):
    p = production
    aid, bound, result = recovered_original(p)
    digest = bound['request_sha256']
    if outcome == 'failed':
        result.success = False
        result.data['dispatch_status'] = 'failed'
        result.data['conditioning_receipt']['dispatch_status'] = 'failed'
        assert execution.reconcile_attempt(p.root, aid, result, request_sha256=digest)['status'] == 'failed'
    else:
        if outcome == 'no_output':
            Path(bound['submitted_inputs']['output_path']).unlink()
        else:
            digest = '0' * 64
        with pytest.raises(execution.ProductionGovernanceError):
            execution.reconcile_attempt(p.root, aid, result, request_sha256=digest)
    assert not observations(p.root, aid)


def test_original_prompt_missing_source_has_no_retention_clock(rejected):
    p, aid, _, _, _ = rejected
    assert collect(rejected)['would_collect_original'] is True
    assert not observations(p.root, aid)
    p.transport.original_sessions[aid]['artifact'].unlink()
    with pytest.raises((execution.ProductionGovernanceError, GrokCLIContractError)):
        collect(rejected, dry_run=False)
    assert not observations(p.root, aid)


def test_openart_missing_output_receipt_has_no_retention_clock(tmp_path, monkeypatch):
    from lib import openart_jobs as jobs
    inputs, _, state = synthetic_openart_project(tmp_path, monkeypatch)
    original = OpenArtCLIVideo().execute(inputs)
    aid, digest = original.data['production_attempt_id'], original.data['production_request_sha256']
    collect_job = jobs.collect_job
    def missing_output(*args, **kwargs):
        collected = collect_job(*args, **kwargs)
        collected['output'] = None
        return collected
    monkeypatch.setattr(jobs, 'collect_job', missing_output)
    with pytest.raises(execution.ProductionGovernanceError, match='collected output differs'):
        execution.collect_openart_attempt(tmp_path, aid, request_sha256=digest)
    assert not observations(tmp_path, aid)
    assert not (tmp_path / 'production_attempts' / aid / 'reconciliation.json').exists()
    assert state['submits'] == 1


def test_openart_pending_then_terminal_failure_never_observes_video(tmp_path, monkeypatch):
    from lib import openart_jobs as jobs
    inputs, _, state = synthetic_openart_project(tmp_path, monkeypatch)
    original = OpenArtCLIVideo().execute(inputs)
    aid, digest = original.data['production_attempt_id'], original.data['production_request_sha256']
    state['pending'] = True
    assert execution.collect_openart_attempt(tmp_path, aid, request_sha256=digest)['status'] == 'pending'
    assert not observations(tmp_path, aid)
    binding = copy.deepcopy(state['launch'][aid]['binding'])
    stable = {'attempt_id': aid, 'binding': binding, 'job_id_sha256': 'j' * 64,
              'terminal_failure_sha256': 'f' * 64, 'account_id_sha256': binding['account_id_sha256'], 'process_state': 'dead'}
    monkeypatch.setattr(jobs, 'collect_job', lambda *a, **k: {'status': 'failed_terminal'})
    monkeypatch.setattr(jobs, 'reconcile_job', lambda *a: {'state': 'failed_terminal', 'binding': binding})
    monkeypatch.setattr(jobs, 'verify_terminal_failure', lambda *a: copy.deepcopy(stable))
    failed = execution.collect_openart_attempt(tmp_path, aid, request_sha256=digest)
    assert failed['status'] == 'failed' and failed['output'] is None
    assert execution.collect_openart_attempt(tmp_path, aid, request_sha256=digest) == failed
    assert not observations(tmp_path, aid)
    assert state['submits'] == 1
