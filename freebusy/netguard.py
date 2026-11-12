"""Where the plugin may connect: https only, public addresses only.

Every CalDAV, feed and mail host passes ``check_url`` or ``check_host`` before a
socket is opened. A name that resolves to a private, loopback, link-local,
multicast, reserved or unspecified address is refused, so a config value or a
server redirect cannot point the plugin at this machine or the local network.

One override exists, for tests against local servers only:
``FREEBUSY_ALLOW_LOCAL_TEST_SERVERS=1`` allows plain http and plaintext mail,
and loopback addresses (127.0.0.0/8, ::1). Private and link-local ranges stay
blocked even then. It is read from the process environment, never from config.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Callable, Optional
from urllib.parse import urlsplit

TEST_OVERRIDE = "FREEBUSY_ALLOW_LOCAL_TEST_SERVERS"


class UnsafeTarget(PermissionError):
    pass


def local_test_override() -> bool:
    return os.environ.get(TEST_OVERRIDE, "") == "1"


def _resolve(host: str, port: int) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


def check_ip(ip: str, allow_loopback: bool = False) -> None:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        if allow_loopback:
            return
        raise UnsafeTarget(f"{ip} is a loopback address")
    for flag, what in ((addr.is_private, "private"), (addr.is_link_local, "link-local"), (addr.is_multicast, "multicast"),
                       (addr.is_reserved, "reserved"), (addr.is_unspecified, "unspecified")):
        if flag:
            raise UnsafeTarget(f"{ip} is a {what} address")
    if not addr.is_global:
        raise UnsafeTarget(f"{ip} is not a public address")


def check_host(host: str, port: int, resolve: Callable[[str, int], list[str]] = _resolve) -> list[str]:
    """Resolve ``host`` and refuse it unless every address is public (loopback too under the test override)."""
    if not host or any(c in host for c in "/@ \t\r\n"):
        raise UnsafeTarget(f"not a host name: {host!r}")
    try:
        ips = resolve(host, port)
    except OSError as exc:
        raise UnsafeTarget(f"{host} does not resolve: {exc}") from exc
    if not ips:
        raise UnsafeTarget(f"{host} does not resolve")
    for ip in ips:
        check_ip(ip, allow_loopback=local_test_override())
    return ips


def check_url(url: str, resolve: Optional[Callable[[str, int], list[str]]] = None) -> str:
    """Refuse anything but https to a public host. Returns the URL unchanged when it passes."""
    parts = urlsplit(url or "")
    allowed = {"https", "http"} if local_test_override() else {"https"}
    if parts.scheme not in allowed:
        raise UnsafeTarget(f"only https URLs are allowed, not {parts.scheme or 'no scheme'}: {url}")
    if parts.username or parts.password:
        raise UnsafeTarget("credentials in a URL are refused; passwords live in the keychain")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    check_host(parts.hostname or "", port, resolve or _resolve)
    return url


def check_link(url: str) -> str:
    """A meeting link shown to people: https, a real host name, no credentials. Not resolved (it is never fetched)."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
        raise UnsafeTarget("a meeting link must be an https URL, for example https://meet.jit.si/your-room")
    if parts.username or parts.password:
        raise UnsafeTarget("a meeting link may not carry a user name or password")
    try:
        ipaddress.ip_address(parts.hostname)
        raise UnsafeTarget("a meeting link must use a host name, not an IP address")
    except ValueError:
        pass
    return url.strip()
