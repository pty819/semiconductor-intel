"""core tables: auth/workspace/source/pool + RLS (spec 03 §1-§3, §8; 10 §1)

Hand-written — no autogenerate. 21 tables:

- auth (no RLS): users, auth_sessions
- G (no RLS): source_templates, parser_versions
- O (RLS on app.owner_id): industries, industry_revisions, owner_feeds,
  source_runs, discovery_items, blobs, documents, document_origins,
  captures, fetch_observations, parsed_artifacts, document_diffs, chunks,
  processing_decisions
- I (RLS on app.owner_id + app.industry_id): topics, topic_revisions,
  industry_sources

Common-column rules (spec 03 §1): mutable entities get id uuid PK /
created_at / updated_at / row_version; version tables get version int /
recorded_at / created_by_job_id? / schema_version + UNIQUE(parent_id,
version). Exception: source_templates.id is a stable seed-derived string.

RLS (spec 03 §8, 10 §1): every O/I table gets ENABLE + FORCE ROW LEVEL
SECURITY with policies bound to transaction-local GUCs; the app role must
not be owner/superuser/BYPASSRLS. The intel_app role is created here as a
NOLOGIN stub — table grants land in a later migration (Task 17). WITH CHECK
mirrors USING so writes cannot escape scope; a missing GUC makes the policy
expression NULL, which PostgreSQL treats as deny (fail closed).

Deferred by design (later migrations, spec 03 §8): pg_textsearch BM25 index
on chunks.text (needs the jieba text_config binding), normalized_terms
trigram index, and pgvectorscale diskann — all are index_generation-managed
and re-verified against filtered recall. The vector column itself is
dimension-less until the config migration pins the model dimension.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from intel.db.base import VectorType

# revision identifiers, used by Alembic.
revision: str = "0001_core_tables"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of intel.db.models scope classification — the RLS block and the
# unit-test manifest (tests/unit/test_schema_manifest.py) both read these.
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

_UUID = postgresql.UUID()
_JSONB = postgresql.JSONB()
_TS = sa.DateTime(timezone=True)
_TEXT_ARR = postgresql.ARRAY(sa.Text())
_UUID_ARR = postgresql.ARRAY(postgresql.UUID())


def _common() -> list[sa.Column]:
    """created_at / updated_at / row_version of mutable entities."""
    return [
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("row_version", sa.BigInteger, server_default="1", nullable=False),
    ]


def _version_common() -> list[sa.Column]:
    """recorded_at / created_by_job_id / schema_version of version tables."""
    return [
        sa.Column("recorded_at", _TS, server_default=sa.text("now()"), nullable=False),
        # FK to jobs.id added by the jobs migration.
        sa.Column("created_by_job_id", _UUID, nullable=True),
        sa.Column("schema_version", sa.Integer, server_default="1", nullable=False),
    ]


def upgrade() -> None:
    # pgvector provides the `vector` type for chunks.embedding (spec 10 §4:
    # the deployment image ships pgvector/pgvectorscale preinstalled).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # -- auth (no RLS) ------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("login", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("timezone", sa.Text, nullable=False),
        sa.Column("disabled_at", _TS, nullable=True),
        sa.Column("password_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("login", name=op.f("uq_users_login")),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("csrf_hash", sa.Text, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("password_version", sa.Integer, nullable=False),
        sa.Column("revoked_at", _TS, nullable=True),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_auth_sessions_user_id_users")
        ),
    )

    # -- workspace: industries + revisions (O) ------------------------------
    op.create_table(
        "industries",
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("current_revision_id", _UUID, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_industries")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_industries_owner_id")),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name=op.f("ck_industries_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_industries_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "current_revision_id"],
            ["industry_revisions.owner_id", "industry_revisions.id"],
            name=op.f("fk_industries_owner_id_industry_revisions"),
            use_alter=True,
        ),
    )
    op.create_table(
        "industry_revisions",
        sa.Column("industry_id", _UUID, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("included_scope", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("excluded_scope", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("profile", _JSONB, nullable=True),
        sa.Column(
            "settings",
            _JSONB,
            server_default=sa.text("""'{"pool_scope": "all_public"}'"""),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        *_version_common(),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_industry_revisions")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_industry_revisions_owner_id")
        ),
        sa.UniqueConstraint(
            "industry_id", "version", name="uq_industry_revisions_industry_version"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_industry_revisions_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_industry_revisions_owner_id_industries"),
        ),
    )

    # -- workspace: topics + revisions (I) -----------------------------------
    op.create_table(
        "topics",
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("current_revision_id", _UUID, nullable=True),
        sa.Column("priority", sa.Integer, server_default="0", nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topics")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_topics_owner_id")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name=op.f("ck_topics_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_topics_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            [
                "topic_revisions.owner_id",
                "topic_revisions.industry_id",
                "topic_revisions.id",
            ],
            name=op.f("fk_topics_owner_id_topic_revisions"),
            use_alter=True,
        ),
    )
    op.create_table(
        "topic_revisions",
        sa.Column("topic_id", _UUID, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("positive_examples", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("negative_examples", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("aliases", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("entity_ids", _UUID_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("questions", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("analysis_template", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        *_version_common(),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topic_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_topic_revisions_owner_id")
        ),
        sa.UniqueConstraint(
            "topic_id", "version", name="uq_topic_revisions_topic_version"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
            name=op.f("fk_topic_revisions_owner_id_topics"),
        ),
    )

    # -- global source catalog (G, no RLS) ------------------------------------
    op.create_table(
        "source_templates",
        sa.Column("id", sa.String(255), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("homepage", sa.Text, nullable=False),
        sa.Column("canonical_seed", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("tags", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("access_notes", sa.Text, nullable=False),
        sa.Column("provenance", _JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("true"), nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_templates")),
        sa.UniqueConstraint(
            "canonical_seed", "kind", name="uq_source_templates_seed_kind"
        ),
    )
    op.create_table(
        "parser_versions",
        sa.Column("parser_key", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("config", _JSONB, nullable=False),
        sa.Column("config_hash", sa.Text, nullable=False),
        sa.Column("code_commit", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("fixture_manifest", _JSONB, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parser_versions")),
        sa.UniqueConstraint(
            "parser_key", "version", name="uq_parser_versions_key_version"
        ),
    )

    # -- owner feeds / industry subscriptions / runs --------------------------
    op.create_table(
        "owner_feeds",
        sa.Column("template_id", sa.String(255), nullable=True),
        sa.Column("seed_url", sa.Text, nullable=False),
        sa.Column("adapter_type", sa.Text, nullable=False),
        sa.Column("credential_ref", sa.Text, nullable=True),
        sa.Column("access_scope_key", sa.Text, nullable=False),
        sa.Column("parser_version_id", _UUID, nullable=False),
        sa.Column("interval_seconds", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("next_poll_at", _TS, nullable=True),
        sa.Column("discovery_cursor", sa.Text, nullable=True),
        sa.Column("cursor_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("config", _JSONB, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("user_enabled", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_owner_feeds")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_owner_feeds_owner_id")),
        sa.UniqueConstraint(
            "owner_id", "seed_url", "access_scope_key", name="uq_owner_feeds_seed_scope"
        ),
        sa.CheckConstraint(
            "adapter_type IN ('rss', 'atom', 'api', 'html_list', 'sitemap',"
            " 'page_monitor')",
            name=op.f("ck_owner_feeds_adapter_type"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused')", name=op.f("ck_owner_feeds_status")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_owner_feeds_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["source_templates.id"],
            name=op.f("fk_owner_feeds_template_id_source_templates"),
        ),
        sa.ForeignKeyConstraint(
            ["parser_version_id"],
            ["parser_versions.id"],
            name=op.f("fk_owner_feeds_parser_version_id_parser_versions"),
        ),
    )
    op.create_table(
        "industry_sources",
        sa.Column("feed_id", _UUID, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("backfill_from", _TS, nullable=True),
        sa.Column("subscribed_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_industry_sources")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_industry_sources_owner_id")
        ),
        sa.UniqueConstraint(
            "industry_id", "feed_id", name="uq_industry_sources_industry_feed"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused')",
            name=op.f("ck_industry_sources_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_industry_sources_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "feed_id"],
            ["owner_feeds.owner_id", "owner_feeds.id"],
            name=op.f("fk_industry_sources_owner_id_owner_feeds"),
        ),
    )
    op.create_table(
        "source_runs",
        sa.Column("feed_id", _UUID, nullable=False),
        # FK to jobs.id added by the jobs migration.
        sa.Column("job_id", _UUID, nullable=False),
        sa.Column("started_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", _TS, nullable=True),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("discovered_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("fetched_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("unresolved_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("coverage_end", _TS, nullable=True),
        sa.Column("cursor_before", sa.Text, nullable=True),
        sa.Column("cursor_after", sa.Text, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_runs")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_source_runs_owner_id")),
        sa.CheckConstraint(
            "outcome IN ('success', 'no_change', 'partial', 'failed')",
            name=op.f("ck_source_runs_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_source_runs_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "feed_id"],
            ["owner_feeds.owner_id", "owner_feeds.id"],
            name=op.f("fk_source_runs_owner_id_owner_feeds"),
        ),
    )

    # -- raw pool: discovery → blobs/documents → origins/captures -------------
    op.create_table(
        "discovery_items",
        sa.Column("feed_id", _UUID, nullable=True),
        sa.Column("run_id", _UUID, nullable=True),
        sa.Column("origin_kind", sa.Text, nullable=False),
        sa.Column("target_industry_id", _UUID, nullable=True),
        sa.Column("discovered_url", sa.Text, nullable=False),
        sa.Column("canonical_url", sa.Text, nullable=False),
        sa.Column("title_hint", sa.Text, nullable=True),
        sa.Column("published_hint", _TS, nullable=True),
        sa.Column("first_seen_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("next_retry_at", _TS, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_items")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_discovery_items_owner_id")
        ),
        # Feed-mode idempotency; non-feed rows (NULL feed_id) dedupe at the
        # service layer on origin_request_id + URL (spec 03 §3).
        sa.UniqueConstraint(
            "owner_id", "feed_id", "canonical_url",
            name="uq_discovery_items_feed_canonical_url",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_discovery_items_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "feed_id"],
            ["owner_feeds.owner_id", "owner_feeds.id"],
            name=op.f("fk_discovery_items_owner_id_owner_feeds"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "run_id"],
            ["source_runs.owner_id", "source_runs.id"],
            name=op.f("fk_discovery_items_owner_id_source_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "target_industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_discovery_items_owner_id_industries"),
        ),
    )
    op.create_table(
        "blobs",
        sa.Column("object_key", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("byte_size", sa.BigInteger, nullable=False),
        sa.Column("retention_class", sa.Text, nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blobs")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_blobs_owner_id")),
        # 不跨用户 dedup (spec 03 §3).
        sa.UniqueConstraint(
            "owner_id", "sha256", "media_type", name="uq_blobs_dedup"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_blobs_owner_id_users")
        ),
    )
    op.create_table(
        "documents",
        sa.Column("canonical_url", sa.Text, nullable=False),
        sa.Column("identity_namespace", sa.Text, nullable=False),
        sa.Column("identity_value", sa.Text, nullable=False),
        sa.Column("visibility_scope_key", sa.Text, nullable=False),
        sa.Column("origin_kind", sa.Text, nullable=False),
        sa.Column("target_industry_id", _UUID, nullable=True),
        sa.Column("first_seen_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("current_capture_id", _UUID, nullable=True),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_documents_owner_id")),
        # 私人调查同 URL 不与公共版本自动合并 (spec 03 §3).
        sa.UniqueConstraint(
            "owner_id",
            "visibility_scope_key",
            "identity_namespace",
            "identity_value",
            name="uq_documents_identity",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_documents_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "target_industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_documents_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "current_capture_id"],
            ["captures.owner_id", "captures.id"],
            name=op.f("fk_documents_owner_id_captures"),
            use_alter=True,
        ),
    )
    op.create_table(
        "document_origins",
        sa.Column("document_id", _UUID, nullable=False),
        sa.Column("discovery_item_id", _UUID, nullable=False),
        sa.Column("feed_id", _UUID, nullable=True),
        sa.Column("target_industry_id", _UUID, nullable=True),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_origins")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_document_origins_owner_id")
        ),
        sa.UniqueConstraint(
            "document_id", "discovery_item_id",
            name="uq_document_origins_document_item",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_document_origins_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "document_id"],
            ["documents.owner_id", "documents.id"],
            name=op.f("fk_document_origins_owner_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "discovery_item_id"],
            ["discovery_items.owner_id", "discovery_items.id"],
            name=op.f("fk_document_origins_owner_id_discovery_items"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "feed_id"],
            ["owner_feeds.owner_id", "owner_feeds.id"],
            name=op.f("fk_document_origins_owner_id_owner_feeds"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "target_industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_document_origins_owner_id_industries"),
        ),
    )
    op.create_table(
        "captures",
        sa.Column("document_id", _UUID, nullable=False),
        sa.Column("raw_blob_id", _UUID, nullable=False),
        sa.Column("response_status", sa.Integer, nullable=False),
        sa.Column("fetched_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("effective_url", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("etag", sa.Text, nullable=True),
        sa.Column("last_modified", _TS, nullable=True),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("retrieval_scope", sa.Text, nullable=False),
        sa.Column("access_policy", sa.Text, nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_captures")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_captures_owner_id")),
        sa.UniqueConstraint(
            "document_id", "content_hash", "access_policy", name="uq_captures_content"
        ),
        sa.CheckConstraint(
            "retrieval_scope IN ('metadata', 'abstract', 'partial', 'fulltext')",
            name=op.f("ck_captures_retrieval_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_captures_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "document_id"],
            ["documents.owner_id", "documents.id"],
            name=op.f("fk_captures_owner_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "raw_blob_id"],
            ["blobs.owner_id", "blobs.id"],
            name=op.f("fk_captures_owner_id_blobs"),
        ),
    )
    op.create_table(
        "fetch_observations",
        sa.Column("document_id", _UUID, nullable=True),
        sa.Column("discovery_item_id", _UUID, nullable=False),
        sa.Column("capture_id", _UUID, nullable=True),
        sa.Column("fetched_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("etag", sa.Text, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_observations")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_fetch_observations_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_fetch_observations_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "document_id"],
            ["documents.owner_id", "documents.id"],
            name=op.f("fk_fetch_observations_owner_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "discovery_item_id"],
            ["discovery_items.owner_id", "discovery_items.id"],
            name=op.f("fk_fetch_observations_owner_id_discovery_items"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "capture_id"],
            ["captures.owner_id", "captures.id"],
            name=op.f("fk_fetch_observations_owner_id_captures"),
        ),
    )

    # -- raw pool: parses, diffs, chunks, decisions ---------------------------
    op.create_table(
        "parsed_artifacts",
        sa.Column("capture_id", _UUID, nullable=False),
        sa.Column("parser_version_id", _UUID, nullable=False),
        sa.Column("normalized_blob_id", _UUID, nullable=False),
        sa.Column("text_hash", sa.Text, nullable=False),
        sa.Column("blocks", _JSONB, nullable=False),
        sa.Column("metadata", _JSONB, nullable=False),
        sa.Column("parse_status", sa.Text, nullable=False),
        sa.Column("coverage", _JSONB, nullable=False),
        sa.Column("quality_flags", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("parsed_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parsed_artifacts")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_parsed_artifacts_owner_id")
        ),
        # 新 parser 不覆盖旧 parse (spec 03 §3).
        sa.UniqueConstraint(
            "capture_id", "parser_version_id",
            name="uq_parsed_artifacts_capture_parser",
        ),
        sa.CheckConstraint(
            "parse_status IN ('ok', 'partial', 'failed')",
            name=op.f("ck_parsed_artifacts_parse_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_parsed_artifacts_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "capture_id"],
            ["captures.owner_id", "captures.id"],
            name=op.f("fk_parsed_artifacts_owner_id_captures"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "normalized_blob_id"],
            ["blobs.owner_id", "blobs.id"],
            name=op.f("fk_parsed_artifacts_owner_id_blobs"),
        ),
        sa.ForeignKeyConstraint(
            ["parser_version_id"],
            ["parser_versions.id"],
            name=op.f("fk_parsed_artifacts_parser_version_id_parser_versions"),
        ),
    )
    op.create_table(
        "document_diffs",
        sa.Column("from_parse_id", _UUID, nullable=False),
        sa.Column("to_parse_id", _UUID, nullable=False),
        sa.Column("diff_algorithm_version", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("changed_blocks", _JSONB, nullable=False),
        sa.Column("field_changes", _JSONB, nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_diffs")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_document_diffs_owner_id")),
        sa.UniqueConstraint(
            "from_parse_id", "to_parse_id", "diff_algorithm_version",
            name="uq_document_diffs_from_to_algo",
        ),
        sa.CheckConstraint(
            "kind IN ('content_change', 'parser_change', 'mixed')",
            name=op.f("ck_document_diffs_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_document_diffs_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "from_parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name="fk_document_diffs_from_parse",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "to_parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name="fk_document_diffs_to_parse",
        ),
    )
    op.create_table(
        "chunks",
        sa.Column("parsed_artifact_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("block_ids", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("normalized_terms", sa.Text, nullable=False),
        # Dimension-less vector: pinned by the config/index_generation
        # migration (spec 03 §8). 缺 embedding 不妨碍正文保存.
        sa.Column("embedding", VectorType(), nullable=True),
        sa.Column("embedding_model_version", sa.Text, nullable=True),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_chunks_owner_id")),
        sa.UniqueConstraint(
            "parsed_artifact_id", "ordinal", name="uq_chunks_parse_ordinal"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_chunks_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "parsed_artifact_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name=op.f("fk_chunks_owner_id_parsed_artifacts"),
        ),
    )
    op.create_table(
        "processing_decisions",
        sa.Column("parse_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.Column("industry_revision_id", _UUID, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("reasons", _TEXT_ARR, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("candidate_claims", _JSONB, nullable=True),
        # FKs land with the jobs / knowledge tables migrations.
        sa.Column("model_run_id", _UUID, nullable=True),
        sa.Column("override_id", _UUID, nullable=True),
        sa.Column("analysis_version", sa.Integer, server_default="1", nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_decisions")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_processing_decisions_owner_id")
        ),
        sa.UniqueConstraint(
            "parse_id", "industry_revision_id", "analysis_version",
            name="uq_processing_decisions_parse_revision",
        ),
        sa.CheckConstraint(
            "outcome IN ('direct', 'background', 'uncertain', 'unrelated')",
            name=op.f("ck_processing_decisions_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"],
            name=op.f("fk_processing_decisions_owner_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name=op.f("fk_processing_decisions_owner_id_parsed_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_processing_decisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_revision_id"],
            ["industry_revisions.owner_id", "industry_revisions.id"],
            name=op.f("fk_processing_decisions_owner_id_industry_revisions"),
        ),
    )

    _create_indexes()
    _apply_rls()


def _create_indexes() -> None:
    """Indexes per spec 03 §8: every O/I table indexed on owner_id, every I
    table on (owner_id, industry_id), key FKs indexed."""
    btree = [
        # (name, table, columns)
        ("ix_auth_sessions_user_id", "auth_sessions", ["user_id"]),
        ("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"]),
        ("ix_industries_owner_id", "industries", ["owner_id"]),
        ("ix_industry_revisions_owner_industry", "industry_revisions",
         ["owner_id", "industry_id"]),
        ("ix_topics_owner_industry", "topics", ["owner_id", "industry_id"]),
        ("ix_topic_revisions_owner_industry_topic", "topic_revisions",
         ["owner_id", "industry_id", "topic_id"]),
        ("ix_owner_feeds_owner_id", "owner_feeds", ["owner_id"]),
        ("ix_owner_feeds_parser_version_id", "owner_feeds", ["parser_version_id"]),
        ("ix_owner_feeds_template_id", "owner_feeds", ["template_id"]),
        ("ix_industry_sources_owner_industry", "industry_sources",
         ["owner_id", "industry_id"]),
        ("ix_source_runs_owner_feed", "source_runs", ["owner_id", "feed_id"]),
        ("ix_discovery_items_owner_feed", "discovery_items",
         ["owner_id", "feed_id"]),
        ("ix_discovery_items_owner_state", "discovery_items",
         ["owner_id", "state"]),
        ("ix_blobs_owner_id", "blobs", ["owner_id"]),
        ("ix_documents_owner_id", "documents", ["owner_id"]),
        ("ix_documents_owner_target_industry", "documents",
         ["owner_id", "target_industry_id"]),
        ("ix_document_origins_owner_document", "document_origins",
         ["owner_id", "document_id"]),
        ("ix_captures_owner_document", "captures", ["owner_id", "document_id"]),
        ("ix_fetch_observations_owner_discovery", "fetch_observations",
         ["owner_id", "discovery_item_id"]),
        ("ix_fetch_observations_owner_document", "fetch_observations",
         ["owner_id", "document_id"]),
        ("ix_parsed_artifacts_owner_capture", "parsed_artifacts",
         ["owner_id", "capture_id"]),
        ("ix_parsed_artifacts_parser_version_id", "parsed_artifacts",
         ["parser_version_id"]),
        ("ix_document_diffs_owner_from", "document_diffs",
         ["owner_id", "from_parse_id"]),
        ("ix_chunks_owner_parse", "chunks", ["owner_id", "parsed_artifact_id"]),
        ("ix_processing_decisions_owner_parse", "processing_decisions",
         ["owner_id", "parse_id"]),
    ]
    for name, table, columns in btree:
        colspec = ", ".join(columns)
        op.execute(f"CREATE INDEX {name} ON {table} ({colspec})")

    # Partial unique indexes: active-name uniqueness (spec 03 §2).
    op.execute(
        "CREATE UNIQUE INDEX uq_industries_active_name ON industries"
        " (owner_id, name) WHERE status <> 'archived' AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_topics_active_name ON topics"
        " (owner_id, industry_id, name) WHERE status <> 'archived'"
    )


def _apply_rls() -> None:
    """ENABLE + FORCE RLS on every O/I table with GUC-bound policies.

    The intel_app role is created as a NOLOGIN stub here; GRANTs arrive in a
    later migration (Task 17). The application role must never be superuser,
    table owner, or BYPASSRLS (spec 10 §1). A missing GUC makes the policy
    expression NULL → deny (fail closed).
    """
    # intel_app grant stub: GRANT USAGE/SELECT/INSERT/UPDATE on these tables
    # to intel_app arrives in a later migration (Task 17); the app role must
    # not own tables, be superuser, or hold BYPASSRLS (spec 10 §1).
    op.execute(
        "DO $$ BEGIN"
        " IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'intel_app') THEN"
        " CREATE ROLE intel_app NOLOGIN;"
        " END IF;"
        " END $$;"
    )

    owner_pred = "owner_id = current_setting('app.owner_id', true)::uuid"
    industry_pred = (
        "owner_id = current_setting('app.owner_id', true)::uuid"
        " AND industry_id = current_setting('app.industry_id', true)::uuid"
    )
    for table, predicate, policy in [
        *[(t, owner_pred, f"{t}_owner_scope") for t in RLS_OWNER_TABLES],
        *[(t, industry_pred, f"{t}_industry_scope") for t in RLS_INDUSTRY_TABLES],
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {policy} ON {table} TO intel_app"
            f" USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    # Reverse dependency order; dropping the tables drops their policies.
    # The intel_app role is intentionally left in place — dropping a role
    # that may hold grants elsewhere is a deployment decision.
    for table in reversed(
        [
            "processing_decisions",
            "chunks",
            "document_diffs",
            "parsed_artifacts",
            "fetch_observations",
            "captures",
            "document_origins",
            "documents",
            "blobs",
            "discovery_items",
            "source_runs",
            "industry_sources",
            "owner_feeds",
            "parser_versions",
            "source_templates",
            "topic_revisions",
            "topics",
            "industry_revisions",
            "industries",
            "auth_sessions",
            "users",
        ]
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
