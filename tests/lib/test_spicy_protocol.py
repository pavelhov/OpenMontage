"""Offline HTTP boundary tests for the native Spicy client and quote parser."""
import json

import pytest

from lib import spicy_jobs


def client(monkeypatch, transport):
    monkeypatch.setenv("SPICY_PROTOCOL_TEST_KEY", "synthetic-offline-only")
    return spicy_jobs.SpicyClient("env:SPICY_PROTOCOL_TEST_KEY", transport=transport)


def test_success_requires_http_success_code_200_and_object_data(monkeypatch):
    for status, code, data in [(200, 0, {}), (200, 202, {}), (202, 200, None), (200, 200, [])]:
        c = client(monkeypatch, lambda *args, s=status, n=code, d=data: (s, {}, json.dumps({"code": n, "data": d}).encode()))
        with pytest.raises(spicy_jobs.SpicyError):
            c.get_balance()


def test_success_normalizes_metadata_and_drops_provider_message(monkeypatch):
    c = client(monkeypatch, lambda *args: (200, {"Retry-After": "15", "X-Request-Id": "unsafe"},
            b'{"code":200,"msg":"secret-like message","request_id":"req_fixture-1","data":{}}'))
    result = c.get_balance()
    assert result == {"data": {}, "request_id": "req_fixture-1", "http_status": 200}


def test_decimal_quote_preserves_small_fixed_point_amount():
    value = spicy_jobs._decimal("0.00000001", "fixture")
    assert format(value, "f") == "0.00000001"
    with pytest.raises(spicy_jobs.SpicyError):
        spicy_jobs._decimal("1E-8", "fixture")
    with pytest.raises(spicy_jobs.SpicyError):
        spicy_jobs._decimal(1.25, "fixture")


def test_sdk_requests_stay_on_fixed_api_origin(monkeypatch):
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append(url)
        return 200, {}, b'{"code":200,"msg":"success","data":{"taskId":"t"}}'
    c = client(monkeypatch, transport)
    c.get_task("../../evil.example/x?y=1")
    assert calls and all(url.startswith(spicy_jobs.API_BASE + "/") for url in calls)
    assert "evil.example/x?" not in calls[0]
    with pytest.raises(spicy_jobs.SpicyError) as error:
        spicy_jobs.SpicyClient("env:SPICY_PROTOCOL_TEST_KEY", transport=transport, base_url="https://evil.example/api/v1")
    assert error.value.kind == "invalid_api_origin"


def test_qualified_upload_uses_injected_storage_and_derived_content_length(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))])
    calls = []
    def mocked(method, url, headers, body):
        calls.append((method, url, headers, body))
        return 200, {}, b""
    monkeypatch.setenv("SPICY_PROTOCOL_TEST_KEY", "synthetic-offline-only")
    c = spicy_jobs.SpicyClient("env:SPICY_PROTOCOL_TEST_KEY", storage_transport=mocked,
                               qualified_storage_hosts=["fixture.r2.cloudflarestorage.com"])
    result = c.put_presigned("https://fixture.r2.cloudflarestorage.com/path?signature=fixture",
                             b"fixture-bytes", {"Content-Type": "image/png"})
    assert result == {"status": 200}
    assert calls[0][0] == "PUT"
    assert calls[0][2] == {"content-type": "image/png", "content-length": "13"}
    assert calls[0][3] == b"fixture-bytes"
