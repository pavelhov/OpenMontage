"""Offline factory contracts; no keys, authenticated calls or paid operations."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib import production_autonomy as autonomy, production_execution as execution, production_request as preparation

MODEL = 'bytedance/seedance-2.0-fast/image-to-video'
H = 'a' * 64


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    import socket
    import ssl  # Load socket-derived SSL classes before replacing socket.socket.
    def blocked(*args, **kwargs):
        pytest.fail('Spicy factory fixtures must not use real network transport')
    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'getaddrinfo', blocked)


def spicy_spec(**changes):
    value = {'id': 'spicy', 'billing': 'quoted_usd_budget', 'account_label': 'offline-fixture',
             'credential_ref': 'env:SPICY_OFFLINE_FIXTURE_KEY', 'credential_digest': H,
             'qualified_storage_hosts': ['fixture-upload.r2.cloudflarestorage.com', 'fixture-account.r2.cloudflarestorage.com'],
             'routes': [{'model': MODEL, 'mode': 'image_to_video'}],
             'total_cap_usd': '0.30', 'per_attempt_cap_usd': '0.20'}
    value.update(changes)
    return value


@pytest.fixture
def authority(tmp_path, monkeypatch):
    from lib import spicy_jobs, spicy_catalog
    native = {'provider': 'spicy', 'model': MODEL, 'mode': 'image_to_video', 'request_sha256': H}
    inputs = {'governance': {'shot_id': 'shot'}, 'spicy_quote_id': 'quote-fixture',
              'account_label': 'offline-fixture', 'credential_ref': 'env:SPICY_OFFLINE_FIXTURE_KEY'}
    now = datetime.now(timezone.utc)
    quote = {'quote_id': 'quote-fixture', 'provider_quote_id': 'provider-fixture',
             'native_request_sha256': H, 'credential_digest': H, 'account_label': 'offline-fixture',
             'model': MODEL, 'currency': 'USD', 'estimated_cost_usd': '0.10', 'max_charge_usd': '0.15',
             'obtained_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=5)).isoformat()}
    policy = {'providers': [spicy_spec()]}
    monkeypatch.setattr(spicy_jobs, 'credential_binding', lambda *_: {'credential_digest': H})
    monkeypatch.setattr(spicy_jobs, 'load_quote_artifact', lambda *_: copy.deepcopy(quote))
    monkeypatch.setattr(spicy_jobs, 'billing_anomalies', lambda *args, **kwargs: [], raising=False)
    monkeypatch.setattr(spicy_catalog, 'load_account_observation', lambda *_: {'credential_binding': {'credential_digest': H}})
    return tmp_path, inputs, native, quote, policy


def test_exact_decimal_quote_reservation(authority):
    root, inputs, native, quote, policy = authority
    result = autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)
    assert result['remaining_total_usd'] == '0.30'
    assert result['quote'] == quote
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('damage', ['cap', 'credential', 'request', 'account', 'stale', 'future', 'nan', 'negative', 'negative_estimate', 'currency', 'missing_quote'])
def test_quote_authority_failures_leave_project_untouched(authority, damage):
    root, inputs, native, quote, policy = authority
    if damage == 'cap': quote['max_charge_usd'] = '0.21'
    elif damage == 'credential': policy['providers'][0]['credential_digest'] = 'b' * 64
    elif damage == 'request': quote['native_request_sha256'] = 'b' * 64
    elif damage == 'account': inputs['account_label'] = 'another-account'
    elif damage == 'stale': quote['obtained_at'] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    elif damage == 'future': quote['obtained_at'] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    elif damage == 'nan': quote['max_charge_usd'] = 'NaN'
    elif damage == 'negative': quote['max_charge_usd'] = '-0.1'
    elif damage == 'negative_estimate': quote['estimated_cost_usd'] = '-0.1'
    elif damage == 'currency': quote['currency'] = 'EUR'
    elif damage == 'missing_quote': inputs.pop('spicy_quote_id')
    with pytest.raises(ValueError):
        autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)
    assert list(root.iterdir()) == []


def test_uncertain_original_reserves_full_maximum_and_excludes_itself(authority):
    root, inputs, native, quote, policy = authority
    directory = root / 'production_attempts' / 'original'
    directory.mkdir(parents=True)
    retained = {'attempt_id': 'original', 'shot_id': 'shot', 'scope_id': 'scope', 'scope': {'provider': 'spicy'},
                'spicy_native': native,
                'spicy_authority': {'account_label': 'offline-fixture', 'credential_digest': H,
                                    'quote': dict(quote, max_charge_usd='0.16')}}
    (directory / 'request.json').write_text(json.dumps(retained))
    with pytest.raises(ValueError, match='cumulative'):
        autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)
    token = execution._ACTIVE.set({'provider': 'spicy', 'root': root, 'session_id': 'original'})
    try:
        with pytest.raises(ValueError, match='exclusion'):
            autonomy.verify_spicy_dispatch(root, dict(inputs, cli_session_id='original'), native, policy=policy)
    finally:
        execution._ACTIVE.reset(token)
    retained['spicy_authority']['quote'] = quote
    (directory / 'request.json').write_text(json.dumps(retained))
    own = dict(inputs, cli_session_id='original', governance={'shot_id': 'shot', 'scope_id': 'scope'})
    assert autonomy.verify_spicy_dispatch(root, own, native, policy=policy)['remaining_total_usd'] == '0.15'
    token = execution._ACTIVE.set({'provider': 'spicy', 'root': root, 'session_id': 'original'})
    try:
        assert autonomy.verify_spicy_dispatch(root, own, native, policy=policy)['remaining_total_usd'] == '0.30'
    finally:
        execution._ACTIVE.reset(token)
    assert not (directory / 'spicy').exists()


def test_missing_explicit_route_authority_is_disabled(authority):
    root, inputs, native, _, policy = authority
    policy['providers'] = []
    with pytest.raises(ValueError, match='disabled'):
        autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)


def test_spicy_schema_requires_named_binding_decimal_caps():
    from schemas.artifacts import load_schema
    from jsonschema import Draft202012Validator
    schema = load_schema('autonomy_policy')
    validator = Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/spicy_quoted_usd_budget'})
    assert not list(validator.iter_errors(spicy_spec()))
    for patch in ({'total_cap_usd': 0.30}, {'credential_ref': 'SPICY_API_KEY'}, {'billing': 'quoted_credit_budget'}, {'account_label': ''}):
        assert list(validator.iter_errors(spicy_spec(**patch)))


def test_gateway_rejects_spicy_without_project_before_any_transport(tmp_path):
    from types import SimpleNamespace
    tool = SimpleNamespace(provider='spicy', name='spicy_video', capability='video_generation')
    with pytest.raises(ValueError, match='strict enrollment'):
        execution.preflight(tool, {'model': MODEL, 'output_path': str(tmp_path / 'out.mp4')})
    assert list(tmp_path.iterdir()) == []


def test_spicy_native_body_keeps_exact_payload_and_source_roles():
    native = {'provider': 'spicy', 'transport': 'spicy_native_jobs_v1', 'model': MODEL,
              'request_sha256': H, 'input': {'prompt': 'literal', 'duration_seconds': 5},
              'sources': [{'role': 'first_frame', 'path': '/synthetic/first.png', 'sha256': H}]}
    before = copy.deepcopy(native)
    assert preparation._body(native)['params']['duration'] == 5
    binding = preparation._native_binding(native)
    assert binding['request_sha256'] == H and binding['provider'] == 'spicy'
    assert native == before
    packet = {'binding': {'references': [{'id': 'first', 'role': 'start_frame', 'sha256': H}]}, 'shot': {'asset_ids': ['first']}}
    preparation._validate_spicy_asset_roles(packet, native)
    native['sources'][0]['sha256'] = 'b' * 64
    with pytest.raises(ValueError, match='role/hash'):
        preparation._validate_spicy_asset_roles(packet, native)


def test_factory_templates_use_exact_spicy_pins_and_explicit_audio():
    package = {'boards': {'references': [],
               'image_nodes': [{'slot_id': 'start'}, {'slot_id': 'end'}],
               'shots': [{'shot_id': 'shot', 'roles': [
                   {'asset_id': 'start', 'role': 'start_frame'}, {'asset_id': 'end', 'role': 'end_frame'}]}]},
               'shots': [{'id': 'shot', 'duration_seconds': 5, 'action': 'SYNTHETIC fixture action'}]}
    settings = {'provider': 'spicy', 'model': MODEL, 'mode': 'image_to_video',
                'resolution': '720p', 'generate_audio': False, 'account_label': 'offline-fixture',
                'credential_ref': 'env:SPICY_OFFLINE_FIXTURE_KEY'}
    rows = preparation.build_video_request_templates(package, settings)
    assert rows['shot']['input_assets'] == [
        {'role': 'first_frame', 'source_path': {'$board': {'asset_id': 'start'}}},
        {'role': 'last_frame', 'source_path': {'$board': {'asset_id': 'end'}}}]
    assert rows['shot']['duration'] == 5 and rows['shot']['generate_audio'] is False
    assert rows['shot']['preferred_provider'] == 'spicy'
    with pytest.raises(ValueError, match='explicit generate_audio'):
        preparation.build_video_request_templates(package, {k: v for k, v in settings.items() if k != 'generate_audio'})
    package['boards']['shots'][0]['roles'].append({'asset_id': 'start', 'role': 'reference_image'})
    with pytest.raises(ValueError, match='unsupported'):
        preparation.build_video_request_templates(package, settings)


def test_spicy_conditioning_refuses_missing_or_foreign_start_frame():
    packet = {'binding': {'references': [{'id': 'start', 'role': 'start_frame', 'sha256': H}]},
              'shot': {'asset_ids': ['start']}}
    native = {'provider': 'spicy', 'sources': []}
    with pytest.raises(ValueError, match='requires its reviewed start'):
        preparation._validate_spicy_asset_roles(packet, native)
    native['sources'] = [{'role': 'last_frame', 'path': '/synthetic', 'sha256': H}]
    with pytest.raises(ValueError, match='role/hash'):
        preparation._validate_spicy_asset_roles(packet, native)


def test_nested_spicy_adapter_receives_only_trusted_reservation_metadata(tmp_path):
    from types import SimpleNamespace
    tool = SimpleNamespace(provider='spicy', input_schema={})
    owner = {'project_dir': str(tmp_path), 'governance': {'shot_id': 'shot', 'scope_id': 'scope'},
             'spicy_quote_id': 'retained-quote', 'compiled_request_id': 'compiled', 'preparation_review_id': 'review'}
    token = execution._ACTIVE.set({'provider': 'spicy', 'submitted_inputs': owner})
    try:
        result = execution._provider_inputs(tool, {'prompt': 'literal', 'spicy_quote_id': 'foreign-quote'}, 'trusted-session')
        assert result['spicy_quote_id'] == 'retained-quote'
        assert result['governance'] == owner['governance']
        assert result['project_dir'] == str(tmp_path) and result['cli_session_id'] == 'trusted-session'
    finally:
        execution._ACTIVE.reset(token)


def test_historical_original_keeps_expired_source_uri_and_rehashes_bytes(tmp_path):
    source = tmp_path / 'start.png'
    source.write_bytes(b'SYNTHETIC immutable source bytes; no real media claim')
    source_hash = preparation.file_sha256(source)
    payload = {'prompt': 'SYNTHETIC original prompt', 'duration_seconds': 5,
               'resolution': '720p', 'generate_audio': False, 'image_url': 'spicy://fixture/start.png'}
    native = {'provider': 'spicy', 'transport': 'spicy_native_jobs_v1', 'model': MODEL,
              'mode': 'image_to_video', 'input': payload,
              'request_sha256': preparation.digest({'model': MODEL, 'input': payload}),
              'sources': [{'role': 'first_frame', 'source_path': str(source), 'source_sha256': source_hash,
                           'uri': payload['image_url'], 'upload_expires_at': '2000-01-01T00:00:00Z'}]}
    inputs = {'model': MODEL, 'prompt': payload['prompt'], 'duration': 5, 'resolution': '720p',
              'generate_audio': False, 'input_assets': [{'role': 'first_frame', 'source_path': str(source)}]}
    assert preparation._historical_spicy_native(inputs, native) == native
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='source bytes changed'):
        preparation._historical_spicy_native(inputs, native)


def test_standing_spicy_account_and_source_transfer_context_must_match(tmp_path):
    from tests.lib.test_production_factory import standing_fixture, creator_fixture
    kwargs = standing_fixture(tmp_path)
    kwargs['policy']['providers'] = [spicy_spec()]
    setup = json.loads((tmp_path / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    setup['providers'] = [spicy_spec()]
    setup['provider_accounts'] = {'spicy': 'a-different-account'}
    term = setup['source_transfer'][0]
    term.update(provider='spicy', account='a-different-account')
    setup['source_transfer'] = [term]
    kwargs['standing_setup'] = creator_fixture(tmp_path, 'spicy-standing', setup)
    command = json.loads((tmp_path / kwargs['run_command']['path']).read_text())
    command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(tmp_path, 'spicy-command', command)
    with pytest.raises(ValueError, match='source-transfer account context'):
        autonomy.activate_standing_run(tmp_path, **kwargs)
    assert not (tmp_path / 'production_attempts').exists()


def test_zero_cost_exact_quote_is_valid(authority):
    root, inputs, native, quote, policy = authority
    quote['estimated_cost_usd'] = '0'
    quote['max_charge_usd'] = '0'
    result = autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)
    assert result['quote']['max_charge_usd'] == '0'
    assert result['remaining_total_usd'] == '0.30'


def test_corrupt_prior_quote_cannot_reduce_reserved_exposure(authority):
    root, inputs, native, quote, policy = authority
    directory = root / 'production_attempts' / 'prior'
    directory.mkdir(parents=True)
    retained = {'attempt_id': 'prior', 'scope': {'provider': 'spicy'},
                'spicy_authority': {'account_label': 'offline-fixture', 'credential_digest': H,
                                    'quote': dict(quote, max_charge_usd='-0.05')}}
    (directory / 'request.json').write_text(json.dumps(retained))
    with pytest.raises(ValueError, match='nonnegative'):
        autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)


def _standing_spicy_upload_fixture(root, monkeypatch, *, max_batches=1):
    from tests.lib.test_production_factory import standing_fixture, creator_fixture
    kwargs = standing_fixture(root)
    kwargs['policy']['providers'] = [spicy_spec()]
    inputs = {'model': MODEL, 'mode': 'image_to_video', 'duration': 8, 'resolution': '720p',
              'generate_audio': False, 'account_label': 'offline-fixture',
              'credential_ref': 'env:SPICY_OFFLINE_FIXTURE_KEY', 'image_path': str(root / 'assets/start.svg')}
    kwargs['baselines']['entry']['planned_request_template'] = execution.planned_request_template(inputs, project_dir=root)
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    setup['providers'] = [spicy_spec()]
    setup['provider_accounts'] = {'spicy': 'offline-fixture'}
    term = setup['source_transfer'][0]
    term.update(provider='spicy', account='offline-fixture', max_batches=max_batches)
    setup['source_transfer'] = [term]
    kwargs['standing_setup'] = creator_fixture(root, 'spicy-upload-standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'spicy-upload-command', command)
    autonomy.activate_standing_run(root, **kwargs)
    monkeypatch.setattr(autonomy, 'verify_spicy_account', lambda *_: {'credential_digest': H})
    inputs.update(project_dir=str(root), governance={'shot_id': 'entry'}, compiled_request_id='source-one')
    return inputs


def test_uncertain_precompiled_transfer_is_counted_and_original_intent_replays(tmp_path, monkeypatch):
    inputs = _standing_spicy_upload_fixture(tmp_path, monkeypatch)
    first = autonomy.reserve_spicy_source_transfer(tmp_path, inputs)
    assert first['replayed'] is False
    assert not (tmp_path / 'production_attempts').exists()
    # No success receipt exists: this is an uncertain original transfer and it
    # still consumes the one authorized batch. Resuming identifies that original.
    second = autonomy.reserve_spicy_source_transfer(tmp_path, inputs)
    assert second == {**first, 'replayed': True}
    with pytest.raises(ValueError, match='max_batches exhausted'):
        autonomy.reserve_spicy_source_transfer(tmp_path, dict(inputs, compiled_request_id='another-preparation'))
    assert len(list((tmp_path / 'artifacts').glob('spicy_source_transfer-*.json'))) == 1


def test_concurrent_source_preparations_reserve_combined_cap_under_project_lock(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    inputs = _standing_spicy_upload_fixture(tmp_path, monkeypatch)
    def reserve(preparation_id):
        try:
            return autonomy.reserve_spicy_source_transfer(tmp_path, dict(inputs, compiled_request_id=preparation_id))
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ['concurrent-a', 'concurrent-b']))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert sum(isinstance(row, str) and 'max_batches exhausted' in row for row in results) == 1
    assert len(list((tmp_path / 'artifacts').glob('spicy_source_transfer-*.json'))) == 1
    assert not (tmp_path / 'production_attempts').exists()


class _NativeJobsHTTPFixture:
    """Synthetic native HTTP envelopes, not replacement job/governance functions."""
    def __init__(self, binding, video_bytes):
        self.binding = binding
        self.qualified_storage_hosts = frozenset(spicy_spec()['qualified_storage_hosts'])
        self.video_bytes = video_bytes
        self.calls = []
        self.uploads = {}
        self.creates = []
        self.lost_create_response = False
        self.settled_cost = '0.10'
        self.output_url = 'https://fixture-account.r2.cloudflarestorage.com/spicy-production-results/original.mp4'

    # Named SDK-backed client surface used by production code; each delegates to the
    # test-only request_json recorder below so subclasses/monkeypatches keep working.
    def get_model(self, model, **kwargs):
        return self.request_json('GET', '/models/' + model)

    def list_models(self, **query):
        return self.request_json('GET', '/models', query=query or None)

    def get_balance(self):
        return self.request_json('GET', '/chat/credit')

    def quote_task(self, *, model, input_data):
        return self.request_json('POST', '/jobs/quote', {'model': model, 'input': input_data})

    def create_task(self, *, model, input_data, idempotency_key, quote_id, expected_cost):
        body = {'model': model, 'input': input_data, 'quoteId': quote_id, 'expectedCost': expected_cost}
        return self.request_json('POST', '/jobs/createTask', body, headers={'Idempotency-Key': idempotency_key})

    def get_task(self, task_id):
        return self.request_json('GET', '/jobs/recordInfo', query={'taskId': task_id})

    def create_upload_url(self, *, content_type, byte_count):
        return self.request_json('POST', '/common/upload-url', {'contentType': content_type, 'bytes': byte_count})

    def commit_file(self, file_id):
        return self.request_json('POST', '/files/' + file_id + '/commit', {})

    def create_download_url(self, task_id, key=None):
        return self.request_json('POST', '/common/download-url', {'taskId': task_id, 'key': key})

    def request_json(self, method, path, body=None, *, query=None, headers=None, **kwargs):
        from lib import spicy_catalog
        body = body if body is not None else kwargs.get('payload', kwargs.get('json_body', kwargs.get('json')))
        self.calls.append((method, path, copy.deepcopy(body), copy.deepcopy(headers or {})))
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
        if path == '/models':
            data = {'total': 1, 'items': [{'model': MODEL, 'enabled': True, 'available': True,
                    'inputSchema': spicy_catalog.describe_video_inputs(MODEL)['root_schema']}]}
        elif path == '/common/upload-url':
            file_id = 'fixture-source-' + str(len(self.uploads) + 1)
            self.uploads[file_id] = {'ticket': copy.deepcopy(body)}
            data = {'fileId': file_id, 'key': 'uploads/' + file_id, 'method': 'PUT', 'headers': {'Content-Type': body['contentType']},
                    'uploadUrl': 'https://fixture-upload.r2.cloudflarestorage.com/' + file_id,
                    'maxBytes': 50 * 1024 * 1024, 'expiresAt': expires}
        elif path.startswith('/files/') and path.endswith('/commit'):
            import hashlib
            file_id = path.split('/')[2]
            row = self.uploads[file_id]
            payload = row['bytes']
            assert len(payload) == row['ticket']['bytes']
            data = {'fileId': file_id, 'status': 'ready', 'bytes': len(payload),
                    'contentType': row['ticket']['contentType'], 'sha256': hashlib.sha256(payload).hexdigest(),
                    'uri': 'spicy://f/' + file_id, 'expiresAt': expires}
        elif path == '/jobs/quote':
            data = {'quoteId': 'fixture-provider-quote', 'model': MODEL, 'estimatedCost': '0.10',
                    'maxCharge': '0.15', 'currency': 'USD', 'expiresAt': expires}
        elif path == '/jobs/createTask':
            self.creates.append((copy.deepcopy(body), copy.deepcopy(headers or {})))
            if not self.lost_create_response:
                self.lost_create_response = True
                from lib.spicy_jobs import SpicyTransportError
                raise SpicyTransportError('SYNTHETIC accepted original task; response lost', kind='transport_uncertain')
            data = {'taskId': 'fixture-original-task', 'model': MODEL, 'state': 'queued',
                    'estimatedCost': '0.10', 'deadlineAt': expires}
        elif path == '/jobs/recordInfo':
            assert query['taskId'] == 'fixture-original-task'
            data = {'taskId': 'fixture-original-task', 'model': MODEL, 'state': 'succeeded',
                    'settled': True, 'cost': self.settled_cost, 'output': {'assets': [
                        {'key': 'fixture-original', 'url': self.output_url, 'expiresAt': expires,
                         'mime': 'video/mp4', 'width': 720, 'height': 720,
                         'durationSeconds': 8, 'bytes': len(self.video_bytes)}]}}
        elif path == '/common/download-url':
            assert body['taskId'] == 'fixture-original-task'
            data = {'url': self.output_url, 'expiresAt': expires}
        else:
            pytest.fail('Unexpected synthetic HTTP request: ' + method + ' ' + path)
        return {'code': 200, 'msg': 'success', 'request_id': 'SYNTHETIC-request', 'data': data}

    def put_presigned(self, url, data, headers=None, **kwargs):
        from urllib.parse import urlsplit
        assert not any(k.lower() == 'authorization' for k in (headers or {}))
        file_id = urlsplit(url).path.rsplit('/', 1)[-1]
        if isinstance(data, (str, Path)):
            data = Path(data).read_bytes()
        self.uploads[file_id]['bytes'] = data
        return {'status': 200}


def _actual_core_factory_fixture(root, monkeypatch, *, binding, max_batches=3):
    """Retain real standing authorization and canonical source/preparation inputs."""
    from PIL import Image
    from tests.lib.test_production_factory import standing_fixture, creator_fixture
    from lib.shot_contract import file_sha256, project_review_subject, shot_review_subject
    kwargs = standing_fixture(root)
    contract = execution.load_shot_contract(root)
    for index, asset in enumerate(contract['assets']):
        path = root / 'assets' / (asset['id'] + '.png')
        Image.new('RGB', (512, 512), (20 + index * 30, 90, 160)).save(path)
        asset['path'] = str(path.relative_to(root))
        asset['sha256'] = file_sha256(path)
        asset['review']['subject_sha256'] = asset['sha256']
    contract['project_review']['subject_sha256'] = project_review_subject(contract)
    for shot in contract['shots']:
        shot['review']['subject_sha256'] = shot_review_subject(contract, shot['id'])
    (root / 'artifacts/shot_contract.json').write_text(json.dumps(contract))
    authored = preparation.compile_provider_prompt(root, 'entry', provider='spicy')
    inputs = {'project_dir': str(root), 'governance': {'shot_id': 'entry'},
              'model': MODEL, 'mode': 'image_to_video', 'operation': 'image_to_video',
              'preferred_provider': 'spicy', 'allowed_providers': ['spicy'], 'preferred_tool': 'spicy_video',
              'duration': 8, 'resolution': '720p', 'generate_audio': False,
              'account_label': 'offline-fixture', 'credential_ref': binding['credential_ref'],
              'prompt': authored['prompt'], 'input_assets': [
                  {'role': role, 'source_path': str(root / next(a['path'] for a in contract['assets'] if a['id'] == aid))}
                  for role, aid in [('first_frame', 'start'), ('last_frame', 'end')]],
              'compiled_request_id': 'actual-core-fixture', 'preparation_review_id': 'actual-core-fixture',
              'output_path': str(root / 'assets/video/actual-core-fixture.mp4')}
    (root / 'assets/video').mkdir(parents=True, exist_ok=True)
    spec = spicy_spec(credential_digest=binding['credential_digest'])
    kwargs['policy']['providers'] = [spec]
    kwargs['policy']['locked'] = {'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}}
    kwargs['baselines'] = autonomy.standing_baselines(root, {'entry': inputs})
    setup = json.loads((root / kwargs['standing_setup']['path']).read_text())
    setup.pop('authorization')
    setup.update(providers=[spec], provider_accounts={'spicy': 'offline-fixture'},
                 approved_source_sha256=[a['sha256'] for a in contract['assets']])
    setup['source_transfer'] = [{**setup['source_transfer'][0], 'provider': 'spicy', 'account': 'offline-fixture', 'max_batches': max_batches}]
    kwargs['standing_setup'] = creator_fixture(root, 'actual-core-standing', setup)
    command = json.loads((root / kwargs['run_command']['path']).read_text())
    command.pop('authorization')
    command['standing_setup_sha256'] = kwargs['standing_setup']['sha256']
    kwargs['run_command'] = creator_fixture(root, 'actual-core-command', command)
    autonomy.activate_standing_run(root, **kwargs)
    return inputs, authored, contract


def _retain_actual_core_preparation(root, inputs, authored, contract):
    from lib import spicy_jobs
    native = spicy_jobs.build_native_request(inputs)
    shot = contract['shots'][0]
    timing = {'method': 'segmented_estimate', 'duration_seconds': 8, 'language': 'en', 'margin_seconds': .5,
              'rationale': 'SYNTHETIC offline timing fixture', 'overlap_policy': 'serial',
              'overlap_rationale': 'SYNTHETIC separated windows',
              'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key,
                  'start_seconds': start, 'end_seconds': end, 'rationale': 'SYNTHETIC authored interval'}
                  for key, start, end in [('dominant_action', 4.1, 7), ('completed_end_state', 7, 7.4)]],
              'segments': [{'dialogue_index': i, 'language': 'en', 'words_per_minute': 600,
                  'pause_seconds': 0, 'rationale': 'SYNTHETIC timing fixture'} for i in range(len(shot['dialogue']))]}
    start = next(a for a in contract['assets'] if a['id'] == 'start')
    review = {'version': '1.0', 'reviewer': 'SYNTHETIC offline reviewer, no real viewing claim',
              'status': 'pass', 'evidence_kind': 'reviewed',
              'predicates': [{'name': name, 'status': 'pass', 'severity': 'critical', 'evidence': 'SYNTHETIC fixture'}
                             for name in sorted(preparation.PREDICATES)],
              'composite_boards': [{'sha256': start['sha256'], 'role': 'start_frame',
                  'members': [{k: a[k] for k in ('sha256', 'role', 'cast_ids')}
                      for a in contract['assets'] if a['role'] == 'identity_reference'
                      and set(a['cast_ids']).intersection(shot['cast_ids'])]}]}
    preparation.prepare_compiled_request(inputs, native, {'source': 'real'},
        coverage=authored['coverage'], timing=timing, review=review, persist=True)
    scope = autonomy.derive_scope(root, inputs, provider='spicy')
    inputs['governance']['scope_id'] = scope['id']
    return native, scope


def _install_synthetic_download_transport(monkeypatch, media_bytes):
    import socket
    from lib import openart_download
    from tests.lib.test_openart_download import Response
    calls = []
    def addresses(host, port, **kwargs):
        assert host.endswith('.r2.cloudflarestorage.com')
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', port))]
    monkeypatch.setattr(socket, 'getaddrinfo', addresses)
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('Real network is forbidden in factory fixtures'))
    class Connection:
        def __init__(self, host, address, timeout):
            calls.append({'host': host, 'address': address})
        def request(self, method, target, headers):
            assert method == 'GET'
            assert not any(k.lower() in {'authorization', 'cookie'} for k in headers)
            calls[-1].update(method=method, target=target, headers=headers)
        def getresponse(self):
            return Response(media_bytes, headers={'Content-Type': 'video/mp4', 'Content-Length': str(len(media_bytes))})
        def set_timeout(self, timeout):
            pass
        def close(self):
            pass
    monkeypatch.setattr(openart_download, '_PinnedHTTPSConnection', Connection)
    return calls


@pytest.mark.parametrize(('settled_cost', 'parent_damage', 'dispatch_selector'), [
    (cost, damage, False) for cost in ['0.10', '0.20'] for damage in ['intact', 'deleted', 'changed']
] + [('0.10', 'intact', True)])
def test_actual_core_factory_lost_create_response_recovers_one_original_and_selects(tmp_path, monkeypatch, settled_cost, parent_damage, dispatch_selector):
    """Real engine lifecycle + mocked HTTP + local synthetic FFmpeg media only."""
    import shutil
    import subprocess
    from lib import spicy_catalog, spicy_jobs
    from lib.shot_contract import file_sha256, selection_digest, UPSTREAM_PREDICATES
    from tests.integration.test_first_pass_workflow import attestation
    from tools.video.spicy_video import SpicyVideo
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Synthetic media acceptance requires local FFmpeg/ffprobe')
    monkeypatch.setenv('SPICY_OFFLINE_FIXTURE_KEY', 'SYNTHETIC-NONWORKING-OFFLINE-KEY')
    binding = spicy_jobs.credential_binding('env:SPICY_OFFLINE_FIXTURE_KEY')
    provider_media = tmp_path / 'SYNTHETIC-provider-original.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=720x720:r=24:d=8',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                    str(provider_media)], check=True, capture_output=True)
    http = _NativeJobsHTTPFixture(binding, provider_media.read_bytes())
    http.settled_cost = settled_cost
    downloads = _install_synthetic_download_transport(monkeypatch, http.video_bytes)
    monkeypatch.setattr(spicy_jobs, 'client_for_credential', lambda *_, **kwargs: http)
    root = tmp_path / 'canonical-project'
    root.mkdir()
    inputs, authored, contract = _actual_core_factory_fixture(root, monkeypatch, binding=binding)
    spicy_catalog.observe_account(root, http, MODEL)
    spicy_jobs.upload_sources(root, inputs, client=http)
    native = spicy_jobs.build_native_request(inputs)
    assert native['payload_state'] == 'ready'
    quote = spicy_jobs.quote_native_request(root, native, account_label='offline-fixture',
                                            credential_ref=binding['credential_ref'], client=http)
    inputs['spicy_quote_id'] = quote['quote_id']
    _retain_actual_core_preparation(root, inputs, authored, contract)
    dispatch_tool = SpicyVideo()
    if dispatch_selector:
        from tools.video.video_selector import VideoSelector
        monkeypatch.setattr(VideoSelector, '_providers', lambda self: [SpicyVideo()])
        dispatch_tool = VideoSelector()
    try:
        result = dispatch_tool.execute(inputs)
        assert not result.success
    except spicy_jobs.SpicyError:
        # Both exception and typed uncertain returns are legitimate transport
        # surfaces; the retained original journal/attempt is the authority.
        pass
    attempts = execution._attempts(root)
    assert len(attempts) == 1
    original = attempts[0]
    assert len(http.creates) == 1
    assert execution._state(root, original)['status'] == 'uncertain'
    mutable_parent = Path(inputs['input_assets'][0]['source_path'])
    parent_bytes = mutable_parent.read_bytes()
    if parent_damage == 'deleted': mutable_parent.unlink()
    elif parent_damage == 'changed': mutable_parent.write_bytes(b'SYNTHETIC drifted mutable source parent')
    completed = execution.resume_original_attempt(root, original['attempt_id'], request_sha256=original['request_sha256'])
    assert completed['status'] == 'generated'
    assert len(execution._attempts(root)) == 1 and len(http.creates) == 2
    assert http.creates[0] == http.creates[1]
    assert len(downloads) == 1
    assert len(list((root / 'artifacts').glob('spicy_source_transfer-*.json'))) == 1
    assert autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])['total'] == 1
    before = (len(http.calls), len(downloads))
    assert execution.resume_original_attempt(root, original['attempt_id'], request_sha256=original['request_sha256']) == completed
    assert (len(http.calls), len(downloads)) == before
    outgoing = root / 'assets/video/actual-outgoing.png'
    subprocess.run(['ffmpeg', '-v', 'error', '-ss', '7.95', '-i', completed['output']['path'],
                    '-frames:v', '1', str(outgoing)], check=True, capture_output=True)
    selection = {'attempt_id': original['attempt_id'], 'output': completed['output'],
                 'outgoing_frame': {'path': str(outgoing), 'sha256': file_sha256(outgoing)}}
    selection['review'] = attestation(selection_digest(selection), UPSTREAM_PREDICATES, contract['story_revision'])
    if parent_damage != 'intact':
        # Recovery uses immutable sent snapshots. Current source/semantic review
        # independently remains a selection gate; restoring exact parent bytes
        # satisfies that current gate without changing historical evidence.
        with pytest.raises((ValueError, OSError)):
            execution.record_selection(root, 'entry', selection)
        mutable_parent.write_bytes(parent_bytes)
    assert execution.record_selection(root, 'entry', selection) == selection
    assert execution.record_selection(root, 'entry', selection) == selection
    assert len(execution._attempts(root)) == 1 and len(http.creates) == 2
    assert not list(root.glob('artifacts/final_review*.json'))
    from lib import production_draft as draft
    rows = draft.first_cut_candidates(root)
    assert next(row for row in rows if row['shot_id'] == 'entry')['status'] == 'candidate'
    counts_before_delivery = (len(http.creates), len(downloads), len(execution._attempts(root)))
    delivery = draft.deliver_standalone(root, operation_id='SYNTHETIC-spicy-draft-delivery')
    assert delivery['status'] in {'complete', 'partial'}
    assert Path(delivery['export_path']).is_file()
    assert delivery['not_certification'] is True and delivery['provider_calls'] == 0 and delivery['posted'] is False
    assert delivery['clips'][0]['attempt_id'] == original['attempt_id']
    assert draft.deliver_standalone(root, operation_id='SYNTHETIC-spicy-draft-delivery')['first_cut'] == delivery['first_cut']
    assert counts_before_delivery == (len(http.creates), len(downloads), len(execution._attempts(root)))
    assert not list(root.glob('artifacts/final_review*.json'))
    if settled_cost == '0.20':
        assert spicy_jobs.billing_anomaly(root, original['attempt_id'])['kind'] == 'billing_anomaly'
        with pytest.raises(ValueError, match='billing anomaly'):
            autonomy.verify_spicy_dispatch(root, inputs, native)
        assert len(execution._attempts(root)) == 1


def test_exact_spicy_model_fit_requires_start_frame_even_before_upload():
    from types import SimpleNamespace
    from tools.base_tool import ToolStatus
    from lib.video_model_selection import _candidate
    tool = SimpleNamespace(provider='spicy', name='spicy_video', get_status=lambda: ToolStatus.AVAILABLE)
    inputs = {'model': MODEL, 'mode': 'image_to_video', 'operation': 'image_to_video',
              'prompt': 'SYNTHETIC source-free request', 'duration': 5,
              'resolution': '720p', 'generate_audio': False}
    candidate, reason = _candidate(tool, {'provider': 'spicy', 'model': MODEL}, inputs)
    assert candidate is None and reason == 'missing_controls:first_frame'


def test_registered_spicy_contract_is_offline_custom_credential_and_quote_only(monkeypatch):
    from tools.video import spicy_video
    from tools.tool_registry import ToolRegistry
    from tools.base_tool import ToolStatus
    from tools.provider_pricing import PriceQuoteRequired
    from jsonschema import Draft202012Validator
    monkeypatch.delenv('SPICY_API_KEY', raising=False)
    registry = ToolRegistry()
    assert 'spicy_video' in registry.register_module(spicy_video)
    tool = registry.get('spicy_video')
    assert tool.get_status() == ToolStatus.AVAILABLE
    assert tool.get_info()['default_generation_enabled'] is False
    assert tool.retry_policy.max_retries == 0 and not tool.fallback_tools
    assert tool.agent_skills == ['spicy-api']
    with pytest.raises(PriceQuoteRequired):
        tool.estimate_cost({})
    body = {'project_dir': '/synthetic', 'governance': {'scope_id': 'scope', 'shot_id': 'shot'},
            'model': MODEL, 'mode': 'image_to_video', 'prompt': 'synthetic', 'duration': 8,
            'resolution': '720p', 'generate_audio': False, 'account_label': 'offline-fixture',
            'credential_ref': 'env:SPICY_OFFLINE_FIXTURE_KEY', 'spicy_quote_id': 'synthetic',
            'compiled_request_id': 'synthetic', 'preparation_review_id': 'synthetic',
            'output_path': '/synthetic/output.mp4', 'input_assets': [
                {'role': 'first_frame', 'source_path': '/synthetic/input.png', 'source_sha256': H}]}
    validate = Draft202012Validator(tool.input_schema)
    assert not list(validate.iter_errors(body))
    for damage in ({'duration': 8.5}, {'generate_audio': 'false'}, {'model': 'family-alias'},
                   {'operation': 'first_last_frame'}, {'image_url': 'https://synthetic.invalid/a.png'}, {'callback': 'https://synthetic.invalid'}):
        assert list(validate.iter_errors(dict(body, **damage)))
    before = copy.deepcopy(body)
    with pytest.raises(ValueError, match='missing project.json'):
        tool.execute(body)
    assert before == body


def test_spicy_storage_hosts_are_exact_policy_bound(authority):
    root, inputs, native, quote, policy = authority
    assert autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)['qualified_storage_hosts'] == sorted(spicy_spec()['qualified_storage_hosts'])
    for hosts in ([], ['https://fixture.example'], ['*.example.com'], ['Fixture.example.com'], ['a.example.com', 'a.example.com']):
        policy['providers'][0]['qualified_storage_hosts'] = hosts
        with pytest.raises(ValueError, match='storage hosts'):
            autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)


@pytest.mark.parametrize('max_batches', [1, 3])
def test_actual_core_factory_uncertain_upload_is_counted_and_never_reuploaded(tmp_path, monkeypatch, max_batches):
    from lib import spicy_jobs, spicy_catalog
    from lib.spicy_uploads import upload_sources
    monkeypatch.setenv('SPICY_OFFLINE_FIXTURE_KEY', 'SYNTHETIC-NONWORKING-OFFLINE-KEY')
    binding = spicy_jobs.credential_binding('env:SPICY_OFFLINE_FIXTURE_KEY')
    http = _NativeJobsHTTPFixture(binding, b'')
    http.qualified_storage_hosts = frozenset(spicy_spec()['qualified_storage_hosts'])
    root = tmp_path / 'canonical-project'
    root.mkdir()
    inputs, authored, contract = _actual_core_factory_fixture(root, monkeypatch, binding=binding, max_batches=max_batches)
    spicy_catalog.observe_account(root, http, MODEL)
    put_calls = []
    original_put = http.put_presigned
    def lost_put(*args, **kwargs):
        put_calls.append(1)
        original_put(*args, **kwargs)
        raise spicy_jobs.SpicyTransportError('SYNTHETIC PUT response lost', kind='transport_uncertain')
    monkeypatch.setattr(http, 'put_presigned', lost_put)
    with pytest.raises(spicy_jobs.SpicyError) as upload_error:
        upload_sources(root, inputs, client=http)
    assert len(put_calls) == 1, (upload_error.value.kind, str(upload_error.value))
    assert len(list((root / 'artifacts').glob('spicy_source_transfer-*.json'))) == 1
    before = len(http.calls)
    with pytest.raises(spicy_jobs.SpicyError):
        upload_sources(root, inputs, client=http)
    assert len(http.calls) == before and len(put_calls) == 1
    assert not (root / 'production_attempts').exists()
    if max_batches == 1:
        with pytest.raises(ValueError, match='max_batches'):
            autonomy.reserve_spicy_source_transfer(root, dict(inputs, compiled_request_id='different-preparation'))
    else:
        with pytest.raises(spicy_jobs.SpicyError, match='unresolved original'):
            upload_sources(root, dict(inputs, compiled_request_id='different-preparation'), client=http)
        assert len(http.calls) == before and len(put_calls) == 1


@pytest.mark.parametrize('damage', ['deleted_parent', 'changed_parent'])
def test_spicy_historical_native_uses_canonical_snapshot_not_mutable_parent(tmp_path, damage):
    import hashlib
    source = tmp_path / 'source.png'
    source.write_bytes(b'SYNTHETIC original bytes')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    inputs = {'project_dir': str(tmp_path), 'governance': {'scope_id': 'scope', 'shot_id': 'shot'},
              'model': MODEL, 'mode': 'image_to_video', 'prompt': 'synthetic', 'duration': 8,
              'resolution': '720p', 'generate_audio': False,
              'input_assets': [{'role': 'first_frame', 'source_path': str(source), 'source_sha256': sha}],
              'output_path': str(tmp_path / 'output.mp4')}
    request_sha = execution.planned_request_digest(inputs, project_dir=tmp_path)
    directory = tmp_path / 'production_attempts' / 'original'
    snapshot = directory / 'inputs' / 'source.png'
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(source.read_bytes())
    submitted = copy.deepcopy(inputs)
    submitted['input_assets'][0]['source_path'] = str(snapshot)
    submitted['cli_session_id'] = 'original'
    request = {'attempt_id': 'original', 'request_sha256': request_sha, 'scope': {'provider': 'spicy'},
               'submitted_inputs': submitted, 'input_assets': [
                   {'role': 'first_frame', 'path': str(snapshot), 'original_path': str(source), 'sha256': sha}]}
    (directory / 'request.json').write_text(json.dumps(request))
    payload = {'prompt': 'synthetic', 'duration_seconds': 8, 'resolution': '720p',
               'generate_audio': False, 'aspect_ratio': 'adaptive', 'image_url': 'spicy://f/synthetic'}
    native = {'model': MODEL, 'input': payload, 'request_sha256': preparation.digest({'model': MODEL, 'input': payload}),
              'sources': [{'role': 'first_frame', 'source_path': str(source), 'source_sha256': sha}]}
    proof = preparation._SpicySnapshotProof(tmp_path, request)
    if damage == 'deleted_parent': source.unlink()
    else: source.write_bytes(b'SYNTHETIC changed mutable parent')
    assert proof.request_digest(inputs) == request_sha
    assert preparation._historical_spicy_native(inputs, native, _snapshot_proof=proof) == native
    with pytest.raises((ValueError, OSError)):
        preparation._historical_spicy_native(inputs, native)
    with pytest.raises(ValueError, match='internal evidence'):
        preparation._historical_spicy_native(inputs, native, _snapshot_proof={'sources': request['input_assets']})
    snapshot.write_bytes(b'SYNTHETIC tampered canonical bytes')
    with pytest.raises(ValueError):
        preparation._historical_spicy_native(inputs, native, _snapshot_proof=proof)


def test_spicy_usd_authority_never_serializes_exponents(authority):
    root, inputs, native, quote, policy = authority
    quote.update(estimated_cost_usd='0', max_charge_usd='0')
    policy['providers'][0].update(per_attempt_cap_usd='0.00000001', total_cap_usd='0.00000002')
    result = autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)
    assert result['per_attempt_cap_usd'] == '0.00000001'
    assert result['remaining_total_usd'] == '0.00000002'


def test_spicy_reservations_do_not_round_away_small_exposure():
    from decimal import Decimal, localcontext
    with localcontext() as context:
        context.prec = 3
        total = autonomy._spicy_usd_arithmetic(Decimal('1000'), Decimal('0.0000000000000000000000000001'))
        assert format(total, 'f') == '1000.0000000000000000000000000001'
        assert total > Decimal('1000')


def test_actual_core_factory_concurrent_prepare_cannot_exceed_upload_batch_cap(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from lib import spicy_jobs, spicy_catalog
    from lib.spicy_uploads import upload_sources
    monkeypatch.setenv('SPICY_OFFLINE_FIXTURE_KEY', 'SYNTHETIC-NONWORKING-OFFLINE-KEY')
    binding = spicy_jobs.credential_binding('env:SPICY_OFFLINE_FIXTURE_KEY')
    http = _NativeJobsHTTPFixture(binding, b'')
    root = tmp_path / 'canonical-project'
    root.mkdir()
    inputs, _, contract = _actual_core_factory_fixture(root, monkeypatch, binding=binding, max_batches=1)
    spicy_catalog.observe_account(root, http, MODEL)
    second_inputs = copy.deepcopy(inputs)
    second_inputs['compiled_request_id'] = 'concurrent-preparation'
    # The original is held on its first source. The separately reviewed end
    # source has no upload journal yet, so only the combined batch cap stops it.
    second_inputs['input_assets'] = [{'role': 'first_frame',
        'source_path': inputs['input_assets'][1]['source_path']}]
    ticket_started, continue_ticket = Event(), Event()
    original_request = http.request_json
    def held_ticket(method, path, *args, **kwargs):
        if path == '/common/upload-url' and not ticket_started.is_set():
            ticket_started.set()
            assert continue_ticket.wait(10), 'Synthetic ticket release was not signaled'
        return original_request(method, path, *args, **kwargs)
    monkeypatch.setattr(http, 'request_json', held_ticket)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(upload_sources, root, inputs, client=http)
        try:
            assert ticket_started.wait(5), 'Original transfer never reached synthetic ticket transport'
            with pytest.raises(ValueError, match='max_batches'):
                upload_sources(root, second_inputs, client=http)
        finally:
            continue_ticket.set()
        assert first.result(timeout=10)['status'] == 'ready'
    assert len(http.uploads) == 2
    assert len(list((root / 'artifacts').glob('spicy_source_transfer-*.json'))) == 1
    assert not (root / 'production_attempts').exists()


def test_spicy_retained_billing_anomaly_freezes_new_dispatch(authority, monkeypatch):
    from lib import spicy_jobs
    root, inputs, native, quote, policy = authority
    monkeypatch.setattr(spicy_jobs, 'billing_anomalies', lambda *args, **kwargs: [
        {'kind': 'billing_anomaly', 'credential_digest': H, 'account_label': 'offline-fixture',
         'reasons': ['cost_exceeds_max_charge']}])
    with pytest.raises(ValueError, match='billing anomaly'):
        autonomy.verify_spicy_dispatch(root, inputs, native, policy=policy)
    assert list(root.iterdir()) == []


def test_spicy_registry_reports_native_code_without_configuring_an_account(monkeypatch):
    from lib import spicy_jobs, spicy_catalog
    from tools.tool_registry import ToolRegistry
    from tools.video.spicy_video import SpicyVideo
    from tools.base_tool import BaseTool
    def forbidden(*args, **kwargs):
        pytest.fail('Provider menu must not read credentials, probe or activate an account')
    monkeypatch.setattr(spicy_jobs, 'credential_binding', forbidden)
    monkeypatch.setattr(spicy_catalog, 'load_account_observation', forbidden)
    monkeypatch.setattr(autonomy, 'require_active_policy', forbidden)
    class ExistingAvailableTool(BaseTool):
        name = 'synthetic_existing_tool'
        provider = 'synthetic_existing'
        capability = 'video_generation'
        def execute(self, inputs):
            raise AssertionError('Menu never executes a provider')
    registry = ToolRegistry()
    registry.register(SpicyVideo())
    registry.register(ExistingAvailableTool())
    registry._discovered_packages.add('tools')
    bucket = registry.provider_menu()['video_generation']
    assert bucket['configured'] == 1 and bucket['total'] == 2
    assert [row['provider'] for row in bucket['available']] == ['synthetic_existing']
    entry = bucket['unavailable'][0]
    assert entry['provider'] == 'spicy' and entry['code_status'] == 'available'
    assert entry['production_available'] is False and entry['default_generation_enabled'] is False
    assert entry['account_readiness'] == 'unproven'
    assert entry['dispatch_readiness']['ready'] is False
    assert entry['native_routes'][0]['model'] == MODEL
    assert entry['native_routes'][0]['roles']['start_frame']['required'] is True
    assert entry['native_routes'][0]['roles']['timed_keyframe']['supported'] is False


def test_native_host_recovers_exact_spicy_collection_without_a_generation_envelope(tmp_path):
    import shutil
    import subprocess
    node = shutil.which('node')
    if node is None:
        pytest.skip('Native host bridge verification requires Node.js')
    step = {'status': 'mechanical', 'action': {'kind': 'collect_spicy_original',
            'action_id': 'SYNTHETIC-spicy-collection', 'subject': 'original-attempt',
            'binding': {'attempt_id': 'original-attempt', 'request_sha256': H}}, 'provider_calls': 0}
    completion_path = tmp_path / 'SYNTHETIC-completed-step.json'
    completion_path.write_text(json.dumps(step))
    bridge = Path(__file__).resolve().parents[2] / 'scripts/native_host_receipt.js'
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {canonicalProductionStep, consumeSavedNativeCompletion, consumeNativeHostAction} = require(process.argv[1]);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
(async () => {
  const completionPath = process.argv[2], step = JSON.parse(await fs.readFile(completionPath, 'utf8'));
  assert.equal(canonicalProductionStep(step), step);
  let reads = 0, nativeCalls = 0;
  const tools = {mcp__node_repl__js: async request => {
    reads++;
    let output;
    await new AsyncFunction('nodeRepl', request.code)({write: value => output = value});
    return {content: [{type:'text', text: output}]};
  }};
  const restored = await consumeSavedNativeCompletion(tools,
    {completion: {exit_code: 0, output: 'SYNTHETIC truncated stdout'}, completionPath});
  assert.deepEqual(restored.action, step.action);
  assert.equal(restored.status, step.status);
  assert.equal(restored.provider_calls, 0);
  assert.equal(reads, 1);
  await assert.rejects(consumeNativeHostAction(tools, {...step,
    host_envelope: {tool_name:'spicy_video', arguments:{model:'SYNTHETIC'}}}, {
    nativeTool: {tool_name:'spicy_video', invoke: async () => { nativeCalls++; }},
    receiptPath: completionPath + '.receipt', getObservation: async () => null,
    retainObservation: async () => {}, completeAction: async () => step,
  }), /exact emitted generation envelope/);
  assert.equal(nativeCalls, 0);
  assert.equal(reads, 1);
  process.stdout.write(JSON.stringify({action: restored.action, nativeCalls, reads}));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    observed = subprocess.run([node, '-e', script, str(bridge), str(completion_path)],
                              check=True, capture_output=True, text=True, timeout=30)
    result = json.loads(observed.stdout)
    assert result['action'] == step['action'] and result['nativeCalls'] == 0 and result['reads'] == 1
