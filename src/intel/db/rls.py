"""Row-level-security scope helpers (spec 03 §1 作用域, spec 10 §1 两层隔离).

Every scoped transaction opens with a parameterized, transaction-local
``set_config('app.owner_id', :owner, true)``. Because the GUC is LOCAL, it
vanishes at COMMIT/ROLLBACK — a pooled connection can never hand owner A's
scope to owner B. RLS policies on O/I tables compare ``owner_id`` (and for
I tables ``industry_id``) against these GUCs; when the GUC is absent the
policy expression evaluates to NULL, which PostgreSQL treats as "row not
visible / row rejected" — fail closed.

``require_owner_guc`` is the belt-and-braces guard for write paths: repository
code calls it before INSERT/UPDATE so a missing scope surfaces as an explicit
``ScopeMissing`` instead of a raw RLS violation deep in the driver.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

OWNER_GUC = "app.owner_id"
INDUSTRY_GUC = "app.industry_id"

_SET_CONFIG = text("SELECT set_config(:name, :value, true)")
_CURRENT_SETTING = text("SELECT current_setting(:name, true) AS value")


class ScopeMissing(RuntimeError):
    """A scoped write path ran without ``app.owner_id`` set (fail closed)."""


async def set_scope(
    conn: AsyncConnection,
    owner_id: UUID | str,
    industry_id: UUID | str | None = None,
) -> None:
    """Bind the transaction-local scope GUCs on ``conn``.

    Must be called inside the transaction that performs the scoped work
    (SQLAlchemy's autobegin counts). ``industry_id`` is optional: O-scope
    (raw pool) work runs with the owner GUC only; I-scope work sets both.
    The values are passed as bound parameters — never string-interpolated
    (spec 10 §1 禁止让 SQL 参数进入字符串拼接).
    """
    if owner_id is None:
        raise ScopeMissing("set_scope requires a non-None owner_id")
    await conn.execute(_SET_CONFIG, {"name": OWNER_GUC, "value": str(owner_id)})
    if industry_id is not None:
        await conn.execute(
            _SET_CONFIG, {"name": INDUSTRY_GUC, "value": str(industry_id)}
        )


async def require_owner_guc(conn: AsyncConnection) -> str:
    """Return ``app.owner_id`` or raise ``ScopeMissing`` when it is unset.

    Call on every write path before issuing INSERT/UPDATE/DELETE. An empty
    string counts as unset. The GUC read is transaction-local: after the
    owning transaction commits, the value is gone (verified in
    tests/integration/test_rls.py).
    """
    result = await conn.execute(_CURRENT_SETTING, {"name": OWNER_GUC})
    row: Any = result.one_or_none()
    value: str | None = row[0] if row is not None else None
    if not value:
        raise ScopeMissing(
            "app.owner_id is not set for this transaction; refusing to write "
            "(fail closed, spec 10 §1)"
        )
    return value
