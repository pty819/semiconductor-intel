"""Lease token/timeout helpers (spec 07 §2 领取与 fencing).

Constants: a claimed job holds a 90-second lease; workers heartbeat every
30 seconds. Every step/terminal commit re-checks the lease through the
fencing predicate (``lease_token = :token AND lease_until > now()``), so a
worker that lost its lease cannot write business results.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

#: How long a claim holds (spec 07 §2: lease_until 默认90秒).
LEASE_SECONDS = 90

#: Heartbeat cadence (spec 07 §2: 每30秒 heartbeat).
HEARTBEAT_SECONDS = 30


def new_lease_token() -> str:
    """Random lease token — unpredictable per claim (spec 07 §2)."""
    return secrets.token_urlsafe(24)


@dataclass(frozen=True, slots=True)
class Lease:
    """One claim's fencing credentials."""

    token: str
    until: datetime
    heartbeat_at: datetime

    @property
    def seconds(self) -> int:
        return LEASE_SECONDS


def new_lease(now: datetime, *, seconds: int = LEASE_SECONDS) -> Lease:
    """A fresh lease taken at ``now`` (heartbeat due in 30s, expiry in 90s)."""
    return Lease(
        token=new_lease_token(),
        until=now + timedelta(seconds=seconds),
        heartbeat_at=now + timedelta(seconds=HEARTBEAT_SECONDS),
    )


def heartbeat_lease(lease: Lease, *, now: datetime) -> Lease:
    """Extend a lease by one full window from ``now``."""
    return Lease(
        token=lease.token,
        until=now + timedelta(seconds=LEASE_SECONDS),
        heartbeat_at=now + timedelta(seconds=HEARTBEAT_SECONDS),
    )


def lease_is_current(lease_until: datetime | None, *, now: datetime) -> bool:
    """The fencing predicate evaluated client-side (store-side is the truth)."""
    return lease_until is not None and lease_until > now
