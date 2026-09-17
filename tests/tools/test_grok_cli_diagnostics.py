"""Timeout observations must never be mistaken for proof of no remote spend."""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from tests.tools.test_grok_cli_media import CLI_HELP

from tools._grok_cli_media import (
    GrokCLIContractError, _activity_summary, _run_process,
    _session_diagnostics, execute_grok_cli_media, MIN_CLI_VERSION,
)


def ndjson(*events):
    return ('\n'.join(json.dumps(event) for event in events) + '\n').encode()


@pytest.mark.parametrize('raw,stage', [
    (None, 'no_activity_recorded'),
    (b'not json\n{"type":', 'no_activity_recorded'),
    (ndjson({'type': 'turn_started'}), 'no_tool_call_recorded'),
    (ndjson({'type': 'tool_call', 'rawInput': {'prompt': 'private'}}), 'tool_call_recorded'),
    (ndjson({'type': 'tool_call_update', 'status': 'completed', 'rawOutput': {'url': 'secret'}}), 'tool_completion_recorded'),
])
def test_partial_stdout_summary_is_structured_and_private(raw, stage):
    summary = _activity_summary(raw)
    assert summary['observed_stage'] == stage
    assert 'private' not in json.dumps(summary)
    assert 'secret' not in json.dumps(summary)


def test_timeout_recovers_correlated_session_when_stdout_is_empty(monkeypatch, tmp_path):
    sid = '34526031-8418-4ac6-ae77-a47bba5988c9'
    monkeypatch.setattr('tools._grok_cli_media.uuid4', lambda: sid)
    sessions = tmp_path / 'sessions'
    directory = sessions / quote(str(tmp_path), safe='') / sid
    directory.mkdir(parents=True)
    (directory / 'events.jsonl').write_bytes(ndjson(
        {'type': 'mcp_config_resolved', 'target': 'PRIVATE_TOKEN'},
        {'type': 'turn_started', 'session_id': sid},
    ))
    (directory / 'updates.jsonl').write_bytes(ndjson({'params': {'sessionId': sid, 'update': {
        'sessionUpdate': 'user_message_chunk', 'content': {'text': 'PRIVATE_PROMPT'}}}}))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if '--version' in argv:
            return subprocess.CompletedProcess(argv, 0, f'grok {MIN_CLI_VERSION}', '')
        if '--help' in argv:
            return subprocess.CompletedProcess(argv, 0, CLI_HELP, '')
        assert argv[argv.index('--session-id') + 1] == sid
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output=b'', stderr=b'PRIVATE_STDERR')
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', run)
    result = execute_grok_cli_media(tool_name='image_to_video', arguments={'prompt': 'PRIVATE_PROMPT'},
        output_path=str(tmp_path / 'clip.mp4'), cwd=str(tmp_path), grok_path='fake-grok',
        sessions_root=str(sessions), timeout_seconds=600, media_kind='video')
    assert not result.success
    assert result.data['dispatch_status'] == 'indeterminate'
    assert result.data['retry_attempted'] is False and result.data['fallback_attempted'] is False
    assert len(calls) == 3
    diagnostics = result.data['diagnostics']
    assert diagnostics['session_id'] == sid
    assert diagnostics['process_stage'] == 'media_dispatch'
    assert diagnostics['timeout_seconds'] == 600
    assert diagnostics['events.jsonl']['observed_stage'] == 'no_tool_call_recorded'
    assert diagnostics['updates.jsonl']['observed_stage'] == 'no_tool_call_recorded'
    assert 'PRIVATE' not in json.dumps(result.data)
    assert 'PRIVATE' not in result.error


def test_recorded_submission_does_not_trigger_retry(monkeypatch, tmp_path):
    runner = Mock(side_effect=subprocess.TimeoutExpired(['grok'], 30, output=ndjson(
        {'type': 'tool_call', 'rawInput': {'token': 'SECRET'}})))
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', runner)
    with pytest.raises(GrokCLIContractError) as raised:
        _run_process(['grok', '--prompt-file', 'fixture'], cwd=tmp_path, timeout=30)
    assert raised.value.dispatch_status == 'indeterminate'
    assert raised.value.diagnostics['stdout']['tool_call_observed'] is True
    assert runner.call_count == 1


def test_version_timeout_cannot_be_a_media_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr('tools._grok_cli_media.subprocess.run', Mock(side_effect=subprocess.TimeoutExpired(['grok'], 10)))
    with pytest.raises(GrokCLIContractError) as raised:
        _run_process(['grok', '--version'], cwd=tmp_path, timeout=10)
    assert raised.value.dispatch_status == 'not_dispatched'
    assert raised.value.diagnostics['process_stage'] == 'version_check'


def test_missing_and_bounded_logs_are_nonfatal(tmp_path):
    diagnostics = _session_diagnostics(tmp_path / 'missing', tmp_path, 'fixture')
    assert diagnostics['events.jsonl'] == {'readable': False}
    summary = _activity_summary(b'x' * (300 * 1024) + b'\n' + ndjson({'type': 'tool_started'}))
    assert summary['truncated'] is True and summary['tool_call_observed'] is True
    assert summary['bytes_examined'] == 256 * 1024
