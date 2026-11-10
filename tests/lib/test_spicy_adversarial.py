"""Independent offline Spicy regressions; no credentials or provider requests."""
import copy

from lib import spicy_catalog as catalog


def native_schema():
    route = catalog.VIDEO_ROUTES[catalog.VIDEO_MODEL]
    return {'type': 'object', 'properties': copy.deepcopy(route['properties']),
            'required': list(route['required']), 'additionalProperties': False}


def test_authenticated_narrower_numeric_limits_not_qualified():
    schema = native_schema()
    schema['properties']['duration_seconds']['maximum'] = 10
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


def test_authenticated_narrower_text_limits_not_qualified():
    schema = native_schema()
    schema['properties']['prompt']['maxLength'] = 10
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


def test_authenticated_extra_required_native_control_not_qualified():
    schema = native_schema()
    schema['required'].append('last_image_url')
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


def test_account_observation_rejects_stale_future_and_invalid_time(tmp_path):
    from datetime import datetime, timedelta, timezone
    import json
    schema = native_schema()
    now = datetime.now(timezone.utc)
    base = {'version': catalog.OBSERVATION_VERSION, 'model': catalog.VIDEO_MODEL,
            'authenticated': True, 'enabled': True, 'available': True,
            'credential_binding': {'credential_digest': 'a' * 64},
            'input_schema': schema, 'schema_sha256': catalog.digest(schema)}
    path = catalog.observation_path(tmp_path, catalog.VIDEO_MODEL)
    path.parent.mkdir()
    for observed_at in ((now - timedelta(days=1)).isoformat(),
                        (now + timedelta(hours=1)).isoformat(), 'bad-timestamp'):
        path.write_text(json.dumps(dict(base, observed_at=observed_at)))
        assert catalog.load_account_observation(tmp_path, catalog.VIDEO_MODEL) is None
    path.write_text(json.dumps(dict(base, observed_at=now.isoformat())))
    assert catalog.load_account_observation(tmp_path, catalog.VIDEO_MODEL) is not None


import pytest


@pytest.mark.parametrize('constraint', [
    {'type': 'array'}, {'maxProperties': 1}, {'minProperties': 100},
    {'propertyNames': {'enum': ['not-a-native-field']}}, {'enum': [{}]},
])
def test_authenticated_root_constraints_cannot_overclaim_envelope(constraint):
    schema = native_schema()
    schema.update(constraint)
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize('field, constraint', [
    ('generate_audio', {'enum': [True]}), ('prompt', {'enum': ['fixed']}),
    ('image_url', {'maxLength': 1}), ('aspect_ratio', {'minLength': 100}),
])
def test_authenticated_property_constraints_cannot_overclaim_envelope(field, constraint):
    schema = native_schema()
    schema['properties'][field].update(constraint)
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


def test_authenticated_wider_constraints_and_ui_annotations_are_allowed():
    schema = native_schema()
    schema.update(minProperties=1, maxProperties=100, unevaluatedProperties=False,
                  title='Video controls', description='Fixture annotation',
                  **{'x-ui': {'layout': 'native'}})
    schema['properties']['prompt'].update(minLength=0, maxLength=5000,
                                        **{'x-ui': {'widget': 'textarea'}})
    schema['properties']['duration_seconds'].update(minimum=-5, maximum=10 ** 400)
    schema['properties']['generate_audio']['enum'] = [False, True]
    assert catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


@pytest.mark.parametrize('constraint', [{'patternProperties': {'^prompt$': False}},
                                        {'allOf': [False]}, {'not': {}},
                                        {'dependentRequired': {'prompt': ['last_image_url']}}])
def test_unhandled_root_assertions_fail_closed(constraint):
    schema = native_schema()
    schema.update(constraint)
    assert not catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


def test_atomic_journal_directory_sync_error_propagates(tmp_path, monkeypatch):
    import errno
    import os
    import stat
    original_fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'fixture directory durability failure')
        return original_fsync(fd)
    monkeypatch.setattr(catalog.os, 'fsync', fail_directory)
    with pytest.raises(OSError) as error:
        catalog.atomic_write_json(tmp_path / 'request.json', {'fixture': 'pre-send journal'})
    assert error.value.errno == errno.EIO


