"""Synthetic same-original lifecycle regressions; no live account or generation."""
import copy
import hashlib
import json
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib import production_autonomy as autonomy, production_execution as execution
from lib import spicy_catalog as catalog, spicy_jobs as jobs, spicy_lifecycle as lifecycle

MODEL = catalog.VIDEO_MODEL
HOST = 'fixture-account.r2.cloudflarestorage.com'


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    import socket
    import ssl
    def blocked(*args, **kwargs):
        pytest.fail('Lifecycle tests must never open a real network connection')
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)


class HTTP:
    def __init__(self, binding):
        self.binding = binding
        self.qualified_storage_hosts = frozenset({HOST})
        self.calls = []
        self.lose_first = True
        self.create_data = {'taskId': 'original-task', 'model': MODEL, 'state': 'queued', 'estimatedCost': '0.10'}
        self.task = {'taskId': 'original-task', 'model': MODEL, 'state': 'running', 'settled': False, 'cost': None}

    def get_task(self, task_id):
        return self.request_json('GET', '/jobs/recordInfo', query={'taskId': task_id})

    def create_task(self, *, model, input_data, idempotency_key, quote_id, expected_cost):
        body = {'model': model, 'input': input_data, 'quoteId': quote_id, 'expectedCost': expected_cost}
        return self.request_json('POST', '/jobs/createTask', body, headers={'Idempotency-Key': idempotency_key})

    def create_download_url(self, task_id, key=None):
        return self.request_json('POST', '/common/download-url', {'taskId': task_id, 'key': key})

    # Test-only recorder; production code calls only the named SDK-backed methods above.
    def request_json(self, method, path, body=None, *, query=None, headers=None):
        self.calls.append((method, path, copy.deepcopy(body), copy.deepcopy(query), copy.deepcopy(headers)))
        if path == '/jobs/createTask':
            if self.lose_first:
                self.lose_first = False
                raise jobs.SpicyTransportError('SYNTHETIC response loss signed?secret=never-retain', kind='transport_uncertain')
            return {'code': 200, 'data': copy.deepcopy(self.create_data)}
        if path == '/jobs/recordInfo':
            assert query == {'taskId': 'original-task'}
            return {'code': 200, 'data': copy.deepcopy(self.task)}
        if path == '/common/download-url':
            assert body == {'taskId': 'original-task', 'key': 'original/video.mp4'}
            return {'code': 200, 'data': {'url': 'https://' + HOST + '/original.mp4?signature=DO_NOT_RETAIN',
                'expiresAt': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}}
        pytest.fail('Unexpected synthetic API endpoint')


