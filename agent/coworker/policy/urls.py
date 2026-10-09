"""Web addresses the browser and fetch tools may use.

Two layers, because a name can point anywhere. ``check_url`` is a pure check on
the text of an address: scheme, userinfo, host spelling and literal IP
addresses. ``resolve_and_pin`` resolves a host and refuses when any answer is
not a public address, so a public-looking name that points at 127.0.0.1 or at
the cloud metadata service (169.254.169.254, inside the link-local range) cannot
be used. The fetch tool then connects to one of the returned addresses itself,
so DNS cannot change its answer between the check and the connection.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Optional, Union
from urllib.parse import urlsplit

REFUSED = "url_refused"

# Control characters and backslashes. Browsers read a backslash as a path
# separator and urlsplit does not, so "https://evil.example\@10.0.0.1" would be
# judged one way here and fetched another way.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f\\]")
# A top-level domain is never all digits. A last label such as "1", "123" or
# "0x7f" means an IPv4 spelling (127.1, 2130706433, 0x7f000001) that the
# resolver may still accept, so it is refused without resolving.
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|\d+")

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class UrlRefused(ValueError):
    """Raised by ``resolve_and_pin`` when a host may not be fetched."""

    code = REFUSED


def check_url(url: str) -> Optional[str]:
    """``url_refused`` when the address may not be opened, else None. Literal hosts only."""
    if not isinstance(url, str) or not url or _UNSAFE.search(url):
        return REFUSED
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a malformed or out-of-range port
    except ValueError:
        return REFUSED
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return REFUSED
    host = (parts.hostname or "").rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return REFUSED
    literal = _literal_ip(host)
    if literal is not None:
        return None if _is_public(literal) else REFUSED
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        return REFUSED
    return None


def resolve_and_pin(host: str) -> list[str]:
    """Every address the host resolves to, each once, in resolver order.

    Raises ``UrlRefused`` when any address is not public. A name that does not
    resolve raises the resolver's ``OSError`` unchanged.
    """
    addresses: list[str] = []
    for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
        text = info[4][0].split("%", 1)[0]
        if not _is_public(ipaddress.ip_address(text)):
            raise UrlRefused(f"{host} resolves to a non-public address")
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise UrlRefused(f"{host} has no address")
    return addresses


def _literal_ip(host: str) -> Optional[IPAddress]:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _is_public(address: IPAddress) -> bool:
    """Public means globally routable and not in any special-purpose range."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.sixtofour is not None:
            address = address.sixtofour
    # is_global reports multicast addresses (224.0.0.1, ff02::1) as global, so
    # the special-purpose categories are checked by name before is_global.
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return address.is_global
