"""Shared bounded network and filesystem helpers used by backend operations."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_MAX_DOWNLOAD = 64 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def validate_http_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Only absolute HTTP(S) URLs are supported.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not supported.")
    return parsed.geturl()


# Well-known prefixes that embed an IPv4 address. Python reports some of them
# as global even though the embedded address is link-local or loopback.
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_6TO4_NETWORK = ipaddress.ip_network("2002::/16")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    if any(ip in network for network in _NAT64_NETWORKS):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _6TO4_NETWORK:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def is_public_ip(address) -> bool:
    """True when *address* is a globally routable unicast address.

    Rejects loopback, private, link-local, multicast, reserved, and IPv6
    forms that embed one of those (IPv4-mapped, NAT64, 6to4).
    """
    if isinstance(address, str):
        address = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            address = embedded
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return False
    if (
        address.is_multicast
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return bool(address.is_global)


def _addresses_for_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    host = str(host or "").strip().rstrip(".")
    if not host:
        raise ValueError("The download URL needs a host.")
    if host.casefold() == "localhost" or host.casefold().endswith(".localhost"):
        raise ValueError("Downloads may only target public addresses.")
    try:
        return [ipaddress.ip_address(host.split("%", 1)[0])]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as error:
        raise ValueError("The download host could not be resolved.") from error
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        except (IndexError, ValueError) as error:
            raise ValueError("The download host resolved to an invalid address.") from error
    if not found:
        raise ValueError("The download host could not be resolved.")
    return found


def assert_public_url(url: str) -> str:
    """Return *url* when every resolved address is public. Fail closed."""
    cleaned = validate_http_url(url)
    host = urlparse(cleaned).hostname
    if not host:
        raise ValueError("The download URL needs a host.")
    for address in _addresses_for_host(host):
        if not is_public_ip(address):
            raise ValueError("Downloads may only target public addresses.")
    return cleaned


def public_https_url(url: str) -> str:
    """Return *url* when it is public HTTPS, otherwise an empty string."""
    try:
        cleaned = assert_public_url(url)
    except ValueError:
        return ""
    if urlparse(cleaned).scheme != "https":
        return ""
    return cleaned


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect target before following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urljoin(getattr(req, "full_url", "") or "", newurl)
        assert_public_url(absolute)
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def guarded_urlopen(request, timeout=30):
    """urlopen that refuses redirects onto a private or unresolved host."""
    opener = urllib.request.build_opener(_PublicRedirectHandler)
    return opener.open(request, timeout=timeout)


def read_limited(response, max_bytes=MAX_RESPONSE_BYTES) -> bytes:
    """Read a response without allowing an unbounded API payload into memory."""
    declared = 0
    headers = getattr(response, "headers", None)
    if headers:
        try:
            declared = int(headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            declared = 0
    if declared < 0 or declared > max_bytes:
        raise ValueError("The remote response is too large.")
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(CHUNK_SIZE, max(0, max_bytes - total + 1)))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("The remote response is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(Path(path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def atomic_write_bytes(path: Path, data: bytes, *, mode=0o600) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def atomic_write_text(path: Path, value: str, *, mode=0o600) -> Path:
    return atomic_write_bytes(Path(path), str(value).encode("utf-8"), mode=mode)


def atomic_copy_stream(source, path: Path, *, mode=0o600, max_bytes=None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError("The file is larger than the allowed limit.")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def contained_path(path: Path, roots, *, must_exist=False) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    allowed = [Path(root).expanduser().resolve(strict=False) for root in roots]
    if not any(candidate == root or root in candidate.parents for root in allowed):
        raise ValueError(f"Path is outside an approved OpenBox directory: {candidate}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(str(candidate))
    return candidate


def download_file(
    url: str,
    destination: Path,
    *,
    expected_types=(),
    max_bytes=DEFAULT_MAX_DOWNLOAD,
    timeout=30,
    opener=urlopen,
    headers=None,
    sha256="",
    mode=0o644,
) -> Path:
    use_guard = opener is None or opener is urlopen
    url = assert_public_url(url) if use_guard else validate_http_url(url)
    request = Request(url, headers={"User-Agent": "OpenBox/1", **(headers or {})})
    if use_guard:
        opener = guarded_urlopen
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    digest = hashlib.sha256()
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status and int(status) >= 400:
                raise ValueError(f"The remote server returned HTTP {status}.")
            headers = getattr(response, "headers", {}) or {}
            if hasattr(headers, "get_content_type"):
                content_type = headers.get_content_type()
            else:
                content_type = str(headers.get("Content-Type", "")).split(";", 1)[0] if headers else ""
            if expected_types and not any(content_type.startswith(item) for item in expected_types):
                raise ValueError(f"The remote server returned an unsupported content type: {content_type or 'unknown'}")
            try:
                declared = int(headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                declared = 0
            if declared > max_bytes:
                raise ValueError("The download is too large.")
            fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
            with os.fdopen(fd, "wb") as output:
                total = 0
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("The download is too large.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if sha256 and digest.hexdigest().casefold() != str(sha256).casefold():
            raise ValueError("The downloaded file failed checksum verification.")
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, destination)
        fsync_directory(destination.parent)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
    return destination

