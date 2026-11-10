"""Native SpicyAPI job core for the exact governed image-to-video route.

Public API (stable names agreed with the factory owner):

* credential_binding(ref) / SpicyClient / client_for_credential(ref)
* build_native_request(inputs, profile=None)       -- pure, no network
* request_quote / write_quote_artifact / load_quote_artifact / quote_native_request
* upload_sources(project_dir, inputs, *, client)    -- once-only journaled uploads
* submit_original(...) used by tools.video.spicy_video.SpicyVideo.execute
* resume_original(project_dir, attempt_id, client=None)
* validate_collected_original(project_dir, attempt_id) -- pure, credential-free

Every network method refuses inside offline preparation. Nothing here retries a
paid create with a new identity, falls back to another route, or echoes a key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from lib import spicy_catalog as catalog
from lib.spicy_catalog import atomic_write_json, digest, parse_utc
from lib.spicy_credentials import SpicyCredentialError, resolve_credential

API_BASE = "https://api.spicyapi.ai/api/v1"
TRANSPORT = "spicy_native_jobs_v1"
TRANSPORT_V2 = "spicy_native_jobs_v2"
CREDENTIAL_DOMAIN = b"openmontage.spicy.credential.v1\0"
QUOTE_TTL_SECONDS = 300
UPLOAD_SAFETY_SECONDS = 120
REPLAY_WINDOW = timedelta(hours=23)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
RESULT_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.r2\.cloudflarestorage\.com$")
_ENV_REF = re.compile(r"^env:([A-Z_][A-Z0-9_]{0,127})$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{1,128}$")
SOURCE_ROLES = {"first_frame": "image_url", "last_frame": "last_image_url"}
ALIAS_ROLES = {"image_path": "first_frame", "last_image_path": "last_frame"}

# Pipeline/governance plumbing that never becomes part of the native request.
PLUMBING_KEYS = {
    "project_dir", "governance", "compiled_request_id", "preparation_review_id", "spicy_quote_id",
    "cli_session_id", "output_path", "preferred_provider", "allowed_providers", "account_label",
    "credential_ref", "mode", "model", "scene_id", "operation", "preferred_tool", "hosting_provider",
    "input_assets", "image_path", "last_image_path", "shot_id", "scope_id", "production_phase",
    "shot_contract_path",
}
CREATIVE_KEYS = {"prompt", "duration", "duration_seconds", "resolution", "aspect_ratio", "generate_audio"}
# Vendor business codes that prove the create was rejected (no task created).
REJECTED_CODES = {40001, 40101, 40201, 40202, 40301, 40401, 409, 40901, 42201, 42901}


class SpicyError(ValueError):
    """Sanitized Spicy failure. Never contains credentials or signed URLs."""

    def __init__(self, message: str, *, kind: str = "spicy_error", code: Any = None,
                 http_status: int | None = None, request_id: str | None = None,
                 retry_after: str | None = None):
        super().__init__(message)
        self.kind, self.code, self.http_status = kind, code, http_status
        self.request_id, self.retry_after = request_id, retry_after

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": str(self), "code": self.code,
                "http_status": self.http_status, "request_id": self.request_id,
                "retry_after": self.retry_after}


class SpicyAPIError(SpicyError):
    """The API answered with a definite non-success envelope."""


class SpicyTransportError(SpicyError):
    """Outcome unknown: the request may or may not have been processed."""


SpicyInputError = SpicyError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact_url(value: Any) -> str | None:
    """Keep only scheme/host/path; signed query strings are credentials."""
    if not isinstance(value, str):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "[redacted]"
    if not parts.scheme or not parts.netloc:
        return "[redacted]"
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}{parts.path}" + ("?[redacted]" if parts.query else "")


def _valid_request_id(value: Any) -> str | None:
    if isinstance(value, str) and _REQUEST_ID.fullmatch(value):
        return value
    return None


# --------------------------------------------------------------------------- credentials

def _credential_snapshot(ref: Any) -> tuple[dict[str, str], str]:
    """Read one credential value and derive the exact binding from that snapshot."""
    match = _ENV_REF.match(ref) if isinstance(ref, str) else None
    if not match:
        raise SpicyError("Spicy credential_ref must be env:<NAME>", kind="credential_ref_invalid")
    try:
        secret = resolve_credential(match.group(1))
    except SpicyCredentialError as exc:
        raise SpicyError(str(exc), kind=exc.kind) from None
    binding = {"credential_ref": ref, "credential_env": match.group(1),
               "credential_digest": hashlib.sha256(CREDENTIAL_DOMAIN + secret.encode()).hexdigest()}
    return binding, secret


def credential_binding(ref: Any) -> dict[str, str]:
    """Identity of the selected credential: env name plus a domain-separated digest."""
    return _credential_snapshot(ref)[0]


# Official SDK byte transport: (method, url, headers, body, timeout) -> (status, headers, raw).
Transport = Callable[[str, str, dict, "bytes | None", float], tuple]
# Offline test seam for the separately journaled signed-storage PUT.
StorageTransport = Callable[[str, str, dict, bytes], tuple]
SDK_VERSION = "0.3.0"
MAX_API_RESPONSE_BYTES = 4 * 1024 * 1024
API_TIMEOUT_SECONDS = 60.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _guarded_transport(method: str, url: str, headers, body: bytes | None, timeout: float):
    """Byte transport handed to the official SDK.

    The SDK builds every endpoint, header, body, envelope and error. This only
    enforces the engine's network policy: the fixed API origin, no ambient
    proxies, no credential-bearing redirects and a bounded response.
    """
    if not isinstance(url, str) or not url.startswith(API_BASE + "/"):
        raise SpicyError("Spicy SDK request left the fixed API origin", kind="invalid_api_origin")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with opener.open(request, timeout=timeout) as response:
            return (response.status, dict(response.headers.items()),
                    response.read(MAX_API_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        try:
            return (exc.code, dict(exc.headers.items()) if exc.headers else {},
                    exc.read(MAX_API_RESPONSE_BYTES + 1))
        finally:
            exc.close()


def _offline_guard() -> None:
    from tools.base_tool import in_offline_preparation
    if in_offline_preparation():
        raise SpicyError("Spicy network calls are refused during offline preparation", kind="offline")


def _load_sdk():
    import sys
    if sys.version_info < (3, 11):
        raise SpicyError("The official Spicy SDK requires Python 3.11+; use the engine .venv311",
                         kind="sdk_unavailable")
    try:
        import spicyapi
    except ImportError:
        raise SpicyError("The official Spicy SDK (spicyapi==0.3.0) is not installed in this interpreter",
                         kind="sdk_unavailable") from None
    if getattr(spicyapi, "__version__", None) != SDK_VERSION:
        raise SpicyError("The installed Spicy SDK version differs from the pinned spicyapi==0.3.0",
                         kind="sdk_unavailable")
    return spicyapi


def _header(headers: Any, name: str) -> Any:
    if not hasattr(headers, "items"):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value
    return None


class SpicyClient:
    """Governed wrapper over the official ``spicyapi`` SDK.

    Named methods call the SDK's public methods; the SDK owns endpoints,
    authentication, serialization and envelope/error parsing. Automatic SDK
    retries are disabled (``max_retries=0``) because once-only intent, quote
    caps and recovery are journaled by the workflow. The wrapper rechecks the
    credential binding, maps SDK failures to sanitized engine errors and keeps
    request-local correlation metadata.
    """

    def __init__(self, credential_ref: str, transport: Transport | None = None, *,
                 base_url: str = API_BASE, qualified_storage_hosts=(),
                 storage_transport: StorageTransport | None = None):
        self.binding, secret = _credential_snapshot(credential_ref)
        try:
            parsed_base = urllib.parse.urlsplit(base_url)
            valid_base = (parsed_base.scheme == "https" and parsed_base.hostname == "api.spicyapi.ai"
                and parsed_base.port in (None, 443) and not parsed_base.username and not parsed_base.password
                and parsed_base.path.rstrip("/") == "/api/v1" and not parsed_base.query and not parsed_base.fragment)
        except (TypeError, ValueError):
            valid_base = False
        if not valid_base:
            raise SpicyError("Spicy API origin is fixed to the official HTTPS endpoint", kind="invalid_api_origin")
        if not isinstance(qualified_storage_hosts, (set, frozenset, list, tuple)):
            raise SpicyError("Spicy storage hosts require an explicit hostname list", kind="storage_host_unqualified")
        normalized_hosts = set()
        for host in qualified_storage_hosts:
            if not isinstance(host, str) or host != host.lower() or not RESULT_HOST.fullmatch(host):
                raise SpicyError("Spicy storage host is not an exact qualified R2 hostname", kind="storage_host_unqualified")
            normalized_hosts.add(host)
        if len(normalized_hosts) > 16:
            raise SpicyError("Spicy storage host list is too large", kind="storage_host_unqualified")
        self.qualified_storage_hosts = frozenset(normalized_hosts)
        self._byte_transport = transport or _guarded_transport
        self._sdk_transport_injected = transport is not None
        self._storage_transport = storage_transport
        # Request-local: concurrent calls on one shared client never see each other's metadata.
        self._metadata: ContextVar[dict | None] = ContextVar("spicy_sdk_request_metadata", default=None)
        spicyapi = _load_sdk()
        self.sdk = spicyapi.SpicyClient(secret, base_url=API_BASE, transport=self._observed_transport,
                                        max_retries=0, request_timeout_seconds=API_TIMEOUT_SECONDS)

    def __repr__(self) -> str:
        return f"SpicyClient(credential_ref={self.binding['credential_ref']!r})"

    def _observed_transport(self, method, url, headers, body, timeout):
        metadata = self._metadata.get()
        if metadata is not None:
            metadata["sent"] = True
        result = self._byte_transport(method, url, headers, body, timeout)
        if not isinstance(result, tuple) or len(result) != 3:
            raise SpicyTransportError("Spicy transport returned a malformed response", kind="malformed_uncertain")
        status, response_headers, raw = result
        if type(status) is not int or not 100 <= status <= 599:
            raise SpicyTransportError("Spicy transport returned an invalid HTTP status", kind="malformed_uncertain")
        if not isinstance(raw, (bytes, str)) or len(raw) > MAX_API_RESPONSE_BYTES:
            raise SpicyTransportError("Spicy response exceeded its size limit", kind="malformed_uncertain",
                                      http_status=status)
        if metadata is not None:
            # Correlation only; acceptance and error semantics stay with the SDK.
            request_id = _valid_request_id(_header(response_headers, "x-request-id"))
            try:
                decoded = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                decoded = None
            if isinstance(decoded, dict):
                request_id = _valid_request_id(decoded.get("request_id")) or request_id
            metadata["http_status"], metadata["request_id"] = status, request_id
        return status, dict(response_headers or {}), raw

    def _call(self, operation: Callable, *args, **kwargs) -> dict[str, Any]:
        _offline_guard()
        current_binding, _ = _credential_snapshot(self.binding["credential_ref"])
        if current_binding != self.binding:
            raise SpicyError("Spicy credential changed after client binding", kind="credential_changed")
        spicyapi = _load_sdk()
        token = self._metadata.set({"sent": False, "http_status": None, "request_id": None})
        try:
            try:
                data = operation(*args, **kwargs)
            except SpicyError:
                raise
            except spicyapi.SpicyTimeoutError:
                raise SpicyTransportError("Spicy request outcome unknown: timeout",
                                          kind="transport_uncertain") from None
            except spicyapi.SpicyApiError as exc:
                raise self._sdk_failure(exc) from None
            except Exception as exc:
                if not self._metadata.get()["sent"]:
                    raise SpicyError("Spicy SDK refused the request before sending it",
                                     kind="invalid_input") from None
                raise SpicyTransportError("Spicy request outcome unknown: " + type(exc).__name__,
                                          kind="transport_uncertain") from None
            metadata = self._metadata.get()
            if not isinstance(data, dict):
                raise SpicyTransportError("Spicy success response is missing data", kind="malformed_uncertain",
                                          http_status=metadata["http_status"], request_id=metadata["request_id"])
            return {"data": data, "request_id": metadata["request_id"], "http_status": metadata["http_status"]}
        finally:
            self._metadata.reset(token)

    def _sdk_failure(self, exc) -> SpicyError:
        """Map an SDK failure to a sanitized engine error; the SDK's message is never copied."""
        metadata = self._metadata.get()
        status = exc.status if type(exc.status) is int else 0
        code = exc.code if type(exc.code) is int else None
        request_id = _valid_request_id(exc.request_id) or metadata["request_id"]
        seconds = exc.retry_after_seconds
        retry_after = (str(int(seconds)) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                       and 0 <= seconds <= 86400 else None)
        if status == 0:
            return SpicyTransportError("Spicy request outcome unknown: network failure", kind="transport_uncertain")
        if status >= 500 or status == 408:
            return SpicyTransportError(f"Spicy server error HTTP {status}", kind="server_uncertain",
                                       http_status=status, request_id=request_id, retry_after=retry_after)
        if code is None or (code == 200 and 200 <= status < 300):
            return SpicyTransportError("Spicy response envelope is malformed", kind="malformed_uncertain",
                                       http_status=status, request_id=request_id)
        return SpicyAPIError("Spicy API rejected request", kind=_code_kind(code, status), code=code,
                             http_status=status, request_id=request_id, retry_after=retry_after)

    # Named SDK operations. Each returns {"data", "request_id", "http_status"}.
    def get_balance(self) -> dict[str, Any]:
        return self._call(self.sdk.get_balance)

    def get_model(self, model: str) -> dict[str, Any]:
        return self._call(self.sdk.get_model, model)

    def list_models(self, **filters) -> dict[str, Any]:
        return self._call(self.sdk.list_models, **filters)

    def get_usage(self, **window) -> dict[str, Any]:
        return self._call(self.sdk.get_usage, **window)

    def list_tasks(self, **filters) -> dict[str, Any]:
        return self._call(self.sdk.list_tasks, **filters)

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._call(self.sdk.get_task, task_id)

    def quote_task(self, *, model: str, input_data: dict) -> dict[str, Any]:
        return self._call(self.sdk.quote_task, model=model, input_data=input_data)

    def create_task(self, *, model: str, input_data: dict, idempotency_key: str,
                    quote_id: str, expected_cost: str) -> dict[str, Any]:
        return self._call(self.sdk.create_task, model=model, input_data=input_data,
                          idempotency_key=idempotency_key, quote_id=quote_id, expected_cost=expected_cost)

    def create_upload_url(self, *, content_type: str, byte_count: int) -> dict[str, Any]:
        return self._call(self.sdk.create_upload_url, content_type=content_type, byte_count=byte_count)

    def commit_file(self, file_id: str) -> dict[str, Any]:
        return self._call(self.sdk.commit_file, file_id)

    def create_download_url(self, task_id: str, key: str | None = None) -> dict[str, Any]:
        return self._call(self.sdk.create_download_url, task_id, key)

    def purge_task(self, task_id: str) -> dict[str, Any]:
        return self._call(self.sdk.purge_task, task_id)

    def list_assets(self, **filters) -> dict[str, Any]:
        return self._call(self.sdk.assets.list, **filters)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._call(self.sdk.assets.get, asset_id)

    def asset_usage(self) -> dict[str, Any]:
        return self._call(self.sdk.assets.usage)

    def list_characters(self, **filters) -> dict[str, Any]:
        return self._call(self.sdk.assets.characters.list, **filters)

    def get_character(self, character_id: str) -> dict[str, Any]:
        return self._call(self.sdk.assets.characters.get, character_id)

    def put_presigned(self, url: str, data: bytes, headers: dict | None = None, *, max_bytes: int = MAX_UPLOAD_BYTES) -> dict[str, Any]:
        """PUT bytes to the exact presigned URL. Never sends the API credential."""
        _offline_guard()
        if not self.qualified_storage_hosts:
            raise SpicyError("Spicy presigned upload host was not explicitly qualified", kind="storage_host_unqualified")
        if type(max_bytes) is not int or not 0 < max_bytes <= 90 * 1024 * 1024 or not isinstance(data, bytes) or not data or len(data) > max_bytes:
            raise SpicyError("Spicy upload body is empty or exceeds its transfer limit", kind="upload_too_large")
        try:
            from lib.openart_download import _url
            host, path, query = _url(url, self.qualified_storage_hosts)
        except Exception:
            raise SpicyError("Spicy presigned upload URL is invalid or unqualified", kind="storage_host_unqualified") from None
        if headers is None:
            headers = {}
        if not isinstance(headers, dict):
            raise SpicyError("Spicy upload ticket headers are malformed", kind="upload_ticket_invalid")
        safe_headers = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise SpicyError("Spicy upload ticket headers are malformed", kind="upload_ticket_invalid")
            lower_name = name.lower()
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    or "\r" in value or "\n" in value
                    or lower_name not in {"content-type", "content-length"}
                    or lower_name in safe_headers):
                raise SpicyError("Spicy upload ticket contains an unsafe header", kind="upload_ticket_invalid")
            safe_headers[lower_name] = value
        if not safe_headers.get("content-type"):
            raise SpicyError("Spicy upload ticket headers do not match the bytes", kind="upload_ticket_invalid")
        # The provider ticket may only sign Content-Type; the exact body length is
        # locally known and is safe to add when the ticket did not supply it.
        if "content-length" in safe_headers and safe_headers["content-length"] != str(len(data)):
            raise SpicyError("Spicy upload ticket headers do not match the bytes", kind="upload_ticket_invalid")
        safe_headers["content-length"] = str(len(data))
        if self._storage_transport is None and self._sdk_transport_injected:
            raise SpicyError("Spicy test client has no storage transport; refusing real storage access",
                             kind="storage_transport_unconfigured")
        try:
            import time
            from lib.openart_download import _PinnedHTTPSConnection, _remaining, _resolve_public
            deadline = time.monotonic() + 60.0
            address = None if self._storage_transport is not None else _resolve_public(host, deadline)
            if self._storage_transport is not None:
                # Injected storage transports are offline test seams; production always uses pinned TLS below.
                status, _, response_body = self._storage_transport("PUT", url, safe_headers, data)
            else:
                connection = _PinnedHTTPSConnection(host, address, _remaining(deadline))
                response = None
                try:
                    target = path + ("?" + query if query else "")
                    connection.request("PUT", target, body=data, headers=safe_headers)
                    connection.set_timeout(_remaining(deadline))
                    response = connection.getresponse()
                    status = response.status
                    response_body = response.read(64 * 1024 + 1)
                finally:
                    if response is not None:
                        response.close()
                    connection.close()
        except Exception as exc:
            if isinstance(exc, SpicyError):
                raise
            raise SpicyTransportError("Spicy upload outcome unknown: " + type(exc).__name__,
                                      kind="transport_uncertain") from None
        if not isinstance(response_body, (bytes, str)) or len(response_body) > 64 * 1024:
            raise SpicyTransportError("Spicy upload response exceeded its size limit", kind="malformed_uncertain")
        if type(status) is not int or not 100 <= status <= 599:
            raise SpicyTransportError("Spicy storage returned an invalid HTTP status", kind="malformed_uncertain")
        if status >= 500:
            raise SpicyTransportError(f"Spicy upload storage HTTP {status}", kind="server_uncertain", http_status=status)
        if not 200 <= status < 300:
            raise SpicyAPIError(f"Spicy upload storage rejected HTTP {status}", kind="upload_rejected", http_status=status)
        return {"status": status}