def test_directory_open_error_propagates(tmp_path, monkeypatch):
    import errno
    def fail_open(*args, **kwargs):
        raise OSError(errno.EIO, 'fixture directory open failure')
    monkeypatch.setattr(catalog.os, 'open', fail_open)
    with pytest.raises(OSError) as error:
        catalog._fsync_dir(tmp_path)
    assert error.value.errno == errno.EIO


@pytest.mark.parametrize('value', [4.5, 5.0, '5', True])
def test_native_fractional_or_coerced_durations_refused(value):
    from lib import spicy_jobs
    with pytest.raises(spicy_jobs.SpicyError) as error:
        spicy_jobs.build_native_request({'model': catalog.VIDEO_MODEL, 'prompt': 'fixture', 'duration': value,
                                         'resolution': '720p', 'generate_audio': False})
    assert error.value.kind == 'invalid_duration'


def test_client_credential_rotation_cannot_send_a_different_key(monkeypatch):
    from lib import spicy_jobs
    key_env = 'SPICY_ADVERSARIAL_OFFLINE_FIXTURE'
    monkeypatch.setenv(key_env, 'synthetic-original-offline-value')
    calls = []
    def mocked(method, url, headers, body, timeout=None):
        calls.append(headers)
        return 200, {}, b'{"code":200,"msg":"success","data":{"items":[],"total":0}}'
    client = spicy_jobs.SpicyClient('env:' + key_env, transport=mocked)
    monkeypatch.setenv(key_env, 'synthetic-rotated-offline-value')
    try:
        client.list_models()
    except spicy_jobs.SpicyError:
        assert not calls
    else:
        assert calls[0]['Authorization'] == 'Bearer synthetic-original-offline-value'


def test_provider_error_cannot_echo_selected_secret(monkeypatch):
    import json
    from lib import spicy_jobs
    marker = 'synthetic-private-offline-fixture-value'
    monkeypatch.setenv('SPICY_ADVERSARIAL_OFFLINE_FIXTURE', marker)
    def mocked(*args):
        return 400, {}, json.dumps({'code': 40001, 'msg': 'rejected ' + marker}).encode()
    client = spicy_jobs.SpicyClient('env:SPICY_ADVERSARIAL_OFFLINE_FIXTURE', transport=mocked)
    with pytest.raises(spicy_jobs.SpicyError) as error:
        client.create_task(model=catalog.VIDEO_MODEL, input_data={'prompt': 'fixture'},
                           idempotency_key='fixture-key', quote_id='fixture-quote', expected_cost='0.10')
    assert marker not in str(error.value)
    assert marker not in json.dumps(error.value.as_dict())


@pytest.mark.parametrize('url', ['http://localhost/private', 'https://127.0.0.1/private',
                                 'https://unqualified.invalid/private'])
def test_storage_put_refuses_unsafe_target_before_transport(monkeypatch, url):
    from lib import spicy_jobs
    monkeypatch.setenv('SPICY_ADVERSARIAL_OFFLINE_FIXTURE', 'synthetic-offline-only')
    calls = []
    def mocked(*args):
        calls.append(args)
        return 200, {}, b''
    client = spicy_jobs.SpicyClient('env:SPICY_ADVERSARIAL_OFFLINE_FIXTURE', transport=mocked,
                                    qualified_storage_hosts=['fixture.r2.cloudflarestorage.com'])
    with pytest.raises(spicy_jobs.SpicyError):
        client.put_presigned(url, b'fixture source bytes')
    assert not calls


@pytest.mark.parametrize('header', ['Host', 'Cookie', 'Proxy-Authorization'])
def test_storage_put_refuses_foreign_auth_headers(monkeypatch, header):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    from lib import spicy_jobs
    monkeypatch.setenv('SPICY_ADVERSARIAL_OFFLINE_FIXTURE', 'synthetic-offline-only')
    calls = []
    def mocked(*args):
        calls.append(args)
        return 200, {}, b''
    client = spicy_jobs.SpicyClient('env:SPICY_ADVERSARIAL_OFFLINE_FIXTURE', transport=mocked,
                                    qualified_storage_hosts=['fixture.r2.cloudflarestorage.com'])
    with pytest.raises(spicy_jobs.SpicyError):
        client.put_presigned('https://fixture.r2.cloudflarestorage.com/upload', b'fixture',
                             {header: 'foreign-fixture-credential'})
    assert not calls