@pytest.fixture
def original(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    attempt = str(uuid.uuid4())
    directory = root / 'production_attempts' / attempt
    snapshot = directory / 'inputs' / 'original.png'
    snapshot.parent.mkdir(parents=True)
    source = root / 'original.png'
    source.write_bytes(b'SYNTHETIC reviewed source fixture')
    snapshot.write_bytes(source.read_bytes())
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    binding = {'credential_ref': 'env:SPICY_LIFECYCLE_OFFLINE_KEY', 'credential_digest': 'a' * 64}
    payload = {'image_url': 'spicy://f/original', 'prompt': 'SYNTHETIC fixture', 'duration_seconds': 4,
        'resolution': '480p', 'aspect_ratio': 'adaptive', 'generate_audio': False}
    native = {'provider': 'spicy', 'transport': jobs.TRANSPORT, 'model': MODEL, 'mode': 'image_to_video',
        'input': payload, 'payload_state': 'ready', 'request_sha256': catalog.digest({'model': MODEL, 'input': payload}),
        'sources': [{'role': 'first_frame', 'source_path': str(source), 'source_sha256': sha,
            'uri': payload['image_url'], 'upload_expires_at': '2000-01-01T00:00:00Z'}]}
    quote = {'quote_id': 'spq-synthetic', 'provider_quote_id': 'provider-original', 'currency': 'USD',
        'native_request_sha256': native['request_sha256'], 'model': MODEL, 'account_label': 'offline-fixture',
        'credential_digest': binding['credential_digest'], 'estimated_cost_usd': '0.10', 'max_charge_usd': '0.15',
        'obtained_at': '2000-01-01T00:00:00Z', 'expires_at': '2000-01-01T00:05:00Z'}
    authority = dict(binding, account_label='offline-fixture', qualified_storage_hosts=[HOST], quote=quote)
    inputs = {'project_dir': str(root), 'cli_session_id': attempt, 'credential_ref': binding['credential_ref'],
        'account_label': 'offline-fixture', 'spicy_quote_id': quote['quote_id'],
        'governance': {'scope_id': 'scope', 'shot_id': 'shot'},
        'input_assets': [{'role': 'first_frame', 'source_path': str(snapshot), 'source_sha256': sha}],
        'output_path': str(root / 'assets/video/original.mp4')}
    request = {'attempt_id': attempt, 'request_sha256': 'b' * 64, 'scope': {'provider': 'spicy'},
        'submitted_inputs': inputs, 'spicy_native': native, 'spicy_authority': authority,
        'input_assets': [{'role': 'first_frame', 'original_path': str(source), 'path': str(snapshot), 'sha256': sha}]}
    catalog.atomic_write_json(directory / 'request.json', request)
    monkeypatch.setattr(autonomy, 'verify_spicy_dispatch', lambda *args, **kwargs: copy.deepcopy(authority))
    http = HTTP(binding)
    monkeypatch.setattr(jobs, 'client_for_credential', lambda *args, **kwargs: http)
    def submit():
        token = execution._ACTIVE.set({'provider': 'spicy', 'provider_called': True, 'session_id': attempt,
            'root': root, 'directory': directory, 'submitted_inputs': inputs})
        try:
            return jobs.submit_original(inputs, client=http)
        finally:
            execution._ACTIVE.reset(token)
    return {'root': root, 'attempt': attempt, 'directory': directory, 'source': source, 'snapshot': snapshot,
        'inputs': inputs, 'request': request, 'authority': authority, 'http': http, 'submit': submit,
        'journal': directory / 'spicy_job.json'}


def test_ungoverned_direct_submit_has_no_side_effects(tmp_path):
    with pytest.raises(jobs.SpicyError, match='canonical production gateway'):
        jobs.submit_original({'project_dir': str(tmp_path)})
    assert list(tmp_path.iterdir()) == []


def test_lost_reply_replays_exact_original_after_quote_and_parent_expiry(original, monkeypatch):
    o = original
    assert o['submit']().data['dispatch_status'] == 'acceptance_unknown'
    saved = json.loads(o['journal'].read_text())
    assert saved['acceptance_state'] == 'acceptance_unknown' and saved['task_id'] is None
    assert 'never-retain' not in o['journal'].read_text()
    o['source'].unlink()
    monkeypatch.setattr(autonomy, 'verify_spicy_dispatch', lambda *a, **k: pytest.fail('Original recovery must not refresh authority'))
    result = jobs.resume_original(o['root'], o['attempt'])
    assert not result.success and result.data['task_id'] == 'original-task'
    creates = [c for c in o['http'].calls if c[1] == '/jobs/createTask']
    assert len(creates) == 2 and creates[0] == creates[1]
    assert jobs.billing_anomalies(o['root']) == []  # Unsettled cost:null is unknown.


@pytest.mark.parametrize('offset', [timedelta(hours=23), timedelta(hours=30), timedelta(seconds=-1)])
def test_acceptance_unknown_stops_at_safe_deadline_and_backward_clock(original, monkeypatch, offset):
    o = original
    o['submit']()
    saved = json.loads(o['journal'].read_text())
    monkeypatch.setattr(jobs, '_now', lambda: catalog.parse_utc(saved['started_at']) + offset)
    before = len(o['http'].calls)
    result = jobs.resume_original(o['root'], o['attempt'])
    assert not result.success and result.data['error_category'] == 'replay_window_closed'
    assert len(o['http'].calls) == before


def test_known_original_polls_outside_replay_window_without_new_create(original, monkeypatch):
    o = original
    o['http'].lose_first = False
    o['submit']()
    saved = json.loads(o['journal'].read_text())
    monkeypatch.setattr(jobs, '_now', lambda: catalog.parse_utc(saved['started_at']) + timedelta(days=9))
    result = jobs.resume_original(o['root'], o['attempt'])
    assert result.data['task_id'] == 'original-task'
    assert [c[1] for c in o['http'].calls].count('/jobs/createTask') == 1


def test_known_task_saved_before_malformed_estimate_state_and_model(original):
    o = original
    o['http'].lose_first = False
    o['http'].create_data.update(estimatedCost='secret-not-a-decimal', model='unexpected', state='unsupported')
    assert not o['submit']().success
    saved = json.loads(o['journal'].read_text())
    assert saved['task_id'] == 'original-task' and saved['acceptance_state'] == 'accepted'
    assert saved['billing_anomaly']['kind'] == 'billing_anomaly'
    assert 'secret-not-a-decimal' not in o['journal'].read_text()
    jobs.resume_original(o['root'], o['attempt'])
    assert [c[1] for c in o['http'].calls].count('/jobs/createTask') == 1


@pytest.mark.parametrize('state', ['failed', 'expired', 'canceled'])
def test_terminal_failure_keeps_original_and_never_regenerates(original, state):
    o = original
    o['http'].lose_first = False
    o['http'].task['state'] = state
    assert o['submit']().data['dispatch_status'] == 'failed'
    assert jobs.resume_original(o['root'], o['attempt']).data['dispatch_status'] == 'failed'
    assert [c[1] for c in o['http'].calls].count('/jobs/createTask') == 1


def test_concurrent_unknown_recovery_replays_key_once_then_only_polls(original):
    o = original
    o['submit']()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: jobs.resume_original(o['root'], o['attempt']), range(4)))
    assert all(r.data['task_id'] == 'original-task' for r in results)
    creates = [c for c in o['http'].calls if c[1] == '/jobs/createTask']
    assert len(creates) == 2 and creates[0] == creates[1]