def _code_kind(code: Any, status: int) -> str:
    return {40201: "insufficient_balance", 40202: "spend_cap", 409: "idempotency_mismatch",
            40901: "quote_stale"}.get(code, "auth" if status in (401, 403) else "rejected")


def client_for_credential(ref: str, transport: Transport | None = None, *,
                          qualified_storage_hosts=(), storage_transport: StorageTransport | None = None) -> SpicyClient:
    return SpicyClient(ref, transport, qualified_storage_hosts=qualified_storage_hosts,
                       storage_transport=storage_transport)


def connection_check(credential_ref: str = "env:SPICY_API_KEY", *, client=None) -> dict[str, Any]:
    """Explicit free GET-only diagnostic; never creates project readiness records."""
    client = client or client_for_credential(credential_ref)
    if client.binding.get("credential_ref") != credential_ref:
        raise SpicyError("Spicy diagnostic credential differs", kind="credential_mismatch")
    credit = client.get_balance()["data"]
    balance = {}
    for field in ("available", "held", "total"):
        value = credit.get(field)
        if (not isinstance(value, str) or len(value) > 64
                or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value)
                or (field == "held" and value.startswith("-"))):
            raise SpicyError("Spicy balance response is malformed", kind="invalid_balance")
        # Approved credit usage can make available/total negative; this is a
        # balance observation, separate from nonnegative quoted-cost admission.
        balance[field] = value
    data = client.list_models(include_schema=True)["data"]
    items = data.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise SpicyError("Spicy model listing response is malformed", kind="invalid_catalog")
    selected = [item for item in items if item.get("model") == catalog.VIDEO_MODEL]
    if len(selected) > 1:
        raise SpicyError("Spicy model listing is ambiguous", kind="invalid_catalog")
    item = selected[0] if selected else {}
    enabled = item.get("enabled") is True
    available = item.get("available") is True
    compatible = catalog.schema_compatible(catalog.VIDEO_MODEL, item.get("inputSchema"))
    return {
        "ok": True, "authenticated": True, "read_only": True,
        "balance_usd": balance,
        "catalogue": {
            "total": len(items),
            "enabled": sum(item.get("enabled") is True for item in items),
            "available": sum(item.get("available") is True for item in items),
            "video_enabled_available": sum(item.get("modality") == "video"
                and item.get("enabled") is True and item.get("available") is True for item in items),
        },
        "video_route": {"model": catalog.VIDEO_MODEL, "found": bool(selected),
                        "enabled": enabled, "available": available,
                        "schema_compatible": compatible,
                        "callable": enabled and available and compatible},
        "production_observation_written": False, "authority": "none",
    }


