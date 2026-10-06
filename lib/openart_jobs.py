"""U2 OpenArt CLI async job lifecycle (submit-once, hold, collect, reconcile).

Pure APIs for U3 (compiler) and U4 (ledger). No creative orchestration here.

Invariants:
- Generation is unavailable unless a *real* captured qualification profile exists.
- Launch requires a real active reservation found through the registered lookup
  (U4 ledger); caller-supplied dicts/tokens are never trusted. Default fails closed.
- Raw stdout/stderr files are opened (exclusive, 0600) before Popen and survive
  parent death. Submission is never retried and never resumed as a new attempt.
- Unknown job id -> hold_unknown_job; never matched by timestamp/prompt similarity.
- No outcome here releases a reservation; reconcile only reports evidence.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import math
import json
import re
import inspect
import os
import stat
import struct
import sys
import time
import uuid
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable, Optional

from tools import _openart_cli as cli
from tools._openart_cli import OpenArtCLIError

PROFILE_VERSION = "1"
ALLOWED_INPUTS = ("prompt", "model", "mode", "duration", "aspect_ratio", "resolution", "output_path",
                  "image_path", "image_upload_id", "operation", "native_dry_run_receipt_id",
                  "native_dry_run_receipt_sha256")
# Tolerated pass-through keys that never enter the native request.
IGNORED_INPUTS = ("project_dir", "output_path", "operation", "cli_session_id",
                  "native_dry_run_receipt_id", "native_dry_run_receipt_sha256")
UNSUPPORTED_INPUTS = ("image", "image_url", "first_frame", "start_frame", "last_frame",
                      "end_frame", "last_image_path", "end_image_path", "reference_image_path",
                      "reference_image_paths", "reference_images", "audio", "audio_path", "keyframes",
                      "voices", "rich_references", "end_image", "last_frame_path", "audio_url")
_PROFILE_REQUIRED = ("version", "source", "cli_version", "account_id_sha256", "model", "mode",
                     "form_sha256", "form_defaults", "tier", "json_paths", "dry_run_endpoint")
_JSON_PATHS_REQUIRED = ("account_id", "submit_job_id", "result_job_id", "status",
                        "status_terminal_ok", "status_terminal_fail", "urls", "url_hosts")
REQUIRED_RECEIPT_KINDS = ("version", "account", "form", "dry_run", "submit", "creation_get")
_BODY_PARAM_NAMES = {"prompt": "prompt", "duration": "duration", "aspect_ratio": "aspectRatio",
                     "resolution": "resolution"}


def _canon(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


# ---------------------------------------------------------------- qualification

def profile_path() -> Path:
    return cli.state_dir() / "qualification" / "profile.json"


def profile_path_for(model: str, mode: str) -> Path:
    """Per-model/mode private catalog path (alternating Turbo/Max never overwrite each other)."""
    return cli.state_dir() / "qualification" / cli._safe_part(model) / cli._safe_part(mode) / "profile.json"


def _receipt_parsed(receipt_id: str, receipt_sha256: str) -> Any:
    """Verify one captured private receipt (permissions + digest) and return its parsed body."""
    path = cli.receipt_path(receipt_id)
    try:
        cli._check_private(path, False)
        raw = path.read_bytes()
    except (OSError, OpenArtCLIError):
        raise OpenArtCLIError("generation_unqualified", "captured receipt missing or not private")
    if hashlib.sha256(raw).hexdigest() != receipt_sha256:
        raise OpenArtCLIError("generation_unqualified", "captured receipt digest mismatch")
    try:
        return json.loads(raw).get("parsed")
    except (ValueError, AttributeError):
        raise OpenArtCLIError("generation_unqualified", "captured receipt unreadable")


def _qual():
    from lib import openart_qualification as qual
    return qual


def _delegate(fn, *args, **kwargs):
    qual = _qual()
    try:
        return fn(qual)(*args, **kwargs)
    except qual.OpenArtQualificationError as exc:
        raise OpenArtCLIError(exc.kind, exc.message) from None


def _verify_captured(profile: dict) -> None:
    _delegate(lambda q: q.verify_captured, profile)


def _validate_upload_contract(profile: dict) -> None:
    _delegate(lambda q: q.validate_upload_contract, profile)


def validate_profile(profile: Any, *, allow_fixture: bool = False, require: str = "full") -> dict:
    """Delegates to lib.openart_qualification (real captured-proof / upload-guarantee checks).

    For staged profiles (result_contract unqualified) `require='full'` is satisfied only by
    load_qualification, which checks the separate result proof bound to the unchanged SHA.
    """
    return _delegate(lambda q: q.validate_profile, profile, allow_fixture=allow_fixture, require=require)


def _is_staged(profile: dict) -> bool:
    return isinstance(profile, dict) and "result_contract" in profile


def result_proof_path(model: str, mode: str, origin_sha: str) -> Path:
    if not isinstance(origin_sha, str) or len(origin_sha) != 64:
        raise OpenArtCLIError("result_contract_unqualified", "invalid origin profile digest")
    return (cli.state_dir() / "qualification" / cli._safe_part(model) / cli._safe_part(mode)
            / "results" / (cli._safe_part(origin_sha) + ".json"))


def load_result_proof(profile: dict) -> dict:
    """Pure: verify the immutable private result proof bound to this exact origin profile SHA.

    Returns {result_contract_sha256, result_proof_id, json_paths, evidence}. Never rewrites
    the profile or launch. Missing/tampered -> result_contract_unqualified.
    """
    origin = profile.get("profile_sha256") or sha256_json(
        {k: v for k, v in profile.items() if k != "profile_sha256"})
    if not _is_staged(profile):
        return {"result_contract_sha256": None, "result_proof_id": None,
                "json_paths": profile["json_paths"], "evidence": None, "legacy_full": True}
    try:
        target = result_proof_path(profile["model"], profile["mode"], origin)
        cli._check_private(target, False)
        with open(target, "rb") as fh:
            raw = fh.read(cli.MAX_STDOUT + 1)
        if len(raw) > cli.MAX_STDOUT:
            raise ValueError
        proof = json.loads(raw)
    except (OSError, ValueError, OpenArtCLIError):
        raise OpenArtCLIError("result_contract_unqualified", "no verified result proof for this profile")
    if not isinstance(proof, dict) or proof.get("origin_profile_sha256") != origin \
            or proof.get("model") != profile["model"] or proof.get("mode") != profile["mode"] \
            or proof.get("account_id_sha256") != profile["account_id_sha256"] \
            or not isinstance(proof.get("json_paths"), dict) or not isinstance(proof.get("evidence"), dict):
        raise OpenArtCLIError("result_contract_unqualified", "result proof does not bind this profile")
    _verify_result_evidence(profile, proof)
    return {"result_contract_sha256": hashlib.sha256(raw).hexdigest(),
            "result_proof_id": origin, "json_paths": proof["json_paths"],
            "evidence": proof["evidence"], "legacy_full": False}


def _verify_result_evidence(profile: dict, proof: dict) -> None:
    """Pure full re-verification on every proof load (no CLI/lock; uses the proof's own paths).

    Original governed qualification launch (sha + frozen origin profile + purpose), original raw
    submit stdout re-parsed with the proof's submit path == recorded job and the single parsed
    event, actual account receipt == profile account, and creation_get receipt for that exact
    job with qualified terminal-ok status and exactly one qualified-host URL.
    """
    bad = "result_contract_unqualified"
    ev = proof["evidence"]
    paths = proof["json_paths"]
    try:
        aid = cli._safe_part(ev["attempt_id"])
        launch = launch_record(aid)
        if launch is None or _launch_sha256(aid) != ev["launch_sha256"]:
            raise OpenArtCLIError(bad, "result proof launch differs")
        lprof = launch.get("profile") or {}
        origin = proof["origin_profile_sha256"]
        if launch.get("purpose") != QUALIFICATION_PURPOSE or lprof.get("profile_sha256") != origin \
                or sha256_json({k: v for k, v in lprof.items() if k != "profile_sha256"}) != origin \
                or lprof.get("source") != "real" or launch["binding"].get("profile_sha256") != origin \
                or launch["binding"].get("account_id_sha256") != profile["account_id_sha256"]:
            raise OpenArtCLIError(bad, "result proof launch is not the origin qualification attempt")
        _verify_origin_frozen(aid, launch, bad)
        if set(paths) != {"submit_job_id", "result_job_id", "status", "urls", "status_terminal_ok",
                          "status_terminal_fail", "url_hosts"}:
            raise OpenArtCLIError(bad, "result proof paths incomplete")
        _validate_result_paths(paths, bad)
        job_id = original_job_id(aid)
        if job_id is None or _sha(job_id) != ev["job_id_sha256"]:
            raise OpenArtCLIError(bad, "result proof job differs")
        merged = dict(launch, profile=dict(lprof, json_paths=dict(lprof["json_paths"], **paths)))
        submit = _verify_raw_submit(aid, merged, job_id, bad, proven=False)
        if submit["submit_stdout_sha256"] != ev["submit_stdout_sha256"] \
                or submit["submit_parse_sha256"] != ev["submit_parse_sha256"]:
            raise OpenArtCLIError(bad, "result proof raw submit differs")
        arec = _qual_record(ev["account_receipt"], "result_account", bad)
        if _load_receipt(ev["account_receipt"]["receipt_id"], ev["account_receipt"]["receipt_sha256"],
                         bad).get("parsed") != arec.get("parsed"):
            raise OpenArtCLIError(bad, "result proof account receipt differs")
        if arec.get("argv") != ["account"] + cli.GLOBAL_FLAGS:
            raise OpenArtCLIError(bad, "result proof account argv differs")
        acct = lookup_path(arec.get("parsed"), profile["json_paths"]["account_id"])
        if not isinstance(acct, str) or _sha(acct) != profile["account_id_sha256"]:
            raise OpenArtCLIError(bad, "result proof account differs")
        crec = _qual_record(ev["creation_get_receipt"], "result_creation_get", bad)
        _proof_status(crec, job_id, paths, bad)
    except OpenArtCLIError as exc:
        raise OpenArtCLIError(bad, exc.message if hasattr(exc, "message") else str(exc))
    except (KeyError, TypeError, AttributeError, ValueError):
        raise OpenArtCLIError(bad, "result proof evidence malformed")


def _verify_origin_frozen(aid: str, launch: dict, kind: str) -> dict:
    """Original frozen snapshot/native/profile still bind exactly to the launch marker (pure)."""
    try:
        frozen = load_frozen_request(aid)
    except OpenArtCLIError:
        raise OpenArtCLIError(kind, "original frozen request missing or tampered")
    native, b = frozen["native"], launch.get("binding") or {}
    gflags = list(cli.GLOBAL_FLAGS)
    largv = launch.get("argv")
    if frozen["snapshot_sha256"] != b.get("snapshot_sha256") or frozen["profile"] != launch.get("profile") \
            or not isinstance(largv, list) or len(largv) < len(gflags) \
            or largv[len(largv) - len(gflags):] != gflags \
            or sha256_json(largv[:len(largv) - len(gflags)]) != b.get("native_argv_sha256") \
            or any(native.get(k) != b.get(k) for k in (
                "native_controls_sha256", "native_argv_sha256", "profile_sha256",
                "account_id_sha256", "native_body_sha256")) \
            or native.get("account_id_sha256") != frozen["profile"].get("account_id_sha256"):
        raise OpenArtCLIError(kind, "original frozen request differs from launch binding")
    return frozen


def _validate_result_paths(paths: dict, kind: str) -> None:
    """Reuse qualification validators: dotted paths, control-free terminal values, DNS-only hosts."""
    from lib import openart_qualification as qual
    for key in ("submit_job_id", "result_job_id", "status", "urls"):
        if not qual._path(paths.get(key)):
            raise OpenArtCLIError(kind, f"result path {key} is not a qualified json path")
    for key in ("status_terminal_ok", "status_terminal_fail"):
        if not qual._text(paths.get(key)):
            raise OpenArtCLIError(kind, f"terminal value {key} is not qualified text")
    if paths["status_terminal_ok"] == paths["status_terminal_fail"]:
        raise OpenArtCLIError(kind, "terminal ok/fail values must differ")
    try:
        qual._hosts(paths.get("url_hosts"), kind)
    except qual.OpenArtQualificationError as exc:
        raise OpenArtCLIError(kind, str(exc))


def _qual_record(entry: dict, name: str, kind: str) -> dict:
    """Transport receipt + retained private stdout stream integrity via qual._record."""
    from lib import openart_qualification as qual
    try:
        return qual._record({"kind": name, "receipt_id": entry["receipt_id"],
                             "receipt_sha256": entry["receipt_sha256"]}, kind)
    except qual.OpenArtQualificationError as exc:
        raise OpenArtCLIError(kind, str(exc))


def _proof_status(rec: dict, job_id: str, paths: dict, kind: str) -> str:
    """creation_get receipt for job_id with qualified ok status and exactly one qualified URL."""
    if rec.get("argv") != ["creation", "get", job_id] + cli.GLOBAL_FLAGS:
        raise OpenArtCLIError(kind, "creation_get argv differs from original job")
    parsed = rec.get("parsed")
    if lookup_path(parsed, paths["result_job_id"]) != job_id:
        raise OpenArtCLIError(kind, "creation_get does not correlate to original job")
    if lookup_path(parsed, paths["status"]) != paths["status_terminal_ok"]:
        raise OpenArtCLIError(kind, "creation_get status is not the qualified terminal success")
    urls = lookup_path(parsed, paths["urls"])
    urls = [urls] if isinstance(urls, str) else urls
    if not isinstance(urls, list) or len(urls) != 1:
        raise OpenArtCLIError(kind, "creation_get lacks exactly one qualified-host URL")
    from lib import openart_qualification as qual
    try:
        qual._url(urls[0], list(paths["url_hosts"]), kind)
    except qual.OpenArtQualificationError:
        raise OpenArtCLIError(kind, "creation_get lacks exactly one qualified-host URL")
    return urls[0]


def _effective_paths(profile: dict) -> dict:
    """json_paths in force for status/collection: profile paths + result-proof paths (staged)."""
    if not _is_staged(profile):
        return profile["json_paths"]
    proof = load_result_proof(profile)
    return dict(profile["json_paths"], **proof["json_paths"])


def qualification_status(model: str, mode: str) -> dict:
    """Pure readiness: level in {none, inspected, pre_submit, full}; never raises, no CLI."""
    row = {"level": "none", "profile_sha256": None, "result_contract_sha256": None,
           "result_proof_id": None, "error": None}
    for level in ("inspected", "pre_submit", "full"):
        try:
            prof = load_qualification(model=model, mode=mode, require=level)
        except OpenArtCLIError as exc:
            if row["error"] is None or level == "inspected":
                row["error"] = exc.kind
            break
        row.update(level=level, profile_sha256=prof["profile_sha256"], error=None)
        if level == "full" and _is_staged(prof):
            proof = load_result_proof(prof)
            row.update(result_contract_sha256=proof["result_contract_sha256"],
                       result_proof_id=proof["result_proof_id"])
    return row


def save_profile(profile: dict, *, require: str = "inspected") -> dict:
    """Validate (real only) then atomically publish private profile.<sha>.json + profile.json.

    Returns {profile_id, profile_sha256, level}; never a private path.
    """
    body = {k: v for k, v in profile.items() if k != "profile_sha256"}
    if body.get("source") != "real":
        raise OpenArtCLIError("generation_unqualified", "only real captured profiles may be saved")
    body = validate_profile(body, require=require)
    sha = sha256_json(body)
    folder = cli.private_dir("qualification", body["model"], body["mode"])
    data = _canon(body)
    snap = folder / f"profile.{sha}.json"
    if not snap.exists():
        cli.write_private(snap, data)
    tmp = folder / f".profile.{sha}.{os.getpid()}.tmp"
    cli.write_private(tmp, data)
    os.replace(tmp, folder / "profile.json")
    dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    level = _qual().profile_level(body)
    return {"profile_id": sha, "profile_sha256": sha, "level": level}


def load_qualification(path: Optional[Path] = None, *, model: Optional[str] = None,
                       mode: Optional[str] = None, allow_fixture: bool = False,
                       require: str = "full") -> dict:
    """Load the private captured profile. Missing/non-real/unproven -> generation_unqualified.

    `allow_fixture` is a test seam only; fixture profiles keep source='fixture' and can
    never launch unless the module test seam `_ALLOW_FIXTURE_LAUNCH` is set.
    """
    if path is not None:
        target = Path(path)
    elif model is not None or mode is not None:
        if not model or not mode:
            raise OpenArtCLIError("generation_unqualified", "model and mode are both required")
        try:
            target = profile_path_for(model, mode)
        except OpenArtCLIError:
            raise OpenArtCLIError("generation_unqualified", "unsafe model/mode for profile path")
    else:
        target = profile_path()
    root = cli.state_dir()
    try:
        target.relative_to(root)
    except ValueError:
        raise OpenArtCLIError("generation_unqualified", "profile must live in private state")
    if not target.exists() and not target.is_symlink():
        raise OpenArtCLIError("generation_unqualified", "no captured OpenArt qualification profile")
    try:
        cli._check_private(target, False)
        profile = json.loads(target.read_text())
    except OpenArtCLIError:
        raise OpenArtCLIError("generation_unqualified", "qualification profile is not private")
    except (OSError, ValueError):
        raise OpenArtCLIError("generation_unqualified", "qualification profile unreadable")
    staged_full = require == "full" and _is_staged(profile)
    profile = validate_profile(profile, allow_fixture=allow_fixture,
                               require="pre_submit" if staged_full else require)
    if (model is not None and profile["model"] != model) or (mode is not None and profile["mode"] != mode):
        raise OpenArtCLIError("generation_unqualified", "profile model/mode differs from catalog path")
    out = dict(profile, profile_sha256=sha256_json(profile))
    if staged_full:
        load_result_proof(out)  # raises result_contract_unqualified; returned profile is unchanged
    return out


def list_qualifications() -> list[dict]:
    """Pure metadata for U5 model selection. Never raises, never calls the CLI."""
    out: list[dict] = []
    try:
        base = cli.state_dir(create=False) / "qualification"
        candidates = sorted(base.glob("*/*/profile.json")) if base.is_dir() else []
        if (base / "profile.json").is_file():
            candidates.insert(0, base / "profile.json")
    except Exception:
        return out
    for target in candidates:
        row = {"model": None, "mode": None, "source": None, "profile_sha256": None, "cli_version": None,
               "tier": None, "image2video_qualified": False, "valid": False, "error": None,
               "level": "none", "result_contract_sha256": None, "result_proof_id": None}
        try:
            raw = json.loads(target.read_text())
            if isinstance(raw, dict):
                row.update(model=raw.get("model"), mode=raw.get("mode"), source=raw.get("source"),
                           cli_version=raw.get("cli_version"), tier=raw.get("tier"))
            # Validate at the lowest stage so staged rows report truthful levels.
            prof = load_qualification(target, allow_fixture=True, require="inspected")
            row["profile_sha256"] = prof["profile_sha256"]
            if prof["source"] != "real":
                row["error"] = "fixture_profile"
            else:
                status = qualification_status(prof["model"], prof["mode"])
                if status["profile_sha256"] == prof["profile_sha256"]:
                    row.update(level=status["level"],
                               result_contract_sha256=status["result_contract_sha256"],
                               result_proof_id=status["result_proof_id"])
                    if status["level"] != "full":
                        row["error"] = status["error"] or "result_contract_unqualified"
                else:
                    row["error"] = status["error"] or "profile_not_current"
                full = row["level"] == "full"
                row.update(valid=full, image2video_qualified=full and prof["mode"] == "image2video")
        except OpenArtCLIError as exc:
            row["error"] = exc.kind
        except Exception:
            row["error"] = "unreadable"
        out.append(row)
    return out


# ---------------------------------------------------------------- native request

def _verify_dry_run_evidence(dry_run: dict, controls: dict, profile: dict, creative: list) -> dict:
    """Verify retained native preview evidence against the exact creative request.

    Accepts either evidence {endpoint, body_sha256, receipt_id, receipt_sha256} (re-verified from
    the private receipt) or raw cli.dry_run_request output (fixture/component use only).
    """
    if not isinstance(dry_run, dict):
        raise OpenArtCLIError("dry_run_mismatch", "dry-run evidence must be an object")
    receipt_argv = None
    if dry_run.get("receipt_id") is not None or dry_run.get("receipt_sha256") is not None:
        rid, rsha = dry_run.get("receipt_id"), dry_run.get("receipt_sha256")
        receipt = _load_receipt(rid, rsha, "native_preview_invalid")
        receipt_argv = receipt.get("argv")
        try:
            parsed = cli.dry_run_request(receipt.get("parsed"))
        except OpenArtCLIError:
            raise OpenArtCLIError("native_preview_invalid", "retained receipt is not a dry-run preview")
        if dry_run.get("body_sha256") not in (None, parsed["body_sha256"]):
            raise OpenArtCLIError("dry_run_mismatch", "evidence body digest differs from retained receipt")
        body, endpoint, body_sha = parsed["body"], parsed["endpoint"], parsed["body_sha256"]
        if receipt_argv != list(creative) + ["--dry-run"] + cli.GLOBAL_FLAGS:
            raise OpenArtCLIError("dry_run_mismatch", "retained preview argv differs from creative request")
    else:
        if profile.get("source") == "real":
            raise OpenArtCLIError("native_preview_required", "real requests need a retained private preview receipt")
        body = dry_run.get("body")
        if not isinstance(body, dict):
            raise OpenArtCLIError("dry_run_mismatch", "dry-run evidence lacks body")
        endpoint, body_sha = dry_run.get("endpoint"), sha256_json(body)
        if dry_run.get("body_sha256") not in (None, body_sha):
            raise OpenArtCLIError("dry_run_mismatch", "dry-run body digest mismatch")
    if endpoint != profile["dry_run_endpoint"]:
        raise OpenArtCLIError("dry_run_mismatch", "dry-run endpoint differs from qualified endpoint")
    expected_params = {native: controls[ours] for ours, native in _BODY_PARAM_NAMES.items()
                       if controls.get(ours) is not None}
    if controls.get("image_url") is not None:
        expected_params["image"] = controls["image_url"]
    if set(body) != {"model", "media", "mode", "params"} or body.get("model") != controls["model"] \
            or body.get("media") != "video" or body.get("mode") != controls["mode"] \
            or body.get("params") != expected_params:
        raise OpenArtCLIError("dry_run_mismatch", "native dry-run body differs from requested controls")
    return {"endpoint": endpoint, "body_sha256": body_sha,
            "receipt_id": dry_run.get("receipt_id"), "receipt_sha256": dry_run.get("receipt_sha256")}


def _load_receipt(receipt_id: Any, receipt_sha256: Any, kind: str) -> dict:
    """Read one private receipt verifying permission + digest. Pure (no CLI, no lock)."""
    if not isinstance(receipt_id, str) or not isinstance(receipt_sha256, str):
        raise OpenArtCLIError(kind, "receipt id/sha256 required")
    try:
        path = cli.receipt_path(receipt_id)
        cli._check_private(path, False)
        raw = path.read_bytes()
    except (OSError, OpenArtCLIError):
        raise OpenArtCLIError(kind, "receipt missing or not private")
    if hashlib.sha256(raw).hexdigest() != receipt_sha256:
        raise OpenArtCLIError(kind, "receipt digest mismatch")
    try:
        data = json.loads(raw)
    except ValueError:
        raise OpenArtCLIError(kind, "receipt unreadable")
    if not isinstance(data, dict):
        raise OpenArtCLIError(kind, "receipt unreadable")
    return data


def _file_sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


_OPERATION_MODES = {"text_to_video": "text2video", "image_to_video": "image2video"}


def _native_controls(inputs: dict, profile: dict) -> tuple[dict, Optional[dict]]:
    if not isinstance(inputs, dict):
        raise OpenArtCLIError("invalid_argument", "inputs must be an object")
    for key in UNSUPPORTED_INPUTS:
        if key in inputs:
            raise OpenArtCLIError("unsupported_gate", f"{key} is not a qualified OpenArt control")
    unknown = sorted(k for k in inputs if k not in ALLOWED_INPUTS and k not in IGNORED_INPUTS)
    if unknown:
        raise OpenArtCLIError("unsupported_gate", f"unsupported OpenArt controls {unknown}")
    model = inputs.get("model")
    if model != profile["model"]:
        raise OpenArtCLIError("unsupported_gate", "model differs from qualified profile")
    wants_image = "image_path" in inputs or "image_upload_id" in inputs
    if wants_image and inputs.get("operation") != "image_to_video":
        raise OpenArtCLIError("unsupported_gate", "image_path requires operation='image_to_video'")
    mode = "image2video" if wants_image else inputs.get("mode", "text2video")
    if inputs.get("mode", mode) != mode or mode != profile["mode"]:
        raise OpenArtCLIError("unsupported_gate", "mode differs from qualified profile")
    operation = inputs.get("operation")
    if operation is not None and _OPERATION_MODES.get(operation) != mode:
        raise OpenArtCLIError("unsupported_gate", "operation is not a qualified OpenArt operation for this mode")
    image_upload = None
    image_url = None
    if mode == "image2video":
        _validate_upload_contract(profile)
        upload_id, image_path = inputs.get("image_upload_id"), inputs.get("image_path")
        if not upload_id or not image_path:
            raise OpenArtCLIError("unsupported_gate", "image2video needs image_path and image_upload_id")
        try:
            source_sha, _ = _file_sha256(Path(image_path))
        except OSError:
            raise OpenArtCLIError("upload_mismatch", "image_path unreadable")
        image_url = upload_url_for(upload_id, source_sha256=source_sha,
                                   account_id_sha256=profile["account_id_sha256"], profile=profile)
        image_upload = {"upload_id": upload_id, "source_sha256": source_sha, "url_sha256": _sha(image_url)}
    controls = {
        "prompt": inputs.get("prompt"),
        "model": model,
        "mode": mode,
        "duration": inputs.get("duration"),
        "aspect_ratio": inputs.get("aspect_ratio"),
        "resolution": inputs.get("resolution"),
    }
    if image_url is not None:
        controls["image_url"] = image_url
    return controls, image_upload


def _creative_argv(controls: dict) -> list[str]:
    return cli.native_video_argv(controls["prompt"], model=controls["model"], mode=controls["mode"],
                                 duration=controls["duration"], aspect_ratio=controls["aspect_ratio"],
                                 resolution=controls["resolution"], image_url=controls.get("image_url"))


def prepare_native_request(inputs: dict, profile: dict) -> dict:
    """Pure retained-preview builder: requires inputs native_dry_run_receipt_id/sha256.

    Missing -> native_preview_required; bad/non-private/digest -> native_preview_invalid;
    argv/endpoint/body differ -> dry_run_mismatch. No CLI call, no lock.
    """
    if not isinstance(inputs, dict) or not inputs.get("native_dry_run_receipt_id") \
            or not inputs.get("native_dry_run_receipt_sha256"):
        raise OpenArtCLIError("native_preview_required", "retained native dry-run receipt is required")
    evidence = {"receipt_id": inputs["native_dry_run_receipt_id"],
                "receipt_sha256": inputs["native_dry_run_receipt_sha256"]}
    return native_request(inputs, profile, dry_run=evidence)


def native_request(inputs: dict, profile: dict, dry_run: Optional[dict] = None) -> dict:
    """Build the frozen native submit request. Distinct from strict planned_request_digest.

    Deterministic for identical inputs/profile/evidence. Without dry_run evidence
    `native_body_sha256` is None and launch refuses.
    """
    controls, image_upload = _native_controls(inputs, profile)
    creative = _creative_argv(controls)
    argv = cli.check_submit_argv(creative + ["--async"], allow_image="image_url" in controls)
    evidence = _verify_dry_run_evidence(dry_run, controls, profile, creative) if dry_run is not None else None
    profile_digest = profile.get("profile_sha256") or sha256_json(
        {k: v for k, v in profile.items() if k != "profile_sha256"})
    public_controls = {k: v for k, v in controls.items() if k != "image_url"}
    if image_upload:
        public_controls["image_url_sha256"] = image_upload["url_sha256"]
    return {
        "argv": argv,
        "creative_argv": creative,
        "native_controls": public_controls,
        "native_controls_sha256": sha256_json(controls),
        "native_argv_sha256": sha256_json(argv),
        "native_body_sha256": evidence["body_sha256"] if evidence else None,
        "dry_run": evidence,
        "endpoint": profile["dry_run_endpoint"],
        "profile_sha256": profile_digest,
        "form_sha256": profile["form_sha256"],
        "form_defaults": profile["form_defaults"],
        "form_defaults_sha256": sha256_json(profile["form_defaults"]),
        "cli_version": profile["cli_version"],
        "tier": profile["tier"],
        "account_id_sha256": profile["account_id_sha256"],
        "source": profile["source"],
        "model": controls["model"],
        "mode": controls["mode"],
        "image_upload": image_upload,
    }


# ---------------------------------------------------------------- uploads (qualified nonspending only)

def _no_upload_approval(project_root: Path, upload_id: str, source_sha256: str) -> Optional[dict]:
    return None


_UPLOAD_APPROVAL_LOOKUP: Callable[[Path, str, str], Optional[dict]] = _no_upload_approval


def register_upload_approval_lookup(fn: Optional[Callable[[Path, str, str], Optional[dict]]]) -> None:
    """U3 installs the reviewed-pack approval lookup. None restores fail-closed default."""
    global _UPLOAD_APPROVAL_LOOKUP
    _UPLOAD_APPROVAL_LOOKUP = fn or _no_upload_approval


def _upload_record_path(upload_id: str) -> Path:
    return cli.state_dir() / "uploads" / f"{cli._safe_part(upload_id)}.json"


def _upload_snapshot_path(upload_id: str, suffix: str) -> Path:
    if not re.fullmatch(r"(\.[A-Za-z0-9]{1,8})?", suffix or ""):
        raise OpenArtCLIError("invalid_argument", "unsupported upload source extension")
    return cli.state_dir() / "uploads" / f"{cli._safe_part(upload_id)}.source{suffix.lower()}"


def _snapshot_source(source: Path, dest: Path, max_bytes: int = 64 * 1024 * 1024) -> tuple[str, int]:
    """Copy approved source bytes into a private 0600 immutable snapshot; hash the copied bytes."""
    digest, size = hashlib.sha256(), 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    src_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        out_fd = os.open(dest, flags, 0o600)
    except BaseException:
        os.close(src_fd)
        raise
    try:
        while True:
            chunk = os.read(src_fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise OpenArtCLIError("invalid_argument", "upload source too large")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                n = os.write(out_fd, view)
                if n <= 0:
                    raise OpenArtCLIError("unsafe_state", "short write to upload snapshot")
                view = view[n:]
        os.fsync(out_fd)
    except BaseException:
        os.close(out_fd)
        os.close(src_fd)
        try:
            dest.unlink()
        except OSError:
            pass
        raise
    os.close(out_fd)
    os.close(src_fd)
    os.chmod(dest, 0o400)
    _fsync_dir(dest.parent)
    return digest.hexdigest(), size


def _validate_guarantee(profile: dict) -> None:
    """Pre-upload raw provider guarantee (owned by lib.openart_setup); lazy import, fail closed."""
    try:
        from lib.openart_setup import validate_upload_guarantee
    except ImportError:
        raise OpenArtCLIError("upload_unqualified", "upload guarantee validator unavailable")
    try:
        validate_upload_guarantee(profile)
    except OpenArtCLIError:
        raise
    except Exception:
        raise OpenArtCLIError("upload_unqualified", "upload guarantee validation failed")


def _verify_current(profile: dict, timeout: float) -> None:
    """Fresh version/account/tier/form/defaults recheck (lib.openart_setup.verify_current); fail closed."""
    try:
        from lib.openart_setup import verify_current
    except ImportError:
        raise OpenArtCLIError("upload_unqualified", "current-contract verifier unavailable")
    verify_current(profile, timeout=timeout)


def upload_binding(profile: dict) -> dict:
    """Stable upload binding that survives staged promotion (upload/dry_run receipts added).

    Binds source/model/mode/account/version/tier/form/defaults + the declared guarantee and its
    captured raw receipt identity/integrity + URL path/hosts. Excludes the exact profile SHA.
    Caller must already have validated the guarantee.
    """
    upload = profile.get("upload") if isinstance(profile, dict) else None
    if not isinstance(upload, dict):
        raise OpenArtCLIError("upload_unqualified", "profile declares no upload contract")
    entries = [e for e in upload.get("receipts") or []
               if isinstance(e, dict) and e.get("kind") == "nonspending_guarantee"]
    if len(entries) != 1:
        raise OpenArtCLIError("upload_unqualified", "exactly one captured guarantee receipt required")
    return {"source": profile.get("source"), "model": profile.get("model"), "mode": profile.get("mode"),
            "account_id_sha256": profile.get("account_id_sha256"),
            "cli_version": profile.get("cli_version"), "tier": profile.get("tier"),
            "form_sha256": profile.get("form_sha256"),
            "form_defaults_sha256": sha256_json(profile.get("form_defaults")),
            "guarantee_sha256": sha256_json(upload.get("guarantee")),
            "guarantee_receipt_id": entries[0].get("receipt_id"),
            "guarantee_receipt_sha256": entries[0].get("receipt_sha256"),
            "url_path": (upload.get("json_paths") or {}).get("upload_url"),
            "url_hosts": list(upload.get("url_hosts") or [])}


def _upload_receipt_guaranteed(receipt: dict, profile: dict, kind: str) -> None:
    """The raw upload response itself must carry both provider guarantee assertions."""
    guarantee = profile["upload"]["guarantee"]
    for name in ("nonspending", "no_delayed_charge"):
        assertion = guarantee[name]
        value = lookup_path(receipt.get("parsed"), assertion["path"])
        if type(value) is not type(assertion["expected"]) or value != assertion["expected"]:
            raise OpenArtCLIError(kind, "raw upload receipt does not prove both upload guarantees")


def _upload_profile(model: Any, mode: Any, kind: str) -> dict:
    try:
        profile = load_qualification(model=model, mode=mode, allow_fixture=_ALLOW_FIXTURE_UPLOAD,
                                     require="inspected")
        _validate_guarantee(profile)
        return profile
    except (OpenArtCLIError, TypeError) as exc:
        raise OpenArtCLIError(kind, f"no qualified nonspending upload contract ({getattr(exc, 'kind', 'invalid')})")


def upload_reference(project_root: Path, upload_id: str, source_path: Path, *, model: str, mode: str,
                     timeout: float = cli.DEFAULT_TIMEOUT) -> dict:
    """Run exact official `upload add` ONLY under a qualified nonspending upload guarantee.

    Order: inspected real profile + captured raw provider guarantee (else upload_unqualified, zero
    CLI calls) -> private 0400 snapshot -> snapshot hash == approved source hash -> approval seam ->
    transport lock: account check -> `upload add <snapshot>` -> exact receipt argv, raw guarantee
    assertions in the upload response, qualified URL host -> record bound by upload_binding().
    """
    profile = _upload_profile(model, mode, "upload_unqualified")
    if profile["mode"] != "image2video":
        raise OpenArtCLIError("upload_unqualified", "uploads are only qualified for image2video")
    binding = upload_binding(profile)
    upload_id = cli._safe_part(upload_id)
    source = Path(source_path)
    if source.is_symlink() or not source.is_file():
        raise OpenArtCLIError("invalid_argument", "upload source must be an existing regular file")
    if _upload_record_path(upload_id).exists():
        raise OpenArtCLIError("already_uploaded", "upload id already has a retained record")
    pre_sha, _ = _file_sha256(source)
    approval = _UPLOAD_APPROVAL_LOOKUP(Path(project_root), upload_id, pre_sha)
    if not isinstance(approval, dict) or approval.get("state") != "approved" \
            or approval.get("upload_id") != upload_id or approval.get("source_sha256") != pre_sha \
            or not approval.get("approval_sha256"):
        raise OpenArtCLIError("upload_unapproved", "no matching approved reference for this source")
    cli.private_dir("uploads")
    snapshot = _upload_snapshot_path(upload_id, source.suffix)
    try:
        snap_sha, snap_size = _snapshot_source(source, snapshot)
    except FileExistsError:
        raise OpenArtCLIError("already_uploaded", "upload snapshot already exists")
    if snap_sha != pre_sha:
        raise OpenArtCLIError("upload_mismatch", "source changed after approval; snapshot not uploaded")
    deadline = time.monotonic() + cli.validate_timeout(timeout)
    with cli.transport_lock(wait_timeout=timeout):
        _verify_current(profile, cli.lock_remaining(deadline))  # full inspected contract, same lock
        account = _current_account(profile, timeout=cli.lock_remaining(deadline))
        if account != profile["account_id_sha256"]:
            raise OpenArtCLIError("account_mismatch", "current account differs from qualified account")
        cli.lock_remaining(deadline)  # no upload once the overall budget is exhausted
        with cli.allow_upload_reference():
            got = cli.run_upload_reference(snapshot, timeout=cli.lock_remaining(deadline))
    expected_argv = ["upload", "add", str(snapshot)] + cli.GLOBAL_FLAGS
    if got.get("argv") != expected_argv:
        raise OpenArtCLIError("upload_mismatch", "upload receipt argv differs from snapshot upload")
    _upload_receipt_guaranteed(got, profile, "upload_mismatch")
    url = lookup_path(got["parsed"], binding["url_path"])
    if not _url_host_ok(url, binding["url_hosts"]):
        raise OpenArtCLIError("upload_url_unqualified", "upload URL absent or host not qualified")
    record = {"version": "2", "upload_id": upload_id, "source_sha256": snap_sha,
              "source_size": snap_size, "snapshot_name": snapshot.name, "url": url, "url_sha256": _sha(url),
              "receipt_id": got["receipt_id"], "receipt_sha256": got["receipt_sha256"],
              "account_id_sha256": account, "approval_sha256": approval["approval_sha256"],
              "model": profile["model"], "mode": profile["mode"], "profile_source": profile["source"],
              "binding": binding, "binding_sha256": sha256_json(binding),
              "url_path": binding["url_path"]}
    cli.write_private(_upload_record_path(upload_id), _canon(record))
    _fsync_dir(_upload_record_path(upload_id).parent)
    return {k: record[k] for k in ("upload_id", "source_sha256", "source_size", "url_sha256", "receipt_id",
                                   "receipt_sha256", "account_id_sha256", "approval_sha256",
                                   "model", "mode", "binding_sha256")}


def _url_host_ok(url: Any, hosts: list) -> bool:
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in hosts and not parts.username \
        and not parts.password and parts.port in (None, 443)


def _verified_upload(upload_id: str, profile: Optional[dict]) -> tuple[dict, dict]:
    """Pure: (record, upload receipt) after full stable-binding revalidation, else upload_mismatch."""
    bad = "upload_mismatch"
    try:
        path = _upload_record_path(upload_id)
        cli._check_private(path, False)
        record = json.loads(path.read_bytes())
    except (OSError, ValueError, OpenArtCLIError):
        raise OpenArtCLIError(bad, "no private retained upload record")
    if not isinstance(record, dict) or record.get("upload_id") != cli._safe_part(upload_id) \
            or not isinstance(record.get("binding"), dict):
        raise OpenArtCLIError(bad, "upload record id/binding invalid")
    if profile is None:
        # Re-derive the retained real profile (never fixture) and revalidate its guarantee.
        if record.get("profile_source") != "real":
            raise OpenArtCLIError(bad, "fixture upload records need an explicit profile")
        profile = _upload_profile(record.get("model"), record.get("mode"), bad)
    else:
        try:
            _validate_guarantee(profile)
        except OpenArtCLIError:
            raise OpenArtCLIError(bad, "profile has no qualified upload guarantee")
    try:
        binding = upload_binding(profile)
    except OpenArtCLIError:
        raise OpenArtCLIError(bad, "profile has no upload binding")
    if binding != record["binding"] or record.get("binding_sha256") != sha256_json(binding) \
            or record.get("model") != binding["model"] or record.get("mode") != binding["mode"] \
            or record.get("profile_source") != binding["source"] \
            or record.get("account_id_sha256") != binding["account_id_sha256"] \
            or record.get("url_path") != binding["url_path"]:
        raise OpenArtCLIError(bad, "upload record not bound to this account/form/version/guarantee")
    receipt = _load_receipt(record.get("receipt_id"), record.get("receipt_sha256"), bad)
    try:
        snapshot = _upload_snapshot_path(upload_id, Path(record.get("snapshot_name") or "").suffix)
    except OpenArtCLIError:
        raise OpenArtCLIError(bad, "upload snapshot name invalid")
    if snapshot.name != record.get("snapshot_name") \
            or receipt.get("argv") != ["upload", "add", str(snapshot)] + cli.GLOBAL_FLAGS:
        raise OpenArtCLIError(bad, "receipt is not the exact snapshot upload")
    _upload_receipt_guaranteed(receipt, profile, bad)
    try:
        if snapshot.is_symlink():
            raise OSError("symlink")
        snap_sha, snap_size = _file_sha256(snapshot)
    except OSError:
        raise OpenArtCLIError(bad, "upload snapshot missing")
    if snap_sha != record.get("source_sha256") or snap_size != record.get("source_size"):
        raise OpenArtCLIError(bad, "upload snapshot bytes changed")
    url = lookup_path(receipt.get("parsed"), binding["url_path"])
    if url != record.get("url") or _sha(url) != record.get("url_sha256") \
            or not _url_host_ok(url, binding["url_hosts"]):
        raise OpenArtCLIError(bad, "retained URL differs from receipt or host unqualified")
    return record, receipt


def upload_url_for(upload_id: str, *, source_sha256: Optional[str] = None,
                   account_id_sha256: Optional[str] = None, profile: Optional[dict] = None) -> str:
    """Return the retained HTTPS URL after revalidating record, snapshot, receipt and binding.

    Pure: no CLI/lock. Binding is upload_binding(profile) (stable across staged promotion), not the
    exact profile SHA. Without `profile`, the record's real model/mode profile is reloaded at
    inspected level and its guarantee revalidated. Any failure -> upload_mismatch.
    """
    record, _ = _verified_upload(upload_id, profile)
    if source_sha256 is not None and record.get("source_sha256") != source_sha256:
        raise OpenArtCLIError("upload_mismatch", "source bytes differ from uploaded reference")
    if account_id_sha256 is not None and record.get("account_id_sha256") != account_id_sha256:
        raise OpenArtCLIError("upload_mismatch", "upload account differs")
    return record["url"]


def retained_upload_evidence(upload_id: str, *, profile: dict) -> dict:
    """Pure: {kind:'upload', receipt_id, receipt_sha256} for promoting the profile upload contract."""
    record, _ = _verified_upload(upload_id, profile)
    return {"kind": "upload", "receipt_id": record["receipt_id"], "receipt_sha256": record["receipt_sha256"]}


# ---------------------------------------------------------------- frozen request snapshots

def _frozen_path(attempt_id: str) -> Path:
    return job_dir(attempt_id) / "frozen_request.json"


def freeze_request(attempt_id: str, inputs: dict, native: dict, profile: dict) -> dict:
    """Exclusive private 0600 immutable snapshot. Identical re-freeze is idempotent."""
    aid = cli._safe_part(attempt_id)
    snapshot = {"version": "1", "attempt_id": aid, "inputs": inputs, "native": native, "profile": profile}
    data = _canon(snapshot)
    path = _frozen_path(aid)
    try:
        cli.write_private(path, data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise OpenArtCLIError("already_frozen", "attempt already has a different frozen request")
    return {"snapshot_id": aid, "snapshot_sha256": hashlib.sha256(data).hexdigest(),
            "profile_sha256": profile.get("profile_sha256") or sha256_json(profile),
            "native_argv_sha256": native.get("native_argv_sha256"),
            "native_body_sha256": native.get("native_body_sha256"),
            "native_controls_sha256": native.get("native_controls_sha256"),
            "inputs_sha256": sha256_json(inputs)}


def load_frozen_request(attempt_id: str) -> dict:
    aid = cli._safe_part(attempt_id)
    path = job_dir(aid, create=False) / "frozen_request.json"
    try:
        cli._check_private(path, False)
        data = path.read_bytes()
        snap = json.loads(data)
    except (OSError, ValueError, OpenArtCLIError):
        raise OpenArtCLIError("frozen_request_tampered", "frozen request missing or not private")
    if not isinstance(snap, dict) or snap.get("attempt_id") != aid or _canon(snap) != data \
            or set(snap) != {"version", "attempt_id", "inputs", "native", "profile"}:
        raise OpenArtCLIError("frozen_request_tampered", "frozen request bytes are not canonical")
    native, profile = snap["native"], snap["profile"]
    if not isinstance(native, dict) or not isinstance(profile, dict) \
            or native.get("native_argv_sha256") != sha256_json(native.get("argv")) \
            or (profile.get("profile_sha256") and profile["profile_sha256"] != sha256_json(
                {k: v for k, v in profile.items() if k != "profile_sha256"})):
        raise OpenArtCLIError("frozen_request_tampered", "frozen request internal digests differ")
    return {"inputs": snap["inputs"], "native": native, "profile": profile,
            "snapshot_id": aid, "snapshot_sha256": hashlib.sha256(data).hexdigest()}


def public_frozen_request(attempt_id: str) -> dict:
    snap = load_frozen_request(attempt_id)
    return cli.redact({k: snap[k] for k in ("inputs", "native", "snapshot_id", "snapshot_sha256")}
                      | {"profile_sha256": snap["profile"].get("profile_sha256")})


# ---------------------------------------------------------------- reservation seam

def _no_ledger(project_root: Path, attempt_id: str, request_sha256: str) -> Optional[dict]:
    from lib.openart_dispatch import reservation_lookup
    try:
        return reservation_lookup(project_root,attempt_id,request_sha256)
    except (OpenArtCLIError, ValueError, OSError) as exc:
        raise OpenArtCLIError('no_active_reservation','no matching original submitting credit reservation') from exc


_RESERVATION_LOOKUP: Callable[[Path, str, str], Optional[dict]] = _no_ledger


def register_reservation_lookup(fn: Optional[Callable[[Path, str, str], Optional[dict]]]) -> None:
    """U4 installs the real ledger lookup. None restores the fail-closed default."""
    global _RESERVATION_LOOKUP
    _RESERVATION_LOOKUP = fn or _no_ledger


def get_active_reservation(project_root: Path, attempt_id: str, request_sha256: str) -> dict:
    res = _RESERVATION_LOOKUP(Path(project_root), attempt_id, request_sha256)
    if not isinstance(res, dict) or res.get("state") != "active" \
            or res.get("attempt_id") != attempt_id or res.get("request_sha256") != request_sha256 \
            or not res.get("reservation_id"):
        raise OpenArtCLIError("no_active_reservation", "no matching active ledger reservation")
    return res


# ---------------------------------------------------------------- process identity

def _pid_gone(pid: int) -> bool:
    """True only on ESRCH (no such process). EPERM/other -> exists or unprovable."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


_UNKNOWN = "unknown"


def process_identity(pid: int) -> Optional[dict]:
    """Kernel birth identity of `pid`.

    None only when the kernel says ESRCH. Unreadable/odd evidence -> kind 'unknown';
    platforms without a birth source -> kind 'unsupported'. Neither proves death.
    """
    pid = int(pid)
    if pid <= 0:
        return {"pid": pid, "kind": _UNKNOWN, "start": None}
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw[raw.rindex(")") + 2:].split()
            return {"pid": pid, "kind": "linux_proc_starttime", "start": fields[19]}
        except (OSError, ValueError, IndexError):
            return None if _pid_gone(pid) else {"pid": pid, "kind": _UNKNOWN, "start": None}
    if sys.platform == "darwin":
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib",
                              use_errno=True)
            buf = ctypes.create_string_buffer(136)
            got_size = lib.proc_pidinfo(ctypes.c_int(pid), 3, ctypes.c_uint64(0), buf, 136)
        except OSError:
            got_size = -1
        if got_size == 136:
            (got,) = struct.unpack_from("<I", buf.raw, 12)
            if got == pid:
                sec, usec = struct.unpack_from("<QQ", buf.raw, 120)
                return {"pid": pid, "kind": "darwin_pbi_start", "start": f"{sec}.{usec:06d}"}
        return None if _pid_gone(pid) else {"pid": pid, "kind": _UNKNOWN, "start": None}
    return {"pid": pid, "kind": "unsupported", "start": None}


