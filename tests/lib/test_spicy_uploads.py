"""Offline integration tests for source uploads through the real SpicyClient."""
from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from lib import spicy_jobs, spicy_uploads

HOST = "fixture-upload.r2.cloudflarestorage.com"
HOSTS = [HOST, "fixture-results.r2.cloudflarestorage.com"]
DIGEST = "a" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"SYNTHETIC_OFFLINE_IMAGE_BYTES"


def _now_stamp(minutes=30):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _client(monkeypatch, root, *, fail_put=False):
    calls = []
    put_count = 0

    def addresses(host, port, **kwargs):
        assert host == HOST
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", addresses)
    monkeypatch.setattr(spicy_jobs, "_credential_snapshot",
                        lambda ref: ({"credential_ref": ref, "credential_env": "SPICY_FIXTURE",
                                      "credential_digest": DIGEST}, "SYNTHETIC-NONWORKING-KEY"))

    def transport(method, url, headers, body, timeout=None):
        nonlocal put_count
        path = urlsplit(url).path
        calls.append((method, url, dict(headers), body))
        if method == "POST" and path.endswith("/common/upload-url"):
            params = json.loads(body)
            return 200, {}, json.dumps({"code": 200, "data": {
                "fileId": "fixture-file-1", "key": "spicy-production-inputs/fixture-file-1",
                "uploadUrl": f"https://{HOST}/upload/fixture?signature=SIGNED_FIXTURE_ONLY",
                "method": "PUT", "headers": {"Content-Type": params["contentType"]},
                "expiresAt": _now_stamp(), "maxBytes": params["bytes"]}}).encode()
        if method == "PUT":
            put_count += 1
            assert not any(name.lower() in {"authorization", "cookie", "proxy-authorization"} for name in headers)
            if fail_put:
                raise TimeoutError("SYNTHETIC offline timeout")
            return 200, {}, b""
        if method == "POST" and path.endswith("/files/fixture-file-1/commit"):
            assert body is None
            return 200, {}, json.dumps({"code": 200, "data": {
                "fileId": "fixture-file-1", "status": "ready", "bytes": len(PNG),
                "contentType": "image/png", "sha256": hashlib.sha256(PNG).hexdigest(),
                "uri": "spicy://f/fixture-file-1", "expiresAt": _now_stamp(60)}}).encode()
        raise AssertionError(f"unexpected offline fixture request {method} {path}")

    client = spicy_jobs.SpicyClient("env:SPICY_FIXTURE", transport=transport, storage_transport=transport,
                                    qualified_storage_hosts=HOSTS)
    return client, calls


def _fixture(tmp_path, monkeypatch, *, authority_mismatch=False, fail_put=False, duplicate_source=False):
    root = tmp_path.resolve()
    source = root / "approved.png"
    source.write_bytes(PNG)
    sha = hashlib.sha256(PNG).hexdigest()
    inputs = {"project_dir": str(root), "model": spicy_jobs.catalog.VIDEO_MODEL,
              "mode": "image_to_video", "account_label": "synthetic-account",
              "credential_ref": "env:SPICY_FIXTURE", "compiled_request_id": "fixture-preparation",
              "governance": {"shot_id": "fixture-shot"}, "image_path": str(source)}
    if duplicate_source:
        inputs["last_image_path"] = str(source)
    reservations = []

    def reserve(project_dir, request):
        reservations.append(dict(request))
        authorized = []
        for role, value, _ in spicy_jobs._source_rows(request):
            path = spicy_jobs._resolve_source(value, root)
            bytes_value = path.read_bytes()
            authorized.append({"role": role, "path": str(path),
                              "sha256": ("f" * 64 if authority_mismatch else hashlib.sha256(bytes_value).hexdigest()),
                              "bytes": len(bytes_value)})
        return {"batch_id": "fixture-batch-" + str(request.get("compiled_request_id")),
                "account_label": "synthetic-account", "credential_ref": "env:SPICY_FIXTURE",
                "credential_digest": DIGEST, "qualified_storage_hosts": sorted(HOSTS),
                "files": authorized}

    monkeypatch.setattr("lib.production_autonomy.reserve_spicy_source_transfer", reserve)
    client, calls = _client(monkeypatch, root, fail_put=fail_put)
    return root, source, inputs, client, calls, reservations, sha