# --------------------------------------------------------------------------- native request

def _fail(message: str, kind: str = "invalid_input") -> None:
    raise SpicyError(message, kind=kind)


def _root(inputs: dict) -> Path | None:
    value = inputs.get("project_dir")
    return Path(value).resolve() if value else None


def _source_rows(inputs: dict) -> list[tuple[str, Any, Any]]:
    rows: list[tuple[str, Any, Any]] = []
    assets = inputs.get("input_assets")
    if assets is not None:
        if not isinstance(assets, list):
            _fail("input_assets must be a list of role bindings")
        for item in assets:
            if not isinstance(item, dict) or set(item) - {"role", "source_path", "source_sha256"}:
                _fail("input_assets entries must be {role, source_path}")
            declared = item.get("source_sha256")
            if declared is not None and (not isinstance(declared, str) or not _SHA.fullmatch(declared)):
                _fail("input_assets source_sha256 must be a lowercase SHA-256", "source_hash_invalid")
            rows.append((item.get("role"), item.get("source_path"), declared))
    for key, role in ALIAS_ROLES.items():
        if inputs.get(key) is not None:
            rows.append((role, inputs[key], None))
    roles = [row[0] for row in rows]
    if any(role not in SOURCE_ROLES for role in roles):
        _fail("Spicy route supports only first_frame and last_frame sources", "unsupported_source_role")
    if len(set(roles)) != len(roles):
        _fail("Spicy source role is bound more than once", "duplicate_source_role")
    if rows and roles[0] != "first_frame":
        rows.sort(key=lambda row: list(SOURCE_ROLES).index(row[0]))
    if rows and rows[0][0] != "first_frame":
        _fail("Spicy last_frame requires a first_frame", "missing_first_frame")
    return rows


