"""Listen addresses and origin admission for the single public gateway."""

import ipaddress
from urllib.parse import urlsplit


def bind_host(value: str) -> str:
    if value == "localhost":
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError(
            "Host must be an IPv4/IPv6 address or localhost, without a URL or port"
        ) from None
    if address.is_multicast:
        raise ValueError("Host must be a unicast or wildcard listen address")
    return str(address)


def loopback(host: str) -> bool:
    return host == "localhost" or ipaddress.ip_address(host).is_loopback


def client_host(host: str) -> str:
    """Wildcard addresses bind sockets; local clients connect through loopback."""
    if host == "localhost":
        return host
    address = ipaddress.ip_address(host)
    if address.is_unspecified:
        return "127.0.0.1" if address.version == 4 else "::1"
    return str(address)


def http_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}"


def trusted(headers, listen_host: str) -> bool:
    authority = headers.get("host", "")
    try:
        target = urlsplit(f"http://{authority}")
        hostname = target.hostname
        # Reject malformed authorities, including userinfo and invalid ports.
        if (
            not hostname
            or target.username is not None
            or target.password is not None
            or target.path
            or target.query
            or target.fragment
            or target.port == 0
        ):
            return False
        if hostname != "localhost":
            address = ipaddress.ip_address(hostname)
            if not address.is_loopback:
                if listen_host == "localhost":
                    return False
                listener = ipaddress.ip_address(listen_host)
                if not listener.is_unspecified and address != listener:
                    return False
    except ValueError:
        return False
    # Numeric Host headers avoid allowing arbitrary DNS names on wildcard
    # listeners. Browser requests must still originate from the same authority.
    origin = headers.get("origin")
    if origin and origin.rstrip("/") not in (
        f"http://{authority}",
        f"https://{authority}",
    ):
        return False
    return headers.get("sec-fetch-site") != "cross-site"
