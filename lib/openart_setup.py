"""Staged first-account qualification using actual private readonly captures.

No credit reservation or generation submission occurs here. JSON paths describe
provider fields; absent provider guarantees remain unsupported, never inferred.
"""
from __future__ import annotations

import copy
import hashlib
import time
from contextlib import contextmanager
from typing import Any

from tools import _openart_cli as cli
from lib import openart_qualification as qual


@contextmanager
def _session(timeout: float):
    timeout = cli.validate_timeout(timeout)
    deadline = time.monotonic() + timeout
    with cli.transport_lock(wait_timeout=timeout):
        yield lambda: cli.lock_remaining(deadline)


def verify_current(profile: dict, *, timeout: float = cli.DEFAULT_TIMEOUT) -> None:
    """Recheck the entire inspected contract under one lock and overall budget."""
    with _session(timeout) as remaining:
        _current(profile, remaining())


def _entry(kind: str, receipt: dict) -> dict:
    return {"kind": kind, "receipt_id": receipt["receipt_id"],
            "receipt_sha256": receipt["receipt_sha256"]}


def _checked(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except qual.OpenArtQualificationError as exc:
        raise cli.OpenArtCLIError(exc.kind, exc.message) from exc


def _guarantee_shape(upload: Any) -> dict:
    if not isinstance(upload, dict):
        raise cli.OpenArtCLIError("unsupported_gate", "explicit upload guarantees required")
    paths = upload.get("json_paths")
    if not isinstance(paths, dict) or set(paths) != {"upload_url"} or not qual._path(paths["upload_url"]):
        raise cli.OpenArtCLIError("unsupported_gate", "exact upload URL JSON path required")
    _checked(qual._hosts, upload.get("url_hosts"), "unsupported_gate")
    guarantee = upload.get("guarantee")
    if not isinstance(guarantee, dict) or set(guarantee) != {"argv", "nonspending", "no_delayed_charge"}:
        raise cli.OpenArtCLIError("unsupported_gate", "both explicit upload guarantees required")
    argv = guarantee["argv"]
    if not isinstance(argv, list) or not argv or any(not qual._text(x) for x in argv) or "--dry-run" in argv:
        raise cli.OpenArtCLIError("unsupported_gate", "guarantee requires exact readonly command")
    cli._check_read_only(argv)
    for name in ("nonspending", "no_delayed_charge"):
        assertion = guarantee[name]
        if not isinstance(assertion, dict) or set(assertion) != {"path", "expected"} or not qual._path(assertion["path"]):
            raise cli.OpenArtCLIError("unsupported_gate", "guarantee assertions require exact typed paths")
        value = assertion["expected"]
        if name == "nonspending" and not (value is True or type(value) is int and value == 0) or name == "no_delayed_charge" and value is not True:
            raise cli.OpenArtCLIError("unsupported_gate", "expected values must prove zero spending and no delayed charge")
    return guarantee


def validate_upload_guarantee(profile: dict) -> None:
    """Pure prerequisite for the first upload; rechecks captured raw guarantees."""
    upload = profile.get("upload")
    guarantee = _guarantee_shape(upload)
    entries = upload.get("receipts")
    if not isinstance(entries, list):
        raise cli.OpenArtCLIError("unsupported_gate", "raw upload guarantee receipt required")
    captured = [entry for entry in entries if isinstance(entry, dict) and entry.get("kind") == "nonspending_guarantee"]
    records = _checked(qual._records, captured, {"nonspending_guarantee"}, "unsupported_gate")
    record = records["nonspending_guarantee"]
    if record["argv"] != guarantee["argv"] + cli.GLOBAL_FLAGS:
        raise cli.OpenArtCLIError("unsupported_gate", "guarantee captured command differs")
    _assert_guarantees(guarantee, record)


def _assert_guarantees(guarantee: dict, record: dict, *, kind: str = "unsupported_gate") -> None:
    for name in ("nonspending", "no_delayed_charge"):
        assertion = guarantee[name]
        observed = qual.lookup_path(record["parsed"], assertion["path"])
        if type(observed) is not type(assertion["expected"]) or observed != assertion["expected"]:
            raise cli.OpenArtCLIError(kind, "raw provider evidence does not prove both upload guarantees")


def _capture_inspection(model: str, mode: str, json_paths: dict, timeout: float) -> dict:
    model, mode = cli._ident(model, "model"), cli._ident(mode, "mode")
    if mode not in ("text2video", "image2video") or not isinstance(json_paths, dict):
        raise cli.OpenArtCLIError("unsupported_gate", "supported mode and declared JSON paths required")
    # Validate declarations before any provider call; no field names are guessed.
    dummy = {"version": "1", "source": "fixture", "cli_version": "declaration", "model": model,
             "mode": mode, "tier": "declaration", "account_id_sha256": "0" * 64,
             "form_sha256": "0" * 64, "form_defaults": {}, "json_paths": json_paths,
             "dry_run_endpoint": "POST /api/cli/v1/generate", "result_contract": {"state": "unqualified"}}
    if not qual._path(json_paths.get("account_tier")):
        raise cli.OpenArtCLIError("unsupported_gate", "account tier JSON path required")
    _checked(qual.validate_profile, dict(dummy, mode="text2video"), allow_fixture=True, require="inspected")
    with _session(timeout) as remaining:
        version = cli.run_readonly(["version"], timeout=remaining())
        account = cli.run_readonly(["account"], timeout=remaining())
        form = cli.run_readonly(cli.model_form_argv(model, mode), timeout=remaining())
    identity = qual.lookup_path(account["parsed"], json_paths["account_id"])
    if not qual._text(identity):
        raise cli.OpenArtCLIError("generation_unqualified", "observed account identity missing")
    schema = cli.form_schema(form["parsed"], model=model, mode=mode)
    cli.form_controls(form["parsed"], model=model, mode=mode)
    profile = dict(dummy, source="real", cli_version=qual.lookup_path(version["parsed"], "version"),
                   account_id_sha256=hashlib.sha256(identity.encode()).hexdigest(),
                   tier=qual.lookup_path(account["parsed"], json_paths["account_tier"]),
                   form_sha256=qual._hash(form["parsed"]),
                   form_defaults={name: spec["default"] for name, spec in schema["properties"].items()
                                  if isinstance(spec, dict) and "default" in spec},
                   captured_receipts=[_entry("version", version), _entry("account", account), _entry("form", form)])
    return _checked(qual.validate_profile, profile, require="inspected")


def inspect_qualification(model: str, mode: str, *, json_paths: dict, timeout: float = cli.DEFAULT_TIMEOUT) -> dict:
    from lib import openart_jobs as jobs
    profile = _capture_inspection(model, mode, copy.deepcopy(json_paths), cli.validate_timeout(timeout))
    return jobs.save_profile(profile, require="inspected")


def _current(profile: dict, timeout: float) -> None:
    # Preserve one deadline across inspection and fresh guarantee capture. The
    # reentrant transport lock spans both and the caller's upload/preview launch.
    guarantee = None
    if "upload" in profile:
        validate_upload_guarantee(profile)
        guarantee = _guarantee_shape(profile["upload"])
    with _session(timeout) as remaining:
        observed = _capture_inspection(profile["model"], profile["mode"], profile["json_paths"], remaining())
        keys = ("cli_version", "account_id_sha256", "tier", "form_sha256", "form_defaults", "model", "mode")
        if any(observed[k] != profile[k] for k in keys):
            raise cli.OpenArtCLIError("qualification_changed", "current version/account/tier/form/defaults differ from inspected contract")
        if guarantee is not None:
            receipt = cli.run_readonly(guarantee["argv"], timeout=remaining())
            record = _checked(qual._record, _entry("nonspending_guarantee", receipt), "qualification_changed")
            if record["argv"] != guarantee["argv"] + cli.GLOBAL_FLAGS:
                raise cli.OpenArtCLIError("qualification_changed", "fresh guarantee captured command differs")
            _assert_guarantees(guarantee, record, kind="qualification_changed")


def qualify_upload_guarantee(model: str, mode: str, *, guarantee: dict, json_paths: dict,
                             url_hosts: list, timeout: float = cli.DEFAULT_TIMEOUT) -> dict:
    from lib import openart_jobs as jobs
    upload = copy.deepcopy({"guarantee": guarantee, "json_paths": json_paths, "url_hosts": url_hosts})
    declaration = _guarantee_shape(upload)  # malformed/absent guarantees: zero calls
    timeout = cli.validate_timeout(timeout)
    profile = jobs.load_qualification(model=model, mode=mode, require="inspected")
    if qual.profile_level(profile) != "inspected" or mode != "image2video":
        raise cli.OpenArtCLIError("unsupported_gate", "upload setup requires an inspected image2video profile")
    with _session(timeout) as remaining:
        _current(profile, remaining())
        receipt = cli.run_readonly(declaration["argv"], timeout=remaining())
    upload["receipts"] = [_entry("nonspending_guarantee", receipt)]
    profile["upload"] = upload
    validate_upload_guarantee(profile)
    return jobs.save_profile(profile, require="inspected")


def _with_upload(profile: dict, image_upload_id: str) -> dict:
    from lib import openart_jobs as jobs
    profile = copy.deepcopy(profile)
    validate_upload_guarantee(profile)
    # Main helper revalidates exact source snapshot, raw URL, account, host and argv.
    jobs.upload_url_for(image_upload_id, profile=profile, account_id_sha256=profile["account_id_sha256"])
    evidence = jobs.retained_upload_evidence(image_upload_id, profile=profile)
    profile["upload"]["receipts"] = [e for e in profile["upload"]["receipts"] if e["kind"] == "nonspending_guarantee"] + [evidence]
    _checked(qual.validate_upload_contract, profile)
    return profile


def promote_upload_contract(model: str, mode: str, *, image_upload_id: str) -> dict:
    from lib import openart_jobs as jobs
    profile = jobs.load_qualification(model=model, mode=mode, require="inspected")
    profile = _with_upload(profile, cli._ident(image_upload_id, "image_upload_id"))
    return jobs.save_profile(profile, require="inspected")


def qualify_preview(model: str, mode: str, *, prompt: str, duration: Any = None,
                    aspect_ratio: str | None = None, resolution: str | None = None,
                    image_upload_id: str | None = None, timeout: float = cli.DEFAULT_TIMEOUT) -> dict:
    from lib import openart_jobs as jobs
    timeout = cli.validate_timeout(timeout)
    profile = jobs.load_qualification(model=model, mode=mode, require="inspected")
    if qual.profile_level(profile) != "inspected":
        raise cli.OpenArtCLIError("unsupported_gate", "preview setup requires inspected profile; qualified creative contracts are immutable")
    image_url = None
    if mode == "image2video":
        profile = _with_upload(profile, cli._ident(image_upload_id, "image_upload_id"))
        image_url = jobs.upload_url_for(image_upload_id, profile=profile, account_id_sha256=profile["account_id_sha256"])
    elif image_upload_id is not None:
        raise cli.OpenArtCLIError("unsupported_gate", "text2video cannot bind an image upload")
    argv = cli.native_dry_run_argv(prompt, model=model, mode=mode, duration=duration,
                                   aspect_ratio=aspect_ratio, resolution=resolution, image_url=image_url)
    with _session(timeout) as remaining:
        _current(profile, remaining())
        with cli.allow_image_reference(image_url is not None):
            receipt = cli.run_readonly(argv, timeout=remaining())
    profile["captured_receipts"].append(_entry("dry_run", receipt))
    return jobs.save_profile(profile, require="pre_submit")
