"""SSRF guard battery (SEC-02, spec 11 §2): scheme/port/literal checks,
v4-mapped v6, integer/hex obfuscation, per-resolution validation, and the
pinned-connect (anti-rebinding) backend shape. No network — resolution is
stubbed.
"""

from __future__ import annotations

import pytest

from intel.sources.ssrf import (
    GuardedNetworkBackend,
    UrlBlockedError,
    UrlGuard,
)


class StaticResolver:
    """Deterministic DNS answers; unknown hosts get one public address."""

    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        self.mapping = dict(mapping or {})

    async def resolve(self, host: str) -> list[str]:
        return self.mapping.get(host, ["93.184.216.34"])


def guard(mapping: dict[str, list[str]] | None = None) -> UrlGuard:
    return UrlGuard(resolver=StaticResolver(mapping))


async def blocked(url: str, reason: str, *, resolver_map=None) -> None:
    with pytest.raises(UrlBlockedError) as excinfo:
        await guard(resolver_map).validate(url)
    assert excinfo.value.reason == reason, (
        f"{url}: expected {reason}, got {excinfo.value.reason}"
    )


# -- scheme / port ------------------------------------------------------------


async def test_rejects_non_http_schemes() -> None:
    for url in (
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
    ):
        await blocked(url, "scheme")


async def test_rejects_unexpected_ports() -> None:
    await blocked("http://example.com:8080/", "port")
    await blocked("https://example.com:8443/", "port")
    await blocked("https://example.com:22/", "port")
    await blocked("http://example.com:bad/", "port")


async def test_allows_default_and_explicit_standard_ports() -> None:
    for url in (
        "http://example.com/",
        "http://example.com:80/",
        "https://example.com/",
        "https://example.com:443/",
    ):
        target = await guard().validate(url)
        assert target.port in (80, 443)


# -- literal IPv4 address classes ----------------------------------------------


async def test_rejects_private_v4() -> None:
    for url in (
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://100.64.0.1/",  # carrier-grade NAT
    ):
        await blocked(url, "private")


async def test_rejects_loopback_linklocal_metadata_multicast() -> None:
    await blocked("http://127.0.0.1/", "loopback")
    await blocked("http://127.8.8.8/", "loopback")
    await blocked("http://169.254.169.254/latest/meta-data/", "link_local")
    await blocked("http://169.254.0.1/", "link_local")
    await blocked("http://224.0.0.1/", "multicast")
    await blocked("http://0.0.0.0/", "unspecified")


async def test_allows_public_literal_ips() -> None:
    target = await guard().validate("https://93.184.216.34/")
    assert target.addresses == ("93.184.216.34",)
    # Just outside the RFC1918 blocks.
    await guard().validate("http://172.32.0.1/")
    await guard().validate("http://8.8.8.8/")


# -- IPv6, including v4-mapped / embedded --------------------------------------


async def test_rejects_v6_special_ranges() -> None:
    await blocked("http://[::1]/", "loopback")
    await blocked("http://[fe80::1]/", "link_local")
    await blocked("http://[ff02::1]/", "multicast")
    await blocked("http://[fc00::1]/", "private")
    await blocked("http://[fd12:3456::1]/", "private")


async def test_rejects_v4_mapped_v6() -> None:
    await blocked("http://[::ffff:127.0.0.1]/", "loopback")
    await blocked("http://[::ffff:10.0.0.1]/", "private")
    await blocked("http://[::ffff:169.254.169.254]/", "link_local")


async def test_rejects_6to4_embedded_private() -> None:
    # 2002:7f00:0001:: is 127.0.0.1 wrapped in a 6to4 prefix.
    await blocked("http://[2002:7f00:1::]/", "loopback")


# -- obfuscated numeric hosts ---------------------------------------------------


async def test_rejects_integer_and_hex_ip_obfuscation() -> None:
    await blocked("http://2130706433/", "host_obfuscated")  # 127.0.0.1
    await blocked("http://0x7f.0.0.1/", "host_obfuscated")
    await blocked("http://127.1/", "host_obfuscated")
    await blocked("http://0177.0.0.1/", "host_obfuscated")  # octal


async def test_rejects_invalid_hostnames() -> None:
    await blocked("http://bad_host.example.com/", "host_invalid")
    await blocked("http://user:pass@/", "host_invalid")


# -- DNS resolution: every address validated ------------------------------------


