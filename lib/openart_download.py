"""Collect qualified CLI result URLs without exposing credentials or trusting DNS twice.

The caller owns original-job binding and supplies a finite, qualified host set.
No authorization headers, cookies, proxies, or ambient HTTP credentials are used.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import io
import math
import os
import queue
import re
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit


class OpenArtDownloadError(ValueError):
    """Sanitized, recoverable collection failure; never permission to resubmit."""


def _fail(message):
    raise OpenArtDownloadError(message)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _fail("Result collection timed out")
    return remaining


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        _fail("Invalid qualified result host")
    value = value.lower()
    # DNS names cannot end in an address-like label. This rejects legacy
    # decimal/octal/hex IPv4 spellings before platform resolver interpretation.
    if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", value.rsplit(".", 1)[-1]):
        _fail("Qualified result hosts must be DNS names")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split(".")):
        _fail("Invalid qualified result host")
    return value


def _url(value, hosts):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})", value):
        _fail("Invalid result URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None or "#" in value:
            _fail("Result URL must be credential-free HTTPS without a fragment")
        if parsed.port not in (None, 443):
            _fail("Unsafe result URL port")
        host = _host(parsed.hostname)
        if parsed.netloc.lower() not in (host, host + ":443"):
            _fail("Malformed result URL authority")
        if host not in hosts:
            _fail("Result host is not qualified")
        return host, parsed.path or "/", parsed.query
    except (TypeError, ValueError) as exc:
        if isinstance(exc, OpenArtDownloadError):
            raise
        _fail("Invalid result URL")


def _resolve_public(host, deadline):
    # A daemon resolver bounds the caller's wait even when platform DNS stalls.
    results = queue.Queue(maxsize=1)
    def resolve():
        try:
            results.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP))
        except Exception:
            results.put(None)
    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = results.get(timeout=_remaining(deadline))
    except queue.Empty:
        _fail("Result DNS resolution timed out")
    if not addresses:
        _fail("Result DNS resolution failed")
    for address in addresses:
        try:
            family, socktype, protocol, _, target = address
            ip = ipaddress.ip_address(target[0])
            if family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP or target[1] != 443 or not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified or (isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip in ipaddress.IPv6Network("64:ff9b::/96") or ip in ipaddress.IPv6Network("64:ff9b:1::/48"))):
                _fail("Result DNS includes a nonpublic or unsafe target")
        except (ValueError, IndexError, TypeError):
            _fail("Invalid result DNS target")
    return addresses[0]


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        super().__init__()
        self.sock, self.deadline = sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        return self.sock.recv_into(buffer)


class _DeadlineSocket:
    """Bound each underlying TLS read, including drip-fed HTTP headers."""
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def makefile(self, mode):
        if mode != "rb":
            _fail("Unsupported result socket access")
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))

    def sendall(self, data):
        self.sock.settimeout(_remaining(self.deadline))
        self.sock.sendall(data)

    def settimeout(self, timeout):
        self.sock.settimeout(min(timeout, _remaining(self.deadline)))

    def close(self):
        self.sock.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        context = ssl.create_default_context()
        # Ignore ambient SSLKEYLOGFILE for result URLs and downloaded media.
        context.keylog_filename = None
        super().__init__(host, port=443, timeout=timeout, context=context)
        self._address = address
        self._deadline = time.monotonic() + timeout

    def connect(self):
        family, socktype, protocol, _, target = self._address
        raw = socket.socket(family, socktype, protocol)
        try:
            raw.settimeout(_remaining(self._deadline))
            # target is the numeric vetted sockaddr, never a hostname.
            raw.connect(target)
            raw.settimeout(_remaining(self._deadline))
            tls = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock = _DeadlineSocket(tls, self._deadline)
        except BaseException:
            raw.close()
            raise

    def set_timeout(self, timeout):
        self.timeout = timeout
        if self.sock is not None:
            self.sock.settimeout(timeout)


def _open_output_parent(output_path, output_root):
    root, target = Path(os.path.abspath(output_root)), Path(os.path.abspath(output_path))
    try:
        target.relative_to(root)
    except ValueError:
        _fail("Result output escapes its reserved root")
    if target == root:
        _fail("Result output must name a file")
    # Walk every ancestor with no-follow directory descriptors; these remain
    # bound to the inspected directory if a concurrent writer swaps a path.
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in target.parent.parts[1:]:
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        try:
            os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("Result output already exists")
        return descriptor, target
    except BaseException:
        os.close(descriptor)
        raise


def collect_output(url, output_path, *, allowed_hosts, output_root,
                   expected_sha256=None, max_bytes=536870912, timeout=30.0,
                   max_redirects=5):
    """Download and atomically publish one result; returns verified local metadata.

    Root and destination parent must already exist and have no symlink ancestors.
    All errors are sanitized. Existing destination files are never replaced.
    """
    parent = None
    temporary = None
    connection = None
    response = None
    try:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= 2**63 - 1:
            _fail("Invalid result byte limit")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            _fail("Invalid result timeout")
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or not 0 <= max_redirects <= 20:
            _fail("Invalid result redirect limit")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256)):
            _fail("Invalid expected result digest")
        if not isinstance(allowed_hosts, (set, frozenset, list, tuple)) or not 0 < len(allowed_hosts) <= 100:
            _fail("Result hosts must be an explicit finite collection")
        hosts = {_host(host) for host in allowed_hosts}
        original_host, _, _ = _url(url, hosts)
        # Keep all remote URL path/query material out of public metadata.
        source_url = "https://" + original_host + "/[redacted]"
        parent, target = _open_output_parent(output_path, output_root)
        deadline = time.monotonic() + timeout
        current = url
        for hop in range(max_redirects + 1):
            host, path, query = _url(current, hosts)
            address = _resolve_public(host, deadline)
            connection = _PinnedHTTPSConnection(host, address, _remaining(deadline))
            connection.request("GET", path + ("?" + query if query else ""), headers={"Accept-Encoding": "identity"})
            connection.set_timeout(_remaining(deadline))
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or hop == max_redirects:
                    _fail("Invalid or excessive result redirects")
                if any(ord(c) <= 32 or ord(c) >= 127 for c in location) or "\\" in location:
                    _fail("Invalid result redirect")
                current = urljoin(current, location)
                response.close()
                response = None
                connection.close()
                connection = None
                continue
            if response.status != 200:
                _fail("Result host did not return a successful download")
            if response.getheader("Content-Encoding") not in (None, "identity"):
                _fail("Unsupported result content encoding")
            length = response.getheader("Content-Length")
            if length is not None:
                if not re.fullmatch(r"[0-9]{1,20}", length) or not 0 < int(length) <= max_bytes:
                    _fail("Invalid or oversized result content length")
                length = int(length)
            temporary = ".openart-download-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as output:
                while True:
                    connection.set_timeout(_remaining(deadline))
                    data = response.read(min(65536, max_bytes - size + 1))
                    if not data:
                        break
                    size += len(data)
                    if size > max_bytes:
                        _fail("Result exceeds its byte limit")
                    digest.update(data)
                    output.write(data)
                if size == 0 or (length is not None and size != length):
                    _fail("Result download is empty or incomplete")
                sha256 = digest.hexdigest()
                if expected_sha256 is not None and sha256 != expected_sha256.lower():
                    _fail("Result digest does not match original job evidence")
                output.flush()
                os.fsync(output.fileno())
            _remaining(deadline)
            os.link(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
            temporary = None
            os.fsync(parent)
            return {"path": str(target), "size": size, "sha256": sha256,
                    "source_url": source_url, "source_host": original_host}
        _fail("Result redirect limit exceeded")
    except OpenArtDownloadError:
        raise
    except Exception:
        raise OpenArtDownloadError("Result collection or output publication failed") from None
    finally:
        for resource in (response, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if parent is not None:
            try:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(parent)
