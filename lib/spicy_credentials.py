"""Local Spicy credentials without shell parsing or process-environment mutation.

Only the default SPICY_API_KEY reference may fall back to the user-owned file.
Callers reread it for each binding check, so replacing a key never preserves an
old client's authority. This module never logs or includes values in errors.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

DEFAULT_ENV = "SPICY_API_KEY"
MAX_FILE_BYTES = 16 * 1024
_ASSIGNMENT = re.compile(r"SPICY_API_KEY[ \t]*=[ \t]*(.*)")
_LITERAL = re.compile(r"[A-Za-z0-9._-]+")


class SpicyCredentialError(ValueError):
    """A secret-free local configuration failure."""

    def __init__(self, message: str, *, kind: str = "credential_file_invalid"):
        super().__init__(message)
        self.kind = kind


def _fail(message="Spicy credential file is invalid", *, kind="credential_file_invalid"):
    raise SpicyCredentialError(message, kind=kind) from None


def parse_credential_file(raw: bytes) -> str:
    """Read one literal assignment, never a shell script or general dotenv file."""
    if not isinstance(raw, bytes) or len(raw) > MAX_FILE_BYTES:
        _fail()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail()
    # LF and horizontal tabs are formatting, never part of a credential value.
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159 for char in text):
        _fail()
    secret = None
    for line in text.split("\n"):
        line = line.strip(" \t")
        if not line or line.startswith("#"):
            continue
        assignment = _ASSIGNMENT.fullmatch(line)
        if assignment is None or secret is not None:
            _fail()
        value = assignment.group(1).strip(" \t")
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        if _LITERAL.fullmatch(value) is None:
            _fail()
        secret = value
    if secret is None:
        _fail()
    return secret


def _private_file_bytes() -> bytes:
    """Open via a verified directory FD; refuse links, devices and FIFO races."""
    directory = Path.home() / ".config" / "spicyapi"
    directory_fd = file_fd = None
    try:
        required_flags = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
        if any(not hasattr(os, flag) for flag in required_flags):
            _fail("Spicy credential file security is unsupported on this platform")
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            _fail("Spicy credential directory must be owned by this user with owner-only access")
        file_fd = os.open("credentials.env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_size > MAX_FILE_BYTES):
            _fail("Spicy credential file must be a small regular owner-only file")
        chunks = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_fd)
        fields = ("st_mode", "st_uid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            _fail("Spicy credential file changed while being read")
        return b"".join(chunks)
    except FileNotFoundError:
        _fail("Spicy credential is not configured", kind="credential_missing")
    except OSError:
        _fail("Spicy credential file could not be read securely")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def resolve_credential(name: str) -> str:
    """Preserve environment precedence and existing engine whitespace handling."""
    if name in os.environ:
        secret = os.environ[name].strip()
        if not secret:
            _fail("Spicy credential environment variable is empty", kind="credential_missing")
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in secret):
            _fail("Spicy credential environment variable is invalid", kind="credential_invalid")
        return secret
    if name != DEFAULT_ENV:
        _fail("Spicy credential environment variable is not set", kind="credential_missing")
    return parse_credential_file(_private_file_bytes())
