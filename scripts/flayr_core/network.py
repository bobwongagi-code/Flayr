"""Outbound network policy for provider API calls."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit


DEFAULT_ALLOWED_API_HOSTS = frozenset({
    "api.openai.com",
    "dashscope.aliyuncs.com",
})


class OutboundURLPolicyError(ValueError):
    """Raised when an external URL is outside the trusted network policy."""


@dataclass(frozen=True)
class ValidatedOutboundURL:
    """Validated endpoint identity used for diagnostics and tests."""

    url: str
    hostname: str
    port: int
    resolved_addresses: tuple[str, ...]


def _resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise OutboundURLPolicyError(f"无法解析批准的 API 域名：{hostname}") from exc

    addresses: set[str] = set()
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except (IndexError, ValueError):
            continue
        if not address.is_global:
            raise OutboundURLPolicyError(
                f"API 域名解析到非公网地址，已拒绝：{hostname} -> {address}"
            )
        addresses.add(str(address))
    if not addresses:
        raise OutboundURLPolicyError(f"API 域名没有可用公网地址：{hostname}")
    return tuple(sorted(addresses))


def validate_outbound_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_API_HOSTS,
) -> ValidatedOutboundURL:
    """Validate an API URL before credentials or media leave the worker.

    Redirects are intentionally not followed by the transport layer. A 3xx
    response is a hard failure, so no unvalidated redirect target can become a
    second outbound request. If redirect support is added later, this function
    must be called again for every ``Location`` target.
    """
    try:
        parsed = urlsplit(str(url or ""))
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise OutboundURLPolicyError("API URL 格式无效") from exc

    allowed = {str(host).lower().rstrip(".") for host in allowed_hosts}
    if parsed.scheme.lower() != "https":
        raise OutboundURLPolicyError("API URL 必须使用 HTTPS")
    if not hostname or hostname not in allowed:
        raise OutboundURLPolicyError(f"API 域名不在允许列表中：{hostname or '<empty>'}")
    if parsed.username or parsed.password or parsed.fragment:
        raise OutboundURLPolicyError("API URL 不得包含用户信息或 fragment")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise OutboundURLPolicyError("API URL 不得直接使用 IP 地址")

    resolved_port = port or 443
    addresses = _resolve_public_addresses(hostname, resolved_port)
    return ValidatedOutboundURL(
        url=str(url),
        hostname=hostname,
        port=resolved_port,
        resolved_addresses=addresses,
    )