def process_alive(identity: Optional[dict]) -> str:
    """'alive' | 'dead' | 'unknown'. Dead only on ESRCH or a proven different birth."""
    if not identity or identity.get("kind") in (None, "unsupported", _UNKNOWN) \
            or identity.get("start") is None:
        return "unknown"
    now = process_identity(int(identity["pid"]))
    if now is None:
        return "dead"
    if now.get("kind") != identity.get("kind") or now.get("start") is None:
        return "unknown"
    return "alive" if now["start"] == identity["start"] else "dead"


# ---------------------------------------------------------------- job records

def job_dir(attempt_id: str, create: bool = True) -> Path:
    if create:
        return cli.private_dir("jobs", attempt_id)
    return cli.state_dir() / "jobs" / cli._safe_part(attempt_id)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_event(attempt_id: str, event: dict) -> dict:
    """Append-only, full-write, fsync file + directory. Never rewrites earlier events."""
    record = dict(event, at=time.time(), event_id=uuid.uuid4().hex)
    jdir = job_dir(attempt_id)
    path = jdir / "events.jsonl"
    view = memoryview(_canon(record) + b"\n")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        cli._check_private(path, False)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OpenArtCLIError("unsafe_state", "short write to job event log")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(jdir)
    return record


def read_events(attempt_id: str) -> list[dict]:
    path = job_dir(attempt_id, create=False) / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"type": "corrupt_line"})
    return out


