"""Argv-only, serialized, read-only transport for the official OpenArt CLI.

U1 scope: inspection only. No generation, upload or wait surface is enabled
here. Credentials stay opaque to OpenMontage: the CLI owns its OAuth store and
this module never reads it. Importing this module has no side effects.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 30.0
MAX_STDOUT = 1024 * 1024
MAX_PUBLIC_TEXT = 2048
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_RATIO_RE = re.compile(r"^[0-9]{1,2}:[0-9]{1,2}$")
_RES_RE = re.compile(r"^[0-9]{3,4}p$|^[0-9]{1,2}k$", re.I)
_FILTERED_ENV = {"OPENART_TOKEN", "OPENART_API_KEY"}
GLOBAL_FLAGS = ["--json", "--no-input"]

# Gates that U1 cannot qualify from a fixture. Live captured account evidence
# (recorded by root) is the only thing that may move a gate to "qualified".
UNQUALIFIED_GATES = (
    "account_identity",        # account --json shape / one-account binding
    "model_ids",               # actual model and mode ids for this account
    "form_schema",             # model form JSON Schema per model/mode
    "settings_exact_quote",    # model cost covers model+mode only, not settings
    "async_result_contract",   # generate --async submit id + creation get/wait shape
    "upload_billing",          # upload add cost/retention unknown
    "exhaustive_history",      # creation list paging completeness
    "native_audio",            # no audio flag in v0.1.1 help
    "end_frame_pin",           # no end-frame flag in v0.1.1 help
    "reference_images",        # no rich reference flag in v0.1.1 help
)

_THREAD_LOCK = threading.RLock()


class OpenArtCLIError(Exception):
    def __init__(self, kind: str, message: str, diagnostics: Optional[dict] = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.diagnostics = diagnostics or {}

    def public(self) -> dict:
        return {"kind": self.kind, "message": _bound(redact(self.message)),
                "diagnostics": redact(self.diagnostics)}


# --- redaction -------------------------------------------------------------
_SECRET_KEY = re.compile(r"token|secret|credential|authorization|password|cookie|api[_-]?key|session", re.I)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_SK = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_KV = re.compile(r"(?i)\b(token|secret|password|api[_-]?key)=\S+")
_URL_QUERY = re.compile(r"(https?://[^\s?\"'#]+)\?[^\s\"'#]*")


def _bound(text: str, limit: int = MAX_PUBLIC_TEXT) -> str:
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def redact_text(text: str) -> str:
    text = _JWT.sub("[redacted]", text)
    text = _SK.sub("[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _KV.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return _URL_QUERY.sub(lambda m: f"{m.group(1)}?[redacted]", text)


def redact(value: Any, _depth: int = 0) -> Any:
    """Public diagnostic copy: no secrets, no signed URL query strings, bounded."""
    if _depth > 20:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if _SECRET_KEY.search(str(k)) else redact(v, _depth + 1))
                for k, v in list(value.items())[:500]}
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in list(value)[:500]]
    if isinstance(value, str):
        return _bound(redact_text(value))
    return value


# --- binary ----------------------------------------------------------------
def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_binary() -> str:
    """OPENART_CLI_PATH (explicit, must be executable) > PATH > ~/.local/bin/openart."""
    explicit = os.environ.get("OPENART_CLI_PATH")
    if explicit:
        if _executable(Path(explicit)):
            return str(Path(explicit))
        raise OpenArtCLIError("missing_binary", "OPENART_CLI_PATH is not an executable file")
    found = shutil.which("openart")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "openart"
    if _executable(fallback):
        return str(fallback)
    raise OpenArtCLIError("missing_binary", "openart CLI not found (set OPENART_CLI_PATH or install to ~/.local/bin)")


# --- private state ---------------------------------------------------------
def _check_private(path: Path, want_dir: bool) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise OpenArtCLIError("unsafe_state", f"symlink in private state: {path.name}")
    if want_dir != stat.S_ISDIR(info.st_mode) or (not want_dir and not stat.S_ISREG(info.st_mode)):
        raise OpenArtCLIError("unsafe_state", f"unexpected file type in private state: {path.name}")
    if info.st_uid != os.getuid():
        raise OpenArtCLIError("unsafe_state", f"private state not owned by current user: {path.name}")
    if info.st_mode & 0o077:
        raise OpenArtCLIError("unsafe_state", f"private state is group/world accessible: {path.name}",
                              {"mode": oct(stat.S_IMODE(info.st_mode))})


def state_dir(create: bool = True) -> Path:
    """Private per-user state root outside the checkout (mode 0700, current user)."""
    raw = os.environ.get("OPENMONTAGE_OPENART_STATE_DIR") or str(
        Path.home() / ".openmontage" / "openart")
    path = Path(os.path.abspath(os.path.expanduser(raw)))
    if path.is_symlink():
        raise OpenArtCLIError("unsafe_state", "state dir must not be a symlink")
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise OpenArtCLIError("unsafe_state", "state dir must be outside the OpenMontage checkout")
    if not path.exists():
        if not create:
            return path
        old = os.umask(0o077)
        try:
            path.mkdir(parents=True, mode=0o700)
        finally:
            os.umask(old)
    _check_private(path, True)
    return path


def verify_private_state(root: Optional[Path] = None) -> Path:
    """Reject any non-private entry (receipts, SQLite db/-wal/-shm/-journal, lock)."""
    root = root or state_dir()
    _check_private(root, True)
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            _check_private(Path(current) / name, True)
        for name in files:
            _check_private(Path(current) / name, False)
    return root


def _safe_part(part: Any) -> str:
    if (not isinstance(part, str) or part in {"", ".", ".."} or "/" in part or "\\" in part
            or "\x00" in part or len(part) > 128):
        raise OpenArtCLIError("unsafe_state", "invalid private path component")
    return part


def private_dir(*parts: str) -> Path:
    """Create/verify nested 0700 dirs under the state root; components are single names."""
    root = state_dir()
    current = root
    for part in parts:
        current = current / _safe_part(part)
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
            fsync_dir(current.parent)  # durable directory entry before any child write
        _check_private(current, True)
    return current


def write_private(path: Path, data: bytes) -> Path:
    """Create a new 0600 file under the private state dir (exclusive, full write, fsync)."""
    root = state_dir()
    try:
        rel = Path(path).relative_to(root)
    except ValueError:
        raise OpenArtCLIError("unsafe_state", "private file must live under the state dir")
    parts = [_safe_part(p) for p in rel.parts]
    if not parts:
        raise OpenArtCLIError("unsafe_state", "private file path is the state dir itself")
    target = private_dir(*parts[:-1]) / parts[-1]
    view = memoryview(bytes(data))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OpenArtCLIError("unsafe_state", "short write to private file")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(target.parent)  # publication durable (e.g. launch.json submit-once marker)
    return target


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


_LOCK_DEPTH = threading.local()
LOCK_WAIT = 360.0  # seconds to wait for another CLI process (incl. orphaned children)
_LOCK_POLL = 0.05


def held_lock_fd() -> Optional[int]:
    """fd of the flock held by this thread (pass to CLI children via pass_fds)."""
    return getattr(_LOCK_DEPTH, "fd", None) if getattr(_LOCK_DEPTH, "depth", 0) > 0 else None


@contextmanager
def transport_lock(wait_timeout: Optional[float] = None):
    """One CLI process at a time per user state (OAuth refresh may rewrite creds).

    The flock lives on one open file description that every CLI child inherits through
    ``pass_fds``. The parent never calls LOCK_UN; it only closes its descriptor, so the
    claim lasts until the parent *and* every spawned CLI child (even an orphan surviving a
    parent timeout or death) have closed it. Reentrant within a thread: nested use reuses
    the held descriptor instead of opening a second one (which would self-deadlock).
    Waiting is bounded by LOCK_WAIT -> ``transport_busy``.
    """
    if getattr(_LOCK_DEPTH, "depth", 0) > 0:
        _LOCK_DEPTH.depth += 1
        try:
            yield _LOCK_DEPTH.root
        finally:
            _LOCK_DEPTH.depth -= 1
        return
    wait = LOCK_WAIT if wait_timeout is None else float(wait_timeout)
    if not (wait >= 0) or wait == float("inf"):
        raise OpenArtCLIError("invalid_argument", "lock wait must be finite and non-negative")
    deadline = time.monotonic() + wait
    if not _THREAD_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise OpenArtCLIError("transport_busy", "another in-process OpenArt CLI call holds the transport")
    try:
        root = verify_private_state()
        lock_path = root / "transport.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OpenArtCLIError("transport_busy",
                                              "another OpenArt CLI process still holds the transport")
                    time.sleep(_LOCK_POLL)
            _LOCK_DEPTH.depth, _LOCK_DEPTH.root, _LOCK_DEPTH.fd = 1, root, fd
            try:
                yield root
            finally:
                _LOCK_DEPTH.depth, _LOCK_DEPTH.root, _LOCK_DEPTH.fd = 0, None, None
        finally:
            os.close(fd)  # no LOCK_UN: a live inheriting child keeps the claim
    finally:
        _THREAD_LOCK.release()


def lock_remaining(deadline: float) -> float:
    """Remaining operation budget after lock wait; ``timeout`` if exhausted."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise OpenArtCLIError("timeout", "operation budget exhausted while waiting for transport")
    return left


