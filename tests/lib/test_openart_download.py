"""Network-free proofs of result-collection trust boundaries."""
import hashlib
import socket
from pathlib import Path

import pytest

from lib import openart_download as download


@pytest.fixture(autouse=True)
def disable_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Download boundary tests must not use the network")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


class Response:
    def __init__(self, body=b"footage", status=200, headers=None, error=None):
        self.status, self.headers, self.body, self.error = status, headers or {}, body, error
    def getheader(self, name):
        return self.headers.get(name)
    def read(self, count):
        if self.error:
            raise self.error
        result, self.body = self.body[:count], self.body[count:]
        return result
    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch):
    calls, responses = [], []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))])
    class Connection:
        def __init__(self, host, address, timeout):
            calls.append({"host": host, "address": address, "timeout": timeout})
        def request(self, method, target, headers):
            calls[-1].update(method=method, target=target, headers=headers)
        def getresponse(self):
            return responses.pop(0)
        def close(self):
            pass
        def set_timeout(self, timeout):
            pass
    monkeypatch.setattr(download, "_PinnedHTTPSConnection", Connection)
    return calls, responses


def collect(tmp_path, url="https://cdn.example/clip?signature=secret", **kwargs):
    return download.collect_output(url, tmp_path / "clip.mp4", allowed_hosts={"cdn.example", "other.example"}, output_root=tmp_path, **kwargs)


def test_verified_file_redacts_signed_url(tmp_path, transport):
    calls, responses = transport
    responses.append(Response())
    result = collect(tmp_path, expected_sha256=hashlib.sha256(b"footage").hexdigest())
    assert Path(result["path"]).read_bytes() == b"footage"
    assert result["size"] == 7 and result["sha256"] == hashlib.sha256(b"footage").hexdigest()
    assert "secret" not in str(result) and "signature" not in str(result)
    assert calls[0]["address"][-1] == ("8.8.8.8", 443)
    assert calls[0]["host"] == "cdn.example"
    assert calls[0]["target"] == "/clip?signature=secret"
    assert calls[0]["headers"] == {"Accept-Encoding": "identity"}
    assert Path(result["path"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("url", ["http://cdn.example/a", "https://u:p@cdn.example/a", "https://cdn.example:444/a", "https://cdn.example/a#fragment", "https://evil.example/a", "https://cdn.example/a\n", "https://127.0.0.1/a"])
def test_url_boundary(tmp_path, transport, url):
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path, url)
    assert not transport[0]


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["169.254.1.1"], ["::1"], ["192.0.2.1"], ["::ffff:127.0.0.1"], ["64:ff9b::7f00:1"], ["64:ff9b:1::7f00:1"], ["2002:7f00:1::"], ["2001:0:4136:e378:8000:63bf:3fff:fdd2"], ["::7f00:1"]])
def test_all_dns_targets_must_be_public(tmp_path, transport, monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses])
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path)
    assert not transport[0]


@pytest.mark.parametrize("location", ["https://evil.example/a?token=secret", "https://127.0.0.1/a", "http://cdn.example/a"])
def test_redirect_requalification(tmp_path, transport, location):
    transport[1].append(Response(status=302, headers={"Location": location}))
    with pytest.raises(download.OpenArtDownloadError) as caught:
        collect(tmp_path)
    assert "secret" not in str(caught.value)
    assert len(transport[0]) == 1


def test_redirect_new_host_has_no_credentials(tmp_path, transport):
    transport[1].extend([Response(status=302, headers={"Location": "https://other.example/b"}), Response()])
    collect(tmp_path)
    assert transport[0][1]["target"] == "/b"
    assert transport[0][1]["headers"] == {"Accept-Encoding": "identity"}


@pytest.mark.parametrize("response,kwargs", [(Response(error=OSError("secret")), {}), (Response(body=b"too large"), {"max_bytes": 2}), (Response(), {"expected_sha256": "0" * 64}), (Response(headers={"Content-Length": "20"}), {}), (Response(body=b""), {})])
def test_failed_transfer_never_publishes(tmp_path, transport, response, kwargs):
    transport[1].append(response)
    with pytest.raises(download.OpenArtDownloadError) as caught:
        collect(tmp_path, **kwargs)
    assert "secret" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_path_escape_symlink_and_existing_preserved(tmp_path, transport):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    for target in [outside / "clip", tmp_path / "link" / "clip", tmp_path / ".." / "clip"]:
        with pytest.raises(download.OpenArtDownloadError):
            download.collect_output("https://cdn.example/a", target, allowed_hosts={"cdn.example"}, output_root=tmp_path)
    (tmp_path / "clip.mp4").write_bytes(b"original")
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path)
    assert (tmp_path / "clip.mp4").read_bytes() == b"original"
    assert not transport[0]


@pytest.mark.parametrize("kwargs", [{"max_bytes": True}, {"max_bytes": 0}, {"timeout": float("nan")}, {"timeout": True}, {"max_redirects": True}, {"max_redirects": -1}, {"expected_sha256": "bad"}])
def test_invalid_limits_fail_before_io(tmp_path, transport, kwargs):
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path, **kwargs)
    assert not transport[0]


def test_publication_race_keeps_other_writer(tmp_path, transport, monkeypatch):
    transport[1].append(Response())
    real_link = download.os.link
    def racing_link(src, dst, **kwargs):
        (tmp_path / "clip.mp4").write_bytes(b"other writer")
        return real_link(src, dst, **kwargs)
    monkeypatch.setattr(download.os, "link", racing_link)
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path)
    assert (tmp_path / "clip.mp4").read_bytes() == b"other writer"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip.mp4"]