@pytest.mark.parametrize('damage', ['snapshot', 'native', 'body', 'key', 'credential'])
def test_original_binding_drift_refuses_before_transport(original, damage):
    o = original
    o['submit']()
    saved = json.loads(o['journal'].read_text())
    if damage == 'snapshot': o['snapshot'].write_bytes(b'changed')
    elif damage == 'native': saved['native']['input']['prompt'] = 'changed'
    elif damage == 'body': saved['create_body']['expectedCost'] = '0'
    elif damage == 'key': saved['idempotency_key'] = str(uuid.uuid4())
    elif damage == 'credential': o['http'].binding = dict(o['http'].binding, credential_digest='c' * 64)
    if damage not in {'snapshot', 'credential'}:
        catalog.atomic_write_json(o['journal'], saved)
    before = len(o['http'].calls)
    with pytest.raises(jobs.SpicyError):
        jobs.resume_original(o['root'], o['attempt'])
    assert len(o['http'].calls) == before


@pytest.fixture(scope='module')
def synthetic_video(tmp_path_factory):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Local FFmpeg/ffprobe required for synthetic collection acceptance')
    path = tmp_path_factory.mktemp('spicy-lifecycle-local') / 'synthetic.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=480x480:r=24:d=4',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True)
    return path.read_bytes()


def _completed_task(o, media, monkeypatch):
    from tests.lib.test_spicy_factory import _install_synthetic_download_transport
    downloads = _install_synthetic_download_transport(monkeypatch, media)
    o['http'].lose_first = False
    o['http'].task.update(state='succeeded', settled=True, cost='0.10', output={'assets': [
        {'key': 'original/video.mp4', 'url': 'https://ignored.invalid/?secret=NOT_PERSISTED', 'mime': 'video/mp4',
         'width': 480, 'height': 480, 'durationSeconds': 4, 'bytes': len(media)}]})
    return downloads


