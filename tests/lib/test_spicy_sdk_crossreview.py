"""Independent official-SDK wire coverage; every transport is synthetic."""
import copy
import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from spicyapi import SpicyClient as OfficialClient

from lib import spicy_jobs as jobs


CASES = [
    ('get_balance', (), {}, 'GET', '/chat/credit', {}, None),
    ('get_model', ('vendor/image',), {}, 'GET', '/models/vendor%2Fimage', {}, None),
    ('list_models', (), {'modality': 'video', 'provider': 'Vendor', 'task': 'video-edit',
                        'search': 'literal search', 'include_schema': True},
     'GET', '/models', {'modality': ['video'], 'provider': ['Vendor'], 'task': ['video-edit'],
                        'search': ['literal search'], 'includeSchema': ['1']}, None),
    ('get_usage', (), {'from_date': '2026-10-01', 'to_date': '2026-10-08'},
     'GET', '/usage', {'from': ['2026-10-01'], 'to': ['2026-10-08']}, None),
    ('list_tasks', (), {'from_date': '2026-10-01', 'to_date': '2026-10-08', 'state': 'failed',
                       'model': 'vendor/video', 'limit': 17, 'cursor': 'opaque=='},
     'GET', '/jobs', {'from': ['2026-10-01'], 'to': ['2026-10-08'], 'state': ['failed'],
                     'model': ['vendor/video'], 'limit': ['17'], 'cursor': ['opaque==']}, None),
    ('get_task', ('original-task',), {}, 'GET', '/jobs/recordInfo', {'taskId': ['original-task']}, None),
    ('quote_task', (), {'model': 'vendor/video', 'input_data': {'prompt': 'Québec 雪',
                       'references': [{'weight': 0.5, 'enabled': False}]}},
     'POST', '/jobs/quote', {}, {'model': 'vendor/video', 'input': {'prompt': 'Québec 雪',
                               'references': [{'weight': 0.5, 'enabled': False}]}}),
    ('create_upload_url', (), {'content_type': 'image/png', 'byte_count': 17},
     'POST', '/common/upload-url', {}, {'contentType': 'image/png', 'bytes': 17}),
    ('commit_file', ('file-original',), {}, 'POST', '/files/file-original/commit', {}, None),
    ('create_download_url', ('task-original', 'outputs/image.png'), {},
     'POST', '/common/download-url', {}, {'taskId': 'task-original', 'key': 'outputs/image.png'}),
    ('create_download_url', ('task-original',), {},
     'POST', '/common/download-url', {}, {'taskId': 'task-original'}),
    ('purge_task', ('task-original',), {}, 'POST', '/jobs/purge', {}, {'taskId': 'task-original'}),
    ('list_assets', (), {'kind': 'image', 'source': ['upload', 'task_output'], 'q': 'literal search',
                        'favorite': False, 'collection_id': 'folder-id', 'sort': 'size',
                        'limit': 17, 'cursor': 'opaque=='},
     'GET', '/assets', {'kind': ['image'], 'source': ['upload,task_output'], 'q': ['literal search'],
                       'favorite': ['false'], 'collectionId': ['folder-id'], 'sort': ['size'],
                       'limit': ['17'], 'cursor': ['opaque==']}, None),
    ('get_asset', ('asset-original',), {}, 'GET', '/assets/asset-original', {}, None),
    ('asset_usage', (), {}, 'GET', '/assets/usage', {}, None),
    ('list_characters', (), {'asset_id': 'asset-original', 'limit': 17, 'cursor': 'opaque=='},
     'GET', '/assets/entities', {'assetId': ['asset-original'], 'limit': ['17'], 'cursor': ['opaque==']}, None),
    ('get_character', ('character-original',), {}, 'GET', '/assets/entities/character-original', {}, None),
]


@pytest.mark.parametrize('name,args,kwargs,method,path,query,body', CASES,
                         ids=[row[0] + '-' + str(index) for index, row in enumerate(CASES)])