# --- offline preparation ---------------------------------------------------
def is_offline() -> bool:
    from tools.base_tool import in_offline_preparation
    return in_offline_preparation()


# --- read-only grammar -----------------------------------------------------
# Exact, command-specific grammar. Callers never pass global flags (the transport
# appends --json --no-input). Anything else - unknown/duplicate flags, `--flag=value`,
# `--dry-run=false`, `--`, short flags, extra positionals - is refused before launch.
_DRY_RUN_VALUE_FLAGS = {"--model": _ID_RE, "--duration": re.compile(r"^[1-9][0-9]{0,2}$"),
                        "--aspect-ratio": _RATIO_RE, "--resolution": _RES_RE}
# Retained, already-uploaded HTTPS reference only (never a local path: the CLI would
# auto-upload local files, and upload billing is unqualified). Profile gating lives in U2.
_IMAGE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]{1,253}(?::443)?/[^\s]{1,2000}$")


def _refuse(why: str, argv: list[str]) -> None:
    raise OpenArtCLIError("not_read_only", why, {"command": " ".join(argv[:2])})


def _check_read_only(argv: list[str]) -> None:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise OpenArtCLIError("not_read_only", "argv must be a non-empty list of strings")
    if argv in (["version"], ["account"], ["model", "list"], ["creation", "list"], ["model", "cost"]):
        return
    head, rest = argv[:2], argv[2:]
    if head == ["model", "form"]:
        if len(rest) == 2 and all(_ID_RE.match(x) for x in rest):
            return
        _refuse("model form takes exactly <model-id> <mode>", argv)
    if head == ["model", "cost"]:
        if (len(rest) == 4 and rest[0] == "--model" and rest[2] == "--mode"
                and _ID_RE.match(rest[1]) and _ID_RE.match(rest[3])):
            return
        _refuse("model cost takes only --model <id> --mode <mode>", argv)
    if head == ["creation", "get"]:
        if len(rest) == 1 and _ID_RE.match(rest[0]):
            return
        _refuse("creation get takes exactly <creation-id>", argv)
    if head == ["generate", "video"]:
        if not rest or rest[-1] != "--dry-run":
            _refuse("generate video is only allowed as a trailing --dry-run preview", argv)
        prompt, pairs = rest[0], rest[1:-1]
        if not prompt.strip() or prompt.lstrip().startswith("-"):
            _refuse("prompt must be a non-flag positional", argv)
        if len(pairs) % 2:
            _refuse("generate video flags must be --flag value pairs", argv)
        seen = set()
        for flag, value in zip(pairs[::2], pairs[1::2]):
            pattern = _DRY_RUN_VALUE_FLAGS.get(flag) or (
                _IMAGE_URL_RE if flag == "--image" and _IMAGE_FLAG_ALLOWED.get() else None)
            if pattern is None or flag in seen or not pattern.match(value):
                _refuse(f"flag not allowed on read-only dry-run: {flag[:32]}", argv)
            seen.add(flag)
        if "--model" not in seen:
            _refuse("generate video dry-run requires --model", argv)
        return
    _refuse("command is not on the read-only allowlist", argv)


