"""All ORM models, imported so ``Base.metadata`` is complete for Alembic.

Scope classification (spec 03 §1):
- auth (no RLS): users, auth_sessions
- G (no RLS): source_templates, parser_versions
- O (RLS on app.owner_id): industries, industry_revisions, owner_feeds,
  source_runs, discovery_items, blobs, documents, document_origins,
  captures, fetch_observations, parsed_artifacts, document_diffs, chunks,
  processing_decisions
- I (RLS on app.owner_id + app.industry_id): topics, topic_revisions,
  industry_sources
"""

from intel.db.models.auth import AuthSession, User
from intel.db.models.pool import (
    Blob,
    Capture,
    Chunk,
    DiscoveryItem,
    Document,
    DocumentDiff,
    DocumentOrigin,
    FetchObservation,
    ParsedArtifact,
    ProcessingDecision,
)
from intel.db.models.sources import (
    IndustrySource,
    OwnerFeed,
    ParserVersion,
    SourceRun,
    SourceTemplate,
)
from intel.db.models.workspace import (
    Industry,
    IndustryRevision,
    Topic,
    TopicRevision,
)

AUTH_TABLES = ("users", "auth_sessions")
GLOBAL_TABLES = ("source_templates", "parser_versions")
RLS_OWNER_TABLES = (
    "industries",
    "industry_revisions",
    "owner_feeds",
    "source_runs",
    "discovery_items",
    "blobs",
    "documents",
    "document_origins",
    "captures",
    "fetch_observations",
    "parsed_artifacts",
    "document_diffs",
    "chunks",
    "processing_decisions",
)
RLS_INDUSTRY_TABLES = ("topics", "topic_revisions", "industry_sources")

ALL_TABLES = AUTH_TABLES + GLOBAL_TABLES + RLS_OWNER_TABLES + RLS_INDUSTRY_TABLES

__all__ = [
    "ALL_TABLES",
    "AUTH_TABLES",
    "GLOBAL_TABLES",
    "RLS_INDUSTRY_TABLES",
    "RLS_OWNER_TABLES",
    "AuthSession",
    "Blob",
    "Capture",
    "Chunk",
    "DiscoveryItem",
    "Document",
    "DocumentDiff",
    "DocumentOrigin",
    "FetchObservation",
    "Industry",
    "IndustryRevision",
    "IndustrySource",
    "OwnerFeed",
    "ParsedArtifact",
    "ParserVersion",
    "ProcessingDecision",
    "SourceRun",
    "SourceTemplate",
    "Topic",
    "TopicRevision",
    "User",
]