def test_named_operations_use_installed_sdk_wire_contract(monkeypatch, name, args, kwargs, method, path, query, body):
    monkeypatch.setenv('SPICY_CROSSREVIEW_KEY', 'synthetic-crossreview-only')
    calls = []

    def transport(*arguments):
        calls.append(arguments)
        return 200, {}, b'{"code":200,"request_id":"req_crossreview","data":{"observed":true}}'

    client = jobs.SpicyClient('env:SPICY_CROSSREVIEW_KEY', transport=transport)
    assert isinstance(client.sdk, OfficialClient)
    assert client.sdk.max_retries == 0
    retained = copy.deepcopy((args, kwargs))
    result = getattr(client, name)(*args, **kwargs)
    assert result == {'data': {'observed': True}, 'request_id': 'req_crossreview', 'http_status': 200}
    assert (args, kwargs) == retained
    assert len(calls) == 1
    sent_method, url, headers, raw, timeout = calls[0]
    assert sent_method == method
    assert url.startswith(jobs.API_BASE + '/')
    assert urlsplit(url).path == '/api/v1' + path
    assert parse_qs(urlsplit(url).query) == query
    assert (json.loads(raw) if raw is not None else None) == body
    assert headers['Authorization'] == 'Bearer synthetic-crossreview-only'
    assert headers['User-Agent'] == 'spicyapi-python/0.3.0'
    assert 'Idempotency-Key' not in headers
    if body is not None:
        assert jobs.digest(json.loads(raw)) == jobs.digest(body)


@pytest.mark.parametrize('name,args,kwargs', [(row[0], row[1], row[2]) for row in CASES if row[3] == 'POST'])
def test_source_quote_handoff_and_purge_failures_never_resend(monkeypatch, name, args, kwargs):
    monkeypatch.setenv('SPICY_CROSSREVIEW_KEY', 'synthetic-crossreview-only')
    calls = []

    def transport(*arguments):
        calls.append(arguments)
        return 503, {'Retry-After': '7'}, b'{"code":50301,"msg":"private-provider-marker","data":{}}'

    client = jobs.SpicyClient('env:SPICY_CROSSREVIEW_KEY', transport=transport)
    with pytest.raises(jobs.SpicyTransportError) as error:
        getattr(client, name)(*args, **kwargs)
    assert error.value.kind == 'server_uncertain'
    assert 'private-provider-marker' not in str(error.value)
    assert len(calls) == 1


def _install_real_sdk_factory_transport(monkeypatch):
    """Replace only the fixture's service transport, keeping the actual client.

    Existing factory helpers supply synthetic authorizations, input images and
    local FFmpeg output. Their request recorder acts only as the mock server:
    canonical production receives a real engine SpicyClient and installed SDK.
    """
    from tests.lib import test_spicy_factory as factory
    service = factory._NativeJobsHTTPFixture
    clients = []

    class SDKFixture(jobs.SpicyClient, service):
        def __init__(self, binding, video_bytes):
            service.__init__(self, binding, video_bytes)
            self.sdk_wire_calls = []
            self.sdk_storage_calls = []
            jobs.SpicyClient.__init__(self, binding['credential_ref'], transport=self._wire,
                storage_transport=self._storage, qualified_storage_hosts=self.qualified_storage_hosts)
            assert self.binding['credential_digest'] == binding['credential_digest']
            assert isinstance(self.sdk, OfficialClient) and self.sdk.max_retries == 0
            clients.append(self)

        def _wire(self, method, url, headers, raw, timeout):
            self.sdk_wire_calls.append((method, url, dict(headers), raw))
            parsed = urlsplit(url)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            path = unquote(parsed.path.removeprefix('/api/v1'))
            try:
                envelope = self.request_json(method, path, json.loads(raw) if raw is not None else None,
                                             query=query or None, headers=headers)
            except jobs.SpicyTransportError:
                # The synthetic service accepted the first creation and lost its
                # reply. The actual SDK must surface one uncertain network call.
                raise OSError('synthetic accepted response lost') from None
            envelope = dict(envelope, request_id='req_canonical_sdk')
            return (202 if path == '/jobs/createTask' else 200), {}, json.dumps(envelope).encode()

        def _storage(self, method, url, headers, data):
            assert method == 'PUT'
            assert not any(name.lower() in {'authorization', 'cookie'} for name in headers)
            self.sdk_storage_calls.append((method, url, dict(headers), len(data)))
            result = service.put_presigned(self, url, data, headers)
            return result['status'], {}, b''

    monkeypatch.setattr(factory, '_NativeJobsHTTPFixture', SDKFixture)
    return clients