def test_real_connection_pins_ip_and_verifies_original_tls_host(monkeypatch):
    events = []
    class RawSocket:
        def settimeout(self, value):
            events.append(("timeout", value))
        def connect(self, address):
            events.append(("connect", address))
        def close(self):
            events.append(("close",))
    class Context:
        verify_mode = download.ssl.CERT_REQUIRED
        check_hostname = True
        def wrap_socket(self, raw, *, server_hostname):
            events.append(("tls", server_hostname))
            return raw
    context = download.ssl.create_default_context()
    assert context.check_hostname is True
    assert context.verify_mode == download.ssl.CERT_REQUIRED
    monkeypatch.setattr(download.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(socket, "socket", lambda family, kind, protocol: RawSocket())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("Pinned connect must never resolve again"))
    connection = download._PinnedHTTPSConnection("cdn.example", (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)), 3)
    connection.connect()
    assert ("connect", ("8.8.8.8", 443)) in events
    assert ("tls", "cdn.example") in events
    connection.close()


def test_redirect_to_qualified_host_with_private_dns(tmp_path, transport, monkeypatch):
    def resolve(host, port, **kw):
        ip = "8.8.8.8" if host == "cdn.example" else "10.0.0.2"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    transport[1].append(Response(status=302, headers={"Location": "https://other.example/a"}))
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path)
    assert len(transport[0]) == 1


def test_redirect_limit_and_wildcards_fail_closed(tmp_path, transport):
    transport[1].append(Response(status=302, headers={"Location": "/b"}))
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path, max_redirects=0)
    with pytest.raises(download.OpenArtDownloadError):
        download.collect_output("https://cdn.example/a", tmp_path / "clip", output_root=tmp_path, allowed_hosts={"*.example"})


def test_output_root_symlink_rejected(tmp_path, transport):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(download.OpenArtDownloadError):
        download.collect_output("https://cdn.example/a", alias / "clip", output_root=alias, allowed_hosts={"cdn.example"})
    assert not transport[0]


@pytest.mark.parametrize("elapsed,expected", [(2.0, 1.0), (4.0, None)])
def test_tls_receives_only_remaining_deadline(monkeypatch, elapsed, expected):
    now, events = [10.0], []
    class RawSocket:
        def settimeout(self, value):
            events.append(("timeout", value))
        def connect(self, target):
            now[0] += elapsed
        def close(self):
            pass
    class Context:
        verify_mode = download.ssl.CERT_REQUIRED
        check_hostname = True
        def wrap_socket(self, raw, *, server_hostname):
            events.append(("tls", server_hostname))
            return raw
    monkeypatch.setattr(download.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(download.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(socket, "socket", lambda *a: RawSocket())
    connection = download._PinnedHTTPSConnection("cdn.example", (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)), 3.0)
    if expected is None:
        with pytest.raises(download.OpenArtDownloadError):
            connection.connect()
        assert not any(event[0] == "tls" for event in events)
    else:
        connection.connect()
        assert events[-2:] == [("timeout", expected), ("tls", "cdn.example")]
    connection.close()


def test_drip_reads_cannot_extend_deadline(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(download.time, "monotonic", lambda: now[0])
    class RawSocket:
        def settimeout(self, value):
            pass
        def recv_into(self, buffer):
            now[0] += 0.6
            buffer[0] = 65
            return 1
    reader = download._DeadlineReader(RawSocket(), 11.0)
    assert reader.readinto(bytearray(1)) == 1
    assert reader.readinto(bytearray(1)) == 1
    with pytest.raises(download.OpenArtDownloadError):
        reader.readinto(bytearray(1))


@pytest.mark.parametrize("host", ["127.0.0.1", "2130706433", "0177.0.0.1", "0x7f000001", "0x7f.0.0.0x1", "cdn.123", "cdn.0xabc"])
def test_numeric_hosts_rejected_before_resolution(tmp_path, transport, host):
    with pytest.raises(download.OpenArtDownloadError):
        download.collect_output("https://" + host + "/clip", tmp_path / "clip", allowed_hosts={host}, output_root=tmp_path)
    assert not transport[0]


def test_production_tls_context_disables_ambient_keylog(tmp_path, monkeypatch):
    monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "tls-secrets.log"))
    connection = download._PinnedHTTPSConnection("cdn.example", (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)), 3)
    assert connection._context.keylog_filename is None
    assert connection._context.check_hostname is True
    assert connection._context.verify_mode == download.ssl.CERT_REQUIRED
    connection.close()


def test_postpublication_sync_failure_preserves_verified_bytes(tmp_path, transport, monkeypatch):
    import stat
    transport[1].append(Response())
    real_fsync = download.os.fsync
    def failing_parent_sync(fd):
        if stat.S_ISDIR(download.os.fstat(fd).st_mode):
            raise OSError("signed-token-secret")
        real_fsync(fd)
    monkeypatch.setattr(download.os, "fsync", failing_parent_sync)
    with pytest.raises(download.OpenArtDownloadError) as caught:
        collect(tmp_path)
    assert "signed-token-secret" not in str(caught.value)
    published = tmp_path / "clip.mp4"
    assert published.read_bytes() == b"footage"
    assert list(tmp_path.iterdir()) == [published]
    with pytest.raises(download.OpenArtDownloadError):
        collect(tmp_path)
    assert published.read_bytes() == b"footage"
    assert len(transport[0]) == 1


@pytest.mark.parametrize("host", ["cdn.example", "cdn9.example", "0xcdn.example", "cdn.example.com"])
def test_ordinary_cdn_hosts_remain_qualified(tmp_path, transport, host):
    transport[1].append(Response())
    result = download.collect_output("https://" + host + "/clip", tmp_path / "clip", allowed_hosts={host}, output_root=tmp_path)
    assert result["source_host"] == host
    assert transport[0][0]["host"] == host