def _resolve_source(path_value: Any, root: Path | None) -> Path:
    if not isinstance(path_value, (str, os.PathLike)):
        _fail("Spicy source must be a resolved local file path", "unresolved_source")
    text = os.fspath(path_value)
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        _fail("Spicy sources must be local project files, never URLs", "remote_source")
    path = Path(text).expanduser()
    if not path.is_absolute():
        if root is None:
            _fail("relative Spicy source requires project_dir", "unresolved_source")
        path = root / path
    path = path.resolve()
    if root is not None and not path.is_relative_to(root):
        _fail("Spicy source escapes the project", "source_outside_project")
    if not path.is_file():
        _fail("Spicy source file is missing", "source_missing")
    return path


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def upload_receipt_path(root: Path, source_sha256: str) -> Path:
    return Path(root) / "artifacts" / f"spicy_upload-{source_sha256}.json"


def _current_digest(inputs: dict) -> str | None:
    try:
        return credential_binding(inputs.get("credential_ref"))["credential_digest"]
    except SpicyError:
        return None


def _usable_receipt(root: Path | None, sha: str, credential_digest: str | None) -> dict | None:
    if root is None or credential_digest is None:
        return None
    journal_path = Path(root) / "artifacts" / f"spicy_upload_attempt-{sha}-{credential_digest}.json"
    if journal_path.is_symlink():
        _fail("Spicy original upload journal must not be a symlink", "upload_journal_invalid")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # A cache file alone does not prove the provider commit that created it.
        return None
    except (OSError, ValueError):
        _fail("Spicy original upload journal is unreadable", "upload_journal_invalid")
    if (not isinstance(journal, dict) or journal.get("state") != "committed"
            or not isinstance(journal.get("binding"), dict)
            or journal["binding"].get("source_sha256") != sha
            or journal["binding"].get("credential_digest") != credential_digest
            or not isinstance(journal.get("receipt"), dict)):
        _fail("Spicy source has an unresolved original upload", "upload_original_unresolved")
    receipt_file = upload_receipt_path(root, sha)
    if receipt_file.is_symlink():
        _fail("Spicy source receipt must not be a symlink", "upload_receipt_invalid")
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if receipt != journal["receipt"]:
        _fail("Spicy source receipt differs from its committed upload journal", "upload_receipt_invalid")
    expires = parse_utc(receipt.get("expires_at"))
    if (receipt.get("source_sha256") != sha or receipt.get("credential_digest") != credential_digest
            or not isinstance(receipt.get("uri"), str)
            or not re.fullmatch(r"spicy://f/[A-Za-z0-9._/-]{1,500}", receipt["uri"])
            or expires is None or expires <= _now() + timedelta(seconds=UPLOAD_SAFETY_SECONDS)):
        return None
    return receipt