def _verify_canonical_sdk_roundtrip(clients, root):
    from lib import production_autonomy as autonomy, production_execution as execution, production_provenance
    assert len(clients) == 1
    client = clients[0]
    assert isinstance(client, jobs.SpicyClient) and isinstance(client.sdk, OfficialClient)
    creates = [call for call in client.sdk_wire_calls if urlsplit(call[1]).path.endswith('/jobs/createTask')]
    assert len(creates) == 2 and creates[0][:3] == creates[1][:3]
    body = json.loads(creates[0][3])
    # Canonical journal persistence can reorder input object members. The
    # vendor's idempotency contract ignores member order and whitespace; every
    # semantic value, the canonical digest and the exact key must stay fixed.
    assert body == json.loads(creates[1][3])
    assert jobs.digest(body) == jobs.digest(json.loads(creates[1][3]))
    assert set(body) == {'model', 'input', 'quoteId', 'expectedCost'}
    assert jobs.digest(body) == jobs.digest(client.creates[0][0])
    assert creates[0][2]['Idempotency-Key'] == client.creates[0][1]['Idempotency-Key']
    tickets = [call for call in client.sdk_wire_calls if urlsplit(call[1]).path.endswith('/common/upload-url')]
    commits = [call for call in client.sdk_wire_calls if urlsplit(call[1]).path.endswith('/commit')]
    assert len(tickets) == len(commits) == len(client.sdk_storage_calls) > 0
    assert any(urlsplit(call[1]).path.endswith('/jobs/quote') for call in client.sdk_wire_calls)
    assert any(urlsplit(call[1]).path.endswith('/common/download-url') for call in client.sdk_wire_calls)
    attempts = execution._attempts(root)
    assert len(attempts) == 1
    assert autonomy.root_attempt_counts(root, autonomy.require_active_policy(root)[0])['total'] == 1
    collected = jobs.validate_collected_original(root, attempts[0]['attempt_id'])
    assert collected['receipt']['submitted_arguments'] == body
    assert collected['receipt']['idempotency_key_sha256'] == jobs.digest(creates[0][2]['Idempotency-Key'])
    assert collected['receipt']['credential_binding']['credential_digest'] == client.binding['credential_digest']
    assert collected['receipt']['input_assets']
    proof = production_provenance.validate_attempt_provenance(root, attempts[0]['attempt_id'], shot_id='entry',
        story_revision=execution.load_shot_contract(root)['story_revision'], expected_output=collected['output'])
    assert proof['result']['output'] == collected['output']


def test_real_sdk_canonical_v1_counted_lost_reply_recovery_collection_and_provenance(tmp_path, monkeypatch):
    from tests.lib.test_spicy_factory import test_actual_core_factory_lost_create_response_recovers_one_original_and_selects
    clients = _install_real_sdk_factory_transport(monkeypatch)
    test_actual_core_factory_lost_create_response_recovers_one_original_and_selects(
        tmp_path, monkeypatch, settled_cost='0.10', parent_damage='deleted', dispatch_selector=False)
    _verify_canonical_sdk_roundtrip(clients, tmp_path / 'canonical-project')


@pytest.mark.parametrize('dispatch_selector', [False, True])
def test_real_sdk_canonical_v2_counted_lost_reply_recovery_collection_and_provenance(tmp_path, monkeypatch, dispatch_selector):
    from tests.lib.test_spicy_factory_v2 import test_actual_v2_roundtrip_preserves_counted_original_and_validates_delivery
    clients = _install_real_sdk_factory_transport(monkeypatch)
    test_actual_v2_roundtrip_preserves_counted_original_and_validates_delivery(
        tmp_path, monkeypatch, task_name='image-to-video', dispatch_selector=dispatch_selector)
    _verify_canonical_sdk_roundtrip(clients, tmp_path / 'project')