def lookup_path(data: Any, dotted: str) -> Any:
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def parse_submit(stdout_text: str, profile: dict, paths: Optional[dict] = None) -> Optional[str]:
    """Job id via the qualified json path only; None -> hold_unknown_job.

    `paths` defaults to the profile's declared paths (tentative for staged profiles; a
    tentative parse is never proof until promote_result_contract binds it).
    """
    try:
        parsed = json.loads(stdout_text)
    except ValueError:
        return None
    job_id = lookup_path(parsed, (paths or profile["json_paths"])["submit_job_id"])
    if isinstance(job_id, str) and cli._ID_RE.match(job_id):
        return job_id
    return None


# ---------------------------------------------------------------- launch (submit once)

_BINDING_KEYS = ("attempt_id", "request_sha256", "reservation_id", "native_controls_sha256",
                 "native_argv_sha256", "profile_sha256", "account_id_sha256", "native_body_sha256",
                 "snapshot_sha256")
_ALLOW_FIXTURE_UPLOAD = False  # module-level test seam only
_ALLOW_FIXTURE_LAUNCH = False  # module-level test seam only; never set from caller data
_POPEN = None  # test seam: replacement for subprocess.Popen (never used to retry)


def _popen(*args, **kwargs):
    import subprocess
    return (_POPEN or subprocess.Popen)(*args, **kwargs)


