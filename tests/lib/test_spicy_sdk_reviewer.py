"""Independent migration acceptance against the installed official SDK."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading
import traceback
from urllib.parse import parse_qs, urlsplit

import pytest
from spicyapi import SpicyClient as OfficialSpicyClient

from lib import spicy_jobs as jobs


def client(monkeypatch, transport):
    monkeypatch.setenv('SPICY_SDK_REVIEW_KEY', 'reviewer-synthetic-only')
    result = jobs.SpicyClient('env:SPICY_SDK_REVIEW_KEY', transport=transport)
    assert isinstance(result.sdk, OfficialSpicyClient)
    assert result.sdk.max_retries == 0
    assert not hasattr(result, 'request_json')
    return result


def test_explicit_replay_preserves_complete_unicode_body_key_and_digest(monkeypatch):
    calls = []
    body = {'model': 'synthetic/video', 'input': {'prompt': 'Québec 雪 ☃', 'native': {'n': [1, False, None]}},
            'quoteId': 'retained-original-quote', 'expectedCost': '0.030000000'}
    retained = copy.deepcopy(body)
    retained_digest = jobs.digest(body)

    def transport(*args):
        calls.append(args)
        return 202, {}, b'{"code":200,"request_id":"req_reviewer","data":{"taskId":"original"}}'

    bound = client(monkeypatch, transport)
    for _ in range(2):
        result = bound.create_task(model=body['model'], input_data=body['input'], quote_id=body['quoteId'],
                                   expected_cost=body['expectedCost'], idempotency_key='retained-original-key')
        assert result['data']['taskId'] == 'original'
    assert body == retained and jobs.digest(body) == retained_digest
    assert len(calls) == 2
    assert calls[0][3] == calls[1][3]
    for method, url, headers, raw, timeout in calls:
        assert method == 'POST' and urlsplit(url).path == '/api/v1/jobs/createTask'
        assert headers['Idempotency-Key'] == 'retained-original-key'
        assert headers['User-Agent'] == 'spicyapi-python/0.3.0'
        assert json.loads(raw) == retained and jobs.digest(json.loads(raw)) == retained_digest


def test_server_failure_cannot_turn_rejected_business_code_into_definite_rejection(monkeypatch):
    calls = []

    def transport(*args):
        calls.append(args)
        return 503, {'Retry-After': '7'}, json.dumps({'code': 40201, 'request_id': 'req_reviewer',
            'msg': 'private-provider-secret-and-signed-url', 'data': {}}).encode()

    bound = client(monkeypatch, transport)
    with pytest.raises(jobs.SpicyTransportError) as error:
        bound.create_task(model='synthetic/video', input_data={'prompt': 'fixture'}, quote_id='quote',
                          expected_cost='0.03', idempotency_key='retained-key')
    assert error.value.kind == 'server_uncertain'
    assert error.value.request_id == 'req_reviewer'
    assert error.value.retry_after == '7'
    assert error.value.code is None
    assert len(calls) == 1
    assert 'private-provider' not in str(error.value)


@pytest.mark.parametrize('failure', [OSError('private-review-marker'), TimeoutError('private-review-marker')])
def test_network_exceptions_are_single_attempt_and_redacted(monkeypatch, failure):
    calls = []

    def transport(*args):
        calls.append(args)
        raise failure

    bound = client(monkeypatch, transport)
    try:
        bound.get_balance()
    except jobs.SpicyTransportError:
        assert 'private-review-marker' not in traceback.format_exc()
    else:
        pytest.fail('uncertain network outcome must not succeed')
    assert len(calls) == 1


@pytest.mark.parametrize('data', [None, [], 'private-raw-data', 7, True])
def test_sdk_success_data_still_requires_a_workflow_object(monkeypatch, data):
    bound = client(monkeypatch, lambda *args: (200, {}, json.dumps({'code': 200,
        'request_id': 'req_reviewer', 'data': data}).encode()))
    with pytest.raises(jobs.SpicyTransportError) as error:
        bound.get_balance()
    assert error.value.kind == 'malformed_uncertain'
    assert error.value.request_id == 'req_reviewer'
    assert 'private-raw-data' not in str(error.value)


def test_shared_client_metadata_stays_with_its_own_sdk_response(monkeypatch):
    first_observed = threading.Event()
    second_observed = threading.Event()

    def transport(method, url, headers, body, timeout):
        task_id = parse_qs(urlsplit(url).query)['taskId'][0]
        status = 202 if task_id == 'first' else 200
        return status, {}, json.dumps({'code': 200, 'request_id': 'req_' + task_id,
                                      'data': {'taskId': task_id}}).encode()

    bound = client(monkeypatch, transport)
    original = bound.sdk.transport

    def overlap(method, url, headers, body, timeout):
        result = original(method, url, headers, body, timeout)
        if parse_qs(urlsplit(url).query)['taskId'][0] == 'first':
            first_observed.set()
            assert second_observed.wait(5)
        else:
            second_observed.set()
        return result

    bound.sdk.transport = overlap
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(bound.get_task, 'first')
        assert first_observed.wait(5)
        second = workers.submit(bound.get_task, 'second')
        first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    assert first_result == {'data': {'taskId': 'first'}, 'request_id': 'req_first', 'http_status': 202}
    assert second_result == {'data': {'taskId': 'second'}, 'request_id': 'req_second', 'http_status': 200}
