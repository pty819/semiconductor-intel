"""SSRF guard for every outbound fetch (SEC-02, spec 11 §2 test SEC-02).

One rule set, enforced at three points:

1. **Pre-flight** (:meth:`UrlGuard.validate`) — scheme allowlist, port
   allowlist, literal-IP checks (loopback / private / link-local /
   multicast / reserved / unspecified, v4 and v6, v4-mapped ``::ffff:``
   and 6to4/Teredo embedded v4), integer/hex IP-obfuscation rejection,
   then DNS resolution with *every* resolved address validated.
2. **Per redirect hop** — the fetcher follows redirects manually and
   re-runs ``validate`` on each ``Location`` (spec: 每次重定向重校验).
3. **At the socket** (:class:`GuardedNetworkBackend`) — the httpx
   connection pool's TCP connects are pinned to a freshly validated
   address, so a DNS answer that flips between validation and connect
   (rebinding) cannot redirect the stream into an internal host. TLS SNI
   and certificate verification still use the original hostname because
   httpcore calls ``start_tls(server_hostname=host)`` on the stream the
   backend returns.

Residual gap, documented honestly: DNS answers are validated but the
pinning backend resolves independently, so each of the two resolutions is
independently validated — an attacker whose DNS alternates between a
public and a private address cannot win (the pinned connect re-validates
and fails closed), but we do not prove both resolutions saw the same
address. TLS connection coalescing across origins does not occur because
each pinned connect goes to the validated literal IP.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpcore
import httpx

#: Only http/https leave the process.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Unexpected ports are rejected; 80/443 only by default (SEC-02).
DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})

_SCHEME_DEFAULT_PORTS = {"http": 80, "https": 443}

#: A syntactically valid DNS name (RFC 1123 labels, no underscores).
_DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_REJECTION_REASONS = (
    "loopback",
    "link_local",  # covers 169.254.169.254 metadata endpoints
    "multicast",
    "unspecified",
    "private",
    "reserved",
)

#: Carrier-grade NAT space — newer ipaddress releases no longer fold it
#: into is_private, but it is never a fetch target.
_SHARED_V4 = ipaddress.ip_network("100.64.0.0/10")


class UrlBlockedError(ValueError):
    """The URL targets an address class the guard refuses (SEC-02).

    ``reason`` is a stable machine-readable code (``scheme``, ``port``,
    ``host_invalid``, ``host_obfuscated``, ``loopback``, …,
    ``resolution_empty``, ``resolution_invalid``, or the checked address
    itself for literal IPs).
    """

    def __init__(self, reason: str, url: str) -> None:
        super().__init__(f"URL blocked by SSRF guard ({reason}): {url}")
        self.reason = reason
        self.url = url


class Resolver(Protocol):
    """DNS resolution seam — tests inject deterministic answers."""

    async def resolve(self, host: str) -> list[str]: ...


class AsyncioResolver:
    """Production resolver over ``loop.getaddrinfo`` (A + AAAA)."""

    async def resolve(self, host: str) -> list[str]:
        import asyncio

        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
        seen: list[str] = []
        for info in infos:
            sockaddr = info[4]
            addr = sockaddr[0]
            if addr not in seen:
                seen.append(addr)
        return seen


def _reject_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Rejection reason for one address, or ``None`` when public.

    IPv6 addresses carrying embedded IPv4 (v4-mapped ``::ffff:x.x.x.x``,
    6to4 ``2002::/16``, Teredo ``2001::/32``) are judged by the embedded
    address too — ``::ffff:127.0.0.1`` is loopback, not a shiny new v6
    host.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return _reject_address(mapped)
        sixtofour = ip.sixtofour
        if sixtofour is not None:
            return _reject_address(sixtofour)
        teredo = ip.teredo
        if teredo is not None:
            for embedded in teredo:
                reason = _reject_address(embedded)
                if reason is not None:
                    return reason
    for reason in _REJECTION_REASONS:
        if getattr(ip, f"is_{reason}"):
            return reason
    if ip.version == 4 and ip in _SHARED_V4:
        return "private"
    return None


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """A URL that passed every check, with the addresses DNS returned."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