async def test_resolves_and_validates_every_address() -> None:
    target = await guard().validate("https://dualstack.example.com/")
    assert set(target.addresses) == {"93.184.216.34"}


async def test_rejects_when_any_resolved_address_is_private() -> None:
    await blocked(
        "https://mixed.example.com/",
        "private",
        resolver_map={"mixed.example.com": ["93.184.216.34", "10.0.0.9"]},
    )
    await blocked(
        "https://v6mixed.example.com/",
        "loopback",
        resolver_map={"v6mixed.example.com": ["::ffff:127.0.0.1"]},
    )


async def test_rejects_empty_resolution() -> None:
    await blocked(
        "https://nxdomain.example.com/",
        "resolution_empty",
        resolver_map={"nxdomain.example.com": []},
    )


async def test_rejects_unparseable_resolution() -> None:
    await blocked(
        "https://garbage.example.com/",
        "resolution_invalid",
        resolver_map={"garbage.example.com": ["not-an-ip"]},
    )


async def test_dns_name_resolving_to_loopback_is_blocked() -> None:
    await blocked(
        "http://localhost/",
        "loopback",
        resolver_map={"localhost": ["127.0.0.1", "::1"]},
    )


# -- anti-rebinding: the pinned backend -----------------------------------------


class FakeDelegate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def connect_tcp(
        self, host, port, *, timeout=None, local_address=None, socket_options=None
    ):
        self.calls.append((host, port))
        return object()  # sentinel stream


async def test_backend_pins_connect_to_validated_ip() -> None:
    delegate = FakeDelegate()
    backend = GuardedNetworkBackend(
        guard({"good.example.com": ["93.184.216.34", "2606:2800:220:1::1"]}),
        delegate=delegate,
    )
    await backend.connect_tcp("good.example.com", 443)
    # The socket dialed the validated address literal, never the hostname
    # (anti-rebinding: a second, different DNS answer cannot redirect it).
    assert delegate.calls[0][0] in ("93.184.216.34", "2606:2800:220:1::1")
    assert delegate.calls[0][1] == 443


async def test_backend_rejects_rebinding_to_private() -> None:
    delegate = FakeDelegate()
    backend = GuardedNetworkBackend(
        guard({"evil.example.com": ["10.0.0.5"]}), delegate=delegate
    )
    with pytest.raises(UrlBlockedError) as excinfo:
        await backend.connect_tcp("evil.example.com", 443)
    assert excinfo.value.reason == "private"
    assert delegate.calls == []  # nothing dialed


async def test_backend_rejects_unexpected_port() -> None:
    delegate = FakeDelegate()
    backend = GuardedNetworkBackend(guard(), delegate=delegate)
    with pytest.raises(UrlBlockedError) as excinfo:
        await backend.connect_tcp("good.example.com", 8080)
    assert excinfo.value.reason == "port"
    assert delegate.calls == []


# -- guarded client construction (Finding 2: no silent degradation) ----------------


async def test_guarded_async_client_pins_the_pool_backend() -> None:

    from intel.sources.ssrf import (
        GuardedNetworkBackend,
        assert_guarded_client,
        guarded_async_client,
    )

    g = guard()
    client = guarded_async_client(g)
    try:
        backend = assert_guarded_client(client)  # raises if not pinned
        assert isinstance(backend, GuardedNetworkBackend)
        assert backend._guard is g  # the pin uses OUR guard
    finally:
        await client.aclose()
    # The factory's redirect posture: hops are re-validated manually.
    assert client.follow_redirects is False


def test_assert_guarded_client_fails_loudly_on_unguarded_clients() -> None:
    import httpx
    import pytest as _pytest

    from intel.sources.ssrf import GuardNotInstalled, assert_guarded_client

    with _pytest.raises(GuardNotInstalled):
        assert_guarded_client(httpx.AsyncClient())
    mocked = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200))
    )
    with _pytest.raises(GuardNotInstalled):
        assert_guarded_client(mocked)  # MockTransport has no pool to pin


async def test_http_fetcher_builds_a_guarded_client_offline() -> None:
    import httpx

    from intel.sources.fetcher import HttpFetcher
    from intel.sources.ssrf import assert_guarded_client

    fetcher = HttpFetcher(guard=guard())
    client = fetcher._ensure_client()
    try:
        assert isinstance(assert_guarded_client(client), object)
    finally:
        await fetcher.aclose()
    _ = httpx  # silence import grouping