def _controls(inputs: dict) -> dict[str, Any]:
    unknown = set(inputs) - PLUMBING_KEYS - CREATIVE_KEYS
    if unknown:
        _fail("Spicy route does not support controls: " + ", ".join(sorted(unknown)), "unsupported_control")
    have = [k for k in ("duration", "duration_seconds") if k in inputs]
    for key in have:
        if type(inputs[key]) is not int:
            _fail("Spicy duration must be an exact integer number of seconds", "invalid_duration")
    if len(have) == 2 and inputs["duration"] != inputs["duration_seconds"]:
        _fail("Spicy duration and duration_seconds conflict", "conflicting_duration")
    if not have:
        _fail("Spicy requires an explicit integer duration", "missing_duration")
    if type(inputs.get("generate_audio")) is not bool:
        _fail("Spicy requires explicit boolean generate_audio", "missing_generate_audio")
    if not isinstance(inputs.get("resolution"), str):
        _fail("Spicy requires explicit resolution", "missing_resolution")
    if not isinstance(inputs.get("prompt"), str) or not inputs["prompt"].strip():
        _fail("Spicy requires an authored prompt", "missing_prompt")
    return {"prompt": inputs["prompt"], "duration_seconds": inputs[have[0]],
            "resolution": inputs["resolution"],
            "aspect_ratio": inputs.get("aspect_ratio", catalog.route(catalog.VIDEO_MODEL)["defaults"]["aspect_ratio"]),
            "generate_audio": inputs["generate_audio"]}


def build_native_request(inputs: dict, profile: Any = None) -> dict[str, Any]:
    """Pure compile of the exact createTask/quote ``{model, input}`` body.

    Sources are bound by role and byte hash. A committed, unexpired upload
    receipt for the current credential supplies the spicy:// URI; otherwise a
    ``$upload:<sha256>`` placeholder marks ``payload_state='needs_upload'``.
    """
    if not isinstance(inputs, dict):
        _fail("Spicy inputs must be an object")
    if "native_input" in inputs or inputs.get("transport") == TRANSPORT_V2:
        return build_native_request_v2(inputs)
    model = inputs.get("model")
    if not isinstance(model, str) or not model:
        _fail("Spicy requires an explicit exact model", "missing_model")
    try:
        catalog.route(model)
    except catalog.SpicyCatalogError as exc:
        raise SpicyError(str(exc), kind="unsupported_model") from None
    if inputs.get("mode", catalog.VIDEO_MODE) != catalog.VIDEO_MODE:
        _fail("Spicy route mode must be image_to_video", "unsupported_mode")
    payload = _controls(inputs)
    rows = _source_rows(inputs)
    operation = inputs.get("operation")
    if operation not in (None, "image_to_video", "first_last_frame"):
        _fail("Spicy route does not support operation " + str(operation)[:40], "unsupported_operation")
    if operation == "first_last_frame" and "last_frame" not in [row[0] for row in rows]:
        _fail("Spicy first_last_frame requires a last_frame source", "missing_last_frame")
    root = _root(inputs)
    current = _current_digest(inputs)
    sources = []
    for role, value, declared in rows:
        path = _resolve_source(value, root)
        sha = _file_sha256(path)
        if declared is not None and declared != sha:
            _fail("Spicy source bytes differ from declared source_sha256", "source_hash_mismatch")
        receipt = _usable_receipt(root, sha, current)
        payload[SOURCE_ROLES[role]] = receipt["uri"] if receipt else "$upload:" + sha
        sources.append({"role": role, "source_path": str(path), "source_sha256": sha,
                        "path": str(path), "sha256": sha,
                        "uri": receipt["uri"] if receipt else None,
                        "upload_expires_at": receipt["expires_at"] if receipt else None,
                        "upload_credential_digest": receipt["credential_digest"] if receipt else None})
    if not sources:
        state = "missing_sources"
        check = dict(payload, image_url="$upload:" + "0" * 64)
    else:
        state = "ready" if all(s["uri"] for s in sources) else "needs_upload"
        check = payload
    try:
        catalog.validate_native_input(model, check, allow_placeholders=True)
    except catalog.SpicyCatalogError as exc:
        raise SpicyError(str(exc), kind="invalid_native_input") from None
    return {"provider": "spicy", "transport": TRANSPORT, "model": model, "mode": catalog.VIDEO_MODE,
            "input": payload, "sources": sources, "payload_state": state,
            "request_sha256": digest({"model": model, "input": payload})}


def require_ready(native: dict) -> None:
    if native.get("transport") == TRANSPORT_V2:
        return require_ready_v2(native)
    if native.get("payload_state") != "ready":
        _fail("Spicy native request still has unresolved source uploads", "needs_upload")
    catalog.validate_native_input(native["model"], native["input"])
    if native["request_sha256"] != digest({"model": native["model"], "input": native["input"]}):
        _fail("Spicy native request digest differs from payload", "native_digest_mismatch")


# --------------------------------------------------------------------------- quotes

def _decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        _fail(f"Spicy {label} is not a decimal USD string", "malformed_quote")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        _fail(f"Spicy {label} is not a decimal amount", "malformed_quote")
    if not amount.is_finite() or amount < 0:
        _fail(f"Spicy {label} must be finite and nonnegative", "malformed_quote")
    return amount