class UrlGuard:
    """Validates and pins outbound URLs (SEC-02)."""

    def __init__(
        self,
        *,
        allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
        resolver: Resolver | None = None,
    ) -> None:
        self._allowed_ports = frozenset(allowed_ports)
        self._resolver = resolver if resolver is not None else AsyncioResolver()

    # -- static checks -------------------------------------------------------

    def parse(self, url: str) -> tuple[str, str, int]:
        """Scheme/port/literal-host checks without touching DNS.

        Returns ``(scheme, host, port)`` with the host lowercased and
        IPv6 brackets stripped (``urlsplit.hostname`` form).
        """
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise UrlBlockedError("scheme", url)
        host = parts.hostname
        if not host:
            raise UrlBlockedError("host_invalid", url)
        try:
            port = parts.port
        except ValueError as exc:
            raise UrlBlockedError("port", url) from exc
        port = _SCHEME_DEFAULT_PORTS.get(scheme) if port is None else port
        if port not in self._allowed_ports:
            raise UrlBlockedError("port", url)
        self._check_host_literal(host, url)
        return scheme, host, port

    def _check_host_literal(self, host: str, url: str) -> None:
        """Literal-IP and obfuscation checks for one host string."""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            reason = _reject_address(ip)
            if reason is not None:
                raise UrlBlockedError(reason, url)
            return
        # Not a strict literal. A DNS name must look like one and must not
        # be a numeric form socket APIs would happily resolve to an IP:
        # "2130706433", "0x7f.0.0.1", "127.1" all pass inet_aton and all
        # mean loopback.
        labels = host.split(".")
        if len(host) > 253 or not all(_DNS_LABEL.match(label) for label in labels):
            raise UrlBlockedError("host_invalid", url)
        if all(label.isdigit() for label in labels) and len(labels) <= 4:
            raise UrlBlockedError("host_obfuscated", url)
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            # inet_aton accepts hex/octal/short forms ipaddress rejects.
            raise UrlBlockedError("host_obfuscated", url)

    # -- resolution + full validation ---------------------------------------

    async def resolve_and_validate(self, host: str, port: int) -> list[str]:
        """Resolve *host* and validate every address; returns the pinned
        address strings in resolver order."""
        if port not in self._allowed_ports:
            raise UrlBlockedError("port", f"{host}:{port}")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            reason = _reject_address(ip)
            if reason is not None:
                raise UrlBlockedError(reason, host)
            return [str(ip)]
        self._check_host_literal(host, host)
        addresses = await self._resolver.resolve(host)
        if not addresses:
            raise UrlBlockedError("resolution_empty", host)
        validated: list[str] = []
        for raw in addresses:
            try:
                resolved = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise UrlBlockedError("resolution_invalid", host) from exc
            reason = _reject_address(resolved)
            if reason is not None:
                raise UrlBlockedError(reason, host)
            validated.append(str(resolved))
        return validated

    async def validate(self, url: str) -> ValidatedTarget:
        """Full pre-flight: static checks plus every resolved address."""
        scheme, host, port = self.parse(url)
        addresses = await self.resolve_and_validate(host, port)
        return ValidatedTarget(
            url=url,
            scheme=scheme,
            host=host,
            port=port,
            addresses=tuple(addresses),
        )


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Pins every httpcore TCP connect to a validated address.

    httpcore passes the *origin hostname* to ``connect_tcp`` and then
    wraps the returned stream in TLS with ``server_hostname=<hostname>``,
    so connecting to the validated literal keeps SNI and certificate
    verification intact while the socket peer is guaranteed to be an
    address this guard validated moments earlier (anti-rebinding).
    """

    def __init__(
        self,
        guard: UrlGuard,
        delegate: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._guard = guard
        self._delegate = (
            delegate if delegate is not None else httpcore.AsyncNetworkBackend()
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._guard.resolve_and_validate(host, port)
        return await self._delegate.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: object | None = None
    ) -> httpcore.AsyncNetworkStream:
        # The guarded fetcher never dials unix sockets; the delegate's
        # default (not implemented) stands.
        return await self._delegate.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class GuardNotInstalled(RuntimeError):
    """A client that should be SSRF-guarded is not (SEC-02).

    Raised at construction/startup when an httpx/httpcore upgrade
    defeats the connection-pool patch — pinning must fail loudly, never
    silently degrade to an unguarded pool.
    """


def guarded_async_client(
    guard: UrlGuard, *, timeout: float = 60.0
) -> httpx.AsyncClient:
    """Build an httpx client whose every TCP connect is guard-pinned.

    The returned client follows no redirects automatically (callers
    re-validate each hop manually); ``assert_guarded_client`` is invoked
    before returning so the pin is proven, not assumed.
    """
    transport = httpx.AsyncHTTPTransport()
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.AsyncConnectionPool):
        raise GuardNotInstalled(
            "httpx transport no longer exposes an httpcore"
            " AsyncConnectionPool; the guarded client cannot be built"
            " (SEC-02) — pin the httpx/httpcore versions or port the"
            " patch to the new layout"
        )
    pool._network_backend = GuardedNetworkBackend(
        guard, delegate=pool._network_backend
    )
    client = httpx.AsyncClient(
        transport=transport, follow_redirects=False, timeout=timeout
    )
    assert_guarded_client(client)
    return client


def assert_guarded_client(
    client: httpx.Client | httpx.AsyncClient,
) -> GuardedNetworkBackend:
    """Startup assertion (Finding 2): the client's pool backend must be a
    :class:`GuardedNetworkBackend`. Call wherever a production client is
    built or injected so a dependency upgrade fails at boot, not in the
    field."""
    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if not isinstance(backend, GuardedNetworkBackend):
        raise GuardNotInstalled(
            f"client {type(client).__name__} has no GuardedNetworkBackend"
            " on its connection pool — outbound fetches would dial"
            " unvalidated addresses (SEC-02)"
        )
    return backend
