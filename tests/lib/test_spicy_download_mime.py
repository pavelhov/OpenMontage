"""Offline expected-media-type tests for Spicy original collection."""
import socket
from pathlib import Path

import pytest

from lib import openart_download


class Response:
    status = 200
    def __init__(self, body, content_type):
        self.body = body
        self.content_type = content_type
    def getheader(self, name):
        return self.content_type if name == "Content-Type" else None
    def read(self, count):
        chunk, self.body = self.body[:count], self.body[count:]
        return chunk
    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch):
    calls, responses = [], []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))])
    class Connection:
        def __init__(self, host, address, timeout):
            calls.append(host)
        def request(self, method, target, headers):
            calls.append((method, target, headers))
        def getresponse(self):
            return responses.pop(0)
        def set_timeout(self, timeout):
            pass
        def close(self):
            pass
    monkeypatch.setattr(openart_download, "_PinnedHTTPSConnection", Connection)
    return calls, responses


def collect(root, **kwargs):
    return openart_download.collect_output(
        "https://fixture.r2.cloudflarestorage.com/result?signature=offline",
        root / "result.mp4", allowed_hosts={"fixture.r2.cloudflarestorage.com"},
        output_root=root, expected_mime="video/mp4", **kwargs)


def test_expected_mime_is_normalized_and_returned_only_when_requested(tmp_path, transport):
    calls, responses = transport
    responses.append(Response(b"synthetic-video", "Video/MP4; codecs=avc1"))
    result = collect(tmp_path)
    assert result["mime"] == "video/mp4"
    assert Path(result["path"]).read_bytes() == b"synthetic-video"


@pytest.mark.parametrize("content_type", [None, "application/octet-stream", "video/webm; codecs=vp9"])
def test_missing_or_wrong_mime_never_publishes(tmp_path, transport, content_type):
    calls, responses = transport
    responses.append(Response(b"synthetic-video", content_type))
    with pytest.raises(openart_download.OpenArtDownloadError):
        collect(tmp_path)
    assert not (tmp_path / "result.mp4").exists()
    assert [path.name for path in tmp_path.iterdir()] == []


def test_expected_mime_is_opt_in_and_preserves_old_result_shape(tmp_path, transport):
    calls, responses = transport
    responses.append(Response(b"existing-provider-result", "application/octet-stream"))
    result = openart_download.collect_output(
        "https://fixture.r2.cloudflarestorage.com/result",
        tmp_path / "result.bin", allowed_hosts={"fixture.r2.cloudflarestorage.com"},
        output_root=tmp_path)
    assert "mime" not in result
    assert Path(result["path"]).read_bytes() == b"existing-provider-result"


def test_finite_mime_reconciliation_retains_transport_type_and_validates_before_publish(tmp_path, transport):
    calls, responses = transport
    responses.append(Response(b"bounded-image-bytes", "image/jpeg"))
    observations = []
    result = openart_download.collect_output(
        "https://fixture.r2.cloudflarestorage.com/opaque-key.jpg", tmp_path / "image.png",
        allowed_hosts={"fixture.r2.cloudflarestorage.com"}, output_root=tmp_path,
        expected_mime="image/png", accepted_mimes={"image/png", "image/jpeg"},
        before_publish=lambda metadata: observations.append(metadata))
    assert result['mime'] == observations[0]['mime'] == 'image/jpeg'
    assert Path(observations[0]['publication_source']).name.startswith('.openart-download-')


def test_reconciliation_requires_validator_and_cannot_accept_outside_finite_types(tmp_path, transport):
    calls, responses = transport
    with pytest.raises(openart_download.OpenArtDownloadError, match='policy'):
        collect(tmp_path, accepted_mimes={'video/mp4'})
    assert not calls
    responses.append(Response(b'html', 'text/html'))
    with pytest.raises(openart_download.OpenArtDownloadError, match='media type'):
        collect(tmp_path, accepted_mimes={'video/mp4'}, before_publish=lambda metadata: pytest.fail('No unqualified body'))
    assert not list(tmp_path.iterdir())