def request_quote(client, native: dict) -> dict[str, Any]:
    """Read-only exact quote of the ready native body. No reservation, no spend."""
    require_ready(native)
    # The quote body is exactly {model, input} by construction (both digest-bound above).
    response = client.quote_task(model=native["model"], input_data=copy.deepcopy(native["input"]))
    data = response.get("data")
    if (not isinstance(data, dict) or not isinstance(data.get("quoteId"), str)
            or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,4096}", data["quoteId"])):
        _fail("Spicy quote response is malformed", "malformed_quote")
    if data.get("model") != native["model"]:
        _fail("Spicy quote model differs", "malformed_quote")
    estimate = _decimal(data.get("estimatedCost"), "quote estimate")
    maximum = _decimal(data.get("maxCharge"), "quote maximum")
    if maximum < estimate:
        _fail("Spicy quote maximum is below its estimate", "malformed_quote")
    if data.get("currency") != "USD":
        _fail("Spicy quote currency is not USD", "malformed_quote")
    provider_expiry = parse_utc(data.get("expiresAt"))
    if provider_expiry is None or provider_expiry <= _now():
        _fail("Spicy quote expiry is missing, malformed, or expired", "malformed_quote")
    return {"provider_quote_id": data["quoteId"], "estimated_cost_usd": format(estimate, "f"),
            "max_charge_usd": format(maximum, "f"), "provider_expires_at": data["expiresAt"],
            "request_id": _valid_request_id(response.get("request_id"))}


def quote_path(root: Path, quote_id: str) -> Path:
    if not isinstance(quote_id, str) or not _TOKEN.match(quote_id):
        _fail("Spicy quote id is invalid", "quote_id_invalid")
    return Path(root) / "artifacts" / f"spicy_quote-{quote_id}.json"


def write_quote_artifact(project_dir, native: dict, response: dict, binding: dict,
                         account_label: str) -> dict[str, Any]:
    if not isinstance(account_label, str) or not account_label.strip():
        _fail("Spicy quote requires a named account label", "account_label_missing")
    obtained = _now().replace(microsecond=0)
    expires = obtained + timedelta(seconds=QUOTE_TTL_SECONDS)
    provider_expiry = parse_utc(response.get("provider_expires_at"))
    if provider_expiry is None or provider_expiry <= obtained:
        _fail("Spicy quote lacks a valid future provider expiry", "malformed_quote")
    expires = min(expires, provider_expiry)
    record = {"version": "1.0", "provider": "spicy", "provider_quote_id": response["provider_quote_id"],
              "native_request_sha256": native["request_sha256"], "model": native["model"],
              "credential_digest": binding["credential_digest"], "account_label": account_label,
              "estimated_cost_usd": response["estimated_cost_usd"], "max_charge_usd": response["max_charge_usd"],
              "currency": "USD", "obtained_at": _stamp(obtained), "expires_at": _stamp(expires),
              "request_id": response.get("request_id")}
    record["quote_id"] = "spq-" + digest(record)[:40]
    atomic_write_json(quote_path(Path(project_dir).resolve(), record["quote_id"]), record, exclusive=True)
    return copy.deepcopy(record)