def test_collected_original_pure_verification_survives_key_and_parent_removal(original, synthetic_video, monkeypatch):
    o = original
    downloads = _completed_task(o, synthetic_video, monkeypatch)
    result = o['submit']()
    assert result.success
    assert result.data['conditioning_receipt']['settlement'] == 'settled'
    o['source'].unlink()
    monkeypatch.setattr(jobs, 'client_for_credential', lambda *a, **k: pytest.fail('Pure collection must not resolve credentials'))
    before = len(o['http'].calls)
    proof = jobs.validate_collected_original(o['root'], o['attempt'])
    assert proof['receipt'] == result.data['conditioning_receipt'] and proof['task_id'] == 'original-task'
    assert jobs.resume_original(o['root'], o['attempt']).success
    assert len(o['http'].calls) == before and len(downloads) == 1
    text = o['journal'].read_text()
    assert 'DO_NOT_RETAIN' not in text and 'NOT_PERSISTED' not in text and 'signature=' not in text


@pytest.mark.parametrize('crash', ['before_link', 'after_link'])
def test_publication_crash_recovers_verified_bytes_without_any_api_call(original, synthetic_video, monkeypatch, crash):
    o = original
    downloads = _completed_task(o, synthetic_video, monkeypatch)
    original_finish = lifecycle._finish_collection
    def crash_finish(*args, **kwargs):
        raise RuntimeError('SYNTHETIC process interruption after atomic media publication')
    monkeypatch.setattr(lifecycle, '_finish_collection', crash_finish)
    assert not o['submit']().success
    saved = json.loads(o['journal'].read_text())
    target = Path(saved['collection_intent']['path'])
    assert target.exists() and 'output' not in saved
    if crash == 'before_link':
        target.rename(saved['collection_intent']['publication_source'])
    monkeypatch.setattr(lifecycle, '_finish_collection', original_finish)
    monkeypatch.setattr(jobs, 'client_for_credential', lambda *a, **k: pytest.fail('Publication recovery must not call API'))
    before = len(o['http'].calls)
    assert jobs.resume_original(o['root'], o['attempt']).success
    assert len(o['http'].calls) == before and len(downloads) == 1


@pytest.mark.parametrize('damage', ['hash', 'symlink', 'receipt'])
def test_collected_output_alteration_is_refused_purely(original, synthetic_video, monkeypatch, damage):
    o = original
    _completed_task(o, synthetic_video, monkeypatch)
    assert o['submit']().success
    saved = json.loads(o['journal'].read_text())
    target = Path(saved['output']['path'])
    if damage == 'hash': target.write_bytes(b'changed')
    elif damage == 'symlink':
        target.unlink()
        target.symlink_to(o['snapshot'])
    else:
        saved['conditioning_receipt']['task_id'] = 'different'
        catalog.atomic_write_json(o['journal'], saved)
    before = len(o['http'].calls)
    with pytest.raises(jobs.SpicyError):
        jobs.validate_collected_original(o['root'], o['attempt'])
    assert len(o['http'].calls) == before


@pytest.mark.parametrize('flag', ['pending', 'unavailable'])
def test_successful_task_with_unready_asset_never_creates_replacement(original, flag):
    o = original
    o['http'].lose_first = False
    o['http'].task.update(state='succeeded', output={'assets': [{'key': 'original/video.mp4', 'mime': 'video/mp4', flag: True}]})
    assert not o['submit']().success
    assert not jobs.resume_original(o['root'], o['attempt']).success
    assert [c[1] for c in o['http'].calls].count('/jobs/createTask') == 1
    assert '/common/download-url' not in [c[1] for c in o['http'].calls]


@pytest.mark.parametrize('final_cost', ['0.10', '0.20'])
def test_explicit_settlement_refresh_preserves_original_collected_proof(original, synthetic_video, monkeypatch, final_cost):
    o = original
    downloads = _completed_task(o, synthetic_video, monkeypatch)
    o['http'].task.update(settled=False, cost=None)
    first = o['submit']()
    assert first.success and first.cost_usd is None
    assert first.data['conditioning_receipt']['settlement'] == 'pending'
    proof = jobs.validate_collected_original(o['root'], o['attempt'])
    o['source'].unlink()
    o['http'].task.update(settled=True, cost=final_cost, output={'assets': [
        {'key': 'original/video.mp4', 'unavailable': True, 'pending': True}]})
    before = len(o['http'].calls)
    latest = jobs.refresh_settlement(o['root'], o['attempt'])
    assert len(o['http'].calls) == before + 1 and o['http'].calls[-1][1] == '/jobs/recordInfo'
    assert latest['settlement'] == ('settled' if final_cost == '0.10' else 'unknown')
    assert jobs.validate_collected_original(o['root'], o['attempt']) == proof
    assert jobs.resume_original(o['root'], o['attempt']).success
    assert len(o['http'].calls) == before + 1 and len(downloads) == 1
    incidents = jobs.billing_anomalies(o['root'], credential_digest='a' * 64)
    assert len(incidents) == (1 if final_cost == '0.20' else 0)
    assert jobs.billing_anomalies(o['root'], credential_digest='d' * 64) == []


