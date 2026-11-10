"""Installed official SDK + engine guarded byte transport, against loopback fixtures only.

Scope (kept disjoint from test_spicy_sdk_reviewer.py and test_spicy_sdk_crossreview.py):
the engine-owned network policy around the SDK (fixed origin, no redirects, no ambient
proxies, bounded response), SDK pin enforcement and the storage-transport refusal.
"""
import http.server
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="official spicyapi SDK requires Python 3.11+")

from lib import spicy_jobs as jobs  # noqa: E402

HOST = 'fixture-account.r2.cloudflarestorage.com'


class _Server:
    def __init__(self, responder):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get('Content-Length') or 0)
                outer.requests.append((self.command, self.path, dict(self.headers.items()), self.rfile.read(length)))
                status, headers, body = responder(self.command, self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = 'http://127.0.0.1:%d/api/v1' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def loopback(monkeypatch):
    servers = []

    def make(responder):
        server = _Server(responder)
        servers.append(server)
        # Only the module origin moves to loopback; the SDK and guarded transport are real.
        monkeypatch.setattr(jobs, 'API_BASE', server.base)
        return server
    yield make
    for server in servers:
        server.close()


def _client(monkeypatch, transport=None, **kwargs):
    monkeypatch.setenv('SPICY_SDK_INTEGRATION_KEY', 'integration-synthetic-only')
    return jobs.SpicyClient('env:SPICY_SDK_INTEGRATION_KEY', transport=transport, **kwargs)


def test_guarded_transport_refuses_non_api_origin_before_any_socket(monkeypatch):
    def no_socket(*args, **kwargs):
        raise AssertionError('socket attempted')
    monkeypatch.setattr('socket.create_connection', no_socket)
    for url in ('https://evil.example/api/v1/models', 'https://api.spicyapi.ai.evil.example/api/v1/x',
                jobs.API_BASE + 'x', None):
        with pytest.raises(jobs.SpicyError) as error:
            jobs._guarded_transport('GET', url, {}, None, 1.0)
        assert error.value.kind == 'invalid_api_origin'


def test_real_sdk_over_guarded_loopback_transport_end_to_end(monkeypatch, loopback):
    server = loopback(lambda method, path: (200, {'X-Request-Id': 'req_loop1'},
                      b'{"code":200,"request_id":"req_loop1","data":{"available":"1.00"}}'))
    result = _client(monkeypatch).get_balance()
    assert result['data'] == {'available': '1.00'}
    assert len(server.requests) == 1
    method, path, headers, _ = server.requests[0]
    assert method == 'GET' and path.startswith('/api/v1/')
    assert headers['Authorization'] == 'Bearer integration-synthetic-only'


def test_redirect_is_not_followed_and_credential_never_reaches_target(monkeypatch, loopback):
    def responder(method, path):
        if path.startswith('/api/v1/elsewhere'):
            return 200, {}, b'{"code":200,"data":{"available":"9"}}'
        return 302, {'Location': '/api/v1/elsewhere'}, b''
    server = loopback(responder)
    with pytest.raises(jobs.SpicyError):
        _client(monkeypatch).get_balance()
    assert len(server.requests) == 1


def test_ambient_proxy_environment_is_ignored(monkeypatch, loopback):
    server = loopback(lambda method, path: (200, {}, b'{"code":200,"data":{"available":"1"}}'))
    for name in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY'):
        monkeypatch.setenv(name, 'http://127.0.0.1:9')
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    assert _client(monkeypatch).get_balance()['data'] == {'available': '1'}
    assert len(server.requests) == 1


def test_oversized_response_is_bounded_and_uncertain(monkeypatch, loopback):
    big = b'{"code":200,"data":{"pad":"' + b'x' * (jobs.MAX_API_RESPONSE_BYTES + 64) + b'"}}'
    server = loopback(lambda method, path: (200, {}, big))
    raw = jobs._guarded_transport('GET', jobs.API_BASE + '/chat/credit', {}, None, 5.0)[2]
    assert len(raw) == jobs.MAX_API_RESPONSE_BYTES + 1
    with pytest.raises(jobs.SpicyTransportError) as error:
        _client(monkeypatch).get_balance()
    assert error.value.kind == 'malformed_uncertain'
    assert len(server.requests) == 2


def test_upload_ticket_server_failure_is_one_attempt(monkeypatch, loopback):
    server = loopback(lambda method, path: (503, {'Retry-After': '1'}, b'{"code":503,"msg":"busy","data":{}}'))
    with pytest.raises(jobs.SpicyError):
        _client(monkeypatch).create_upload_url(content_type='image/png', byte_count=10)
    assert len(server.requests) == 1


def test_pinned_sdk_version_is_enforced(monkeypatch):
    import spicyapi
    monkeypatch.setattr(spicyapi, '__version__', '0.3.1')
    with pytest.raises(jobs.SpicyError) as error:
        _client(monkeypatch, transport=lambda *a: (200, {}, b'{}'))
    assert error.value.kind == 'sdk_unavailable'


def test_injected_sdk_transport_without_storage_transport_refuses_real_storage(monkeypatch):
    calls = []
    client = _client(monkeypatch, transport=lambda *a: calls.append(a) or (200, {}, b'{}'),
                     qualified_storage_hosts={HOST})
    with pytest.raises(jobs.SpicyError) as error:
        client.put_presigned('https://' + HOST + '/bucket/file?X-Amz-Signature=secret', b'data',
                             {'Content-Type': 'image/png'})
    assert error.value.kind == 'storage_transport_unconfigured'
    assert calls == []
