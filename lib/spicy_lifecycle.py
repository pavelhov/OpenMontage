"""Durable, same-original Spicy jobs below the canonical production gateway.

The provider subjournal never reserves an attempt and never generates a second
idempotency key for a retained original. Collection has no fresh-policy dependency.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import io
import json
import math
import os
import re
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from lib import spicy_jobs as jobs
from lib.spicy_catalog import atomic_write_json, digest, parse_utc
from tools.base_tool import ToolResult

REPLAY_WINDOW = timedelta(hours=23)
TERMINAL_FAILURES = frozenset({'failed', 'expired', 'canceled'})
STATES = TERMINAL_FAILURES | {'queued', 'running', 'succeeded'}
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ASSET_KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$')


def _fail(message, kind='original_binding_invalid'):
    raise jobs.SpicyError(message, kind=kind)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        _fail('Spicy original record is missing or unreadable')
    if not isinstance(value, dict):
        _fail('Spicy original record must be an object')
    return value


def _sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _inside(root, value):
    path = Path(os.path.abspath(value if Path(value).is_absolute() else root / value))
    try:
        path.relative_to(root)
    except ValueError:
        _fail('Spicy original path escapes its project')
    current = path
    while current != current.parent:
        if current.is_symlink():
            _fail('Spicy original paths must not contain symlinks')
        current = current.parent
    return path


def _paths(project_dir, attempt_id):
    if not isinstance(attempt_id, str) or not _ID.fullmatch(attempt_id):
        _fail('Spicy original attempt id is invalid')
    root = Path(project_dir).resolve()
    directory = _inside(root, Path('production_attempts') / attempt_id)
    return root, directory, directory / 'spicy_job.json'


@contextmanager
def _lock(directory):
    # The parent is the existing counted reservation; never create one here.
    descriptor = os.open(directory / '.spicy-original.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def billing_anomaly(project_dir, attempt_id):
    root, _, path = _paths(project_dir, attempt_id)
    if not path.exists():
        return None
    record = _read(_inside(root, path))
    anomaly = record.get('billing_anomaly')
    return copy.deepcopy(anomaly) if isinstance(anomaly, dict) else None


def billing_anomalies(project_dir, *, credential_digest=None):
    root = Path(project_dir).resolve()
    result = []
    for path in sorted((root / 'production_attempts').glob('*/spicy_job.json')):
        record = _read(_inside(root, path))
        if credential_digest is not None and record.get('credential_binding', {}).get('credential_digest') != credential_digest:
            continue
        anomaly = record.get('billing_anomaly')
        if isinstance(anomaly, dict):
            result.append(copy.deepcopy(anomaly))
    return result


def submit_original(inputs, client=None):
    return _submit_original(inputs, client=client)


def resume_original(project_dir, attempt_id, client=None):
    return _resume_original(project_dir, attempt_id, client=client)


def validate_collected_original(project_dir, attempt_id):
    return _validate_collected_original(project_dir, attempt_id)


def refresh_settlement(project_dir, attempt_id, client=None):
    """Explicit read-only observation of billing on the same original task.

    Collection-time media, assets and conditioning evidence stay immutable.
    """
    root, directory, path = _paths(project_dir, attempt_id)
    with _lock(directory):
        request, record = _bound_journal(root, directory, path, attempt_id)
        if not record.get('task_id'):
            _fail('Spicy settlement refresh requires a known original task')
        jobs._offline_guard()
        client = _client(record, client)
        response = client.get_task(record['task_id'])
        data = response.get('data')
        if (not isinstance(data, dict) or data.get('taskId') != record['task_id']
                or data.get('model') not in (None, record['native']['model'])):
            _fail('Spicy settlement observation differs from the original task', 'task_mismatch')
        _capture_billing(path, record, data)
        return {'task_id': record['task_id'], **_billing_snapshot(record)}


def _canonical(root, directory, attempt_id, *, snapshots=True, frozen_original=False):
    request_path = _inside(root, directory / 'request.json')
    request = _read(request_path)
    native, authority = request.get('spicy_native'), request.get('spicy_authority')
    if (request.get('attempt_id') != attempt_id or request.get('scope', {}).get('provider') != 'spicy'
            or not _SHA.fullmatch(str(request.get('request_sha256', '')))
            or not isinstance(native, dict) or not isinstance(authority, dict)):
        _fail('Spicy recovery requires the exact canonical counted reservation')
    if frozen_original and native.get('transport') == jobs.TRANSPORT_V2:
        jobs.require_original_v2(native)
    else:
        jobs.require_ready(native)
    quote = authority.get('quote')
    if (not isinstance(quote, dict) or quote.get('currency') != 'USD'
            or quote.get('native_request_sha256') != native['request_sha256']
            or quote.get('model') != native['model']
            or quote.get('credential_digest') != authority.get('credential_digest')
            or quote.get('account_label') != authority.get('account_label')):
        _fail('Spicy original quote/account/native binding differs')
    maximum = jobs._decimal(quote.get('max_charge_usd'), 'original maximum')
    estimate = jobs._decimal(quote.get('estimated_cost_usd'), 'original estimate')
    if maximum < estimate:
        _fail('Spicy original quote maximum is below its estimate')
    hosts = authority.get('qualified_storage_hosts')
    if (not isinstance(hosts, list) or not hosts or len(hosts) != len(set(hosts))
            or any(not isinstance(h, str) or not jobs.RESULT_HOST.fullmatch(h) for h in hosts)):
        _fail('Spicy original storage hosts are missing or malformed')
    submitted = request.get('submitted_inputs')
    if (not isinstance(submitted, dict) or submitted.get('cli_session_id') != attempt_id
            or submitted.get('credential_ref') != authority.get('credential_ref')
            or submitted.get('account_label') != authority.get('account_label')
            or submitted.get('spicy_quote_id') != quote.get('quote_id')):
        _fail('Spicy original submitted owner/account/quote binding differs')
    _inside(root, submitted.get('output_path', ''))
    if native.get('transport') == jobs.TRANSPORT_V2:
        _canonical_sources_v2(root, directory, request, native, snapshots=snapshots)
        return request, native, authority
    assets = request.get('input_assets')
    sources = native.get('sources')
    if not isinstance(assets, list) or not isinstance(sources, list) or len(assets) != len(sources) or not sources:
        _fail('Spicy original source snapshot layout differs')
    for source, asset in zip(sources, assets):
        role = source.get('role')
        source_path = source.get('source_path', source.get('path'))
        sha = source.get('source_sha256', source.get('sha256'))
        if (role not in jobs.SOURCE_ROLES or asset.get('role') not in {role, {'first_frame':'image_path', 'last_frame':'last_image_path'}[role]}
                or source_path != asset.get('original_path') or sha != asset.get('sha256')
                or not _SHA.fullmatch(str(sha)) or source.get('uri') != native['input'].get(jobs.SOURCE_ROLES[role])):
            _fail('Spicy original source role/hash/URI binding differs')
        snapshot = _inside(root, asset.get('path', ''))
        try:
            snapshot.relative_to(directory / 'inputs')
        except ValueError:
            _fail('Spicy original source is outside its canonical snapshot')
        if snapshots and (not snapshot.is_file() or _sha(snapshot) != sha):
            _fail('Spicy original source snapshot bytes changed')
    return request, native, authority


def _binding(authority):
    return {'credential_ref': authority['credential_ref'], 'credential_digest': authority['credential_digest']}


def _client(record, client):
    client = client or jobs.client_for_credential(record['credential_binding']['credential_ref'],
        qualified_storage_hosts=record['qualified_storage_hosts'])
    if (any(client.binding.get(k) != v for k, v in record['credential_binding'].items())
            or set(client.qualified_storage_hosts) != set(record['qualified_storage_hosts'])):
        _fail('Spicy original client credential or storage-host binding differs', 'credential_mismatch')
    return client


def _immutable(record):
    return {key: record.get(key) for key in ('version', 'attempt_id', 'request_sha256', 'request_file_sha256',
        'native', 'quote', 'credential_binding', 'account_label', 'qualified_storage_hosts', 'create_body',
        'idempotency_key', 'started_at', 'replay_deadline')}


def _bound_journal(root, directory, path, attempt_id):
    record = _read(_inside(root, path))
    known_v2 = record.get('version') == '2.0' and isinstance(record.get('task_id'), str) and _ID.fullmatch(record['task_id'])
    request, native, authority = _canonical(root, directory, attempt_id, snapshots=not known_v2, frozen_original=bool(known_v2))
    expected_body = {'model': native['model'], 'input': native['input'],
        'quoteId': authority['quote']['provider_quote_id'], 'expectedCost': authority['quote']['estimated_cost_usd']}
    started, deadline = parse_utc(record.get('started_at')), parse_utc(record.get('replay_deadline'))
    if (record.get('version') != ('2.0' if native.get('transport') == jobs.TRANSPORT_V2 else '1.0') or record.get('attempt_id') != attempt_id
            or record.get('request_sha256') != request['request_sha256']
            or record.get('request_file_sha256') != _sha(directory / 'request.json')
            or record.get('native') != native or record.get('quote') != authority['quote']
            or record.get('credential_binding') != _binding(authority)
            or record.get('account_label') != authority['account_label']
            or record.get('qualified_storage_hosts') != authority['qualified_storage_hosts']
            or record.get('create_body') != expected_body
            or not isinstance(record.get('idempotency_key'), str) or not _ID.fullmatch(record['idempotency_key'])
            or started is None or deadline is None or deadline - started != REPLAY_WINDOW
            or record.get('binding_sha256') != digest(_immutable(record))):
        _fail('Spicy original journal differs from its canonical request/body/key binding')
    task_id = record.get('task_id')
    if task_id is not None and (not isinstance(task_id, str) or not _ID.fullmatch(task_id)):
        _fail('Spicy original task id is malformed')
    return request, record


def _pending(record, status='pending', kind=None):
    data = {'provider': 'spicy', 'session_id': record['attempt_id'], 'dispatch_session_id': record['attempt_id'],
        'dispatch_status': status, 'task_id': record.get('task_id'), 'acceptance_state': record['acceptance_state'],
        'retry_attempted': False, 'fallback_attempted': False, 'settled': record.get('settled', False),
        'cost_usd': record.get('final_cost_usd')}
    if kind:
        data['error_category'] = kind
    return ToolResult(success=False, data=data, error='Spicy original task failed' if status == 'failed' else 'Spicy original task awaits recovery or collection',
        cost_usd=float(record['final_cost_usd']) if record.get('final_cost_usd') is not None else None,
        model=record['native']['model'])


def _incident(record, field, reason):
    record.setdefault('billing_anomaly', {'kind': 'billing_anomaly', 'attempt_id': record['attempt_id'],
        'credential_digest': record['credential_binding']['credential_digest'], 'field': field,
        'reason': reason, 'observed_at': jobs._stamp(jobs._now())})


def _billing(record, value, field):
    try:
        if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value):
            raise ValueError()
        amount = jobs._decimal(value, field)
        maximum = jobs._decimal(record['quote']['max_charge_usd'], 'original maximum')
        if amount > maximum:
            raise ValueError()
    except (ValueError, TypeError):
        _incident(record, field, 'malformed_or_above_original_maximum')
        return None
    return format(amount, 'f')


def _capture_billing(path, record, data, *, created=False):
    currency_ok = data.get('currency', 'USD') == 'USD'
    if not currency_ok:
        _incident(record, 'currency', 'non_usd_currency')
    if created or 'estimatedCost' in data:
        record['estimated_cost_usd'] = _billing(record, data.get('estimatedCost'), 'estimated_cost_usd')
    if 'settled' in data:
        settled = data['settled']
        if type(settled) is not bool:
            _incident(record, 'settled', 'malformed_settlement_flag')
            record['settled'] = None
            record['final_cost_usd'] = None
            cost = None
        else:
            cost = _billing(record, data.get('cost'), 'cost_usd') if data.get('cost') is not None or settled else None
            previous = record.get('settled_cost_observation')
            inconsistent = previous is not None and (not settled or cost != previous)
            if inconsistent:
                _incident(record, 'settled', 'settlement_inconsistent')
            record['settled'] = settled
            record['final_cost_usd'] = cost if settled and currency_ok and not inconsistent else None
            if record['final_cost_usd'] is not None:
                record.setdefault('settled_cost_observation', record['final_cost_usd'])
        observation = {'observed_at': jobs._stamp(jobs._now()), 'settled': settled if type(settled) is bool else None,
            'cost_usd': cost if currency_ok else None, 'currency': 'USD' if currency_ok else 'unknown'}
        history = record.setdefault('billing_observations', [])
        if not history or history[-1] != observation:
            history.append(observation)
            # Retain the first observation and bounded recent history.
            if len(history) > 128:
                del history[1:-127]
    atomic_write_json(path, record)


def _billing_snapshot(record):
    snapshot = {'settlement': ('settled' if record.get('settled') and record.get('final_cost_usd') is not None
        else 'unknown' if record.get('billing_anomaly') or record.get('settled') else 'pending'),
        'settled_cost_usd': record.get('final_cost_usd'), 'max_charge_usd': record['quote']['max_charge_usd']}
    if record.get('billing_anomaly'):
        snapshot['billing_anomaly'] = copy.deepcopy(record['billing_anomaly'])
    return snapshot


def _valid_billing_snapshot(snapshot, record):
    if (not isinstance(snapshot, dict) or snapshot.get('max_charge_usd') != record['quote']['max_charge_usd']
            or snapshot.get('settlement') not in {'settled', 'pending', 'unknown'}
            or set(snapshot) - {'max_charge_usd', 'settlement', 'settled_cost_usd', 'billing_anomaly'}):
        return False
    amount = snapshot.get('settled_cost_usd')
    if amount is not None:
        try:
            if jobs._decimal(amount, 'retained settled cost') > jobs._decimal(record['quote']['max_charge_usd'], 'original maximum'):
                return False
        except ValueError:
            return False
    return (snapshot['settlement'] != 'settled' or amount is not None)


def _capture_task(path, record, data, *, created=False):
    if not isinstance(data, dict):
        _fail('Spicy original response lacks a task record', 'malformed_task')
    task_id = data.get('taskId')
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        _fail('Spicy original response lacks a valid task id', 'malformed_task')
    if record.get('task_id') not in (None, task_id):
        _fail('Spicy original task id changed', 'task_mismatch')
    # Commit the only irreplaceable acceptance fact before parsing billing/state.
    record.update(task_id=task_id, acceptance_state='accepted')
    atomic_write_json(path, record)
    _capture_billing(path, record, data, created=created)
    if data.get('model') not in (None, record['native']['model']):
        atomic_write_json(path, record)
        _fail('Spicy original returned another model', 'task_mismatch')
    state = data.get('state')
    if state not in STATES:
        atomic_write_json(path, record)
        _fail('Spicy original returned an unsupported task state', 'malformed_task')
    record['task_state'] = state
    record['last_observed_at'] = jobs._stamp(jobs._now())
    # A billing incident freezes future work even when later output metadata is
    # malformed. Never let a parse failure erase the only observed anomaly.
    atomic_write_json(path, record)
    if record['native'].get('transport') == jobs.TRANSPORT_V2:
        return _capture_assets_v2(path, record, data)
    assets = data.get('output', {}).get('assets', []) if isinstance(data.get('output', {}), dict) else []
    if not isinstance(assets, list):
        atomic_write_json(path, record)
        _fail('Spicy original assets are malformed', 'malformed_task')
    retained = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        key = asset.get('key')
        if not isinstance(key, str) or not _ASSET_KEY.fullmatch(key) or '..' in key.split('/'):
            continue
        safe = {'key': key}
        for flag in ('pending', 'unavailable'):
            if flag in asset:
                if type(asset[flag]) is not bool:
                    _fail('Spicy original output state is malformed', 'malformed_task')
                safe[flag] = asset[flag]
        if asset.get('mime') == 'video/mp4':
            safe['mime'] = 'video/mp4'
        for name in ('width', 'height', 'bytes'):
            if type(asset.get(name)) is int and asset[name] > 0:
                safe[name] = asset[name]
        duration = asset.get('durationSeconds')
        if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
            safe['duration_seconds'] = duration
        retained.append(safe)
    if assets:
        record['assets'] = retained
    atomic_write_json(path, record)


_CREATE_FIELDS = frozenset({'model', 'input', 'quoteId', 'expectedCost'})


def _create(path, record, client):
    now = jobs._now()
    if now < parse_utc(record['started_at']) or now >= parse_utc(record['replay_deadline']):
        return _pending(record, 'acceptance_unknown', 'replay_window_closed')
    # Persist uncertainty before sending, including on the first request. A crash
    # at any later instruction can only replay this exact body and key.
    record['acceptance_state'] = 'acceptance_unknown'
    atomic_write_json(path, record)
    body = copy.deepcopy(record['create_body'])
    if not isinstance(body, dict) or set(body) != _CREATE_FIELDS:
        # The approved body hash covers every top-level field; refuse rather than drop one.
        return _pending(record, 'acceptance_unknown', 'unsupported_create_body')
    try:
        response = client.create_task(model=body['model'], input_data=body['input'],
            idempotency_key=record['idempotency_key'], quote_id=body['quoteId'],
            expected_cost=body['expectedCost'])
    except jobs.SpicyAPIError as exc:
        if exc.code in jobs.REJECTED_CODES:
            record.update(acceptance_state='rejected', task_state='failed')
            atomic_write_json(path, record)
            return _pending(record, 'failed', 'create_rejected')
        return _pending(record, 'acceptance_unknown', 'create_response_unknown')
    except Exception:
        # Never persist exception text: transports may embed URLs or credentials.
        return _pending(record, 'acceptance_unknown', 'create_response_unknown')
    try:
        _capture_task(path, record, response.get('data'), created=True)
    except jobs.SpicyError:
        return _pending(record, 'pending' if record.get('task_id') else 'acceptance_unknown', 'malformed_task')
    return None


def _submit_original(inputs, client=None):
    jobs._offline_guard()
    from lib import production_execution as execution, production_autonomy as autonomy
    active = execution._ACTIVE.get()
    if (not isinstance(inputs, dict) or not active or active.get('provider') != 'spicy'
            or not active.get('provider_called') or inputs.get('cli_session_id') != active.get('session_id')):
        _fail('Spicy dispatch requires the active canonical production gateway', 'governed_dispatch_required')
    root, directory, path = _paths(inputs.get('project_dir', ''), inputs['cli_session_id'])
    if (Path(active['root']).resolve() != root or Path(active['directory']) != directory
            or active.get('submitted_inputs') != inputs):
        _fail('Spicy dispatch differs from the canonical active reservation')
    with _lock(directory):
        request, native, authority = _canonical(root, directory, inputs['cli_session_id'])
        if request['submitted_inputs'] != inputs:
            _fail('Spicy provider inputs differ from the frozen canonical submission')
        if path.exists():
            _, record = _bound_journal(root, directory, path, request['attempt_id'])
            return _resume_locked(root, directory, path, request, record, client)
        checked = autonomy.verify_spicy_dispatch(root, inputs, native)
        counters = {'spent_or_reserved_usd', 'remaining_total_usd'}
        if ({k:v for k,v in checked.items() if k not in counters}
                != {k:v for k,v in authority.items() if k not in counters}):
            _fail('Spicy dispatch authority changed after canonical reservation')
        now = jobs._now().replace(microsecond=0)
        record = {'version': '2.0' if native.get('transport') == jobs.TRANSPORT_V2 else '1.0', 'attempt_id': request['attempt_id'], 'request_sha256': request['request_sha256'],
            'request_file_sha256': _sha(directory / 'request.json'), 'native': copy.deepcopy(native),
            'quote': copy.deepcopy(authority['quote']), 'credential_binding': _binding(authority),
            'account_label': authority['account_label'], 'qualified_storage_hosts': authority['qualified_storage_hosts'],
            'create_body': {'model': native['model'], 'input': copy.deepcopy(native['input']),
                'quoteId': authority['quote']['provider_quote_id'], 'expectedCost': authority['quote']['estimated_cost_usd']},
            'idempotency_key': str(uuid.uuid4()), 'started_at': jobs._stamp(now),
            'replay_deadline': jobs._stamp(now + REPLAY_WINDOW), 'acceptance_state': 'prepared',
            'task_id': None, 'settled': False, 'final_cost_usd': None}
        record['binding_sha256'] = digest(_immutable(record))
        client = _client(record, client)
        atomic_write_json(path, record, exclusive=True)
        pending = _create(path, record, client)
        if pending is not None:
            return pending
        return _resume_locked(root, directory, path, request, record, client)


def _resume_original(project_dir, attempt_id, client=None):
    root, directory, path = _paths(project_dir, attempt_id)
    with _lock(directory):
        request, record = _bound_journal(root, directory, path, attempt_id)
        return _resume_locked(root, directory, path, request, record, client)


def _resume_locked(root, directory, path, request, record, client):
    if record.get('output') is not None:
        collected = _validate_collected_original(root, record['attempt_id'])
        return _completed(record, collected)
    if _recover_publication(root, path, request, record):
        return _completed(record, _validate_collected_original(root, record['attempt_id']))
    if record['acceptance_state'] == 'rejected':
        return _pending(record, 'failed', 'create_rejected')
    jobs._offline_guard()
    client = _client(record, client)
    if not record.get('task_id'):
        pending = _create(path, record, client)
        if pending is not None:
            return pending
    try:
        response = client.get_task(record['task_id'])
        _capture_task(path, record, response.get('data'))
    except Exception:
        return _pending(record, 'pending', 'record_unavailable')
    if record.get('task_state') in TERMINAL_FAILURES:
        return _pending(record, 'failed', 'terminal_task_failure')
    if record.get('task_state') != 'succeeded':
        return _pending(record)
    return _collect(root, path, request, record, client)


def _probe(path, native, asset):
    """Read only local MP4 bytes; external container references are disabled."""
    if not path.is_file() or path.is_symlink():
        _fail('Spicy original output is not a regular file', 'invalid_media')
    with path.open('rb') as handle:
        header = handle.read(32)
    if len(header) < 12 or header[4:8] != b'ftyp':
        _fail('Spicy original output is not an MP4 container', 'invalid_media')
    try:
        process = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-show_entries', 'format=format_name,duration:stream=codec_type,codec_name,width,height,duration,nb_frames',
            '-of', 'json', str(path)], capture_output=True, timeout=30, check=True)
        probe = json.loads(process.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        _fail('Spicy original output failed local video probing', 'invalid_media')
    streams = probe.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video']
    if len(videos) != 1 or not videos[0].get('codec_name'):
        _fail('Spicy original output requires one meaningful video stream', 'invalid_media')
    video = videos[0]
    width, height = video.get('width'), video.get('height')
    try:
        duration = float(probe.get('format', {}).get('duration'))
    except (ValueError, TypeError):
        _fail('Spicy original output has no meaningful duration', 'invalid_media')
    if (type(width) is not int or type(height) is not int or min(width, height) < 16
            or max(width, height) > 16384 or not math.isfinite(duration) or duration <= 0
            or 'mp4' not in probe.get('format', {}).get('format_name', '').split(',')):
        _fail('Spicy original output dimensions/container/duration are invalid', 'invalid_media')
    expected = native['input']['duration_seconds']
    if abs(duration - expected) > max(.5, expected * .10):
        _fail('Spicy original output duration differs from the frozen native request', 'invalid_media')
    # Provider and actual stream metadata must agree; a successful HTTP download
    # never makes a tiny, truncated or differently dimensioned clip acceptable.
    for name, actual in (('width', width), ('height', height)):
        if name in asset and asset[name] != actual:
            _fail('Spicy original output dimensions differ from task metadata', 'invalid_media')
    if 'duration_seconds' in asset and abs(float(asset['duration_seconds']) - duration) > .5:
        _fail('Spicy original output duration differs from task metadata', 'invalid_media')
    expected_short = {'480p': 480, '720p': 720}.get(native['input'].get('resolution'))
    if expected_short and min(width, height) != expected_short:
        _fail('Spicy original output resolution differs from the native request', 'invalid_media')
    ratio = native['input'].get('aspect_ratio', 'adaptive')
    if ratio != 'adaptive':
        numerator, denominator = map(int, ratio.split(':'))
        if abs(width / height - numerator / denominator) > max(.01, 2 / min(width, height)):
            _fail('Spicy original output aspect ratio differs from the native request', 'invalid_media')
    frames = video.get('nb_frames')
    if frames is not None and (not str(frames).isdecimal() or int(frames) < 2):
        _fail('Spicy original output has no meaningful frame sequence', 'invalid_media')
    return {'mime': 'video/mp4', 'width': width, 'height': height, 'duration_seconds': duration,
        'video_codec': video['codec_name'], 'video_streams': 1,
        'audio_streams': sum(s.get('codec_type') == 'audio' for s in streams),
        'frames': int(frames) if frames is not None else None}


def _receipt(request, record, output, media):
    receipt = {'version': '1.0', 'provider': 'spicy', 'transport': jobs.TRANSPORT,
        'native_tool': 'spicy_video', 'adapter_version': '0.1.0', 'session_id': record['attempt_id'],
        'dispatch_status': 'completed', 'submission_evidence': 'verified_native_call',
        'request_sha256': request['request_sha256'], 'native_request_sha256': record['native']['request_sha256'],
        'submitted_arguments': copy.deepcopy(record['create_body']), 'task_id': record['task_id'],
        'model': record['native']['model'], 'quote_id': record['quote']['quote_id'],
        'provider_quote_id': record['quote']['provider_quote_id'],
        'task_state': 'succeeded', **copy.deepcopy(record['collection_intent']['billing_at_collection']),
        'credential_binding': copy.deepcopy(record['credential_binding']),
        'account_label': record['account_label'], 'idempotency_key_sha256': digest(record['idempotency_key']),
        'input_assets': [{'role': source['role'], 'path': asset['path'], 'sha256': asset['sha256'],
            'uri': source['uri']} for source, asset in zip(record['native']['sources'], request['input_assets'])],
        'output': copy.deepcopy(output), 'media_metadata': copy.deepcopy(media)}
    return receipt


def _finish_collection(root, path, request, record):
    intent = record['collection_intent']
    output = {'path': intent['path'], 'sha256': intent['sha256']}
    target = _inside(root, output['path'])
    if not target.is_file() or target.is_symlink() or _sha(target) != output['sha256'] or target.stat().st_size != intent['bytes']:
        _fail('Spicy original published bytes differ from collection intent', 'output_altered')
    media = _probe(target, record['native'], intent['asset'])
    if media != intent['media_metadata']:
        _fail('Spicy original published media differs from collection intent', 'output_altered')
    record['output'] = output
    record['media_metadata'] = media
    record['conditioning_receipt'] = _receipt(request, record, output, media)
    record['collected_at'] = jobs._stamp(jobs._now())
    atomic_write_json(path, record)


def _recover_publication(root, path, request, record):
    if record['native'].get('transport') == jobs.TRANSPORT_V2:
        return _recover_publication_v2(root, path, request, record)
    intent = record.get('collection_intent')
    if intent is None:
        return False
    if (not isinstance(intent, dict) or intent.get('task_id') != record.get('task_id')
            or intent.get('path') != request['submitted_inputs']['output_path']
            or not _SHA.fullmatch(str(intent.get('sha256', '')))
            or intent.get('mime') != 'video/mp4'
            or not _valid_billing_snapshot(intent.get('billing_at_collection'), record)
            or type(intent.get('bytes')) is not int or intent['bytes'] <= 0
            or not isinstance(intent.get('asset'), dict)
            or intent.get('asset') not in record.get('assets', [])):
        _fail('Spicy original collection intent differs from the task/output binding')
    target = _inside(root, intent['path'])
    if not target.exists():
        temporary = _inside(root, intent.get('publication_source', ''))
        if temporary.parent != target.parent or not temporary.name.startswith('.openart-download-'):
            _fail('Spicy original collection temporary path differs')
        if not temporary.is_file() or _sha(temporary) != intent['sha256']:
            # No bytes were published. A future read may download this exact
            # task/key again; the retained intent prevents trusting orphan bytes.
            return False
        if _probe(temporary, record['native'], intent['asset']) != intent.get('media_metadata'):
            _fail('Spicy original collection temporary media differs')
        from lib.openart_download import _open_output_parent
        # Bind publication to the exact no-follow directory walk used by the
        # hardened downloader. A replaced parent can never redirect the link.
        parent_fd, checked_target = _open_output_parent(target, root)
        try:
            source_fd = os.open(temporary.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                source_stat = os.fstat(source_fd)
                if (not stat.S_ISREG(source_stat.st_mode)
                        or source_stat.st_size != intent['bytes']):
                    _fail('Spicy original publication temporary bytes changed')
                with os.fdopen(os.dup(source_fd), 'rb') as stream:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        hasher.update(chunk)
                if hasher.hexdigest() != intent['sha256']:
                    _fail('Spicy original publication temporary hash changed')
                os.link(temporary.name, checked_target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
                os.fsync(parent_fd)
                os.unlink(temporary.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(source_fd)
        finally:
            os.close(parent_fd)
    _finish_collection(root, path, request, record)
    return True


def _collect(root, path, request, record, client):
    if record['native'].get('transport') == jobs.TRANSPORT_V2:
        return _collect_v2(root, path, request, record, client)
    from lib.openart_download import collect_output
    assets = record.get('assets', [])
    available = [a for a in assets if not a.get('pending') and not a.get('unavailable') and a.get('mime') == 'video/mp4']
    if not available:
        return _pending(record, 'output_unavailable' if any(a.get('unavailable') for a in assets) else 'pending', 'output_not_ready')
    if len(available) != 1:
        return _pending(record, 'pending', 'ambiguous_output_assets')
    asset = available[0]
    target = _inside(root, request['submitted_inputs']['output_path'])
    if target.exists():
        _fail('Spicy original output exists without matching collection proof', 'unreceipted_output')
    # The canonical output parent is an engine-owned local destination. Never
    # derive paths, hosts or filenames from any provider response.
    missing, parent = [], target.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    from lib.spicy_catalog import _fsync_dir
    for created in reversed(missing):
        _fsync_dir(created.parent)
    _inside(root, target)
    try:
        response = client.create_download_url(record['task_id'], asset['key'])
        data = response.get('data')
        expires = parse_utc(data.get('expiresAt')) if isinstance(data, dict) else None
        if expires is None or expires <= jobs._now() or not isinstance(data.get('url'), str):
            return _pending(record, 'pending', 'download_url_unavailable')
        def before_publish(metadata):
            temporary = _inside(root, metadata['publication_source'])
            media = _probe(temporary, record['native'], asset)
            if 'bytes' in asset and metadata['size'] != asset['bytes']:
                _fail('Spicy original download byte count differs from task metadata', 'invalid_media')
            intent = {'task_id': record['task_id'], 'asset': copy.deepcopy(asset), 'path': str(target),
                'sha256': metadata['sha256'], 'bytes': metadata['size'], 'mime': 'video/mp4',
                'publication_source': str(temporary), 'media_metadata': media,
                'billing_at_collection': _billing_snapshot(record)}
            record['collection_intent'] = intent
            atomic_write_json(path, record)
        collect_output(data['url'], target, allowed_hosts=tuple(record['qualified_storage_hosts']), output_root=root,
            max_bytes=512 * 1024 * 1024, max_redirects=0, expected_mime='video/mp4', before_publish=before_publish)
        _finish_collection(root, path, request, record)
    except Exception:
        return _pending(record, 'pending', 'collection_unavailable')
    return _completed(record, _validate_collected_original(root, record['attempt_id']))


def _completed(record, collected):
    if record['native'].get('transport') == jobs.TRANSPORT_V2:
        return _completed_v2(record, collected)
    return ToolResult(success=True, data={'provider': 'spicy', 'session_id': record['attempt_id'],
        'dispatch_session_id': record['attempt_id'], 'dispatch_status': 'completed', 'task_id': record['task_id'],
        'conditioning_receipt': copy.deepcopy(collected['receipt']), 'settled': record.get('settled', False),
        'cost_usd': record.get('final_cost_usd'), 'retry_attempted': False, 'fallback_attempted': False,
        **copy.deepcopy(record['media_metadata'])}, artifacts=[collected['output']['path']],
        cost_usd=float(record['final_cost_usd']) if record.get('final_cost_usd') is not None else None,
        model=record['native']['model'], duration_seconds=record['media_metadata']['duration_seconds'])


def _validate_collected_original(project_dir, attempt_id):
    root, directory, path = _paths(project_dir, attempt_id)
    request, record = _bound_journal(root, directory, path, attempt_id)
    if record['native'].get('transport') == jobs.TRANSPORT_V2:
        return _validate_collected_v2(root, request, record)
    if (record.get('acceptance_state') != 'accepted' or not record.get('task_id')
            or record.get('task_state') != 'succeeded' or not isinstance(record.get('output'), dict)):
        _fail('Spicy original has no completed same-task collection proof')
    output = record['output']
    intent = record.get('collection_intent')
    if (not isinstance(intent, dict) or intent.get('task_id') != record['task_id']
            or output != {'path': request['submitted_inputs']['output_path'], 'sha256': intent.get('sha256')}
            or intent.get('path') != output['path'] or intent.get('mime') != 'video/mp4'
            or not _valid_billing_snapshot(intent.get('billing_at_collection'), record)
            or intent.get('asset') not in record.get('assets', [])):
        _fail('Spicy original output lacks exact task publication intent')
    target = _inside(root, output['path'])
    if (not target.is_file() or target.is_symlink() or target.stat().st_size != intent.get('bytes')
            or not _SHA.fullmatch(str(output.get('sha256', ''))) or _sha(target) != output['sha256']):
        _fail('Spicy original collected output bytes changed', 'output_altered')
    media = _probe(target, record['native'], intent['asset'])
    receipt = _receipt(request, record, output, media)
    if (media != intent.get('media_metadata') or media != record.get('media_metadata')
            or record.get('conditioning_receipt') != receipt):
        _fail('Spicy original collected media/conditioning receipt changed', 'output_altered')
    return {'task_id': record['task_id'], 'native': copy.deepcopy(record['native']), 'quote': copy.deepcopy(record['quote']),
        'output': copy.deepcopy(output), 'receipt': receipt}


# V2 keeps its own source/output proof while reusing the once-only original and
# sticky billing state machine. V1 records and their receipt bytes are unchanged.
_V2_MIMES = {'video/mp4': '.mp4', 'video/webm': '.webm', 'image/png': '.png',
    'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif'}
_V2_IMAGE_MIMES = frozenset(mime for mime in _V2_MIMES if mime.startswith('image/'))
_V2_IMAGE_TRANSPORT_MIMES = _V2_IMAGE_MIMES | {'application/octet-stream'}


def _canonical_sources_v2(root, directory, request, native, *, snapshots):
    from lib.spicy_routes import pointer_get
    sources, assets = native.get('sources'), request.get('input_assets')
    submitted = request['submitted_inputs'].get('input_assets', [])
    if not isinstance(sources, list) or not isinstance(assets, list) or not isinstance(submitted, list) or len(sources) != len(assets) or len(sources) != len(submitted):
        _fail('Spicy v2 original source snapshot layout differs')
    by_pointer = {}
    for item, asset in zip(submitted, assets):
        pointer = item.get('pointer')
        if not isinstance(pointer, str) or pointer in by_pointer:
            _fail('Spicy v2 original source pointers differ')
        by_pointer[pointer] = (item, asset)
    aliases = {'first_frame':'first_frame','last_frame':'last_frame',
        'reference_image':'reference_image_paths','reference_video':'reference_video_paths',
        'reference_audio':'reference_audio_paths','source_video':'reference_video_paths',
        'source_audio':'reference_audio_paths','mask':'reference_image_paths','keyframe':'reference_image_paths',
        'character_reference':'reference_image_paths','environment_reference':'reference_image_paths'}
    for source in sources:
        pair = by_pointer.get(source.get('pointer'))
        if pair is None:
            _fail('Spicy v2 original source pointer differs')
        item, asset = pair
        sha = source.get('source_sha256')
        if (source.get('role') != item.get('role') or asset.get('role') != aliases.get(source.get('role'))
                or source.get('source_path') != asset.get('original_path')
                or sha != asset.get('sha256') or sha != item.get('source_sha256')
                or not _SHA.fullmatch(str(sha)) or source.get('uri') != pointer_get(native['input'], source['pointer'])):
            _fail('Spicy v2 original source role/hash/URI binding differs')
        snapshot = _inside(root, asset.get('path', ''))
        try:
            snapshot.relative_to(directory / 'inputs')
        except ValueError:
            _fail('Spicy v2 source is outside canonical snapshots')
        if item.get('source_path') != str(snapshot):
            _fail('Spicy v2 submitted source differs from canonical snapshot')
        if snapshots and (not snapshot.is_file() or _sha(snapshot) != sha or snapshot.stat().st_size != source.get('bytes')):
            _fail('Spicy v2 original source snapshot bytes changed')


def _capture_assets_v2(path, record, data):
    output = data.get('output')
    assets = output.get('assets', []) if isinstance(output, dict) else []
    if not isinstance(assets, list) or len(assets) > 128:
        _fail('Spicy v2 output asset layout is unsupported', 'malformed_task')
    retained, blockers, seen, missing_mimes = [], [], set(), []
    record['max_observed_assets_v2'] = max(record.get('max_observed_assets_v2', 0), len(assets))
    possible_videos = sum(isinstance(row, dict) and (str(row.get('mime', '')).startswith('video/') or not row.get('key') and row.get('pending') and not str(row.get('mime', '')).startswith('image/')) for row in assets)
    if possible_videos > 1:
        record['requires_explicit_primary_v2'] = True
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            blockers.append('malformed_asset_' + str(index))
            continue
        key = asset.get('key')
        safe = {}
        if isinstance(key, str) and _ASSET_KEY.fullmatch(key) and '..' not in key.split('/') and key not in seen:
            safe['key'] = key
            seen.add(key)
        elif not asset.get('pending'):
            blockers.append('missing_or_duplicate_asset_key_' + str(index))
        for flag in ('pending', 'unavailable'):
            if flag in asset:
                if type(asset[flag]) is not bool:
                    _fail('Spicy original output state is malformed', 'malformed_task')
                safe[flag] = asset[flag]
        mime = asset.get('mime')
        if isinstance(mime, str) and re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', mime):
            safe['mime'] = mime
        if mime is None and not asset.get('pending'):
            missing_mimes.append(key if isinstance(key, str) else str(index))
        elif mime not in _V2_MIMES and not asset.get('pending'):
            blockers.append('unsupported_asset_mime_' + str(index))
        role = asset.get('role')
        if isinstance(role, str) and re.fullmatch(r'[A-Za-z0-9_. -]{1,128}', role):
            safe['role'] = role
        for name in ('width', 'height', 'bytes'):
            if type(asset.get(name)) is int and asset[name] > 0:
                safe[name] = asset[name]
        duration = asset.get('durationSeconds')
        if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
            safe['duration_seconds'] = duration
        retained.append(safe)
    # Empty/pending refreshes never erase observed original asset identity.
    if retained:
        prior = {row.get('key'): row for row in record.get('assets', []) if row.get('key')}
        for row in retained:
            if row.get('key') in prior:
                old = prior[row['key']]
                for name in ('mime', 'role', 'width', 'height', 'bytes', 'duration_seconds'):
                    if name in old and name in row and old[name] != row[name]:
                        blockers.append('asset_metadata_changed')
                    elif name in old and name not in row:
                        row[name] = old[name]
        retained += [row for key, row in prior.items() if key not in seen]
        record['assets'] = retained
    record['missing_output_mimes_v2'] = missing_mimes
    if blockers:
        record['output_metadata_blockers'] = sorted(set(record.get('output_metadata_blockers', []) + blockers))
    atomic_write_json(path, record)


def _probe_v2(path, native, asset, *, reconcile_image_mime=False):
    """Probe bounded original bytes through a pipe; no external media protocols."""
    from lib.spicy_media import sniff, SpicyMediaError
    if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= 512 * 1024 * 1024:
        _fail('Spicy v2 output is not bounded regular media', 'invalid_media')
    raw = path.read_bytes()
    try:
        mime = sniff(raw[:4096])
    except SpicyMediaError:
        _fail('Spicy v2 output signature is unsupported', 'invalid_media')
    reconcile_image = reconcile_image_mime and asset.get('mime') in _V2_IMAGE_MIMES and mime in _V2_IMAGE_MIMES
    if mime not in _V2_MIMES or (mime != asset.get('mime') and not reconcile_image):
        _fail('Spicy v2 output MIME differs from task metadata', 'invalid_media')
    if reconcile_image:
        # Independent decode proof prevents a signature/header or filename from
        # changing the original's media kind. Never convert provider bytes.
        from PIL import Image
        from lib.spicy_media import IMAGE_MAX_BYTES
        try:
            if len(raw) > IMAGE_MAX_BYTES:
                raise ValueError()
            with Image.open(io.BytesIO(raw)) as image:
                decoded_mime = Image.MIME.get(image.format)
                decoded_dimensions = image.size
                if decoded_mime != mime or image.width * image.height > 80_000_000:
                    raise ValueError()
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        except Exception:
            _fail('Spicy v2 image signature and bounded decoding proof differ', 'invalid_media')
    try:
        process = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'pipe',
            '-show_entries', 'format=format_name,duration:stream=codec_type,codec_name,width,height,duration,nb_frames',
            '-of', 'json', 'pipe:0'], input=raw, capture_output=True, timeout=30, check=True)
        probe = json.loads(process.stdout)
        streams = probe['streams']
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        _fail('Spicy v2 output failed local media probing', 'invalid_media')
    videos = [row for row in streams if row.get('codec_type') == 'video']
    if len(videos) != 1 or not videos[0].get('codec_name'):
        _fail('Spicy v2 output requires one meaningful visual stream', 'invalid_media')
    width, height = videos[0].get('width'), videos[0].get('height')
    if type(width) is not int or type(height) is not int or min(width, height) < 16 or max(width, height) > 16384:
        _fail('Spicy v2 output dimensions are invalid', 'invalid_media')
    if reconcile_image and decoded_dimensions != (width, height):
        _fail('Spicy v2 image decoders disagree on original dimensions', 'invalid_media')
    media = {'mime': mime, 'width': width, 'height': height}
    if mime.startswith('video/'):
        try:
            duration = float(probe.get('format', {}).get('duration', videos[0].get('duration')))
        except (ValueError, TypeError):
            _fail('Spicy v2 video duration is unknown', 'invalid_media')
        if not math.isfinite(duration) or duration <= 0:
            _fail('Spicy v2 video duration is invalid', 'invalid_media')
        media.update(duration_seconds=duration, video_codec=videos[0]['codec_name'], video_streams=1,
            audio_streams=sum(row.get('codec_type') == 'audio' for row in streams))
    else:
        media['duration_seconds'] = None
    for name, actual in (('width', width), ('height', height)):
        if name in asset and asset[name] != actual:
            _fail('Spicy v2 dimensions differ from task metadata', 'invalid_media')
    if 'duration_seconds' in asset and media['duration_seconds'] is not None and abs(asset['duration_seconds'] - media['duration_seconds']) > .5:
        _fail('Spicy v2 duration differs from task metadata', 'invalid_media')
    return media


def _asset_target_v2(root, request, record, asset):
    target = _inside(root, request['submitted_inputs']['output_path'])
    name = 'spicy-' + record['attempt_id'] + '-' + digest(asset['key'])[:20] + _V2_MIMES[asset['mime']]
    return target.parent / ('stills' if asset['mime'].startswith('image/') else target.stem + '.outputs') / name



def _same_asset_v2(asset, assets):
    if not isinstance(asset, dict):
        return False
    def identity(row):
        return {key: value for key, value in row.items() if key not in ('pending', 'unavailable')}
    original = identity(asset)
    return any(all(key in row and row[key] == value for key, value in original.items()) for row in assets)


def _image_mime_evidence_v2(asset, transport_mime, actual_mime):
    if (asset.get('mime') not in _V2_IMAGE_MIMES or not isinstance(transport_mime, str)
            or transport_mime not in _V2_IMAGE_TRANSPORT_MIMES
            or actual_mime not in _V2_IMAGE_MIMES):
        _fail('Spicy v2 image MIME reconciliation is outside its bounded media kind', 'invalid_media')
    anomalies = []
    if asset['mime'] != transport_mime:
        anomalies.append('provider_transport_mime_mismatch')
    if asset['mime'] != actual_mime:
        anomalies.append('provider_decoded_mime_mismatch')
    if transport_mime != actual_mime:
        anomalies.append('transport_decoded_mime_mismatch')
    suffix = Path(asset['key']).suffix.lower()
    declared_suffixes = {'.jpg', '.jpeg'} if asset['mime'] == 'image/jpeg' else {_V2_MIMES[asset['mime']]}
    if suffix and suffix not in declared_suffixes:
        anomalies.append('asset_key_suffix_provider_mime_mismatch')
    if suffix and suffix not in ({'.jpg', '.jpeg'} if actual_mime == 'image/jpeg' else {_V2_MIMES[actual_mime]}):
        anomalies.append('asset_key_suffix_mime_mismatch')
    return {'version': '1.0', 'provider_declared_mime': asset['mime'],
        'transport_mime': transport_mime, 'sniffed_mime': actual_mime,
        'decoded_mime': actual_mime, 'anomalies': anomalies}


def _probe_intent_v2(path, native, intent, asset=None):
    asset = asset if asset is not None else intent['asset']
    evidence = intent.get('mime_reconciliation')
    if evidence is None:
        return _probe_v2(path, native, asset)
    media = _probe_v2(path, native, asset, reconcile_image_mime=True)
    if (not isinstance(evidence, dict) or evidence != _image_mime_evidence_v2(
            asset, evidence.get('transport_mime'), media['mime']) or intent.get('mime') != media['mime']):
        _fail('Spicy v2 retained image MIME evidence differs', 'output_altered')
    return media


def _verify_asset_v2(root, request, record, intent, *, must_exist=True):
    if (not isinstance(intent, dict) or intent.get('task_id') != record['task_id']
            or not _same_asset_v2(intent.get('asset'), record.get('assets', []))
            or not _SHA.fullmatch(str(intent.get('sha256', '')))
            or (intent.get('mime_reconciliation') is None and intent.get('mime') != intent.get('asset', {}).get('mime'))
            or type(intent.get('bytes')) is not int or intent['bytes'] <= 0
            or intent.get('path') != str(_asset_target_v2(root, request, record, intent['asset']))
            or not _valid_billing_snapshot(intent.get('billing_at_collection'), record)):
        _fail('Spicy v2 collection intent differs from the original task')
    target = _inside(root, intent.get('path', ''))
    if not target.exists() and not must_exist:
        return None
    if (not target.is_file() or target.is_symlink() or target.stat().st_size != intent.get('bytes')
            or _sha(target) != intent['sha256']):
        _fail('Spicy v2 collected original bytes changed', 'output_altered')
    current_asset = next(row for row in record['assets'] if row.get('key') == intent['asset']['key'])
    if _probe_intent_v2(target, record['native'], intent, current_asset) != intent.get('media_metadata'):
        _fail('Spicy v2 collected original metadata changed', 'output_altered')
    return {'key': intent['asset']['key'], 'path': str(target), 'sha256': intent['sha256'],
        'bytes': intent['bytes'], 'mime': intent['mime'],
        **({'mime_reconciliation': copy.deepcopy(intent['mime_reconciliation'])} if 'mime_reconciliation' in intent else {}),
        **({'role': intent['asset']['role']} if 'role' in intent['asset'] else {}),
        'media_metadata': copy.deepcopy(intent['media_metadata'])}


def _recover_asset_v2(root, request, record, intent):
    target = _inside(root, intent.get('path', ''))
    if not target.exists():
        temporary = _inside(root, intent.get('publication_source', ''))
        if temporary.parent != target.parent or not temporary.name.startswith('.openart-download-'):
            _fail('Spicy v2 publication temporary path differs')
        if not temporary.is_file() or _sha(temporary) != intent.get('sha256'):
            return None
        if _probe_intent_v2(temporary, record['native'], intent) != intent.get('media_metadata'):
            _fail('Spicy v2 publication temporary metadata differs')
        from lib.openart_download import _open_output_parent
        parent_fd, checked = _open_output_parent(target, root)
        try:
            os.link(temporary.name, checked.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            os.fsync(parent_fd)
            os.unlink(temporary.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return _verify_asset_v2(root, request, record, intent)


def _receipt_v2(request, record):
    return {'version': '2.0', 'provider': 'spicy', 'transport': jobs.TRANSPORT_V2,
        'native_tool': 'spicy_video', 'adapter_version': '0.2.0', 'session_id': record['attempt_id'],
        'dispatch_status': 'completed', 'submission_evidence': 'verified_native_call',
        'request_sha256': request['request_sha256'], 'native_request_sha256': record['native']['request_sha256'],
        'schema_sha256': record['native']['schema_sha256'],
        'submitted_arguments': copy.deepcopy(record['create_body']), 'task_id': record['task_id'],
        'model': record['native']['model'], 'mode': record['native']['mode'], 'task_state': 'succeeded',
        'quote_id': record['quote']['quote_id'], 'provider_quote_id': record['quote']['provider_quote_id'],
        **copy.deepcopy(record['completion_billing_v2']),
        'credential_binding': copy.deepcopy(record['credential_binding']), 'account_label': record['account_label'],
        'idempotency_key_sha256': digest(record['idempotency_key']),
        'input_assets': [{key: source[key] for key in ('role','pointer','field','media_kind','mime','source_sha256','uri')}
            for source in record['native']['sources']],
        'output': copy.deepcopy(record['output']), 'outputs': copy.deepcopy(record['outputs_v2']),
        'primary_asset_key': record['primary_asset_key'], 'media_metadata': copy.deepcopy(record['media_metadata']),
        **({'primary_control_verification': copy.deepcopy(record['primary_control_verification_v2'])}
            if 'primary_control_verification_v2' in record else {})}



def _primary_controls_v2(native, media):
    expected = native['input'].get('duration_seconds')
    actual = media.get('duration_seconds')
    if type(expected) in (int, float) and expected > 0 and (type(actual) not in (int, float) or not math.isfinite(actual) or abs(actual - expected) > max(.5, expected * .10)):
        _fail('Selected primary video duration differs from the exact authored native duration', 'primary_timing_mismatch')
    controls = native['input']
    width, height = media.get('width'), media.get('height')
    if type(width) is not int or type(height) is not int or min(width, height) < 16 or max(width, height) > 16384:
        _fail('Selected primary video dimensions are invalid', 'primary_dimensions_mismatch')
    if (type(controls.get('width')) is int and controls['width'] != width
            or type(controls.get('height')) is int and controls['height'] != height):
        _fail('Selected primary video dimensions differ from exact native controls', 'primary_dimensions_mismatch')
    short = {'480p':480, '720p':720, '1080p':1080}.get(controls.get('resolution'))
    # The exact provider schema names this tier but defines no pixel geometry.
    # A guessed short-edge rule cannot certify or reject an adaptive original.
    # This is an explicit unknown, never a claim that the requested tier passed.
    geometry_unknown = (native.get('model') == 'bytedance/seedance-2.0-fast/image-to-video'
        and controls.get('resolution') == '720p' and controls.get('aspect_ratio') == 'adaptive')
    if short and not geometry_unknown and min(width, height) != short:
        _fail('Selected primary video resolution differs from exact native control', 'primary_dimensions_mismatch')
    ratio = controls.get('aspect_ratio')
    if isinstance(ratio, str) and re.fullmatch(r'[1-9][0-9]?:[1-9][0-9]?', ratio):
        numerator, denominator = map(int, ratio.split(':'))
        if abs(width / height - numerator / denominator) > max(.01, 2 / min(width, height)):
            _fail('Selected primary aspect ratio differs from exact native control', 'primary_dimensions_mismatch')
    if type(controls.get('generate_audio')) is bool:
        audio_streams = media.get('audio_streams')
        if type(audio_streams) is not int or (audio_streams > 0) != controls['generate_audio']:
            _fail('Selected primary audio differs from exact native control', 'primary_audio_mismatch')
    if geometry_unknown:
        return {'resolution': {'status': 'unverified', 'requested': controls['resolution'],
            'width': width, 'height': height, 'model': native['model'],
            'reason': 'provider_geometry_contract_unavailable'}}
    return None



_primary_timing_v2 = _primary_controls_v2

def _finish_v2(root, path, request, record):
    assets = record.get('assets', [])
    if record.get('output_metadata_blockers') or not assets:
        return False
    intents = record.get('collection_intents_v2', {})
    outputs = []
    for asset in assets:
        if asset.get('key') not in intents:
            return False
        if intents[asset['key']].get('asset', {}).get('key') != asset['key']:
            _fail('Spicy v2 collection intent asset key differs')
        outputs.append(_verify_asset_v2(root, request, record, intents[asset['key']]))
    videos = [row for row in outputs if row['mime'].startswith('video/')]
    if not videos:
        return False
    selection = record.get('primary_selection_v2')
    if len(outputs) < record.get('max_observed_assets_v2', len(assets)):
        return False
    if len(videos) == 1 and not record.get('requires_explicit_primary_v2'):
        chosen = videos[0]
    elif not isinstance(selection, dict):
        record['outputs_v2'] = outputs
        atomic_write_json(path, record)
        return False
    else:
        expected = {'asset_key': selection.get('asset_key'), 'task_id': record['task_id'],
            'request_sha256': request['request_sha256'], 'original_binding_sha256': record['binding_sha256']}
        if selection != expected:
            _fail('Spicy v2 explicit primary selection binding differs')
        chosen = next((row for row in videos if row['key'] == selection['asset_key']), None)
        if chosen is None:
            _fail('Spicy v2 primary selection is not a retained video')
    control_verification = _primary_controls_v2(record['native'], chosen['media_metadata'])
    target = _inside(root, request['submitted_inputs']['output_path'])
    if target.suffix.lower() != _V2_MIMES[chosen['mime']]:
        _fail('Canonical primary extension differs from selected video MIME', 'output_format_mismatch')
    if str(target) != chosen['path']:
        if target.exists():
            if not record.get('primary_publication_v2') or record['primary_publication_v2'] != {'path': str(target), 'sha256': chosen['sha256'], 'asset_key': chosen['key']} or _sha(target) != chosen['sha256']:
                _fail('Spicy v2 primary destination exists without exact publication proof', 'unreceipted_output')
        else:
            record['primary_publication_v2'] = {'path': str(target), 'sha256': chosen['sha256'], 'asset_key': chosen['key']}
            atomic_write_json(path, record)
            # Same filesystem link retains the immutable collected original; never overwrite.
            from lib.openart_download import _open_output_parent
            target.parent.mkdir(parents=True, exist_ok=True)
            parent_fd, checked = _open_output_parent(target, root)
            try:
                os.link(chosen['path'], checked.name, dst_dir_fd=parent_fd, follow_symlinks=False)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    record.update(output={'path': str(target), 'sha256': chosen['sha256']}, outputs_v2=outputs,
        primary_asset_key=chosen['key'], media_metadata=copy.deepcopy(chosen['media_metadata']))
    if control_verification is not None:
        record['primary_control_verification_v2'] = control_verification
    record.setdefault('completion_billing_v2', _billing_snapshot(record))
    record['conditioning_receipt'] = _receipt_v2(request, record)
    record['collected_at'] = jobs._stamp(jobs._now())
    atomic_write_json(path, record)
    return True


def _recover_publication_v2(root, path, request, record):
    if record.get('output'):
        return True
    for intent in record.get('collection_intents_v2', {}).values():
        _recover_asset_v2(root, request, record, intent)
    return _finish_v2(root, path, request, record)


def _collect_v2(root, path, request, record, client):
    from lib.openart_download import collect_output
    assets = record.get('assets', [])
    for asset in assets:
        if asset.get('pending') or asset.get('unavailable') or asset.get('mime') not in _V2_MIMES or not asset.get('key'):
            continue
        key = asset['key']
        target = _asset_target_v2(root, request, record, asset)
        saved = record.get('collection_intents_v2', {}).get(key)
        if saved is not None and _recover_asset_v2(root, request, record, saved) is not None:
            continue
        if target.exists():
            _fail('Spicy v2 output exists without exact collection proof', 'unreceipted_output')
        target.parent.mkdir(parents=True, exist_ok=True)
        _inside(root, target)
        publication_error = [None]
        try:
            response = client.create_download_url(record['task_id'], key)
            data = response.get('data')
            expires = parse_utc(data.get('expiresAt')) if isinstance(data, dict) else None
            if expires is None or expires <= jobs._now() or not isinstance(data.get('url'), str) or data.get('key') not in (None, key):
                return _pending(record, 'pending', 'download_url_unavailable')
            def before_publish(metadata):
                try:
                    retain_publication(metadata)
                except jobs.SpicyError as exc:
                    publication_error[0] = exc
                    raise
            def retain_publication(metadata):
                temporary = _inside(root, metadata['publication_source'])
                # The default downloader and all video collection remain strict.
                # Only ancillary images opt into a finite MIME reconciliation.
                image_evidence = None
                if asset['mime'] in _V2_IMAGE_MIMES and 'mime' in metadata:
                    media = _probe_v2(temporary, record['native'], asset, reconcile_image_mime=True)
                    image_evidence = _image_mime_evidence_v2(asset, metadata['mime'], media['mime'])
                else:
                    media = _probe_v2(temporary, record['native'], asset)
                if 'bytes' in asset and metadata['size'] != asset['bytes']:
                    _fail('Spicy v2 output byte count differs from task metadata', 'invalid_media')
                intent = {'task_id': record['task_id'], 'asset': copy.deepcopy(asset), 'path': str(target),
                    'sha256': metadata['sha256'], 'bytes': metadata['size'], 'mime': media['mime'],
                    'publication_source': str(temporary), 'media_metadata': media,
                    'billing_at_collection': _billing_snapshot(record)}
                if image_evidence is not None:
                    intent['mime_reconciliation'] = image_evidence
                record.setdefault('collection_intents_v2', {})[key] = intent
                atomic_write_json(path, record)
            collect_output(data['url'], target, allowed_hosts=tuple(record['qualified_storage_hosts']), output_root=root,
                max_bytes=(10 if asset['mime'] in _V2_IMAGE_MIMES else 512) * 1024 * 1024,
                max_redirects=0, expected_mime=asset['mime'], before_publish=before_publish,
                **({'accepted_mimes': _V2_IMAGE_TRANSPORT_MIMES} if asset['mime'] in _V2_IMAGE_MIMES else {}))
            _verify_asset_v2(root, request, record, record['collection_intents_v2'][key])
        except jobs.SpicyError:
            raise
        except Exception:
            if publication_error[0] is not None:
                raise publication_error[0]
            return _pending(record, 'pending', 'collection_unavailable')
    if _finish_v2(root, path, request, record):
        return _completed_v2(record, _validate_collected_v2(root, request, record))
    if record.get('output_metadata_blockers'):
        return _pending(record, 'blocked_output', 'unsupported_output_assets')
    if record.get('missing_output_mimes_v2'):
        return _pending(record, 'output_metadata_incomplete', 'asset_mime_missing')
    videos = [row for row in assets if str(row.get('mime', '')).startswith('video/')]
    if len(videos) > 1 and all(row.get('key') in record.get('collection_intents_v2', {}) for row in videos):
        result = _pending(record, 'collected_needs_primary', 'explicit_primary_selection_required')
        result.data['outputs'] = copy.deepcopy(record.get('outputs_v2', []))
        return result
    return _pending(record, 'output_unavailable' if any(row.get('unavailable') for row in assets) else 'pending', 'output_not_ready')


def _validate_collected_v2(root, request, record):
    if record.get('acceptance_state') != 'accepted' or record.get('task_state') != 'succeeded' or not record.get('output'):
        _fail('Spicy v2 original has no completed same-task collection proof')
    intents = record.get('collection_intents_v2', {})
    if any(key != intent.get('asset', {}).get('key') for key, intent in intents.items()):
        _fail('Spicy v2 collection intent asset key differs')
    outputs = [_verify_asset_v2(root, request, record, intent) for intent in intents.values()]
    if sorted(outputs, key=lambda row: row['key']) != sorted(record.get('outputs_v2', []), key=lambda row: row['key']):
        _fail('Spicy v2 retained outputs changed', 'output_altered')
    assets = record.get('assets', [])
    if record.get('output_metadata_blockers') or len(outputs) != len(assets):
        _fail('Spicy v2 output set is incomplete or unsupported')
    primary = next((row for row in outputs if row['key'] == record.get('primary_asset_key') and row['mime'].startswith('video/')), None)
    target = _inside(root, request['submitted_inputs']['output_path'])
    if (primary is None or record['output'] != {'path': str(target), 'sha256': primary['sha256']}
            or not target.is_file() or _sha(target) != primary['sha256'] or record.get('media_metadata') != primary['media_metadata']):
        _fail('Spicy v2 primary original proof changed', 'output_altered')
    control_verification = _primary_controls_v2(record['native'], primary['media_metadata'])
    if ('primary_control_verification_v2' in record
            and control_verification != record['primary_control_verification_v2']):
        _fail('Spicy v2 primary control verification proof changed', 'output_altered')
    videos = [row for row in outputs if row['mime'].startswith('video/')]
    if len(videos) > 1 or record.get('requires_explicit_primary_v2'):
        selection = {'asset_key': primary['key'], 'task_id': record['task_id'],
            'request_sha256': request['request_sha256'], 'original_binding_sha256': record['binding_sha256']}
        if record.get('primary_selection_v2') != selection:
            _fail('Spicy v2 multi-video primary selection proof is missing')
    if not _valid_billing_snapshot(record.get('completion_billing_v2'), record):
        _fail('Spicy v2 completion billing proof changed')
    receipt = _receipt_v2(request, record)
    if record.get('conditioning_receipt') != receipt:
        _fail('Spicy v2 conditioning receipt changed', 'output_altered')
    return {'task_id': record['task_id'], 'native': copy.deepcopy(record['native']), 'quote': copy.deepcopy(record['quote']),
        'output': copy.deepcopy(record['output']), 'outputs': copy.deepcopy(outputs), 'receipt': receipt,
        **({'primary_control_verification': copy.deepcopy(control_verification),
            'primary_control_verification_provenance': {
                'scope': 'retained_completion' if 'primary_control_verification_v2' in record else 'derived_current_read',
                'source': 'collection_time_control_proof' if 'primary_control_verification_v2' in record else 'frozen_native_request_and_verified_local_media'}}
            if control_verification is not None else {})}


def _completed_v2(record, collected):
    return ToolResult(success=True, data={'provider': 'spicy', 'session_id': record['attempt_id'],
        'dispatch_session_id': record['attempt_id'], 'dispatch_status': 'completed', 'task_id': record['task_id'],
        'conditioning_receipt': copy.deepcopy(collected['receipt']), 'outputs': copy.deepcopy(collected['outputs']),
        'settled': record.get('settled', False), 'cost_usd': record.get('final_cost_usd'),
        **({'primary_control_verification': copy.deepcopy(collected['primary_control_verification']),
            'primary_control_verification_provenance': copy.deepcopy(collected['primary_control_verification_provenance']),
            'warnings': ['Requested resolution tier has no verified provider pixel-geometry contract; original is retained with unverified resolution.']}
            if collected.get('primary_control_verification') is not None else {}),
        'retry_attempted': False, 'fallback_attempted': False, **copy.deepcopy(record['media_metadata'])},
        artifacts=list(dict.fromkeys([collected['output']['path']] + [row['path'] for row in collected['outputs']])),
        cost_usd=float(record['final_cost_usd']) if record.get('final_cost_usd') is not None else None,
        model=record['native']['model'], duration_seconds=record['media_metadata']['duration_seconds'])


def select_primary_output(project_dir, attempt_id, asset_key):
    """Retain an explicit exact video selection after collecting all originals."""
    root, directory, path = _paths(project_dir, attempt_id)
    with _lock(directory):
        request, record = _bound_journal(root, directory, path, attempt_id)
        if record['native'].get('transport') != jobs.TRANSPORT_V2 or record.get('output'):
            _fail('Primary selection requires an unselected v2 original')
        if not any(row.get('key') == asset_key and str(row.get('mime', '')).startswith('video/') for row in record.get('assets', [])):
            _fail('Primary selection must name an observed original video asset key')
        intent = record.get('collection_intents_v2', {}).get(asset_key)
        if intent is None:
            _fail('Primary selection awaits collection of the exact original video')
        selected = _verify_asset_v2(root, request, record, intent)
        _primary_controls_v2(record['native'], selected['media_metadata'])
        selection = {'asset_key': asset_key, 'task_id': record['task_id'],
            'request_sha256': request['request_sha256'], 'original_binding_sha256': record['binding_sha256']}
        if record.get('primary_selection_v2') not in (None, selection):
            _fail('Primary original selection was already retained')
        record['primary_selection_v2'] = selection
        atomic_write_json(path, record)
        if _recover_publication_v2(root, path, request, record):
            return _validate_collected_v2(root, request, record)
        return {'task_id': record['task_id'], 'primary_asset_key': asset_key, 'status': 'awaiting_original_collection'}


def original_resolver(owner_root, scope_kind, relative):
    """Canonical owner proof for a separately confirmed future destructive action."""
    if scope_kind != 'openmontage-project' or not isinstance(relative, str) or not re.fullmatch(r'production_attempts/[A-Za-z0-9_.:-]+/spicy_job\.json', relative):
        _fail('Canonical OpenMontage original record is required')
    attempt_id = Path(relative).parent.name
    root, directory, path = _paths(owner_root, attempt_id)
    with _lock(directory):
        request, record = _bound_journal(root, directory, path, attempt_id)
        if record.get('acceptance_state') != 'accepted' or record.get('task_state') not in TERMINAL_FAILURES | {'succeeded'} or not record.get('task_id'):
            _fail('Canonical terminal original proof is required')
        return {'taskId': record['task_id'], 'model': record['native']['model'], 'state': record['task_state'],
            'credential_binding': record['credential_binding']['credential_digest'], 'record_sha256': _sha(path)}

def retry_basis(project_dir, attempt_id):
    """Prepare lineage only; no /jobs/retry, source transfer, quote or create."""
    root, directory, path = _paths(project_dir, attempt_id)
    request, record = _bound_journal(root, directory, path, attempt_id)
    if record.get('task_state') not in {'failed', 'expired'} or record.get('settled') is not True or record.get('final_cost_usd') is None or record.get('billing_anomaly'):
        _fail('New retry intent requires a failed/expired settled known original', 'retry_basis_unproven')
    return {'kind': 'prepare_new_quoted_counted_original', 'parent_task_id': record['task_id'],
        'parent_attempt_id': attempt_id, 'parent_request_sha256': request['request_sha256'],
        'parent_record_sha256': _sha(path), 'model': record['native']['model'],
        'native_request_sha256': record['native']['request_sha256'], 'credential_binding': copy.deepcopy(record['credential_binding']),
        'authority': 'none', 'requires': ['new explicit remaining authority', 'fresh exact quote', 'new canonical counted attempt']}


def collected_outputs(project_dir, attempt_id):
    """Credential-free current-byte proof for every retained v2 original asset.

    Partial multi-output journals are valid archives; this does not select a
    primary video, certify the cut or grant new generation authority.
    """
    root, directory, path = _paths(project_dir, attempt_id)
    request, record = _bound_journal(root, directory, path, attempt_id)
    if record['native'].get('transport') != jobs.TRANSPORT_V2 or record.get('acceptance_state') != 'accepted' or record.get('task_state') != 'succeeded':
        _fail('Retained output enumeration requires a succeeded v2 original')
    intents = record.get('collection_intents_v2', {})
    if any(key != intent.get('asset', {}).get('key') for key, intent in intents.items()):
        _fail('Spicy v2 collection intent asset key differs')
    outputs = [_verify_asset_v2(root, request, record, intent) for intent in intents.values()]
    unresolved = max(0, record.get('max_observed_assets_v2', len(record.get('assets', []))) - len(outputs))
    videos = [row for row in outputs if row['mime'].startswith('video/')]
    ready_selection = (not record.get('output') and not record.get('primary_selection_v2') and unresolved == 0
        and not record.get('output_metadata_blockers') and not record.get('missing_output_mimes_v2')
        and (len(videos) > 1 or record.get('requires_explicit_primary_v2') and videos))
    return {'task_id': record['task_id'], 'attempt_id': attempt_id,
        'request_sha256': request['request_sha256'], 'native_request_sha256': record['native']['request_sha256'],
        'schema_sha256': record['native']['schema_sha256'], 'outputs': outputs,
        'status': 'collected' if record.get('output') else 'partial_or_needs_primary',
        'requires_primary_selection': bool(ready_selection), 'unresolved_asset_count': unresolved,
        'review_status': 'unreviewed', 'authority': 'none'}