_IMAGE_FLAG_ALLOWED = contextvars.ContextVar("openart_image_flag_allowed", default=False)


def _child_env() -> dict:
    """Subscription OAuth route only: never let a stray API token env select another route."""
    return {k: v for k, v in os.environ.items() if k not in _FILTERED_ENV}


MAX_TIMEOUT = 300.0
_RECEIPT_ID = re.compile(r"^[0-9T-]{17}-[0-9a-f]{8}$")


def validate_timeout(value: Any = None) -> float:
    """Finite positive bounded seconds; bool/str/NaN/inf/negative raise a typed error."""
    if value is None:
        return DEFAULT_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpenArtCLIError("invalid_argument", "timeout must be a number of seconds")
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT:
        raise OpenArtCLIError("invalid_argument", f"timeout must be in (0, {MAX_TIMEOUT:g}] seconds")
    return value


def receipt_path(receipt_id: str) -> Path:
    """Resolve an opaque public receipt ID under the private state root (internal use only)."""
    if not isinstance(receipt_id, str) or not _RECEIPT_ID.match(receipt_id):
        raise OpenArtCLIError("invalid_argument", "malformed receipt id")
    return private_dir("receipts") / f"{receipt_id}.json"


def run_readonly(argv: list[str], timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run one allowlisted read-only command; returns parsed/public/receipt_id/receipt_sha256."""
    argv = list(argv)
    timeout = validate_timeout(timeout)
    _check_read_only(argv)
    return _run_checked(argv, timeout)


_UPLOAD_ALLOWED = contextvars.ContextVar("openart_upload_allowed", default=False)


@contextmanager
def allow_upload_reference():
    """Scope for exactly one guarded `upload add`. Only lib.openart_jobs.upload_reference
    enters it, after verifying a qualified nonspending upload contract and source binding."""
    token = _UPLOAD_ALLOWED.set(True)
    try:
        yield
    finally:
        _UPLOAD_ALLOWED.reset(token)


def run_upload_reference(source: Path, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Exact `upload add <abs-file>`; refused outside allow_upload_reference()."""
    if not _UPLOAD_ALLOWED.get():
        raise OpenArtCLIError("upload_unqualified", "upload requires a qualified nonspending upload contract")
    source = Path(source)
    if not source.is_absolute() or str(source).startswith("-") or "\x00" in str(source):
        raise OpenArtCLIError("invalid_argument", "upload source must be an absolute path")
    return _run_checked(["upload", "add", str(source)], validate_timeout(timeout))


def _run_checked(argv: list[str], timeout: float) -> dict:
    if is_offline():
        raise OpenArtCLIError("offline_only", "OpenArt CLI invocation refused during offline preparation")
    binary = resolve_binary()
    full = argv + GLOBAL_FLAGS
    op_deadline = time.monotonic() + timeout
    with transport_lock(wait_timeout=timeout) as root:
        timeout = lock_remaining(op_deadline)
        started = _dt.datetime.now(_dt.timezone.utc).isoformat()
        stem = f"{started[:19].replace(':', '')}-{uuid.uuid4().hex[:8]}"
        streams = private_dir("streams")
        out_path, err_path = streams / f"{stem}.stdout", streams / f"{stem}.stderr"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        out_fd = os.open(out_path, flags, 0o600)
        try:
            err_fd = os.open(err_path, flags, 0o600)
        except BaseException:
            os.close(out_fd)
            raise
        try:
            try:
                proc = subprocess.run([binary, *full], shell=False, stdin=subprocess.DEVNULL,
                                      stdout=out_fd, stderr=err_fd, timeout=timeout,
                                      cwd=str(root), env=_child_env(),
                                      pass_fds=(held_lock_fd(),))
                returncode = proc.returncode
            except FileNotFoundError:
                raise OpenArtCLIError("missing_binary", "openart CLI disappeared before invocation")
            except subprocess.TimeoutExpired:
                raise OpenArtCLIError("timeout", f"openart {' '.join(argv[:2])} exceeded {timeout}s",
                                      {"command": " ".join(argv[:2]), "stderr": _tail(err_path)})
        finally:
            os.close(out_fd)
            os.close(err_fd)
        diag = {"command": " ".join(argv[:2]), "returncode": returncode, "stderr": _tail(err_path)}
        size = out_path.stat().st_size
        if size > MAX_STDOUT:
            raise OpenArtCLIError("output_too_large", f"stdout exceeded {MAX_STDOUT} bytes", diag)
        if returncode != 0:
            raise OpenArtCLIError("nonzero_exit", f"openart exited {returncode}", diag)
        with open(out_path, "rb") as fh:
            stdout = fh.read(MAX_STDOUT + 1)
        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenArtCLIError("malformed_json", "openart --json output was not JSON", diag)
        receipt = {"argv": full, "started_at": started, "returncode": returncode,
                   "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "parsed": parsed,
                   "streams": {"stdout": out_path.name, "stderr": err_path.name}}
        receipt_bytes = json.dumps(receipt, sort_keys=True).encode()
        write_private(root / "receipts" / f"{stem}.json", receipt_bytes)
    return {"argv": full, "parsed": parsed, "public": redact(parsed), "receipt_id": stem,
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "stdout_sha256": receipt["stdout_sha256"]}


def _tail(path: Path) -> str:
    """Bounded, redacted stderr tail for public diagnostics (raw stays private)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, path.stat().st_size - MAX_PUBLIC_TEXT))
            return redact_text(fh.read(MAX_PUBLIC_TEXT).decode("utf-8", "replace"))
    except OSError:
        return ""


# --- argv builders ---------------------------------------------------------
def _ident(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise OpenArtCLIError("invalid_argument", f"{field} must match {_ID_RE.pattern}")
    return value


def model_cost_argv(model: str, mode: str) -> list[str]:
    """Nonspending price for model+mode only; v0.1.1 accepts no settings dimensions."""
    return ["model", "cost", "--model", _ident(model, "model"), "--mode", _ident(mode, "mode")]


def model_form_argv(model: str, mode: str) -> list[str]:
    return ["model", "form", _ident(model, "model"), _ident(mode, "mode")]


def native_video_argv(prompt: str, *, model: str, mode: str, duration: Any = None,
                      aspect_ratio: Optional[str] = None, resolution: Optional[str] = None,
                      image: Optional[str] = None, image_url: Optional[str] = None) -> list[str]:
    """Creative argv shared by native dry-run (U1) and any future frozen submit (U2).

    U1 only appends --dry-run. A later submit must append its own frozen flags to this
    exact list so the creative body stays identical to the inspected dry-run body.
    """
    if not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith("-") or len(prompt) > 8000:
        raise OpenArtCLIError("invalid_argument", "prompt must be non-empty, <=8000 chars, and not start with '-'")
    _ident(mode, "mode")
    if mode not in {"text2video", "image2video"}:
        raise OpenArtCLIError("invalid_argument", "mode must be text2video or image2video (v0.1.1 help)")
    if image is not None:
        # Local files auto-upload inside the CLI; upload billing is unqualified.
        raise OpenArtCLIError("unsupported_gate", "local --image is gated on upload_billing qualification")
    if (mode == "image2video") != (image_url is not None):
        # The CLI infers image2video from --image; only a retained HTTPS upload URL
        # (U2 profile-gated) may represent it truthfully.
        raise OpenArtCLIError("unsupported_gate", "image2video requires a qualified retained upload URL")
    if image_url is not None and (not isinstance(image_url, str) or not _IMAGE_URL_RE.match(image_url)):
        raise OpenArtCLIError("unsupported_gate", "image reference must be a retained https URL")
    argv = ["generate", "video", prompt, "--model", _ident(model, "model")]
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 120:
            raise OpenArtCLIError("invalid_argument", "duration must be an integer second count")
        argv += ["--duration", str(duration)]
    if aspect_ratio is not None:
        if not isinstance(aspect_ratio, str) or not _RATIO_RE.match(aspect_ratio):
            raise OpenArtCLIError("invalid_argument", "aspect_ratio must look like 16:9")
        argv += ["--aspect-ratio", aspect_ratio]
    if resolution is not None:
        if not isinstance(resolution, str) or not _RES_RE.match(resolution):
            raise OpenArtCLIError("invalid_argument", "resolution must look like 720p")
        argv += ["--resolution", resolution]
    if image_url is not None:
        argv += ["--image", image_url]
    return argv


@contextmanager
def allow_image_reference(enabled: bool = True):
    """Scope in which a retained https --image may pass the dry-run grammar.

    Only lib.openart_jobs enters this, after verifying a profile-qualified upload record.
    """
    token = _IMAGE_FLAG_ALLOWED.set(bool(enabled))
    try:
        yield
    finally:
        _IMAGE_FLAG_ALLOWED.reset(token)


def native_dry_run_argv(prompt: str, **kwargs: Any) -> list[str]:
    return native_video_argv(prompt, **kwargs) + ["--dry-run"]


def native_submit_argv(prompt: str, **kwargs: Any) -> list[str]:
    """U2 frozen async submit: the exact creative argv plus a single trailing --async.

    Never combined with --output (async+output is forbidden) and never with --dry-run.
    Only lib.openart_jobs.launch_submit may execute this; run_readonly refuses it.
    """
    return native_video_argv(prompt, **kwargs) + ["--async"]


def check_submit_argv(argv: list[str], *, allow_image: bool = False) -> list[str]:
    """Validate a frozen submit argv: creative dry-run grammar + one trailing --async."""
    argv = list(argv)
    if not argv or argv[-1] != "--async" or argv.count("--async") != 1:
        raise OpenArtCLIError("not_submit", "submit argv must end with exactly one --async")
    if any(a in ("--output", "--dry-run") for a in argv):
        raise OpenArtCLIError("not_submit", "submit argv cannot carry --output/--dry-run")
    if argv.count("--image") > 1 or (not allow_image and "--image" in argv):
        raise OpenArtCLIError("not_submit", "--image requires a profile-qualified upload reference")
    try:
        with allow_image_reference(allow_image):
            _check_read_only(argv[:-1] + ["--dry-run"])
    except OpenArtCLIError as exc:
        raise OpenArtCLIError("not_submit", exc.message)
    if argv[:2] != ["generate", "video"]:
        raise OpenArtCLIError("not_submit", "only generate video may be submitted")
    return argv


def dry_run_request(parsed: Any) -> dict:
    """Extract {endpoint, body, body_sha256} from v0.1.1 `--dry-run --json` output.

    Observed (unauthed dummy model, unqualified): {"endpoint": "POST /api/cli/v1/generate",
    "body": {"model", "media", "mode", "params": {...}}}. Server-side creative defaults
    absent from params are NOT represented; bind `model form` defaults separately.
    """
    if not isinstance(parsed, dict) or not isinstance(parsed.get("body"), dict) \
            or not isinstance(parsed.get("endpoint"), str):
        raise OpenArtCLIError("dry_run_shape_unqualified", "dry-run output lacks endpoint/body")
    body = parsed["body"]
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"endpoint": parsed["endpoint"], "body": body, "body_sha256": digest}


# --- evidence interpretation (pure) ----------------------------------------
def form_schema(form: Any, *, model: Optional[str] = None, mode: Optional[str] = None) -> dict:
    """Extract a form schema without discarding or rewriting the captured response.

    Accept legacy bare/``schema`` forms and the observed CLI 0.1.1
    ``jsonSchema`` wrapper. Metadata is checked when the caller binds the form
    to a model/mode qualification.
    """
    if not isinstance(form, dict):
        raise OpenArtCLIError("form_shape_unqualified", "model form output is not a JSON Schema object")
    if "model" in form and model is not None and form["model"] != model:
        raise OpenArtCLIError("form_shape_unqualified", "model form metadata differs from target model")
    if "mode" in form and mode is not None and form["mode"] != mode:
        raise OpenArtCLIError("form_shape_unqualified", "model form metadata differs from target mode")
    if "media" in form and form["media"] != "video":
        raise OpenArtCLIError("form_shape_unqualified", "model form metadata is not video")

    candidates = []
    for key in ("schema", "jsonSchema"):
        if key in form:
            value = form[key]
            if not isinstance(value, dict):
                raise OpenArtCLIError("form_shape_unqualified", "model form schema wrapper is malformed")
            candidates.append(value)
    if isinstance(form.get("properties"), dict):
        candidates.append(form)
    if not candidates:
        raise OpenArtCLIError("form_shape_unqualified", "model form output is not a JSON Schema object")
    schema = candidates[0]
    if any(candidate != schema for candidate in candidates[1:]):
        raise OpenArtCLIError("form_shape_unqualified", "model form has conflicting schema wrappers")
    if not isinstance(schema.get("properties"), dict):
        raise OpenArtCLIError("form_shape_unqualified", "model form output is not a JSON Schema object")
    return schema


def form_controls(form: Any, *, model: Optional[str] = None, mode: Optional[str] = None) -> dict:
    """Controls from `model form` JSON Schema (defaults inline; defaulted props not required)."""
    form = form_schema(form, model=model, mode=mode)
    required = set(form.get("required") or [])
    out = {}
    for name, spec in form["properties"].items():
        spec = spec if isinstance(spec, dict) else {}
        out[name] = {"type": spec.get("type"), "enum": spec.get("enum"), "default": spec.get("default"),
                     "required": name in required}
    return out


def missing_controls(advertised: Iterable[str], required: Iterable[str]) -> list[str]:
    return sorted(set(required) - set(advertised))


def offline_quote_status(evidence: Optional[dict], request_sha256: Optional[str] = None) -> dict:
    """Pure: is retained quote evidence usable for this exact request? Never calls the CLI."""
    if not isinstance(evidence, dict) or evidence.get("kind") != "model_cost":
        return {"status": "quote_required", "reason": "no_retained_quote"}
    if request_sha256 is not None and evidence.get("request_sha256") != request_sha256:
        return {"status": "quote_required", "reason": "request_mismatch"}
    if evidence.get("covers_settings") is not True:
        return {"status": "quote_required", "reason": "settings_not_covered"}
    return {"status": "retained", "evidence": redact(evidence)}


def readiness(probe: bool = False) -> dict:
    """Truthful readiness: generation is never enabled by U1; gates need live evidence."""
    result = {"provider": "openart", "generation_enabled": False,
              "gates": {gate: "unqualified" for gate in UNQUALIFIED_GATES},
              "binary": {"available": False}}
    try:
        result["binary"] = {"available": True, "path": resolve_binary()}
    except OpenArtCLIError as exc:
        result["binary"] = {"available": False, "error": exc.public()}
    if probe and result["binary"]["available"]:
        try:
            version = run_readonly(["version"], timeout=10)
            result["binary"]["version"] = version["public"]
        except OpenArtCLIError as exc:
            result["binary"]["probe_error"] = exc.public()
    return result