def load_quote_artifact(project_dir, quote_id: str) -> dict[str, Any]:
    path = quote_path(Path(project_dir).resolve(), quote_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SpicyError("Spicy retained quote is missing or unreadable", kind="quote_missing") from None
    body = {k: v for k, v in record.items() if k != "quote_id"} if isinstance(record, dict) else None
    if not isinstance(record, dict) or record.get("quote_id") != quote_id or "spq-" + digest(body)[:40] != quote_id:
        raise SpicyError("Spicy retained quote is altered", kind="quote_altered")
    return record


def quote_native_request(project_dir, native: dict, *, account_label: str, credential_ref: str,
                         client: SpicyClient | None = None) -> dict[str, Any]:
    """Quote the exact ready payload under the named account and retain the evidence."""
    require_ready(native)
    client = client or client_for_credential(credential_ref)
    binding = client.binding
    if binding["credential_ref"] != credential_ref:
        _fail("Spicy quote client credential differs", "credential_mismatch")
    for source in native["sources"]:
        if source.get("upload_credential_digest") != binding["credential_digest"]:
            _fail("Spicy source upload belongs to a different credential", "credential_mismatch")
    response = request_quote(client, native)
    return write_quote_artifact(project_dir, native, response, binding, account_label)


# Lazy lifecycle boundaries keep request/transport and production ownership separate.
def upload_sources(project_dir, inputs, *, client):
    if "native_input" in inputs or inputs.get("transport") == TRANSPORT_V2:
        from lib.spicy_uploads import upload_sources_v2 as upload
    else:
        from lib.spicy_uploads import upload_sources as upload
    return upload(project_dir, inputs, client=client)


def submit_original(inputs, client=None):
    from lib.spicy_lifecycle import submit_original as submit
    return submit(inputs, client=client)


def resume_original(project_dir, attempt_id, client=None):
    from lib.spicy_lifecycle import resume_original as resume
    return resume(project_dir, attempt_id, client=client)


def validate_collected_original(project_dir, attempt_id):
    from lib.spicy_lifecycle import validate_collected_original as validate
    return validate(project_dir, attempt_id)


def billing_anomaly(project_dir, attempt_id):
    from lib.spicy_lifecycle import billing_anomaly as anomaly
    return anomaly(project_dir, attempt_id)


def billing_anomalies(project_dir, *, credential_digest=None):
    from lib.spicy_lifecycle import billing_anomalies as anomalies
    return anomalies(project_dir, credential_digest=credential_digest)


def refresh_settlement(project_dir, attempt_id, client=None):
    from lib.spicy_lifecycle import refresh_settlement as refresh
    return refresh(project_dir, attempt_id, client=client)



def _set_source_pointer(payload, pointer, uri):
    from lib.spicy_routes import parts
    tokens = parts(pointer)
    value = payload
    for index, token in enumerate(tokens):
        final = index == len(tokens) - 1
        replacement = uri if final else [] if tokens[index + 1].isdigit() else {}
        if isinstance(value, dict):
            if final and token in value:
                _fail('native_input must omit source slots; input_assets exclusively binds media', 'raw_media_input')
            if token not in value:
                value[token] = replacement
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', token):
            number = int(token)
            if number > len(value):
                _fail('source array mappings must be contiguous and ordered', 'source_pointer_invalid')
            if number == len(value):
                value.append(replacement)
            elif final:
                _fail('native_input must omit source slots; input_assets exclusively binds media', 'raw_media_input')
            value = value[number]
        else:
            _fail('source pointer container differs from native schema', 'source_pointer_invalid')


def build_native_request_v2(inputs, observation=None):
    """Compile exact current-schema controls and hash-bound local typed media.

    ``observation`` is an internal offline testing seam, never read from inputs.
    Production always reloads the fresh project/key-bound observation from disk.
    """
    from lib import spicy_routes as routes, spicy_media
    from lib.spicy_uploads import usable_receipt_v2
    if not isinstance(inputs, dict) or not isinstance(inputs.get('native_input'), dict):
        _fail('v2 requires native_input object', 'invalid_native_input')
    if inputs.get('transport', TRANSPORT_V2) != TRANSPORT_V2:
        _fail('v2 carrier transport conflicts with native_input', 'unsupported_transport')
    allowed = PLUMBING_KEYS | CREATIVE_KEYS | {'native_input', 'transport'}
    if set(inputs) - allowed:
        _fail('v2 carrier contains unsupported controls', 'unsupported_control')
    root, model, ref = _root(inputs), inputs.get('model'), inputs.get('credential_ref')
    if root is None or not isinstance(model, str) or not model:
        _fail('v2 requires project and exact model', 'missing_model')
    observed = observation if observation is not None else catalog.load_account_observation_v2(root, model, ref)
    if (not isinstance(observed, dict) or observed.get('model') != model
            or observed.get('schema_sha256') != digest(observed.get('input_schema'))
            or observed.get('credential_binding', {}).get('credential_ref') != ref):
        _fail('fresh exact project/key/schema observation required', 'account_observation_missing')
    schema = copy.deepcopy(observed['input_schema'])
    qualified = routes.qualify_route({'model': model, 'modality': observed.get('modality', 'video'),
        'tasks': [observed.get('task')], 'enabled': observed.get('enabled'), 'available': observed.get('available'),
        'inputSchema': schema})
    if qualified['status'] == 'blocked':
        _fail('native video route has unsupported schema or provenance requirements', 'unsupported_model')
    mode = qualified['mode']
    if inputs.get('mode', mode) != mode or inputs.get('operation') not in (None, mode, qualified['task']):
        _fail('exact native task/mode differs', 'unsupported_mode')
    try:
        payload = routes.materialize_defaults(schema, inputs['native_input'], array_item_defaults_only=True)
        for key in CREATIVE_KEYS & set(inputs):
            native_key = 'duration_seconds' if key == 'duration' else key
            if native_key not in payload or digest(inputs[key]) != digest(payload[native_key]):
                _fail('v2 creative carrier differs from exact native_input', 'conflicting_control')
        if 'generate_audio' in schema.get('properties', {}) and type(payload.get('generate_audio')) is not bool:
            _fail('Spicy requires explicit native generate_audio choice', 'missing_generate_audio')
        # A URI present in nonmedia controls never becomes trusted through pointer replacement.
        def raw_guard(value):
            if isinstance(value, dict):
                for child in value.values(): raw_guard(child)
            elif isinstance(value, list):
                for child in value: raw_guard(child)
            elif isinstance(value, str) and (routes.URI.search(value) or value.startswith('$upload:')):
                routes.fail('native_input may not contain raw source URIs')
        raw_guard(payload)
        assets = inputs.get('input_assets', [])
        if not isinstance(assets, list):
            routes.fail('input_assets must be a list')
        sources = []
        seen = set()
        def pointer_order(row):
            tokens = routes.parts(row.get('pointer')) if isinstance(row, dict) else []
            return tuple((0, int(token)) if token.isdigit() else (1, token) for token in tokens)
        for asset in sorted(assets, key=pointer_order):
            if not isinstance(asset, dict) or set(asset) - {'role','pointer','source_path','source_sha256'}:
                routes.fail('input_assets entry must contain exact role/pointer/local source/hash')
            pointer = asset.get('pointer')
            if pointer in seen:
                routes.fail('source pointer is bound more than once')
            seen.add(pointer)
            declaration = routes.media_declaration(schema, pointer)
            if asset.get('role') != declaration['role']:
                routes.fail('source role differs from exact native media declaration')
            path = _resolve_source(asset.get('source_path'), root)
            info = spicy_media.probe(path)
            if info['media_kind'] != declaration['media_kind'] or info['mime'] not in declaration['mimes']:
                routes.fail('source media does not satisfy native slot MIME')
            limits = declaration.get('limits', {})
            if info['bytes'] > limits.get('max_size_mb', 90) * 1024 * 1024:
                routes.fail('source exceeds model-specific byte limit')
            if 'max_duration_seconds' in limits and (info.get('duration_seconds') is None or info['duration_seconds'] > limits['max_duration_seconds']):
                routes.fail('source exceeds model-specific duration limit')
            sha = info['sha256']
            declared = asset.get('source_sha256')
            if declared is not None and declared != sha:
                _fail('Spicy source bytes differ from declared source_sha256', 'source_hash_mismatch')
            current = observed['credential_binding']['credential_digest']
            receipt = usable_receipt_v2(root, sha, current, info['media_kind'], info['mime'])
            if receipt is not None and receipt.get('bytes') != info['bytes']:
                _fail('Spicy source receipt byte count differs', 'upload_receipt_invalid')
            source = {'role': asset['role'], 'pointer': pointer, 'field': declaration['field'],
                'media_kind': info['media_kind'], 'mime': info['mime'], 'source_path': str(path),
                'source_sha256': sha, 'path': str(path), 'sha256': sha, 'bytes': info['bytes'],
                **({'duration_seconds': info['duration_seconds']} if info.get('duration_seconds') is not None else {}),
                'uri': receipt['uri'] if receipt else None,
                'upload_expires_at': receipt['expires_at'] if receipt else None,
                'upload_credential_digest': receipt['credential_digest'] if receipt else None,
                **({key: copy.deepcopy(receipt[key]) for key in ('transfer_binding', 'transfer_binding_sha256')} if receipt else {})}
            _set_source_pointer(payload, pointer, source['uri'] or '$upload:' + sha)
            sources.append(source)
        for declaration in qualified['media']:
            maximum = declaration.get('limits', {}).get('max_total_duration_seconds')
            group = [row for row in sources if row['field'] == declaration['field']]
            if maximum is not None and group and (any(row.get('duration_seconds') is None for row in group) or sum(row['duration_seconds'] for row in group) > maximum):
                routes.fail('source group exceeds model-specific total duration limit')
        _native_semantics_v2(model, payload)
        routes.validate_payload(schema, payload, sources, allow_placeholders=True)
        sources.sort(key=lambda row: row['pointer'])
    except routes.SchemaError as exc:
        raise SpicyError(str(exc), kind='invalid_native_input') from None
    except spicy_media.SpicyMediaError as exc:
        raise SpicyError(str(exc), kind=exc.kind) from None
    return {'provider': 'spicy', 'transport': TRANSPORT_V2, 'model': model, 'mode': mode,
        'task': qualified['task'], 'input': payload, 'input_schema': schema,
        'schema_sha256': digest(schema), 'sources': sources,
        'payload_state': 'ready' if all(row['uri'] for row in sources) else 'needs_upload',
        'request_sha256': digest({'model': model, 'input': payload})}



def _native_semantics_v2(model, payload):
    from lib.spicy_routes import fail
    if model.startswith('kling/4.0/') and 'last_image_url' in payload and len(payload.get('keyframe_image_urls', [])) > 8:
        fail('Kling closing-frame mode permits at most eight intermediate frames')
    if payload.get('shot_type') == 'custom' or payload.get('multi_shots') is True or payload.get('multi_prompt'):
        shots = payload.get('multi_prompt')
        if shots:
            try:
                total = sum(Fraction(str(row.get('duration_seconds', row.get('duration')))) for row in shots)
                expected = Fraction(str(payload['duration_seconds']))
            except (ValueError, TypeError, InvalidOperation, KeyError, AttributeError):
                fail('custom shot durations require explicit exact native timing')
            if total != expected:
                fail('custom shot durations must sum to native duration_seconds')


def require_ready_v2(native):
    """Retained-schema proof only; old original collection never requires fresh sources."""
    from lib import spicy_routes as routes
    if not isinstance(native, dict) or native.get('transport') != TRANSPORT_V2 or native.get('payload_state') != 'ready':
        _fail('Spicy native request still has unresolved source uploads', 'needs_upload')
    if native.get('schema_sha256') != digest(native.get('input_schema')):
        _fail('Spicy native input schema digest differs', 'native_schema_mismatch')
    if native.get('task') not in routes.VIDEO_OUTPUT_TASKS or native.get('mode') != native['task'].replace('-', '_'):
        _fail('Spicy native task/mode differs', 'unsupported_mode')
    if native.get('request_sha256') != digest({'model': native.get('model'), 'input': native.get('input')}):
        _fail('Spicy native request digest differs from payload', 'native_digest_mismatch')
    _validate_transfer_bindings_v2(native)
    try:
        _native_semantics_v2(native['model'], native['input'])
        routes.validate_payload(native['input_schema'], native['input'], native.get('sources', []))
    except routes.SchemaError as exc:
        raise SpicyError(str(exc), kind='invalid_native_input') from None




def _validate_transfer_bindings_v2(native):
    sources = native.get('sources')
    if not isinstance(sources, list):
        _fail('Spicy native source layout is invalid', 'native_source_invalid')
    for source in sources:
        transfer = source.get('transfer_binding')
        if (not isinstance(transfer, dict) or source.get('transfer_binding_sha256') != digest(transfer)
                or any(transfer.get(key) != source.get(other) for key, other in (('source_sha256','source_sha256'), ('credential_digest','upload_credential_digest'), ('media_kind','media_kind'), ('mime','mime'), ('bytes','bytes')))
                or not _SHA.fullmatch(str(source.get('source_sha256', '')))
                or type(source.get('bytes')) is not int or source['bytes'] <= 0
                or not isinstance(transfer.get('credential_ref'), str) or not _ENV_REF.fullmatch(transfer['credential_ref'])
                or not isinstance(transfer.get('qualified_storage_hosts'), list) or not transfer['qualified_storage_hosts']
                or any(not isinstance(host, str) or not RESULT_HOST.fullmatch(host) for host in transfer['qualified_storage_hosts'])
                or transfer['qualified_storage_hosts'] != sorted(set(transfer['qualified_storage_hosts']))):
            _fail('Spicy native source historical transfer proof differs', 'native_source_invalid')


def require_original_v2(native):
    """Frozen original proof, independent of later route qualification changes."""
    from lib.spicy_routes import pointer_get, SchemaError
    if (not isinstance(native, dict) or native.get('transport') != TRANSPORT_V2
            or native.get('payload_state') != 'ready' or not isinstance(native.get('model'), str)
            or not isinstance(native.get('input'), dict) or not isinstance(native.get('input_schema'), dict)
            or native.get('schema_sha256') != digest(native['input_schema'])
            or native.get('request_sha256') != digest({'model': native['model'], 'input': native['input']})):
        _fail('Spicy frozen original native body/schema proof differs', 'original_binding_invalid')
    _validate_transfer_bindings_v2(native)
    seen = set()
    for source in native['sources']:
        pointer = source.get('pointer')
        try:
            value = pointer_get(native['input'], pointer)
        except (SchemaError, TypeError, ValueError):
            _fail('Spicy frozen original source pointer proof differs', 'original_binding_invalid')
        if (pointer in seen or value != source.get('uri') or not isinstance(value, str)
                or not re.fullmatch(r'spicy://f/[A-Za-z0-9._/-]{1,500}', value)):
            _fail('Spicy frozen original source URI proof differs', 'original_binding_invalid')
        seen.add(pointer)


def main(argv=None):
    """Native account CLI. Public docs/status never resolve a credential."""
    import argparse
    from lib import spicy_account
    parser = argparse.ArgumentParser(description='Native Spicy account and original-job utilities')
    parser.add_argument('--credential-ref', default='env:SPICY_API_KEY')
    subparsers = parser.add_subparsers(dest='command', required=True)
    connection = subparsers.add_parser('connection-check', help='explicit read-only account diagnostic')
    connection.add_argument('--credential-ref', default=argparse.SUPPRESS)
    spicy_account.register_cli(subparsers)
    args = parser.parse_args(argv)
    try:
        if args.command == 'connection-check':
            result = connection_check(args.credential_ref)
        else:
            client = client_for_credential(args.credential_ref) if spicy_account.needs_client(args) else None
            owner_resolver = None
            if args.account_command in ('purge-execute', 'purge-reconcile'):
                from lib.spicy_lifecycle import original_resolver
                owner_resolver = original_resolver
            result = spicy_account.execute_cli(client, args, original_resolver=owner_resolver)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (SpicyError, spicy_account.AccountError) as exc:
        print(json.dumps({'ok': False, 'kind': getattr(exc, 'kind', 'account_error'), 'message': str(exc)}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