def test_authenticated_four_second_minimum_covers_callable_duration_envelope():
    schema = native_schema()
    schema['properties']['duration_seconds'].update(minimum=4, maximum=15)
    assert catalog.schema_compatible(catalog.VIDEO_MODEL, schema)


def test_vendor_auto_duration_is_not_callable_application_control():
    with pytest.raises(catalog.SpicyCatalogError):
        catalog.validate_native_input(catalog.VIDEO_MODEL,
                                      {'prompt': 'fixture', 'image_url': 'spicy://fixture',
                                       'duration_seconds': -1})


def test_source_upload_offline_guard_precedes_files_authority_and_transport(tmp_path):
    from lib import spicy_jobs, spicy_uploads
    from tools.base_tool import offline_preparation
    with offline_preparation(), pytest.raises(spicy_jobs.SpicyError) as error:
        spicy_uploads.upload_sources(tmp_path, {}, client=object())
    assert error.value.kind == 'offline'
    assert list(tmp_path.iterdir()) == []


def test_source_upload_symlink_cannot_cross_project_boundary(tmp_path):
    from lib import spicy_jobs, spicy_uploads
    project = tmp_path / 'project'
    project.mkdir()
    outside = tmp_path / 'other-project.png'
    outside.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    (project / 'linked.png').symlink_to(outside)
    with pytest.raises(spicy_jobs.SpicyError) as error:
        spicy_uploads.upload_sources(project, {'project_dir': str(project),
                                               'image_path': str(project / 'linked.png')}, client=object())
    assert error.value.kind == 'source_outside_project'
    assert list(project.iterdir()) == [project / 'linked.png']


def test_source_upload_declared_hash_drift_refused_before_authority(tmp_path):
    from lib import spicy_jobs, spicy_uploads
    source = tmp_path / 'fixture.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    with pytest.raises(spicy_jobs.SpicyError) as error:
        spicy_uploads.upload_sources(tmp_path, {'project_dir': str(tmp_path), 'input_assets': [
            {'role': 'first_frame', 'source_path': str(source), 'source_sha256': 'a' * 64}]}, client=object())
    assert error.value.kind == 'source_hash_mismatch'
    assert list(tmp_path.iterdir()) == [source]


def test_native_builder_rejects_current_upload_uri_that_differs_from_original_commit(tmp_path, monkeypatch):
    import hashlib
    import json
    from datetime import datetime, timedelta, timezone
    from lib import spicy_jobs, spicy_uploads
    monkeypatch.setenv('SPICY_ADVERSARIAL_OFFLINE_FIXTURE', 'synthetic-offline-only')
    binding = spicy_jobs.credential_binding('env:SPICY_ADVERSARIAL_OFFLINE_FIXTURE')
    source = tmp_path / 'fixture.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    receipt = {'source_sha256': source_hash, 'credential_digest': binding['credential_digest'],
               'uri': 'spicy://f/original', 'expires_at': expiry, 'bytes': source.stat().st_size,
               'content_type': 'image/png', 'file_id': 'original', 'role': 'first_frame', 'batch_id': 'fixture'}
    journal = spicy_uploads._journal_path(tmp_path, source_hash, binding['credential_digest'])
    journal.parent.mkdir()
    journal.write_text(json.dumps({'state': 'committed', 'receipt': receipt, 'binding': {
        'source_sha256': source_hash, 'credential_digest': binding['credential_digest'],
        'role': 'first_frame', 'path': str(source), 'bytes': source.stat().st_size,
        'content_type': 'image/png', 'credential_ref': binding['credential_ref'],
        'account_label': 'fixture-account', 'qualified_storage_hosts': ['fixture.r2.cloudflarestorage.com']}}))
    current = spicy_jobs.upload_receipt_path(tmp_path, source_hash)
    current.write_text(json.dumps(dict(receipt, uri='spicy://f/tampered-unapproved-input')))
    with pytest.raises(spicy_jobs.SpicyError) as error:
        spicy_jobs.build_native_request({'model': catalog.VIDEO_MODEL, 'project_dir': str(tmp_path),
            'image_path': str(source), 'credential_ref': binding['credential_ref'], 'prompt': 'fixture',
            'duration': 5, 'resolution': '720p', 'generate_audio': False})
    assert error.value.kind == 'upload_receipt_invalid'