def _sha(text: Optional[str]) -> Optional[str]:
    return hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None


def launch_record(attempt_id: str) -> Optional[dict]:
    path = job_dir(attempt_id, create=False) / "launch.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _launch_sha256(attempt_id: str) -> Optional[str]:
    path = job_dir(attempt_id, create=False) / "launch.json"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def launch_submit(project_root: Path, binding: dict, native: dict, profile: dict,
                  *, wait_timeout: float = cli.MAX_TIMEOUT, deadline: Optional[float] = None) -> dict:
    """Submit exactly once. Any existing launch marker for the attempt refuses a second submit.

    Requires: frozen snapshot matching binding.snapshot_sha256, real active reservation from the
    registered ledger lookup, non-None native body digest from retained preview, and a current
    account check inside the serialized transport (mismatch -> quarantine, no Popen).
    """
    entry_time = time.monotonic()
    if wait_timeout is None:
        raise OpenArtCLIError("invalid_argument", "finite positive launch wait timeout required")
    timeout = cli.validate_timeout(wait_timeout)
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                                or not math.isfinite(deadline)):
        raise OpenArtCLIError("invalid_argument", "finite internal absolute launch deadline required")
    deadline = min(deadline, entry_time + timeout) if deadline is not None else entry_time + timeout
    cli.lock_remaining(deadline)
    missing = [k for k in _BINDING_KEYS if not binding.get(k)]
    if missing:
        raise OpenArtCLIError("binding_incomplete", f"launch binding missing {missing}")
    attempt_id = cli._safe_part(binding["attempt_id"])
    for key in ("native_controls_sha256", "native_argv_sha256", "profile_sha256", "account_id_sha256",
                "native_body_sha256"):
        if binding[key] != native.get(key):
            raise OpenArtCLIError("binding_mismatch", f"{key} differs from frozen native request")
    if native["account_id_sha256"] != profile["account_id_sha256"] \
            or native["profile_sha256"] != profile.get("profile_sha256"):
        raise OpenArtCLIError("binding_mismatch", "native request is not bound to this profile")
    frozen = load_frozen_request(attempt_id)
    if frozen["snapshot_sha256"] != binding["snapshot_sha256"] or frozen["native"] != native \
            or frozen["profile"] != profile:
        raise OpenArtCLIError("binding_mismatch", "launch differs from frozen request snapshot")
    if profile.get("source") != "real" and not _ALLOW_FIXTURE_LAUNCH:
        raise OpenArtCLIError("generation_unqualified", "fixture profile cannot launch real generation")
    reservation = get_active_reservation(project_root, attempt_id, binding["request_sha256"])
    if reservation["reservation_id"] != binding["reservation_id"]:
        raise OpenArtCLIError("no_active_reservation", "reservation differs from binding")
    qualification_attempt = _launch_purpose(profile, reservation)
    argv = cli.check_submit_argv(native["argv"], allow_image=native.get("mode") == "image2video")
    if sha256_json(argv) != native["native_argv_sha256"]:
        raise OpenArtCLIError("binding_mismatch", "submit argv differs from frozen digest")
    if cli.is_offline():
        raise OpenArtCLIError("offline_only", "OpenArt submit refused during offline preparation")
    timeout=cli.lock_remaining(deadline)
    binary = cli.resolve_binary()
    full = argv + cli.GLOBAL_FLAGS
    if job_dir(attempt_id, create=False).joinpath("launch.json").exists():
        raise OpenArtCLIError("already_launched", "attempt already has a launch marker; never resubmit")
    with cli.transport_lock(wait_timeout=timeout) as root:
        # reentrant under the held transport claim; shares the launch budget
        account = _current_account(profile, timeout=cli.lock_remaining(deadline))
        if account != binding["account_id_sha256"]:
            append_event(attempt_id, {"type": "quarantined", "reason": "account_mismatch_prelaunch"})
            raise OpenArtCLIError("account_mismatch", "current account differs; attempt quarantined, not launched")
        if _RESERVATION_LOOKUP is _no_ledger:
            from lib.openart_dispatch import validate_prelaunch
            validate_prelaunch(project_root,attempt_id,binding['request_sha256'],deadline)
        cli.lock_remaining(deadline)  # never spawn a paid submit after the budget is exhausted
        if qualification_attempt:
            _consume_qualification_marker(profile, reservation, attempt_id, account)
        jdir = job_dir(attempt_id)
        launch = {"attempt_id": attempt_id, "binding": {k: binding[k] for k in _BINDING_KEYS},
                  "project_root": str(_abs(project_root)),
                  "argv": full, "cli_version": native["cli_version"], "tier": native["tier"],
                  "form_sha256": native["form_sha256"], "profile_source": profile.get("source"),
                  "profile": {k: v for k, v in profile.items()},
                  "json_paths": profile["json_paths"],
                  "purpose": "result_contract_qualification" if qualification_attempt else "ordinary"}
        try:
            cli.write_private(jdir / "launch.json", _canon(launch))  # exclusive: submit-once marker
        except FileExistsError:
            raise OpenArtCLIError("already_launched", "attempt already has a launch marker; never resubmit")
        if _RESERVATION_LOOKUP is _no_ledger:
            from lib.openart_dispatch import _checkpoint
            _checkpoint('launch_marker',attempt_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        out_fd = os.open(jdir / "submit.stdout", flags, 0o600)
        try:
            err_fd = os.open(jdir / "submit.stderr", flags, 0o600)
        except BaseException:
            os.close(out_fd)
            raise
        try:
            try:
                _fsync_dir(jdir)  # raw stream entries durable before the child exists
                append_event(attempt_id, {"type": "intent", "launch_sha256": _launch_sha256(attempt_id),
                                          "account_check_sha256": account})
                cli.lock_remaining(deadline)  # re-check after persistence, immediately before Popen
            except OpenArtCLIError:
                append_event(attempt_id, {"type": "spawn_skipped", "reason": "budget_exhausted"})
                raise
            try:
                proc = _popen([binary, *full], shell=False, stdin=_devnull(),
                              stdout=out_fd, stderr=err_fd, cwd=str(root), env=cli._child_env(),
                              start_new_session=True, pass_fds=(cli.held_lock_fd(),))
            except OSError:
                append_event(attempt_id, {"type": "spawn_failed"})
                return _launch_result(attempt_id, "uncertain", None)
            append_event(attempt_id, {"type": "spawned", "pid": proc.pid,
                                      "birth": process_identity(proc.pid)})
            if _RESERVATION_LOOKUP is _no_ledger:
                from lib.openart_dispatch import _checkpoint
                _checkpoint("spawned",attempt_id)
        finally:
            os.close(out_fd)
            os.close(err_fd)
        try:
            rc = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        except Exception:
            append_event(attempt_id, {"type": "wait_timeout"})
            return _launch_result(attempt_id, "uncertain", None)
        for name in ("submit.stdout", "submit.stderr"):
            _fsync_file(jdir / name)
        append_event(attempt_id, {"type": "exited", "returncode": rc, "pid": proc.pid,
                                  "proof": "parent_waitpid"})
        if _RESERVATION_LOOKUP is _no_ledger:
            from lib.openart_dispatch import _checkpoint
            _checkpoint("provider_acceptance",attempt_id)
        return _finish_parse(attempt_id, rc, launch["profile"])


QUALIFICATION_PURPOSE = "result_contract_qualification"


def _launch_purpose(profile: dict, reservation: dict) -> bool:
    """True when this launch is the single result-contract qualification attempt.

    Purpose comes ONLY from the trusted registered ledger lookup result. Staged profiles
    without a verified result proof can launch only for that purpose; ordinary launches
    need full qualification.
    """
    if not _is_staged(profile):
        return False
    try:
        load_result_proof(profile)
        return False
    except OpenArtCLIError:
        pass
    if reservation.get("purpose") != QUALIFICATION_PURPOSE:
        raise OpenArtCLIError("result_contract_unqualified",
                              "pre-submit profile needs an approved result_contract_qualification reservation")
    return True


def _marker_key(profile: dict, reservation: dict, account: str) -> str:
    occurrence = reservation.get("authorization_occurrence_id")
    if not isinstance(occurrence, str) or not occurrence:
        raise OpenArtCLIError("result_contract_unqualified",
                              "trusted reservation lacks an authorization_occurrence_id")
    return sha256_json({"authorization_occurrence_id": occurrence, "account_id_sha256": account,
                        "model": profile["model"], "mode": profile["mode"],
                        "origin_profile_sha256": profile["profile_sha256"]})


def _marker_path(profile: dict, key: str) -> Path:
    return (cli.private_dir("qualification", profile["model"], profile["mode"], "attempts")
            / (cli._safe_part(key) + ".json"))


def _consume_qualification_marker(profile: dict, reservation: dict, attempt_id: str, account: str) -> None:
    """Exclusive durable once-marker keyed by the trusted authorization occurrence + account/model/
    mode/origin profile; never expires. A new reservation under the same occurrence is refused; a
    genuinely new occurrence (new exact authorization) may qualify, still gated by U4 slots/holds."""
    key = _marker_key(profile, reservation, account)
    target = _marker_path(profile, key)
    record = {"marker_key": key, "authorization_occurrence_id": reservation["authorization_occurrence_id"],
              "reservation_id": reservation["reservation_id"], "attempt_id": attempt_id,
              "account_id_sha256": account, "model": profile["model"], "mode": profile["mode"],
              "origin_profile_sha256": profile["profile_sha256"]}
    try:
        cli.write_private(target, _canon(record))
    except FileExistsError:
        raise OpenArtCLIError("qualification_authorization_consumed",
                              "this qualification authorization was already used; never retry under it")
    _fsync_dir(target.parent)


def _devnull():
    import subprocess
    return subprocess.DEVNULL


# ---------------------------------------------------------------- result-contract promotion

def promote_result_contract(attempt_id: str, *, json_paths: Optional[dict] = None,
                            timeout: float = cli.DEFAULT_TIMEOUT) -> dict:
    """Promote a staged pre_submit profile to full using the ORIGINAL qualification launch.

    Consumes the governed launch.json, original raw submit stdout + single parsed event, a fresh
    read-only `account` receipt and a read-only `creation get <original job>` receipt. Writes an
    immutable private result proof keyed by the origin profile SHA; never rewrites launch/profile.
    `json_paths` may only add/declare result keys (result_job_id/status/urls/status_terminal_*/
    url_hosts); they stay unqualified until this observed original-job correlation succeeds.
    Returns {result_contract_sha256, result_proof_id, profile_sha256, level:'full'} (no paths).
    """
    bad = "result_contract_unqualified"
    aid = cli._safe_part(attempt_id)
    launch = launch_record(aid)
    if launch is None:
        raise OpenArtCLIError(bad, "attempt was never launched")
    lprof = launch.get("profile") or {}
    origin = lprof.get("profile_sha256")
    if launch.get("purpose") != QUALIFICATION_PURPOSE or lprof.get("source") != "real" \
            or not _is_staged(lprof) or not isinstance(origin, str) \
            or sha256_json({k: v for k, v in lprof.items() if k != "profile_sha256"}) != origin \
            or launch["binding"].get("profile_sha256") != origin:
        raise OpenArtCLIError(bad, "only an original real result_contract_qualification launch can promote")
    _verify_origin_frozen(aid, launch, bad)
    allowed = {"result_job_id", "status", "urls", "status_terminal_ok", "status_terminal_fail", "url_hosts"}
    extra = dict(json_paths or {})
    if set(extra) - allowed:
        raise OpenArtCLIError(bad, "promotion may only declare result-contract json paths")
    paths = dict(lprof["json_paths"], **extra)
    proof_paths = {k: paths.get(k) for k in ("submit_job_id",) + tuple(sorted(allowed))}
    _validate_result_paths(proof_paths, bad)
    job_id = original_job_id(aid)
    if job_id is None:
        raise OpenArtCLIError(bad, "original job id is unknown; keep holding")
    target = result_proof_path(lprof["model"], lprof["mode"], origin)
    if os.path.lexists(target):
        raise OpenArtCLIError("result_contract_exists", "a result proof already exists for this origin profile")
    merged = dict(launch, profile=dict(lprof, json_paths=paths))
    submit = _verify_raw_submit(aid, merged, job_id, bad, proven=False)
    if cli.is_offline():
        raise OpenArtCLIError("offline_only", "OpenArt status read refused during offline preparation")
    deadline = time.monotonic() + cli.validate_timeout(timeout)
    with cli.transport_lock(wait_timeout=cli.lock_remaining(deadline)):
        acct = _account_receipt(lprof, cli.lock_remaining(deadline))
        if acct["account_id_sha256"] != lprof["account_id_sha256"]:
            raise OpenArtCLIError("account_mismatch", "current account differs from qualification account")
        got = cli.run_readonly(["creation", "get", job_id], timeout=cli.lock_remaining(deadline))
    _qual_record(acct, "result_account", bad)
    crec = _qual_record(got, "result_creation_get", bad)
    _proof_status(crec, job_id, proof_paths, bad)
    proof = {"version": "1", "origin_profile_sha256": origin, "model": lprof["model"],
             "mode": lprof["mode"], "account_id_sha256": lprof["account_id_sha256"],
             "json_paths": proof_paths,
             "evidence": {"attempt_id": aid, "launch_sha256": _launch_sha256(aid),
                          "job_id_sha256": _sha(job_id), **submit,
                          "account_receipt": {"receipt_id": acct["receipt_id"],
                                              "receipt_sha256": acct["receipt_sha256"]},
                          "creation_get_receipt": {"receipt_id": got["receipt_id"],
                                                   "receipt_sha256": got["receipt_sha256"]}}}
    cli.private_dir("qualification", lprof["model"], lprof["mode"], "results")
    try:
        cli.write_private(target, _canon(proof))
    except FileExistsError:
        raise OpenArtCLIError("result_contract_exists", "a result proof already exists for this origin profile")
    append_event(aid, {"type": "result_contract_promoted", "origin_profile_sha256": origin,
                       "creation_get_receipt_sha256": got["receipt_sha256"]})
    loaded = load_result_proof(lprof)
    return {"result_contract_sha256": loaded["result_contract_sha256"],
            "result_proof_id": loaded["result_proof_id"], "profile_sha256": origin, "level": "full"}


def effective_result_contract(attempt_id: str) -> dict:
    """Pure: the result contract in force for an attempt's frozen launch profile.

    Independent of later catalog/profile updates. Legacy full profiles return
    result_contract_sha256=None. Raises result_contract_unqualified when staged and unproven.
    """
    aid = cli._safe_part(attempt_id)
    launch = launch_record(aid)
    if launch is None:
        raise OpenArtCLIError("result_contract_unqualified", "attempt was never launched")
    prof = launch["profile"]
    proof = load_result_proof(prof)
    return {"profile_sha256": prof.get("profile_sha256"),
            "result_contract_sha256": proof["result_contract_sha256"],
            "result_proof_id": proof["result_proof_id"],
            "json_paths": dict(prof["json_paths"], **proof["json_paths"])}


def _finish_parse(attempt_id: str, rc: Optional[int], profile: dict) -> dict:
    jdir = job_dir(attempt_id)
    stdout_path = jdir / "submit.stdout"
    raw = b""
    if stdout_path.is_file():
        with open(stdout_path, "rb") as fh:
            raw = fh.read(cli.MAX_STDOUT + 1)
    text = raw[: cli.MAX_STDOUT].decode("utf-8", "replace")
    job_id = parse_submit(text, profile) if len(raw) <= cli.MAX_STDOUT else None
    if job_id is None:
        append_event(attempt_id, {"type": "hold_unknown_job", "returncode": rc,
                                  "stdout_sha256": hashlib.sha256(text.encode()).hexdigest()})
        return _launch_result(attempt_id, "hold_unknown_job", None)
    cli.write_private(jdir / "job_id", job_id.encode())
    append_event(attempt_id, {"type": "parsed", "returncode": rc, "job_id_sha256": _sha(job_id),
                              "stdout_sha256": hashlib.sha256(raw).hexdigest(),
                              "parse_sha256": _parse_sha256(raw, job_id)})
    if _RESERVATION_LOOKUP is _no_ledger:
        from lib.openart_dispatch import _checkpoint
        _checkpoint("parsed_job",attempt_id)
    return _launch_result(attempt_id, "submitted", job_id)


def _parse_sha256(raw: bytes, job_id: str) -> str:
    return sha256_json({"stdout_sha256": hashlib.sha256(raw).hexdigest(), "job_id": job_id})


def _verify_raw_submit(attempt_id: str, launch: dict, job_id: str, kind: str,
                       proven: bool = True) -> dict:
    """Pure: re-read bounded private submit.stdout, re-parse with the launch-bound profile."""
    path = job_dir(attempt_id, create=False) / "submit.stdout"
    try:
        cli._check_private(path, False)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(cli.MAX_STDOUT + 1)
    except (OSError, OpenArtCLIError):
        raise OpenArtCLIError(kind, "original raw submit stdout missing or not private")
    if len(raw) > cli.MAX_STDOUT:
        raise OpenArtCLIError(kind, "original raw submit stdout exceeds bound")
    try:
        paths = _effective_paths(launch["profile"]) if proven else launch["profile"]["json_paths"]
    except OpenArtCLIError:
        raise OpenArtCLIError(kind, "result contract unqualified for this launch profile")
    parsed = parse_submit(raw.decode("utf-8", "replace"), launch["profile"], paths)
    if parsed is None or parsed != job_id:
        raise OpenArtCLIError(kind, "original raw submit does not name the bound job")
    sha, psha = hashlib.sha256(raw).hexdigest(), _parse_sha256(raw, job_id)
    events = [e for e in read_events(attempt_id) if e.get("type") == "parsed"]
    if len(events) != 1 or events[0].get("job_id_sha256") != _sha(job_id) \
            or events[0].get("stdout_sha256") != sha or events[0].get("parse_sha256") != psha:
        raise OpenArtCLIError(kind, "parsed event differs from original raw submit")
    return {"submit_stdout_sha256": sha, "submit_parse_sha256": psha}


def _launch_result(attempt_id: str, status: str, job_id: Optional[str]) -> dict:
    events = read_events(attempt_id)
    return {"attempt_id": attempt_id, "status": status, "job_id_sha256": _sha(job_id),
            "launch_sha256": _launch_sha256(attempt_id), "events_sha256": sha256_json(events)}


def recover_launch(attempt_id: str) -> dict:
    """After parent death: parse the surviving private stdout once the original process is gone.

    Never resubmits. Alive/unknown original process -> 'uncertain' (keep holding).
    """
    if launch_record(attempt_id) is None:
        return {"attempt_id": attempt_id, "status": "not_launched"}
    job_id = original_job_id(attempt_id)
    if job_id:
        return _launch_result(attempt_id, "submitted", job_id)
    events = read_events(attempt_id)
    if any(e.get("type") == "hold_unknown_job" for e in events):
        return _launch_result(attempt_id, "hold_unknown_job", None)
    state = original_process_state(attempt_id)
    if state["state"] not in ("exited", "dead"):
        return dict(_launch_result(attempt_id, "uncertain", None), liveness=state["state"])
    return _finish_parse(attempt_id, state.get("returncode"), launch_record(attempt_id)["profile"])


def original_process_state(attempt_id: str) -> dict:
    """Pure liveness of the ORIGINAL submit child (no CLI/network/lock).

    'exited'  - durable parent waitpid() proof bound to the single spawned PID (authoritative even
                when the child exited before its birth identity could be captured);
    'dead'    - ESRCH or a proven different kernel birth for the recorded identity;
    'alive' | 'unknown' | 'not_spawned' | 'spawn_failed' otherwise. Only exited/dead prove the
    original process is gone; nothing here authorizes release.
    """
    aid = cli._safe_part(attempt_id)
    events = read_events(aid)
    spawned = [e for e in events if e.get("type") == "spawned"]
    if any(e.get("type") == "spawn_failed" for e in events) and not spawned:
        return {"attempt_id": aid, "state": "spawn_failed", "pid": None, "returncode": None}
    if len(spawned) != 1 or not isinstance(spawned[0].get("pid"), int):
        return {"attempt_id": aid, "state": "not_spawned" if not spawned else _UNKNOWN,
                "pid": None, "returncode": None}
    pid = spawned[0]["pid"]
    after = events[events.index(spawned[0]) + 1:]
    exits = [e for e in after if e.get("type") == "exited" and e.get("pid") == pid
             and e.get("proof") == "parent_waitpid" and isinstance(e.get("returncode"), int)]
    if exits:
        return {"attempt_id": aid, "state": "exited", "pid": pid, "returncode": exits[0]["returncode"]}
    return {"attempt_id": aid, "state": process_alive(spawned[0].get("birth")), "pid": pid,
            "returncode": None}


def original_job_id(attempt_id: str) -> Optional[str]:
    path = job_dir(attempt_id, create=False) / "job_id"
    if not path.is_file():
        return None
    value = path.read_text()
    return value if cli._ID_RE.match(value) else None


# ---------------------------------------------------------------- account recheck

def account_fingerprint() -> str:
    """Read-only account identity hash; the qualified source is `account`/whoami output.

    Unqualified until captured: the profile pins the dotted path in json_paths.account_id.
    """
    raise OpenArtCLIError("account_unqualified", "account identity read path is not qualified")


_ACCOUNT_CHECK: Optional[Callable[[dict], str]] = None


def register_account_check(fn: Optional[Callable[[dict], str]]) -> None:
    global _ACCOUNT_CHECK
    _ACCOUNT_CHECK = fn


def _current_account(profile: dict, timeout: Optional[float] = None) -> str:
    budget = cli.DEFAULT_TIMEOUT if timeout is None else timeout
    if _ACCOUNT_CHECK is not None:
        try:
            params = inspect.signature(_ACCOUNT_CHECK).parameters
        except (TypeError, ValueError):
            params = {}
        if "timeout" in params:
            return _ACCOUNT_CHECK(profile, timeout=budget)
        return _ACCOUNT_CHECK(profile)
    path = profile["json_paths"].get("account_id")
    if not path:
        raise OpenArtCLIError("account_unqualified", "account identity read path is not qualified")
    out = cli.run_readonly(["account"], timeout=budget)
    value = lookup_path(out["parsed"], path)
    if not isinstance(value, str) or not value:
        raise OpenArtCLIError("account_unqualified", "account identity absent from CLI output")
    return _sha(value)


# ---------------------------------------------------------------- collection
#
# Collection evidence model:
# - `collect_intent` events bind {intent_id, output_root, output_path, url_sha256, status receipt}
#   BEFORE any download; a later collect never trusts/overwrites a file it did not verify.
# - On success an immutable `collection_evidence.json` (exclusive 0600) anchors launch, original
#   job, account receipt, status receipt, exact URL digest and output bytes. Its file digest is the
#   stable `job_record_sha256`; later status/settlement/refund events never change it.
# - Billing is independent of footage eligibility: nothing here authorizes release.

_TERMINAL_FAIL_FILE = "terminal_failure.json"
_EVIDENCE_FILE = "collection_evidence.json"
_MAX_RECOVERY = 16


def _collect_status(attempt_id: str, status: str, **extra) -> dict:
    base = {"attempt_id": attempt_id, "status": status, "output": None, "billing": "unknown",
            "release_authorized": False}
    base.update(extra)
    return base


def _walk_dir(path: Path) -> int:
    """Open `path` hop-by-hop from '/' with O_DIRECTORY|O_NOFOLLOW; returns a pinned dir fd."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise OSError("unsafe path component")
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _output_meta(path: Path, root: Path) -> Optional[dict]:
    """Hash an existing regular file contained under root via pinned no-follow dir descriptors.

    Every hop (root ancestors and below) is opened O_NOFOLLOW relative to the previous pinned fd,
    and the final file is opened with dir_fd=parent; a concurrent symlink swap cannot redirect it.
    """
    path, root = _abs(path), _abs(root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if not rel.parts or ".." in rel.parts:
        return None
    try:
        parent = _walk_dir(path.parent)
    except OSError:
        return None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        os.close(parent)
        return None
    os.close(parent)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None
    return {"path": str(path), "sha256": digest.hexdigest(), "size": size}


def _abs(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _recovery_path(output_path: Path, n: int) -> Path:
    suffix = "".join(output_path.suffixes[-1:])
    stem = output_path.name[: len(output_path.name) - len(suffix)] if suffix else output_path.name
    return output_path.with_name(f"{stem}.r{n}{suffix}")


def _evidence_path(attempt_id: str) -> Path:
    return job_dir(attempt_id, create=False) / _EVIDENCE_FILE


def _check_status_receipt(rec: dict, job_id: str, profile: dict) -> tuple[Any, Optional[str]]:
    """Return (status, single url) from a raw `creation get` receipt bound to job_id."""
    if rec.get("argv") != ["creation", "get", job_id] + cli.GLOBAL_FLAGS:
        raise OpenArtCLIError("collection_receipt_invalid", "status receipt argv differs from original job")
    paths = _effective_paths(profile)
    parsed = rec.get("parsed")
    if lookup_path(parsed, paths["result_job_id"]) != job_id:
        raise OpenArtCLIError("collection_receipt_invalid", "status receipt does not correlate to original job")
    urls = lookup_path(parsed, paths["urls"])
    if isinstance(urls, str):
        urls = [urls]
    url = urls[0] if isinstance(urls, list) and len(urls) == 1 and isinstance(urls[0], str) else None
    return lookup_path(parsed, paths["status"]), url


def _check_account_receipt(rec: dict, profile: dict) -> str:
    if rec.get("argv") != ["account"] + cli.GLOBAL_FLAGS:
        raise OpenArtCLIError("collection_receipt_invalid", "account receipt argv differs")
    value = lookup_path(rec.get("parsed"), profile["json_paths"]["account_id"])
    if not isinstance(value, str) or not value:
        raise OpenArtCLIError("collection_receipt_invalid", "account receipt lacks qualified identity")
    return _sha(value)


def _account_receipt(profile: dict, timeout: float) -> dict:
    """Read-only account check retaining its private receipt id/sha (never via registered seam)."""
    path = profile["json_paths"].get("account_id")
    if not path:
        raise OpenArtCLIError("account_unqualified", "account identity read path is not qualified")
    out = cli.run_readonly(["account"], timeout=timeout)
    rec = _load_receipt(out["receipt_id"], out["receipt_sha256"], "account_unqualified")
    return {"account_id_sha256": _check_account_receipt(rec, profile),
            "receipt_id": out["receipt_id"], "receipt_sha256": out["receipt_sha256"]}


def collect_job(attempt_id: str, *, output_path: Path, output_root: Path, profile: dict,
                timeout: float = cli.DEFAULT_TIMEOUT) -> dict:
    """Collect the ORIGINAL job's output. Never submits, never releases, never overwrites.

    Returns {attempt_id, status, output, billing:'unknown', release_authorized:False, ...} where
    status in collected | failed_terminal | pending | hold | quarantined | failed.
    """
    from lib.openart_download import OpenArtDownloadError, collect_output

    aid = cli._safe_part(attempt_id)
    launch = launch_record(aid)
    if launch is None:
        return _collect_status(aid, "failed", reason="not_launched")
    binding = launch["binding"]
    out_root = _abs(output_root)
    requested = _abs(output_path)
    if not (requested.parent == out_root or out_root in requested.parents):
        return _collect_status(aid, "hold", reason="output_outside_root")
    if not launch.get("project_root") or out_root != Path(launch["project_root"]):
        return _collect_status(aid, "hold", reason="output_root_not_reserved")
    try:
        bound_output = _bound_output_path(aid, launch)
    except OpenArtCLIError as exc:
        return _collect_status(aid, "hold", reason=exc.kind)
    if requested != bound_output:
        return _collect_status(aid, "hold", reason="output_path_not_bound")
    if _evidence_path(aid).exists():
        try:
            stable = verify_collection_receipt(aid, profile)
        except OpenArtCLIError as exc:
            return _collect_status(aid, "hold", reason=exc.kind)
        return _collect_status(aid, "collected", output=stable["output"],
                               collection_evidence_sha256=stable["job_record_sha256"])
    fail_path = job_dir(aid, create=False) / _TERMINAL_FAIL_FILE
    if fail_path.exists():
        try:
            tf = verify_terminal_failure(aid, profile)
        except OpenArtCLIError as exc:
            return _collect_status(aid, "hold", reason=exc.kind)
        return _collect_status(aid, "failed_terminal", receipt_id=tf["status_receipt_id"],
                               receipt_sha256=tf["status_receipt_sha256"],
                               terminal_failure_sha256=tf["terminal_failure_sha256"])
    events = read_events(aid)
    if any(e.get("type") == "quarantined" for e in events):
        return _collect_status(aid, "quarantined")
    job_id = original_job_id(aid)
    if job_id is None:
        return _collect_status(aid, "hold", reason="unknown_job_id")
    if profile != launch["profile"] or profile.get("profile_sha256") != binding["profile_sha256"]:
        return _collect_status(aid, "hold", reason="profile_changed")
    try:
        frozen = load_frozen_request(aid)
    except OpenArtCLIError as exc:
        return _collect_status(aid, "hold", reason=exc.kind)
    if frozen["snapshot_sha256"] != binding["snapshot_sha256"]:
        return _collect_status(aid, "hold", reason="snapshot_changed")
    try:
        submit = _verify_raw_submit(aid, launch, job_id, "raw_submit_invalid")
    except OpenArtCLIError as exc:
        return _collect_status(aid, "hold", reason=exc.kind)
    deadline = time.monotonic() + cli.validate_timeout(timeout)  # one budget: account+status+download
    try:
        acct = _account_receipt(profile, cli.lock_remaining(deadline))
    except OpenArtCLIError as exc:
        return _collect_status(aid, "hold", reason=exc.kind)
    if acct["account_id_sha256"] != binding["account_id_sha256"]:
        append_event(aid, {"type": "quarantined", "reason": "account_mismatch_collect",
                           "account_receipt_sha256": acct["receipt_sha256"]})
        return _collect_status(aid, "quarantined", reason="account_mismatch")
    try:
        got = cli.run_readonly(["creation", "get", job_id], timeout=cli.lock_remaining(deadline))
        rec = _load_receipt(got["receipt_id"], got["receipt_sha256"], "collection_receipt_invalid")
        status, url = _check_status_receipt(rec, job_id, profile)
    except OpenArtCLIError as exc:
        if exc.kind == "collection_receipt_invalid":
            append_event(aid, {"type": "hold_wrong_job"})
            return _collect_status(aid, "hold", reason="result_job_mismatch")
        return _collect_status(aid, "pending", reason=exc.kind)
    paths = _effective_paths(profile)
    append_event(aid, {"type": "status", "receipt_id": got["receipt_id"],
                       "receipt_sha256": got["receipt_sha256"], "status_sha256": _sha(str(status))})
    if status == paths["status_terminal_fail"]:
        record = {"version": "1", "attempt_id": aid, "binding": binding,
                  "launch_sha256": _launch_sha256(aid), "profile_sha256": profile.get("profile_sha256"),
                  "job_id_sha256": _sha(job_id), **submit,
                  "result_contract_sha256": load_result_proof(profile)["result_contract_sha256"],
                  "account_id_sha256": acct["account_id_sha256"],
                  "status_receipt_id": got["receipt_id"],
                  "status_receipt_sha256": got["receipt_sha256"],
                  "account_receipt_id": acct["receipt_id"],
                  "account_receipt_sha256": acct["receipt_sha256"],
                  "billing": "unknown", "release_authorized": False}
        try:
            cli.write_private(job_dir(aid) / _TERMINAL_FAIL_FILE, _canon(record))
        except FileExistsError:
            pass
        append_event(aid, {"type": "terminal_failed", "receipt_sha256": got["receipt_sha256"]})
        try:
            tf = verify_terminal_failure(aid, profile)
        except OpenArtCLIError as exc:
            return _collect_status(aid, "hold", reason=exc.kind)
        return _collect_status(aid, "failed_terminal", receipt_id=tf["status_receipt_id"],
                               receipt_sha256=tf["status_receipt_sha256"],
                               terminal_failure_sha256=tf["terminal_failure_sha256"])
    if status != paths["status_terminal_ok"]:
        return _collect_status(aid, "pending")
    if url is None or not _url_host_ok(url, list(paths["url_hosts"])):
        return _collect_status(aid, "hold", reason="ambiguous_result_urls")
    # Choose a destination: never overwrite or trust an unverified prior file.
    prior_paths = {e.get("output_path") for e in events if e.get("type") == "collect_intent"}
    target, recovered = requested, False
    for n in range(_MAX_RECOVERY + 1):
        target = requested if n == 0 else _recovery_path(requested, n)
        if not (os.path.lexists(target) or str(target) in prior_paths):
            break
        recovered = True
    else:
        return _collect_status(aid, "hold", reason="recovery_paths_exhausted")
    intent = {"type": "collect_intent", "intent_id": uuid.uuid4().hex, "output_root": str(out_root),
              "output_path": str(target), "url_sha256": _sha(url),
              "status_receipt_id": got["receipt_id"], "status_receipt_sha256": got["receipt_sha256"],
              "recovered": recovered}
    try:
        remaining = cli.lock_remaining(deadline)
    except OpenArtCLIError as exc:
        return _collect_status(aid, "pending", reason=exc.kind)
    append_event(aid, intent)
    try:
        meta = collect_output(url, str(target), allowed_hosts=list(paths["url_hosts"]),
                              output_root=str(out_root), timeout=remaining)
    except OpenArtDownloadError as exc:
        published = os.path.lexists(target)
        append_event(aid, {"type": "collection_failed", "intent_id": intent["intent_id"],
                           "published": published})
        # Recoverable collection failure only; never a generation retry.
        return _collect_status(aid, "hold" if published else "pending",
                               reason="collection_failed_after_publish" if published else "collection_failed",
                               error=str(exc)[:200])
    output = _output_meta(Path(meta["path"]), out_root)
    if meta.get("source_host") not in list(paths["url_hosts"]):
        append_event(aid, {"type": "collection_failed", "intent_id": intent["intent_id"], "published": True})
        return _collect_status(aid, "hold", reason="source_host_unqualified")
    if output is None or output["sha256"] != meta["sha256"] or output["size"] != meta["size"] \
            or _abs(Path(meta["path"])) != target:
        append_event(aid, {"type": "collection_failed", "intent_id": intent["intent_id"], "published": True})
        return _collect_status(aid, "hold", reason="collected_bytes_unverified")
    evidence = {"version": "1", "attempt_id": aid, "binding": binding,
                "launch_sha256": _launch_sha256(aid), "snapshot_sha256": frozen["snapshot_sha256"],
                "profile_sha256": profile.get("profile_sha256"), "job_id_sha256": _sha(job_id),
                "account_id_sha256": acct["account_id_sha256"],
                "account_receipt_id": acct["receipt_id"], "account_receipt_sha256": acct["receipt_sha256"],
                "status_receipt_id": got["receipt_id"], "status_receipt_sha256": got["receipt_sha256"],
                "result_job_id_sha256": _sha(job_id), "url_sha256": _sha(url), **submit,
                "result_contract_sha256": load_result_proof(profile)["result_contract_sha256"],
                "project_root": launch["project_root"],
                "source_host": meta.get("source_host"), "output_root": str(out_root),
                "intent_id": intent["intent_id"], "recovered": recovered,
                "output": {"path": output["path"], "sha256": output["sha256"], "size": output["size"]}}
    try:
        cli.write_private(job_dir(aid) / _EVIDENCE_FILE, _canon(evidence))
    except (OSError, OpenArtCLIError):
        append_event(aid, {"type": "collection_failed", "intent_id": intent["intent_id"], "published": True})
        return _collect_status(aid, "hold", reason="evidence_write_failed")
    append_event(aid, {"type": "collected", "intent_id": intent["intent_id"], **evidence["output"]})
    stable = verify_collection_receipt(aid, profile)
    return _collect_status(aid, "collected", output=stable["output"],
                           collection_evidence_sha256=stable["job_record_sha256"],
                           receipt_id=got["receipt_id"], receipt_sha256=got["receipt_sha256"])


def verify_collection_receipt(attempt_id: str, profile: dict) -> dict:
    """Pure (no CLI, no network, no transport lock): re-verify the immutable collection evidence.

    Returns {attempt_id, binding, job_id_sha256, job_record_sha256, output{path,sha256,size},
    evidence, snapshot_sha256, profile_sha256, account_id_sha256, collection_evidence_sha256,
    billing:'unknown', release_authorized:False}. Raises OpenArtCLIError('collection_receipt_invalid').
    Stable under appended status/settlement/refund events.
    """
    bad = "collection_receipt_invalid"
    aid = cli._safe_part(attempt_id)
    path = _evidence_path(aid)
    try:
        cli._check_private(path, False)
        raw = path.read_bytes()
        ev = json.loads(raw)
    except (OSError, ValueError, OpenArtCLIError):
        raise OpenArtCLIError(bad, "collection evidence missing or not private")
    if not isinstance(ev, dict) or _canon(ev) != raw or ev.get("attempt_id") != aid:
        raise OpenArtCLIError(bad, "collection evidence is not canonical")
    launch = launch_record(aid)
    job_id = original_job_id(aid)
    if launch is None or job_id is None or ev.get("binding") != launch["binding"] \
            or ev.get("launch_sha256") != _launch_sha256(aid) or ev.get("job_id_sha256") != _sha(job_id):
        raise OpenArtCLIError(bad, "collection evidence differs from original launch/job")
    if not isinstance(profile, dict) or profile != launch["profile"] \
            or ev.get("profile_sha256") != profile.get("profile_sha256") \
            or ev["binding"].get("profile_sha256") != profile.get("profile_sha256"):
        raise OpenArtCLIError(bad, "collection evidence profile differs")
    try:
        frozen = load_frozen_request(aid)
    except OpenArtCLIError:
        raise OpenArtCLIError(bad, "frozen request invalid")
    if ev.get("snapshot_sha256") != frozen["snapshot_sha256"] \
            or ev["binding"].get("snapshot_sha256") != frozen["snapshot_sha256"]:
        raise OpenArtCLIError(bad, "collection evidence snapshot differs")
    arec = _load_receipt(ev.get("account_receipt_id"), ev.get("account_receipt_sha256"), bad)
    if _check_account_receipt(arec, profile) != ev.get("account_id_sha256") \
            or ev["account_id_sha256"] != ev["binding"].get("account_id_sha256"):
        raise OpenArtCLIError(bad, "account receipt differs from bound account")
    try:
        contract = load_result_proof(profile)["result_contract_sha256"]
    except OpenArtCLIError:
        raise OpenArtCLIError(bad, "result contract for the origin profile is not verified")
    if "result_contract_sha256" not in ev or ev["result_contract_sha256"] != contract:
        raise OpenArtCLIError(bad, "collection evidence result contract differs")
    srec = _load_receipt(ev.get("status_receipt_id"), ev.get("status_receipt_sha256"), bad)
    status, url = _check_status_receipt(srec, job_id, profile)
    paths = _effective_paths(profile)
    if status != paths["status_terminal_ok"] or url is None or _sha(url) != ev.get("url_sha256") \
            or not _url_host_ok(url, list(paths["url_hosts"])) \
            or ev.get("source_host") not in list(paths["url_hosts"]) \
            or ev.get("result_job_id_sha256") != _sha(job_id):
        raise OpenArtCLIError(bad, "status receipt does not prove terminal output URL")
    submit = _verify_raw_submit(aid, launch, job_id, bad)
    if any(ev.get(k) != v for k, v in submit.items()):
        raise OpenArtCLIError(bad, "collection evidence differs from original raw submit")
    if not launch.get("project_root") or ev.get("output_root") != launch["project_root"] \
            or ev.get("project_root") != launch["project_root"]:
        raise OpenArtCLIError(bad, "collection root is not the launch-reserved root")
    try:
        bound = _bound_output_path(aid, launch)
    except OpenArtCLIError:
        raise OpenArtCLIError(bad, "frozen output path invalid")
    allowed = {str(bound)} | {str(_recovery_path(bound, n)) for n in range(1, _MAX_RECOVERY + 1)}
    if (ev.get("output") or {}).get("path") not in allowed:
        raise OpenArtCLIError(bad, "collected path is not the bound output or an approved recovery path")
    intents = [e for e in read_events(aid) if e.get("type") == "collect_intent"
               and e.get("intent_id") == ev.get("intent_id")]
    out = ev.get("output") or {}
    if len(intents) != 1 or intents[0].get("output_path") != out.get("path") \
            or intents[0].get("output_root") != ev.get("output_root") \
            or intents[0].get("status_receipt_sha256") != ev.get("status_receipt_sha256") \
            or intents[0].get("url_sha256") != ev.get("url_sha256"):
        raise OpenArtCLIError(bad, "collection intent binding differs")
    current = _output_meta(Path(out.get("path", "")), Path(ev.get("output_root", "/nonexistent")))
    if current is None or current != out:
        raise OpenArtCLIError(bad, "collected output bytes differ or escaped the reserved root")
    return {"attempt_id": aid, "binding": ev["binding"], "job_id_sha256": ev["job_id_sha256"],
            "job_record_sha256": hashlib.sha256(raw).hexdigest(), "output": current,
            "evidence": ev, "snapshot_sha256": ev["snapshot_sha256"],
            "profile_sha256": ev["profile_sha256"], "account_id_sha256": ev["account_id_sha256"],
            "result_contract_sha256": contract,
            "collection_evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "billing": "unknown", "release_authorized": False}


def _bound_output_path(attempt_id: str, launch: dict) -> Path:
    """The frozen inputs.output_path resolved inside the launch-reserved project root."""
    frozen = load_frozen_request(attempt_id)
    raw = frozen["inputs"].get("output_path")
    root = Path(launch["project_root"])
    if not isinstance(raw, str) or not raw:
        raise OpenArtCLIError("output_path_unbound", "frozen request has no output_path")
    out = _abs(Path(raw) if os.path.isabs(raw) else root / raw)
    if out == root or root not in out.parents:
        raise OpenArtCLIError("output_path_unbound", "frozen output_path escapes the reserved root")
    return out


def verify_terminal_failure(attempt_id: str, profile: dict) -> dict:
    """Pure (no CLI/network/lock): verify the immutable terminal-failure record.

    Proves original launch/job (incl. raw submit), bound account receipt, status receipt with the
    qualified terminal-fail value for that job, and original process exit/death evidence.
    Returns {attempt_id, binding, job_id_sha256, terminal_failure_sha256, account_id_sha256,
    status_receipt_id, status_receipt_sha256, process_state, billing:'unknown',
    release_authorized:False}. Raises OpenArtCLIError('terminal_failure_invalid').
    Release/refund remains a U4 ledger decision; this never authorizes it.
    """
    bad = "terminal_failure_invalid"
    aid = cli._safe_part(attempt_id)
    path = job_dir(aid, create=False) / _TERMINAL_FAIL_FILE
    try:
        cli._check_private(path, False)
        raw = path.read_bytes()
        rec = json.loads(raw)
    except (OSError, ValueError, OpenArtCLIError):
        raise OpenArtCLIError(bad, "terminal failure record missing or not private")
    if not isinstance(rec, dict) or _canon(rec) != raw or rec.get("attempt_id") != aid:
        raise OpenArtCLIError(bad, "terminal failure record is not canonical")
    launch = launch_record(aid)
    job_id = original_job_id(aid)
    if launch is None or job_id is None or rec.get("binding") != launch["binding"] \
            or rec.get("launch_sha256") != _launch_sha256(aid) or rec.get("job_id_sha256") != _sha(job_id):
        raise OpenArtCLIError(bad, "terminal failure differs from original launch/job")
    if not isinstance(profile, dict) or profile != launch["profile"] \
            or rec.get("profile_sha256") != profile.get("profile_sha256") \
            or rec["binding"].get("profile_sha256") != profile.get("profile_sha256"):
        raise OpenArtCLIError(bad, "terminal failure profile differs")
    submit = _verify_raw_submit(aid, launch, job_id, bad)
    if any(rec.get(k) != v for k, v in submit.items()):
        raise OpenArtCLIError(bad, "terminal failure differs from original raw submit")
    try:
        contract = load_result_proof(profile)["result_contract_sha256"]
    except OpenArtCLIError:
        raise OpenArtCLIError(bad, "terminal semantics unqualified for the origin profile")
    if "result_contract_sha256" not in rec or rec["result_contract_sha256"] != contract:
        raise OpenArtCLIError(bad, "terminal failure result contract differs")
    try:
        arec = _load_receipt(rec.get("account_receipt_id"), rec.get("account_receipt_sha256"), bad)
        account = _check_account_receipt(arec, profile)
        srec = _load_receipt(rec.get("status_receipt_id"), rec.get("status_receipt_sha256"), bad)
        status, _ = _check_status_receipt(srec, job_id, profile)
    except OpenArtCLIError as exc:
        raise OpenArtCLIError(bad, exc.message if hasattr(exc, "message") else str(exc))
    if account != rec.get("account_id_sha256") or account != rec["binding"].get("account_id_sha256"):
        raise OpenArtCLIError(bad, "account receipt differs from bound account")
    if status != _effective_paths(profile)["status_terminal_fail"]:
        raise OpenArtCLIError(bad, "status receipt does not prove terminal failure")
    proc = original_process_state(aid)
    if proc["state"] not in ("exited", "dead"):
        raise OpenArtCLIError(bad, "original submit process is not proven gone")
    return {"attempt_id": aid, "binding": rec["binding"], "job_id_sha256": rec["job_id_sha256"],
            "terminal_failure_sha256": hashlib.sha256(raw).hexdigest(),
            "account_id_sha256": account, "status_receipt_id": rec["status_receipt_id"],
            "status_receipt_sha256": rec["status_receipt_sha256"], **submit,
            "result_contract_sha256": contract,
            "process_state": proc["state"], "billing": "unknown", "release_authorized": False}


def reconcile_job(attempt_id: str) -> dict:
    """Dynamic terminal summary for U4. Never authorizes release.

    `events_sha256` covers the mutable event log; `collection_job_record_sha256` is the immutable
    collection evidence digest (None until collected).
    """
    aid = cli._safe_part(attempt_id)
    launch = launch_record(aid)
    events = read_events(aid)
    evidence_path = _evidence_path(aid)
    evidence_raw = (evidence_path.read_bytes()
                    if evidence_path.is_file() and not evidence_path.is_symlink() else None)
    evidence_sha = hashlib.sha256(evidence_raw).hexdigest() if evidence_raw is not None else None
    output = None
    if evidence_sha:
        output = json.loads(evidence_raw).get("output")
    state = ("collected" if evidence_sha else
             "failed_terminal" if (job_dir(aid, create=False) / _TERMINAL_FAIL_FILE).is_file() else
             "quarantined" if any(e.get("type") == "quarantined" for e in events) else
             "hold_unknown_job" if any(e.get("type") == "hold_unknown_job" for e in events) else
             "open")
    return {"attempt_id": aid, "launched": launch is not None,
            "binding": (launch or {}).get("binding"), "state": state,
            "job_id_sha256": _sha(original_job_id(aid)), "output": output,
            "events_sha256": sha256_json(events), "collection_job_record_sha256": evidence_sha,
            "billing": "unknown", "release_authorized": False}
