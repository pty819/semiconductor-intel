"""Database package: declarative base, scope mixins, RLS helpers, models."""

from intel.db.base import (
    Base,
    IndustryScopeMixin,
    OwnerScopeMixin,
    TimestampMixin,
    UUIDPrimaryKey,
    VersionMixin,
)
from intel.db.rls import (
    INDUSTRY_GUC,
    OWNER_GUC,
    ScopeMissing,
    require_owner_guc,
    set_scope,
)

__all__ = [
    "INDUSTRY_GUC",
    "OWNER_GUC",
    "Base",
    "IndustryScopeMixin",
    "OwnerScopeMixin",
    "ScopeMissing",
    "TimestampMixin",
    "UUIDPrimaryKey",
    "VersionMixin",
    "require_owner_guc",
    "set_scope",
]
