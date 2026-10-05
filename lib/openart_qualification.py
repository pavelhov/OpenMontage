"""Pure, fail-closed validation of private OpenArt qualification evidence.

Receipt validation proves retained byte/command/contract consistency, not receipt
origin, billing price, or creative quality. Synthetic tests are not live evidence.
No provider invocation, reservation, or generation occurs here.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, unquote

from tools import _openart_cli as cli


class OpenArtQualificationError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


_REQUIRED = ("version", "source", "cli_version", "account_id_sha256", "model", "mode",
             "form_sha256", "form_defaults", "tier", "json_paths", "dry_run_endpoint")
_KINDS = {"version", "account", "form", "dry_run", "submit", "creation_get"}
_PATHS = ("account_id", "submit_job_id", "result_job_id", "status", "urls")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+))*\Z")
_HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def _fail(message: str, kind: str = "generation_unqualified") -> None:
    raise OpenArtQualificationError(kind, message)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _path(value: Any) -> bool:
    return isinstance(value, str) and bool(_PATH.fullmatch(value))


def lookup_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _hosts(value: Any, kind: str = "generation_unqualified") -> None:
    if not isinstance(value, list) or not value or any(not isinstance(h, str) or not _HOST.fullmatch(h)
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in h.split(".")) for h in value) or len(set(value)) != len(value):
        _fail("qualified hosts must be unique exact lowercase hostnames", kind)
    for host in value:
        # Match downloader._host: legacy numeric/hex labels are address-like.
        if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.rsplit(".", 1)[-1]):
            _fail("qualified hosts must be DNS names", kind)
        if host == "localhost" or host.endswith((".localhost", ".local")):
            _fail("qualified host cannot be a local target", kind)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue
        _fail("qualified hosts must be DNS names", kind)


def _url(value: Any, hosts: list, kind: str = "generation_unqualified") -> None:
    try:
        if not _text(value) or any(c.isspace() for c in value) or "\\" in value or not _text(unquote(value)):
            raise ValueError
        url = urlsplit(value)
        if url.scheme != "https" or url.hostname not in hosts or url.username is not None \
                or url.password is not None or url.fragment or url.port not in (None, 443):
            raise ValueError
    except (ValueError, TypeError):
        _fail("observed URL is not qualified HTTPS", kind)


def _record(entry: dict, kind: str = "generation_unqualified") -> dict:
    """Validate actual U1 record and retained stdout, including private 0600 files."""
    try:
        if set(entry) != {"kind", "receipt_id", "receipt_sha256"} or not _SHA.fullmatch(entry["receipt_sha256"]):
            raise ValueError
        cli.verify_private_state()
        path = cli.receipt_path(entry["receipt_id"])
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["receipt_sha256"]:
            raise ValueError
        record = json.loads(raw)
        if not isinstance(record, dict) or type(record.get("returncode")) is not int or record["returncode"] != 0 \
                or not _text(record.get("started_at")) or not isinstance(record.get("argv"), list) \
                or any(not _text(a) for a in record["argv"]) or not _SHA.fullmatch(record.get("stdout_sha256", "")):
            raise ValueError
        streams = record.get("streams")
        if not isinstance(streams, dict) or set(streams) != {"stdout", "stderr"}:
            raise ValueError
        for stream in streams.values():
            stream_path = cli.state_dir() / "streams" / cli._safe_part(stream)
            cli._check_private(stream_path, False)
            if stat.S_IMODE(stream_path.stat().st_mode) != 0o600:
                raise ValueError
        stdout = (cli.state_dir() / "streams" / streams["stdout"]).read_bytes()
        if len(stdout) > cli.MAX_STDOUT or hashlib.sha256(stdout).hexdigest() != record["stdout_sha256"] \
                or _hash(json.loads(stdout)) != _hash(record.get("parsed")):
            raise ValueError
        return record
    except (OSError, ValueError, TypeError, KeyError, cli.OpenArtCLIError):
        _fail("captured receipt bytes, transport metadata, or private streams invalid", kind)


def _records(entries: Any, expected: set, kind: str = "generation_unqualified") -> dict:
    if not isinstance(entries, list) or len(entries) != len(expected):
        _fail("captured receipt kinds incomplete or duplicated", kind)
    records = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str) \
                or entry["kind"] not in expected or entry["kind"] in records:
            _fail("unknown or duplicate captured receipt kind", kind)
        records[entry["kind"]] = _record(entry, kind)
    return records


def _argv(record: dict, expected: list) -> None:
    if record["argv"] != expected + cli.GLOBAL_FLAGS:
        _fail("captured command does not bind the qualified contract")


def verify_captured(profile: dict) -> None:
    try:
        _verify_captured(profile)
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("captured profile contract malformed")


def _verify_captured(profile: dict) -> None:
    records = _records(profile.get("captured_receipts"), _KINDS)
    paths = profile["json_paths"]
    _argv(records["version"], ["version"])
    if lookup_path(records["version"]["parsed"], "version") != profile["cli_version"]:
        _fail("observed CLI version differs from profile")
    _argv(records["account"], ["account"])
    account = lookup_path(records["account"]["parsed"], paths["account_id"])
    if not _text(account) or hashlib.sha256(account.encode()).hexdigest() != profile["account_id_sha256"] \
            or lookup_path(records["account"]["parsed"], paths["account_tier"]) != profile["tier"]:
        _fail("observed account identity or tier differs from profile")
    try:
        _argv(records["form"], cli.model_form_argv(profile["model"], profile["mode"]))
        form = records["form"]["parsed"]
        controls = cli.form_controls(form)
    except cli.OpenArtCLIError:
        _fail("model form controls are unsupported", "unsupported_gate")
    if _hash(form) != profile["form_sha256"]:
        _fail("observed form hash differs from profile")
    schema = form.get("schema", form)
    defaults = {name: spec["default"] for name, spec in schema["properties"].items()
                if isinstance(spec, dict) and "default" in spec}
    if _hash(defaults) != _hash(profile["form_defaults"]):
        _fail("observed form defaults differ from profile")
    if "prompt" not in controls:
        _fail("video form lacks prompt control", "unsupported_gate")
    dry = records["dry_run"]["parsed"]
    if not isinstance(dry, dict) or dry.get("endpoint") != profile["dry_run_endpoint"] \
            or profile["dry_run_endpoint"] != "POST /api/cli/v1/generate":
        _fail("observed preview endpoint differs from official contract")
    body = dry.get("body")
    if not isinstance(body, dict) or set(body) != {"model", "media", "mode", "params"} \
            or body["model"] != profile["model"] or body["mode"] != profile["mode"] or body["media"] != "video" \
            or not isinstance(body["params"], dict):
        _fail("preview model/mode/body differs from profile")
    params = body["params"]
    if not set(params) <= {"prompt", "duration", "aspectRatio", "resolution", "image"}:
        _fail("unsupported native controls", "unsupported_gate")
    native_controls = {"prompt": "prompt", "duration": "duration", "aspectRatio": "aspectRatio",
                       "resolution": "resolution", "image": "image"}
    if any(value is None or native_controls[name] not in controls for name, value in params.items()):
        _fail("preview requests absent or null form controls", "unsupported_gate")
    for name, value in params.items():
        enum = controls[native_controls[name]].get("enum")
        if enum is not None and (not isinstance(enum, list) or value not in enum):
            _fail("preview control outside observed form enum", "unsupported_gate")
    try:
        creative = cli.native_video_argv(params.get("prompt"), model=profile["model"], mode=profile["mode"],
            duration=params.get("duration"), aspect_ratio=params.get("aspectRatio"),
            resolution=params.get("resolution"), image_url=params.get("image"))
    except cli.OpenArtCLIError:
        _fail("preview native controls invalid", "unsupported_gate")
    _argv(records["dry_run"], creative + ["--dry-run"])
    _argv(records["submit"], creative + ["--async"])
    job = lookup_path(records["submit"]["parsed"], paths["submit_job_id"])
    if not _text(job) or job.startswith("-"):
        _fail("submit job identity missing or unsafe")
    _argv(records["creation_get"], ["creation", "get", job])
    result = records["creation_get"]["parsed"]
    if lookup_path(result, paths["result_job_id"]) != job:
        _fail("creation result differs from original submit job")
    status = lookup_path(result, paths["status"])
    if status != paths["status_terminal_ok"]:
        _fail("qualification requires observed successful terminal output evidence")
    if status == paths["status_terminal_ok"]:
        urls = lookup_path(result, paths["urls"])
        if not isinstance(urls, list) or not urls:
            _fail("successful result lacks URL examples")
        for url in urls:
            _url(url, paths["url_hosts"])
    if profile["mode"] == "image2video":
        retained_url = _qualified_upload_url(profile)
        if params.get("image") != retained_url:
            _fail("native preview image differs from exact verified upload URL")


def validate_upload_contract(profile: dict) -> None:
    """Validate explicit provider upload guarantees; never infer them from labels."""
    _qualified_upload_url(profile)


def _qualified_upload_url(profile: dict) -> str:
    """Require provider-declared nonspending AND no-delayed-charge guarantees.

    `guarantee` declares exact readonly argv (without global flags), and assertion
    objects `nonspending` and `no_delayed_charge`, each {path, expected}. The former
    accepts native true or integer zero; the latter requires native true. Both
    are checked in raw guarantee AND upload JSON. Labels/balance are insufficient.
    No actual provider field names or guarantee are presumed to exist.
    """
    kind = "unsupported_gate"
    upload = profile.get("upload")
    if not isinstance(upload, dict):
        _fail("image2video requires qualified nonspending upload", kind)
    paths = upload.get("json_paths", {})
    if not isinstance(paths, dict) or not _path(paths.get("upload_url")):
        _fail("upload URL path missing or unsafe", kind)
    _hosts(upload.get("url_hosts"), kind)
    guarantee = upload.get("guarantee")
    if not isinstance(guarantee, dict) or set(guarantee) != {"argv", "nonspending", "no_delayed_charge"}:
        _fail("explicit nonspending and delayed-charge guarantee absent", kind)
    argv = guarantee["argv"]
    try:
        if not isinstance(argv, list) or any(not _text(a) for a in argv) or "--dry-run" in argv:
            raise ValueError
        cli._check_read_only(argv)
    except (ValueError, TypeError, cli.OpenArtCLIError):
        _fail("guarantee command must be exact official readonly grammar", kind)
    for name in ("nonspending", "no_delayed_charge"):
        assertion = guarantee[name]
        if not isinstance(assertion, dict) or set(assertion) != {"path", "expected"} or not _path(assertion["path"]):
            _fail("guarantee assertion must declare raw path and native expected value", kind)
        value = assertion["expected"]
        if name == "nonspending" and not (value is True or type(value) is int and value == 0) \
                or name == "no_delayed_charge" and value is not True:
            _fail("guarantee expected value does not prove nonspending/no delayed charge", kind)
    records = _records(upload.get("receipts"), {"nonspending_guarantee", "upload"}, kind)
    if records["nonspending_guarantee"]["argv"] != argv + cli.GLOBAL_FLAGS:
        _fail("guarantee command differs from captured command", kind)
    upload_argv = records["upload"]["argv"]
    if len(upload_argv) != 5 or upload_argv[:2] != ["upload", "add"] or upload_argv[3:] != cli.GLOBAL_FLAGS \
            or not Path(upload_argv[2]).is_absolute() or not _text(upload_argv[2]):
        _fail("upload receipt does not bind official upload command", kind)
    for record in records.values():
        for name in ("nonspending", "no_delayed_charge"):
            assertion = guarantee[name]
            value = lookup_path(record["parsed"], assertion["path"])
            if type(value) is not type(assertion["expected"]) or value != assertion["expected"]:
                _fail("raw provider receipt does not prove both upload guarantees", kind)
    url = lookup_path(records["upload"]["parsed"], paths["upload_url"])
    _url(url, upload["url_hosts"], kind)
    return url


def validate_profile(profile: dict, *, allow_fixture: bool = False) -> dict:
    if not isinstance(profile, dict) or any(key not in profile for key in _REQUIRED):
        _fail("qualification profile missing required typed fields")
    if profile["version"] != "1" or profile["source"] not in ("real", "fixture") \
            or profile["source"] == "fixture" and not allow_fixture:
        _fail("qualification requires captured real evidence or explicit fixture seam")
    for key in ("cli_version", "model", "tier", "dry_run_endpoint"):
        if not _text(profile[key]):
            _fail("qualification profile text field invalid")
    if profile["mode"] not in ("text2video", "image2video"):
        _fail("unsupported generation mode", "unsupported_gate")
    for key in ("account_id_sha256", "form_sha256"):
        if not isinstance(profile[key], str) or not _SHA.fullmatch(profile[key]):
            _fail("qualification hash field invalid")
    paths = profile["json_paths"]
    if not isinstance(paths, dict) or any(not _path(paths.get(k)) for k in _PATHS) \
            or profile["source"] == "real" and not _path(paths.get("account_tier")):
        _fail("qualification JSON paths missing or unsafe")
    if not _text(paths.get("status_terminal_ok")) or not _text(paths.get("status_terminal_fail")) \
            or paths["status_terminal_ok"] == paths["status_terminal_fail"]:
        _fail("qualified terminal statuses must be distinct native strings")
    _hosts(paths.get("url_hosts"))
    if not isinstance(profile["form_defaults"], dict) or any(not _text(k) for k in profile["form_defaults"]):
        _fail("form defaults must be an object")
    try:
        normalized = json.loads(json.dumps(profile, allow_nan=False))
    except (ValueError, TypeError):
        _fail("profile must contain finite JSON values")
    if profile["source"] == "real":
        verify_captured(normalized)
    if profile["mode"] == "image2video":
        validate_upload_contract(normalized)
    return normalized
