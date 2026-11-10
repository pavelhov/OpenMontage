"""Once-only, authority-bound Spicy source uploads.

Upload tickets are deliberately not retained: they are signed credentials. A
process interruption after a ticket request, PUT, or commit becomes an explicit
uncertain source transfer and requires reconciliation rather than a fresh ticket.
Only a durably proven local ticket rejection before PUT can be explicitly closed;
a fresh transfer still consumes a different authorized batch.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from lib.spicy_catalog import atomic_write_json, parse_utc

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
UPLOAD_SAFETY_SECONDS = 120


def _fail(message: str, kind: str = "upload_authority") -> None:
    from lib.spicy_jobs import SpicyError
    raise SpicyError(message, kind=kind)


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    _fail("Spicy source must be a PNG, JPEG, or WebP image", "source_mime_invalid")


def _safe_error(exc: Exception) -> dict[str, Any]:
    from lib.spicy_jobs import SpicyError
    if isinstance(exc, SpicyError):
        safe = exc.as_dict()
        return {key: safe.get(key) for key in ("kind", "code", "http_status", "request_id", "retry_after")
                if safe.get(key) is not None}
    return {"kind": type(exc).__name__[:80]}


def _journal_path(root: Path, sha: str, credential_digest: str) -> Path:
    # Stable per source+credential, not per preparation batch: a new batch can
    # never create a second upload while an older ticket/PUT/commit is uncertain.
    return root / "artifacts" / f"spicy_upload_attempt-{sha}-{credential_digest}.json"


@contextmanager
def _source_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or stat.S_IMODE(lock_stat.st_mode) != 0o600:
            _fail("Spicy source journal lock is not a regular file", "upload_journal_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_journal(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _fail("Spicy source upload journal is unreadable", "upload_journal_invalid")
    if not isinstance(value, dict):
        _fail("Spicy source upload journal is malformed", "upload_journal_invalid")
    return value


def _receipt_from_journal(path: Path, expected: dict, receipt_path: Path) -> dict | None:
    journal = _read_journal(path)
    if journal is None:
        return None
    journal_binding = journal.get("binding")
    identity_fields = set(expected) - {"role", "path"}
    if (not isinstance(journal_binding, dict)
            or any(journal_binding.get(key) != expected.get(key) for key in identity_fields)):
        _fail("Spicy source upload journal belongs to different source authority", "upload_journal_mismatch")
    if journal.get("state") != "committed" or not isinstance(journal.get("receipt"), dict):
        _fail("Spicy source upload has an unresolved original transfer; do not reticket", "upload_original_unresolved")
    receipt = journal["receipt"]
    expires = parse_utc(receipt.get("expires_at"))
    if expires is None or expires <= _now() + timedelta(seconds=UPLOAD_SAFETY_SECONDS):
        _fail("Original Spicy upload receipt is expired; a new transfer requires fresh authority", "upload_receipt_expired")
    if (receipt.get("source_sha256") != expected["source_sha256"]
            or receipt.get("credential_digest") != expected["credential_digest"]
            or not isinstance(receipt.get("uri"), str)
            or not re.fullmatch(r"spicy://f/[A-Za-z0-9._/-]{1,500}", receipt["uri"])):
        _fail("Original Spicy upload receipt is malformed", "upload_journal_invalid")
    try:
        retained = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        retained = None
    except (OSError, ValueError):
        _fail("Spicy source receipt is unreadable", "upload_receipt_invalid")
    if retained is not None and retained != receipt:
        _fail("Spicy source receipt differs from its original commit journal", "upload_receipt_invalid")
    if retained is None:
        atomic_write_json(receipt_path, receipt)
    return receipt


def _for_source(receipt: dict, source: dict) -> dict:
    """Project a content-addressed receipt onto this authorized logical source."""
    return {**receipt, "role": source["role"], "source_path": source["path"]}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _write_state(path: Path, state: dict) -> None:
    atomic_write_json(path, state)


def upload_sources(project_dir, inputs: dict, *, client) -> dict[str, Any]:
    """Upload approved local source images once and retain verified spicy:// receipts.

    The standing source-transfer batch is reserved by the canonical autonomy
    owner before any upload API call. Per-source journals survive failures and
    deliberately prohibit automatic ticket/PUT/commit replay.
    """
    from lib import production_autonomy as autonomy
    from lib.spicy_jobs import (MAX_UPLOAD_BYTES as _JOB_MAX, _credential_snapshot,
                                _resolve_source, _root, _source_rows,
                                upload_receipt_path)
    from lib.spicy_jobs import SpicyError
    from lib.spicy_jobs import _offline_guard
    _offline_guard()
    if not isinstance(inputs, dict):
        _fail("Spicy upload inputs must be an object")
    root = Path(project_dir).resolve()
    if _root(inputs) != root:
        _fail("Spicy upload project root differs from the request", "project_root_mismatch")
    rows = _source_rows(inputs)
    if not rows:
        _fail("Spicy upload has no local source images", "missing_source")

    # Freeze the exact bytes before reserving authority; this snapshot is the
    # only byte string that may be sent to storage.
    sources = []
    for role, value, declared in rows:
        source = _resolve_source(value, root)
        limit = min(MAX_UPLOAD_BYTES, _JOB_MAX)
        before_size = source.stat().st_size
        if before_size <= 0 or before_size > limit:
            _fail("Spicy source is empty or exceeds the upload limit", "source_size_invalid")
        with source.open("rb") as stream:
            data = stream.read(limit + 1)
        if not data or len(data) > limit or len(data) != before_size or source.stat().st_size != before_size:
            _fail("Spicy source changed or exceeds the upload limit while being read", "source_size_invalid")
        sha = hashlib.sha256(data).hexdigest()
        if declared is not None and declared != sha:
            _fail("Spicy source differs from its declared SHA-256", "source_hash_mismatch")
        sources.append({"role": role, "path": str(source), "source_sha256": sha,
                        "bytes": len(data), "content_type": _mime(data), "data": data})

    binding, _ = _credential_snapshot(inputs.get("credential_ref"))
    if getattr(client, "binding", None) != binding:
        _fail("Spicy upload client credential differs from request", "credential_mismatch")
    client_hosts = getattr(client, "qualified_storage_hosts", None)
    if not isinstance(client_hosts, (set, frozenset)) or not client_hosts:
        _fail("Spicy upload requires explicit qualified storage hosts", "storage_host_unqualified")

    from lib.spicy_jobs import _usable_receipt
    cached = {}
    for source in sources:
        receipt = _usable_receipt(root, source["source_sha256"], binding["credential_digest"])
        if receipt is not None:
            if (receipt.get("source_sha256") != source["source_sha256"]
                    or receipt.get("credential_digest") != binding["credential_digest"]
                    or not isinstance(receipt.get("uri"), str)
                    or not re.fullmatch(r"spicy://f/[A-Za-z0-9._/-]{1,500}", receipt["uri"])
                    or receipt.get("bytes", source["bytes"]) != source["bytes"]
                    or receipt.get("content_type", source["content_type"]) != source["content_type"]):
                _fail("Cached Spicy source receipt does not match frozen source bytes", "upload_receipt_invalid")
            cached[source["role"]] = _for_source(receipt, source)
    if len(cached) == len(sources):
        return {"provider": "spicy", "status": "ready", "uploaded": [], "sources": cached}

    # This call validates active account, fresh schema, exact route, standing
    # transfer authority and the durable canonical batch cap before any ticket.
    authority = autonomy.reserve_spicy_source_transfer(root, inputs)
    if (authority.get("credential_digest") != binding["credential_digest"]
            or authority.get("credential_ref") != binding["credential_ref"]
            or authority.get("account_label") != inputs.get("account_label")
            or authority.get("qualified_storage_hosts") != sorted(client_hosts)):
        _fail("Spicy source transfer client differs from retained authority", "upload_authority_mismatch")
    authorized = sorted(({"role": row.get("role"), "path": row.get("path"), "sha256": row.get("sha256"),
                          "bytes": row.get("bytes")} for row in authority.get("files", [])),
                        key=lambda row: (row["role"] or "", row["path"] or ""))
    observed = sorted(({"role": source["role"], "path": source["path"], "sha256": source["source_sha256"],
                        "bytes": source["bytes"]} for source in sources),
                      key=lambda row: (row["role"], row["path"]))
    if authorized != observed:
        _fail("Spicy source bytes differ from reserved transfer authority", "upload_source_mismatch")

    results = dict(cached)
    uploaded_roles = []
    for source in sources:
        if source["role"] in cached:
            continue
        sha = source["source_sha256"]
        receipt_path = upload_receipt_path(root, sha)
        jp = _journal_path(root, sha, binding["credential_digest"])
        expected = {"role": source["role"], "path": source["path"],
                    "source_sha256": sha, "bytes": source["bytes"], "content_type": source["content_type"],
                    "credential_ref": binding["credential_ref"], "credential_digest": binding["credential_digest"],
                    "account_label": authority["account_label"],
                    "qualified_storage_hosts": authority["qualified_storage_hosts"]}
        with _source_lock(jp):
            recovered = _receipt_from_journal(jp, expected, receipt_path)
            if recovered is not None:
                results[source["role"]] = _for_source(recovered, source)
                continue
            state = {"version": "1.0", "batch_id": authority["batch_id"],
                     "binding": expected, "state": "prepared"}
            _write_state(jp, state)

            # Persist the non-replayable phase before making each request. No URL,
            # authorization header, or ticket header set enters this journal.
            state["state"] = "ticket_requesting"
            _write_state(jp, state)
            durable_commit = False
            try:
                ticket_response = client.create_upload_url(content_type=source["content_type"],
                                                           byte_count=source["bytes"])
                ticket = ticket_response.get("data")
                now = _now()
                expiry = parse_utc(ticket.get("expiresAt")) if isinstance(ticket, dict) else None
                if (not isinstance(ticket, dict) or not isinstance(ticket.get("fileId"), str)
                        or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", ticket["fileId"])
                        or not isinstance(ticket.get("key"), str) or not ticket["key"]
                        or len(ticket["key"]) > 1024 or any(ord(char) < 32 for char in ticket["key"])
                        or ticket.get("method") != "PUT" or not isinstance(ticket.get("uploadUrl"), str)
                        or not isinstance(ticket.get("headers"), dict)
                        or type(ticket.get("maxBytes")) is not int or ticket["maxBytes"] < source["bytes"]
                        or expiry is None or expiry <= now + timedelta(seconds=5)):
                    _fail("Spicy upload ticket is malformed or expired", "upload_ticket_invalid")
                headers = ticket["headers"]
                content_type = next((v for k, v in headers.items()
                                     if isinstance(k, str) and k.lower() == "content-type"), None)
                if content_type != source["content_type"]:
                    _fail("Spicy upload ticket content type differs from frozen source", "upload_ticket_invalid")
                state.update(state="ticket_received", file_id=ticket["fileId"],
                             ticket_expires_at=ticket["expiresAt"])
                _write_state(jp, state)
                state["state"] = "put_started"
                _write_state(jp, state)
                put_result = client.put_presigned(ticket["uploadUrl"], source["data"], headers)
                if not isinstance(put_result, dict) or type(put_result.get("status")) is not int or not 200 <= put_result["status"] < 300:
                    _fail("Spicy storage PUT was not confirmed", "upload_put_unconfirmed")
                state["state"] = "put_complete"
                _write_state(jp, state)
                state["state"] = "commit_started"
                _write_state(jp, state)
                commit_response = client.commit_file(ticket["fileId"])
                committed = commit_response.get("data")
                commit_expiry = parse_utc(committed.get("expiresAt")) if isinstance(committed, dict) else None
                if (not isinstance(committed, dict) or committed.get("fileId") != ticket["fileId"]
                        or committed.get("status") != "ready" or committed.get("bytes") != source["bytes"]
                        or committed.get("contentType") != source["content_type"]
                        or committed.get("sha256") != sha
                        or not isinstance(committed.get("uri"), str)
                        or not re.fullmatch(r"spicy://f/[A-Za-z0-9._/-]{1,500}", committed["uri"])
                        or commit_expiry is None or commit_expiry <= _now() + timedelta(seconds=UPLOAD_SAFETY_SECONDS)):
                    _fail("Spicy upload commit receipt failed byte, type, hash or expiry validation", "upload_commit_invalid")
                receipt = {"source_sha256": sha, "credential_digest": binding["credential_digest"],
                           "uri": committed["uri"], "expires_at": committed["expiresAt"],
                           "file_id": committed["fileId"], "bytes": committed["bytes"],
                           "content_type": committed["contentType"], "role": source["role"],
                           "batch_id": authority["batch_id"]}
                state.update(state="committed", receipt=receipt)
                _write_state(jp, state)
                durable_commit = True
                atomic_write_json(receipt_path, receipt)
                results[source["role"]] = _for_source(receipt, source)
                uploaded_roles.append(source["role"])
            except Exception as exc:
                # Keep the first nonterminal phase and classify it without raw
                # provider text. Any failure blocks silent ticket/PUT replay.
                if not durable_commit:
                    state.update(state="uncertain", failure=_safe_error(exc))
                    _write_state(jp, state)
                elif not isinstance(exc, SpicyError):
                    _fail("Spicy commit is verified but local receipt recovery is required", "upload_receipt_write_failed")
                if isinstance(exc, SpicyError):
                    raise
                _fail("Spicy source transfer outcome is uncertain; preserve its original journal", "upload_uncertain")
    return {"provider": "spicy", "status": "ready", "uploaded": uploaded_roles, "sources": results}


# V2 is deliberately separate: legacy receipts, digests, journals and execution
# above retain their original byte behavior. Typed source receipts are keyed by
# content, credential and media kind; logical pointers stay in native authority.
def _v2_paths(root, sha, credential_digest, media_kind):
    if (not re.fullmatch(r'[a-f0-9]{64}', sha) or not re.fullmatch(r'[a-f0-9]{64}', credential_digest)
            or media_kind not in ('image', 'video', 'audio')):
        _fail('Spicy v2 source receipt identity is invalid', 'upload_receipt_invalid')
    directory = Path(root) / 'artifacts'
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        _fail('Spicy v2 source artifact directory is unsafe', 'upload_receipt_invalid')
    stem = f'{sha}-{credential_digest}-{media_kind}.json'
    return directory / ('spicy_upload_v2-' + stem), directory / ('spicy_upload_v2_attempt-' + stem)


def _read_v2(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or metadata.st_size > 1024 * 1024):
            _fail('Spicy v2 source journal is unsafe', 'upload_receipt_invalid')
        raw = bytearray()
        while len(raw) <= 1024 * 1024:
            block = os.read(fd, 1024 * 1024 + 1 - len(raw))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(fd)
        if len(raw) > 1024 * 1024 or any(getattr(metadata, key) != getattr(after, key)
                for key in ('st_ino', 'st_dev', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            _fail('Spicy v2 source journal changed or exceeds its limit', 'upload_receipt_invalid')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError):
        _fail('Spicy v2 source journal is unreadable', 'upload_receipt_invalid')
    finally:
        if fd is not None:
            os.close(fd)


def reconcile_rejected_ticket_v2(project_dir, *, source_sha256, credential_ref,
                                 media_kind, mime, reason):
    """Close only a locally rejected ticket that provably never reached PUT.

    This explicit, zero-network reconciliation preserves the failed journal.
    A subsequent upload still requires a different counted source batch and
    current authority. Uncertain ticket requests, PUTs and commits stay blocked.
    """
    from lib import spicy_jobs as jobs
    from lib.spicy_catalog import digest
    from lib.spicy_media import MIMES
    if (MIMES.get(mime) != media_kind or not isinstance(reason, str)
            or not reason.strip() or len(reason) > 1000):
        _fail('Rejected ticket reconciliation requires exact media and reason', 'upload_reconciliation_invalid')
    binding = jobs.credential_binding(credential_ref)
    receipt_path, path = _v2_paths(Path(project_dir).resolve(), source_sha256,
                                  binding['credential_digest'], media_kind)
    with _source_lock(path):
        journal = _read_v2(path)
        expected = {'source_sha256': source_sha256, 'credential_ref': credential_ref,
                    'credential_digest': binding['credential_digest'],
                    'media_kind': media_kind, 'mime': mime}
        if (not isinstance(journal, dict) or journal.get('version') != '2.0'
                or any(journal.get('binding', {}).get(k) != v for k, v in expected.items())
                or _read_v2(receipt_path) is not None):
            _fail('Rejected ticket reconciliation binding differs', 'upload_reconciliation_invalid')
        if journal.get('state') == 'rejected_before_put':
            return {'state': journal['state'], 'journal': str(path), 'replayed': True, 'provider_calls': 0}
        # Both local upload_ticket_invalid checks precede put_started. That
        # durable transition always includes file_id and ticket_expires_at.
        if (journal.get('state') != 'uncertain'
                or journal.get('failure') != {'kind': 'upload_ticket_invalid'}
                or any(k in journal for k in ('file_id', 'ticket_expires_at', 'receipt'))):
            _fail('Original transfer may have reached storage; never reticket', 'upload_original_unresolved')
        snapshot = path.with_name(path.stem + '.rejected-' + digest(journal) + '.json')
        if snapshot.exists():
            if _read_v2(snapshot) != journal:
                _fail('Rejected ticket history differs', 'upload_reconciliation_invalid')
        else:
            atomic_write_json(snapshot, journal, exclusive=True)
            snapshot.chmod(0o400)
        reconciled = dict(journal, state='rejected_before_put', reconciliation={
            'reason': reason, 'observed_at': _now().isoformat(),
            'original_journal': snapshot.name, 'original_sha256': digest(journal)})
        _write_state(path, reconciled)
        return {'state': reconciled['state'], 'journal': str(path), 'replayed': False, 'provider_calls': 0}


def usable_receipt_v2(root, sha256, credential_digest, media_kind, mime):
    """Read-only proof of a committed v2 receipt; never consult v1 state."""
    from lib.spicy_media import MIMES
    from lib.spicy_catalog import digest
    from lib.spicy_jobs import RESULT_HOST
    if MIMES.get(mime) != media_kind:
        _fail('Spicy v2 source MIME and media kind differ', 'upload_receipt_invalid')
    receipt_path, journal_path = _v2_paths(root, sha256, credential_digest, media_kind)
    journal, retained = _read_v2(journal_path), _read_v2(receipt_path)
    if journal is None:
        if retained is not None:
            _fail('Spicy v2 source receipt lacks its original commit journal', 'upload_receipt_invalid')
        return None
    binding = journal.get('binding')
    expected = {'source_sha256': sha256, 'credential_digest': credential_digest,
                'media_kind': media_kind, 'mime': mime}
    if (journal.get('version') != '2.0' or not isinstance(binding, dict)
            or any(binding.get(key) != value for key, value in expected.items())):
        _fail('Spicy v2 source journal binding differs', 'upload_receipt_invalid')
    hosts = binding.get('qualified_storage_hosts')
    if (not isinstance(hosts, list) or not hosts
            or any(not isinstance(host, str) or not RESULT_HOST.fullmatch(host) for host in hosts)
            or hosts != sorted(set(hosts))
            or not isinstance(binding.get('credential_ref'), str)
            or not re.fullmatch(r'env:[A-Z_][A-Z0-9_]{0,127}', binding['credential_ref'])
            or not isinstance(binding.get('account_label'), str) or not binding['account_label'].strip()
            or len(binding['account_label']) > 512 or any(ord(char) < 32 for char in binding['account_label'])
            or not isinstance(journal.get('batch_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', journal['batch_id'])):
        _fail('Spicy v2 historical transfer authority is malformed', 'upload_receipt_invalid')
    transfer_binding = {key: binding.get(key) for key in ('source_sha256', 'credential_ref',
        'credential_digest', 'account_label', 'media_kind', 'mime', 'bytes', 'qualified_storage_hosts')}
    transfer_binding['batch_id'] = journal['batch_id']
    if journal.get('state') == 'rejected_before_put':
        reconciliation = journal.get('reconciliation', {})
        original_name = reconciliation.get('original_journal')
        if (retained is not None or not isinstance(original_name, str)
                or Path(original_name).name != original_name):
            _fail('Rejected ticket history is malformed', 'upload_receipt_invalid')
        original = _read_v2(Path(journal_path).with_name(original_name))
        if (not isinstance(original, dict) or original.get('state') != 'uncertain'
                or original.get('failure') != {'kind': 'upload_ticket_invalid'}
                or any(k in original for k in ('file_id', 'ticket_expires_at', 'receipt'))
                or original.get('binding') != binding or original.get('batch_id') != journal['batch_id']
                or digest(original) != reconciliation.get('original_sha256')):
            _fail('Rejected ticket lacks its original pre-PUT proof', 'upload_receipt_invalid')
        return None
    if journal.get('state') != 'committed':
        _fail('Spicy v2 source has an unresolved original transfer; never reticket', 'upload_original_unresolved')
    receipt = journal.get('receipt')
    expiry = parse_utc(receipt.get('expires_at')) if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or receipt.get('version') != '2.0'
            or any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get('content_type') != mime or receipt.get('batch_id') != journal['batch_id'] or type(receipt.get('bytes')) is not int
            or receipt['bytes'] <= 0 or receipt.get('bytes') != binding.get('bytes')
            or not isinstance(receipt.get('uri'), str)
            or not re.fullmatch(r'spicy://f/[A-Za-z0-9._/-]{1,500}', receipt['uri'])
            or not isinstance(receipt.get('file_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', receipt['file_id']) or expiry is None):
        _fail('Spicy v2 committed source receipt is malformed', 'upload_receipt_invalid')
    if (receipt.get('transfer_binding') != transfer_binding
            or receipt.get('transfer_binding_sha256') != digest(transfer_binding)):
        _fail('Spicy v2 receipt lost its immutable historical transfer authority', 'upload_receipt_invalid')
    if retained is not None and retained != receipt:
        _fail('Spicy v2 source receipt differs from its commit journal', 'upload_receipt_invalid')
    if expiry <= _now() + timedelta(seconds=UPLOAD_SAFETY_SECONDS):
        return None
    # The durable journal also survives a missing convenience copy. Compilation
    # calls this reader, so validation must not recreate local artifacts.
    return dict(receipt)


def upload_sources_v2(project_dir, inputs: dict, *, client) -> list[dict]:
    """Transfer exact authority-bound typed snapshots, once only, with no conversion."""
    from lib import production_autonomy as autonomy, spicy_jobs as jobs, spicy_media
    jobs._offline_guard()
    root = Path(project_dir).resolve()
    if not isinstance(inputs, dict) or jobs._root(inputs) != root:
        _fail('Spicy v2 upload project root differs', 'project_root_mismatch')
    native = jobs.build_native_request(inputs)
    if native.get('transport') != 'spicy_native_jobs_v2':
        _fail('Typed source transfer requires native v2 compilation', 'upload_source_mismatch')
    binding, _ = jobs._credential_snapshot(inputs.get('credential_ref'))
    hosts = getattr(client, 'qualified_storage_hosts', None)
    if getattr(client, 'binding', None) != binding:
        _fail('Spicy v2 upload credential differs', 'credential_mismatch')
    if not isinstance(hosts, (set, frozenset)) or not hosts:
        _fail('Spicy v2 source transfer requires exact qualified hosts', 'storage_host_unqualified')
    rows = native.get('sources')
    if not isinstance(rows, list) or not rows:
        _fail('Spicy v2 upload has no typed local sources', 'missing_source')
    pending, results = [], []
    for row in rows:
        if not isinstance(row, dict):
            _fail('Spicy v2 source row is malformed', 'upload_source_mismatch')
        path = jobs._resolve_source(row.get('source_path'), root)
        try:
            data = spicy_media.frozen_bytes(path)
            proof = spicy_media.probe_bytes(data)
        except spicy_media.SpicyMediaError as exc:
            _fail(str(exc), exc.kind)
        if any(row.get(key) != proof[key] for key in ('mime', 'media_kind', 'bytes')) or row.get('source_sha256') != proof['sha256']:
            _fail('Spicy v2 frozen source differs from native preparation', 'upload_source_mismatch')
        receipt_path, journal_path = _v2_paths(root, proof['sha256'], binding['credential_digest'], proof['media_kind'])
        receipt = usable_receipt_v2(root, proof['sha256'], binding['credential_digest'], proof['media_kind'], proof['mime'])
        if receipt is not None:
            if row.get('uri') not in (None, receipt['uri']):
                _fail('Spicy v2 native source URI differs from original commit', 'upload_receipt_invalid')
            results.append(receipt)
            continue
        if row.get('uri') is not None:
            _fail('Spicy v2 native source lacks its current original receipt', 'upload_receipt_invalid')
        prior = _read_v2(journal_path)
        if prior is not None and prior.get('state') != 'rejected_before_put':
            _fail('Expired Spicy v2 upload needs separately reviewed fresh transfer authority', 'upload_expired_needs_fresh_authority')
        pending.append({**row, 'data': data, 'proof': proof, 'path': str(path),
                        'receipt_path': receipt_path, 'journal_path': journal_path, 'prior': prior})
    if not pending:
        return results
    authority = autonomy.reserve_spicy_source_transfer(root, inputs)
    if (authority.get('credential_ref') != binding['credential_ref']
            or authority.get('credential_digest') != binding['credential_digest']
            or authority.get('account_label') != inputs.get('account_label')
            or authority.get('qualified_storage_hosts') != sorted(hosts)):
        _fail('Spicy v2 source transfer differs from exact authority', 'upload_authority_mismatch')
    if (not isinstance(authority.get('batch_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', authority['batch_id'])):
        _fail('Spicy v2 source transfer batch identity is invalid', 'upload_authority_mismatch')
    def authority_row(source):
        return {key: source[key] for key in ('role', 'pointer', 'media_kind', 'mime')} | {
            'path': source['path'], 'sha256': source['source_sha256'], 'bytes': source['bytes']}
    observed = sorted([authority_row(source) for source in pending], key=lambda row: (row['pointer'], row['role']))
    authorized = sorted(authority.get('files', []), key=lambda row: (row.get('pointer', ''), row.get('role', '')))
    if authorized != observed:
        _fail('Spicy v2 typed source bytes/pointers differ from reserved authority', 'upload_source_mismatch')
    if any(source['prior'] is not None and source['prior'].get('batch_id') == authority['batch_id'] for source in pending):
        _fail('Rejected ticket needs a fresh counted source-transfer batch', 'upload_authority_mismatch')
    for source in pending:
        receipt_path, journal_path = source['receipt_path'], source['journal_path']
        expected = {'source_sha256': source['source_sha256'], 'credential_ref': binding['credential_ref'],
            'credential_digest': binding['credential_digest'], 'account_label': authority['account_label'],
            'media_kind': source['media_kind'], 'mime': source['mime'], 'bytes': source['bytes'],
            'qualified_storage_hosts': sorted(hosts)}
        with _source_lock(journal_path):
            receipt = usable_receipt_v2(root, source['source_sha256'], binding['credential_digest'], source['media_kind'], source['mime'])
            if receipt is not None:
                results.append(receipt)
                continue
            if _read_v2(journal_path) != source['prior']:
                _fail('Expired Spicy v2 upload needs fresh authority', 'upload_expired_needs_fresh_authority')
            state = {'version': '2.0', 'batch_id': authority['batch_id'], 'binding': expected, 'state': 'ticket_requesting'}
            if source['prior'] is not None:
                state['previous_rejected_ticket'] = source['prior']
            _write_state(journal_path, state)
            durable_commit = False
            try:
                ticket = client.create_upload_url(content_type=source['mime'], byte_count=source['bytes'])['data']
                expiry = parse_utc(ticket.get('expiresAt')) if isinstance(ticket, dict) else None
                if (not isinstance(ticket, dict) or not isinstance(ticket.get('fileId'), str)
                        or not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', ticket['fileId'])
                        or ticket.get('method') != 'PUT' or not isinstance(ticket.get('headers'), dict)
                        or not isinstance(ticket.get('uploadUrl'), str) or type(ticket.get('maxBytes')) is not int
                        or ticket['maxBytes'] < source['bytes'] or expiry is None or expiry <= _now() + timedelta(seconds=5)):
                    _fail('Spicy v2 upload ticket is malformed or expired', 'upload_ticket_invalid')
                headers = ticket['headers']
                if (any(not isinstance(key, str) or key.lower() not in ('content-type', 'content-length') for key in headers)
                        or next((value for key, value in headers.items() if key.lower() == 'content-type'), None) != source['mime']):
                    _fail('Spicy v2 upload headers do not match exact source MIME', 'upload_ticket_invalid')
                state.update(state='put_started', file_id=ticket['fileId'], ticket_expires_at=ticket['expiresAt'])
                _write_state(journal_path, state)
                put = client.put_presigned(ticket['uploadUrl'], source['data'], headers,
                    max_bytes=spicy_media.IMAGE_MAX_BYTES if source['media_kind'] == 'image' else spicy_media.MEDIA_MAX_BYTES)
                if not isinstance(put, dict) or type(put.get('status')) is not int or not 200 <= put['status'] < 300:
                    _fail('Spicy v2 storage PUT is unconfirmed', 'upload_put_unconfirmed')
                state['state'] = 'commit_started'
                _write_state(journal_path, state)
                committed = client.commit_file(ticket['fileId'])['data']
                expires = parse_utc(committed.get('expiresAt')) if isinstance(committed, dict) else None
                if (not isinstance(committed, dict) or committed.get('fileId') != ticket['fileId']
                        or committed.get('status') != 'ready' or type(committed.get('bytes')) is not int
                        or committed['bytes'] != source['bytes'] or committed.get('contentType') != source['mime']
                        or committed.get('sha256') != source['source_sha256'] or not isinstance(committed.get('uri'), str)
                        or not re.fullmatch(r'spicy://f/[A-Za-z0-9._/-]{1,500}', committed['uri'])
                        or expires is None or expires <= _now() + timedelta(seconds=UPLOAD_SAFETY_SECONDS)):
                    _fail('Spicy v2 commit failed exact byte/type/hash verification', 'upload_commit_invalid')
                receipt = {'version': '2.0', 'source_sha256': source['source_sha256'],
                    'credential_digest': binding['credential_digest'], 'media_kind': source['media_kind'],
                    'mime': source['mime'], 'bytes': source['bytes'], 'uri': committed['uri'],
                    'expires_at': committed['expiresAt'], 'file_id': committed['fileId'],
                    'content_type': committed['contentType'], 'batch_id': authority['batch_id']}
                from lib.spicy_catalog import digest
                transfer_binding = dict(expected, batch_id=authority['batch_id'])
                receipt['transfer_binding'] = transfer_binding
                receipt['transfer_binding_sha256'] = digest(transfer_binding)
                state.update(state='committed', receipt=receipt)
                _write_state(journal_path, state)
                durable_commit = True
                atomic_write_json(receipt_path, receipt)
                results.append(receipt)
            except Exception as exc:
                if not durable_commit:
                    state.update(state='uncertain', failure=_safe_error(exc))
                    _write_state(journal_path, state)
                if isinstance(exc, jobs.SpicyError):
                    raise
                _fail('Spicy v2 original source transfer is uncertain; never reticket', 'upload_uncertain')
    return results