def test_upload_uses_real_client_and_never_persists_signed_url(tmp_path, monkeypatch):
    root, source, inputs, client, calls, reservations, sha = _fixture(tmp_path, monkeypatch)
    result = spicy_uploads.upload_sources(root, inputs, client=client)
    assert result["status"] == "ready"
    assert result["uploaded"] == ["first_frame"]
    receipt = result["sources"]["first_frame"]
    assert receipt["source_sha256"] == sha and receipt["uri"] == "spicy://f/fixture-file-1"
    assert len(reservations) == 1
    assert [urlsplit(call[1]).path.rsplit("/", 1)[-1] for call in calls] == ["upload-url", "fixture", "commit"]
    assert calls[0][2]["Authorization"] == "Bearer SYNTHETIC-NONWORKING-KEY"
    assert "Authorization" not in calls[1][2]
    assert calls[0][3] == json.dumps({"contentType": "image/png", "bytes": len(PNG)}, separators=(",", ":")).encode()
    journal_text = " ".join(path.read_text() for path in (root / "artifacts").glob("spicy_upload_attempt-*.json"))
    assert "SIGNED_FIXTURE_ONLY" not in journal_text
    assert "uploadUrl" not in journal_text and "Authorization" not in journal_text


def test_committed_same_credential_cache_skips_new_reservation_and_network(tmp_path, monkeypatch):
    root, source, inputs, client, calls, reservations, _ = _fixture(tmp_path, monkeypatch)
    first = spicy_uploads.upload_sources(root, inputs, client=client)
    call_count = len(calls)
    second = spicy_uploads.upload_sources(root, inputs, client=client)
    assert first["sources"] == second["sources"]
    assert len(calls) == call_count == 3
    assert len(reservations) == 1


def test_same_bytes_in_first_and_last_roles_use_one_transfer_and_keep_role_mapping(tmp_path, monkeypatch):
    root, source, inputs, client, calls, reservations, _ = _fixture(
        tmp_path, monkeypatch, duplicate_source=True)
    result = spicy_uploads.upload_sources(root, inputs, client=client)
    assert set(result["sources"]) == {"first_frame", "last_frame"}
    assert result["sources"]["first_frame"]["uri"] == result["sources"]["last_frame"]["uri"]
    assert result["sources"]["first_frame"]["role"] == "first_frame"
    assert result["sources"]["last_frame"]["role"] == "last_frame"
    assert result["sources"]["first_frame"]["source_path"] == result["sources"]["last_frame"]["source_path"]
    assert result["uploaded"] == ["first_frame"]
    assert sum(urlsplit(call[1]).path.endswith("/common/upload-url") for call in calls) == 1
    assert len(reservations) == 1


def test_expired_original_commit_fails_closed_without_reticket(tmp_path, monkeypatch):
    root, source, inputs, client, calls, reservations, _ = _fixture(tmp_path, monkeypatch)
    spicy_uploads.upload_sources(root, inputs, client=client)
    expired_now = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(spicy_uploads, "_now", lambda: expired_now)
    monkeypatch.setattr(spicy_jobs, "_now", lambda: expired_now)
    changed_batch = dict(inputs, compiled_request_id="new-batch")
    with pytest.raises(spicy_jobs.SpicyError, match="[Oo]riginal Spicy upload receipt is expired"):
        spicy_uploads.upload_sources(root, changed_batch, client=client)
    assert sum(urlsplit(call[1]).path.endswith("/common/upload-url") for call in calls) == 1
    assert len(reservations) == 2


def test_uncertain_put_blocks_reticket_even_under_new_transfer_batch(tmp_path, monkeypatch):
    root, source, inputs, client, calls, reservations, _ = _fixture(tmp_path, monkeypatch, fail_put=True)
    with pytest.raises(spicy_jobs.SpicyError, match="outcome unknown"):
        spicy_uploads.upload_sources(root, inputs, client=client)
    changed_batch = dict(inputs, compiled_request_id="different-authorized-batch")
    with pytest.raises(spicy_jobs.SpicyError, match="[Uu]nresolved original"):
        spicy_uploads.upload_sources(root, changed_batch, client=client)
    assert sum(urlsplit(call[1]).path.endswith("/common/upload-url") for call in calls) == 1
    assert sum(urlsplit(call[1]).path.endswith("/upload/fixture") for call in calls) == 1
    assert len(reservations) == 1


def test_source_hash_must_match_reserved_authority_before_any_ticket(tmp_path, monkeypatch):
    root, source, inputs, client, calls, reservations, _ = _fixture(
        tmp_path, monkeypatch, authority_mismatch=True)
    with pytest.raises(spicy_jobs.SpicyError, match="differ from reserved transfer authority"):
        spicy_uploads.upload_sources(root, inputs, client=client)
    assert not calls and len(reservations) == 1


def test_client_host_set_must_match_authority_before_any_ticket(tmp_path, monkeypatch):
    root, source, inputs, client, calls, reservations, _ = _fixture(tmp_path, monkeypatch)
    client.qualified_storage_hosts = frozenset(["different.r2.cloudflarestorage.com"])
    with pytest.raises(spicy_jobs.SpicyError, match="differs from retained authority"):
        spicy_uploads.upload_sources(root, inputs, client=client)
    assert not calls and len(reservations) == 1