@pytest.mark.parametrize('damage', ['currency', 'reversal', 'different_final', 'malformed_settled'])
def test_billing_inconsistency_freezes_new_work_without_changing_collection(original, synthetic_video, monkeypatch, damage):
    o = original
    _completed_task(o, synthetic_video, monkeypatch)
    assert o['submit']().success
    proof = jobs.validate_collected_original(o['root'], o['attempt'])
    if damage == 'currency': o['http'].task['currency'] = 'EUR'
    elif damage == 'reversal': o['http'].task.update(settled=False, cost=None)
    elif damage == 'different_final': o['http'].task['cost'] = '0.11'
    elif damage == 'malformed_settled': o['http'].task['settled'] = 'yes'
    latest = jobs.refresh_settlement(o['root'], o['attempt'])
    assert latest['settlement'] == 'unknown'
    assert jobs.billing_anomaly(o['root'], o['attempt'])['kind'] == 'billing_anomaly'
    assert jobs.validate_collected_original(o['root'], o['attempt']) == proof


def test_multiple_video_candidates_refuse_ambiguous_collection(original):
    o = original
    o['http'].lose_first = False
    o['http'].task.update(state='succeeded', output={'assets': [
        {'key': 'a/video.mp4', 'mime': 'video/mp4'}, {'key': 'b/video.mp4', 'mime': 'video/mp4'}]})
    result = o['submit']()
    assert not result.success and result.data['error_category'] == 'ambiguous_output_assets'
    assert '/common/download-url' not in [c[1] for c in o['http'].calls]


@pytest.mark.parametrize('cost', [0.10, 0, True, 'NaN', '-0.1', '1e-2', 'billing-secret-invalid'])
def test_malformed_final_http_billing_is_anomalous_but_original_collects(original, synthetic_video, monkeypatch, cost):
    o = original
    _completed_task(o, synthetic_video, monkeypatch)
    o['http'].task['cost'] = cost
    result = o['submit']()
    assert result.success and result.cost_usd is None
    assert result.data['conditioning_receipt']['settlement'] == 'unknown'
    assert jobs.billing_anomaly(o['root'], o['attempt'])['kind'] == 'billing_anomaly'
    assert 'billing-secret-invalid' not in o['journal'].read_text()
    assert jobs.validate_collected_original(o['root'], o['attempt'])['receipt'] == result.data['conditioning_receipt']


@pytest.mark.parametrize('state', ['running', 'failed', 'succeeded'])
def test_settlement_refresh_can_observe_any_known_original_without_collecting(original, state):
    o = original
    o['http'].lose_first = False
    o['submit']()
    o['http'].task.update(state=state, settled=True, cost='0.10', output={'assets': [
        {'key': 'original/video.mp4', 'unavailable': True}]})
    before = len(o['http'].calls)
    latest = jobs.refresh_settlement(o['root'], o['attempt'])
    assert latest['settlement'] == 'settled' and latest['settled_cost_usd'] == '0.10'
    assert len(o['http'].calls) == before + 1
    assert 'output' not in json.loads(o['journal'].read_text())


def test_settlement_refresh_refuses_unknown_original_before_network(original):
    o = original
    o['submit']()
    before = len(o['http'].calls)
    with pytest.raises(jobs.SpicyError, match='known original'):
        jobs.refresh_settlement(o['root'], o['attempt'])
    assert len(o['http'].calls) == before