def test_first_last_operation_descriptor_requires_end_pin_only_for_that_operation():
    pins = catalog.describe_video_inputs(catalog.VIDEO_MODEL, operation='first_last_frame')
    assert pins['roles']['end_frame']['required'] is True
    assert pins['roles']['end_frame']['minimum'] == 1
    assert 'last_image_url' in pins['root_schema']['required']
    ordinary = catalog.describe_video_inputs(catalog.VIDEO_MODEL, operation='image_to_video')
    assert ordinary['roles']['end_frame']['required'] is False
    assert ordinary['roles']['end_frame']['minimum'] == 0
    assert 'last_image_url' not in ordinary['root_schema']['required']


def _minimal_original_record():
    return {'attempt_id': 'offline-fixture', 'task_id': None,
            'native': {'model': catalog.VIDEO_MODEL}, 'quote': {'max_charge_usd': '0.10'},
            'credential_binding': {'credential_digest': 'a' * 64}}


def test_lifecycle_billing_incident_survives_later_malformed_asset(tmp_path):
    import json
    from lib import spicy_jobs, spicy_lifecycle
    path = tmp_path / 'original.json'
    record = _minimal_original_record()
    with pytest.raises(spicy_jobs.SpicyError):
        spicy_lifecycle._capture_task(path, record, {'taskId': 'original-task', 'state': 'succeeded',
            'settled': True, 'cost': '0.20', 'output': {'assets': [{'key': 'safe/file.mp4', 'pending': 'bad-bool'}]}})
    saved = json.loads(path.read_text())
    assert saved['task_id'] == 'original-task'
    assert saved.get('billing_anomaly', {}).get('kind') == 'billing_anomaly'


def test_lifecycle_signed_url_in_asset_key_is_never_persisted(tmp_path):
    from lib import spicy_jobs, spicy_lifecycle
    path = tmp_path / 'original.json'
    record = _minimal_original_record()
    try:
        spicy_lifecycle._capture_task(path, record, {'taskId': 'original-task', 'state': 'succeeded',
            'output': {'assets': [{'key': 'https://fixture.invalid/result?signature=PRIVATE_SIGNED_VALUE'}]}})
    except spicy_jobs.SpicyError:
        pass
    assert 'PRIVATE_SIGNED_VALUE' not in path.read_text()


def test_lifecycle_replay_keeps_exact_body_key_after_quote_expiry_and_stops_at_deadline(tmp_path, monkeypatch):
    import copy
    from datetime import datetime, timedelta, timezone
    from lib import spicy_jobs, spicy_lifecycle
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    now = [start]
    monkeypatch.setattr(spicy_jobs, '_now', lambda: now[0])
    record = _minimal_original_record()
    record.update(started_at=start.isoformat(), replay_deadline=(start + timedelta(hours=23)).isoformat(),
                  acceptance_state='prepared', idempotency_key='fixed-original-key',
                  create_body={'model': catalog.VIDEO_MODEL, 'input': {'prompt': 'fixture'},
                               'quoteId': 'expired-later-quote', 'expectedCost': '0.10'})
    path = tmp_path / 'original.json'
    calls = []
    class MockClient:
        def create_task(self, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            raise spicy_jobs.SpicyTransportError('fixture lost response')
    client = MockClient()
    first = spicy_lifecycle._create(path, record, client)
    assert first.data['acceptance_state'] == 'acceptance_unknown'
    now[0] = start + timedelta(minutes=10)
    spicy_lifecycle._create(path, record, client)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0] == {'model': catalog.VIDEO_MODEL, 'input_data': {'prompt': 'fixture'},
                        'idempotency_key': 'fixed-original-key', 'quote_id': 'expired-later-quote',
                        'expected_cost': '0.10'}
    now[0] = start + timedelta(hours=23)
    closed = spicy_lifecycle._create(path, record, client)
    assert closed.data['error_category'] == 'replay_window_closed'
    assert len(calls) == 2
