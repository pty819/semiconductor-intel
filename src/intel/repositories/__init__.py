"""Scoped repositories over the RLS-isolated tables (spec 03 §1, 10 §1)."""

from intel.repositories.base import IndustryScope, ScopedRepository
from intel.repositories.idempotency import (
    IdempotencyRecord,
    IdempotencyRepository,
    SqlAlchemyIdempotencyRepository,
)
from intel.repositories.sources import (
    FeedRecord,
    IndustryContext,
    SourceRunRecord,
    SourcesRepository,
    SourceTemplateRecord,
    SqlAlchemySourcesRepository,
    SubscriptionRecord,
)
from intel.repositories.workspace import (
    IndustryRecord,
    IndustryRevisionRecord,
    SqlAlchemyWorkspaceRepository,
    TopicRecord,
    TopicRevisionRecord,
    WorkspaceRepository,
)

__all__ = [
    "FeedRecord",
    "IdempotencyRecord",
    "IdempotencyRepository",
    "IndustryContext",
    "IndustryRecord",
    "IndustryRevisionRecord",
    "IndustryScope",
    "ScopedRepository",
    "SourceRunRecord",
    "SourceTemplateRecord",
    "SourcesRepository",
    "SqlAlchemyIdempotencyRepository",
    "SqlAlchemySourcesRepository",
    "SqlAlchemyWorkspaceRepository",
    "SubscriptionRecord",
    "TopicRecord",
    "TopicRevisionRecord",
    "WorkspaceRepository",
]
