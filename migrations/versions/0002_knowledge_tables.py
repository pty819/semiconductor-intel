"""knowledge/conversation/jobs/generation tables + RLS (spec 03 §4/§6/§7/§9, 15 §3, 16 §7)

Hand-written — no autogenerate. 54 tables on top of 0001's 21:

- knowledge (I): industry_documents, document_topics, entities,
  entity_aliases, claims, claim_revisions, evidence, source_families,
  events, event_revisions, event_topics, event_topic_revisions,
  event_relations, event_relation_revisions, watches, overrides,
  event_read_states + §9 physical association tables
  (topic_revision_entities, claim_revision_entities, event_revision_claims,
  event_revision_entities, event_topic_revision_claims,
  relation_revision_evidence, watch_topics, watch_entities,
  document_association_history, event_association_history,
  event_merge_operations, event_lifecycle_history)
- evolution/report/conversation (I): evolutions, evolution_revisions,
  reports, report_revisions, conversations (state_version etc., 16 §7),
  messages (parent_message_id/turn_index), conversation_state_revisions,
  conversation_summary_revisions, review_tasks, dependency_edges,
  derived_status + publication_citations split into report_citations /
  evolution_citations / message_citations (03 §9: 首版选分表避免多态FK)
- jobs (O/I hybrids, owner-only RLS): jobs, job_steps, job_events
  (PK(job_id, seq)), model_runs, coverage_batches; recall_hits (I);
  api_idempotency (O)
- generation (I, doc 15 §3): generation_runs, generation_run_models,
  output_generations (typed FK columns + num_nonnulls=1 CHECK instead of a
  polymorphic UUID)

Conventions carried over from 0001: version tables are INSERT-only with
UNIQUE(parent_id, version); I→I FKs carry (owner_id, industry_id), O→O and
I→O FKs carry owner_id; RLS ENABLE+FORCE with TO intel_app policies bound
to transaction-local GUCs (fail closed). Hybrids (jobs family, audit_log)
carry a nullable industry_id and use the owner-only predicate — RLS 防跨用
户, repository/service 防同行业内越界 (spec 10 §1).

Deferred FKs: use_alter constraints are silently dropped by CreateTable
rendering (they never reached the DB), so circular/forward references are
regular constraints created here via op.create_foreign_key AFTER both
tables exist. This also repairs the three 0001 use_alter FKs that never
landed (industries/topics current_revision_id, documents.current_capture_id)
and adds the 0001-promised FKs: industry_revisions/topic_revisions
created_by_job_id → jobs, source_runs.job_id → jobs,
processing_decisions.model_run_id → model_runs / .override_id → overrides.

Spec 03 §8 deferrals (Task 10 / index_generation): BM25/trigram/vector
indexes. Documented deviation: event_revisions.identity_key active-uniqueness
is service-enforced (a plain UNIQUE is impossible — every revision of one
event repeats its identity_key); a lookup index is provided.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_knowledge_tables"
down_revision: str | None = "0001_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of intel.db.models scope classification for THIS migration's tables.
RLS_OWNER_TABLES = (
    "api_idempotency",
    # O/I hybrids (nullable industry_id, kind-level scoping).
    "jobs",
    "job_steps",
    "job_events",
    "model_runs",
    "coverage_batches",
    "audit_log",
)
RLS_INDUSTRY_TABLES = (
    "industry_documents",
    "document_topics",
    "entities",
    "entity_aliases",
    "claims",
    "claim_revisions",
    "evidence",
    "source_families",
    "events",
    "event_revisions",
    "event_topics",
    "event_topic_revisions",
    "event_relations",
    "event_relation_revisions",
    "watches",
    "overrides",
    "event_read_states",
    "topic_revision_entities",
    "claim_revision_entities",
    "event_revision_claims",
    "event_revision_entities",
    "event_topic_revision_claims",
    "relation_revision_evidence",
    "watch_topics",
    "watch_entities",
    "document_association_history",
    "event_association_history",
    "event_merge_operations",
    "event_lifecycle_history",
    "evolutions",
    "evolution_revisions",
    "reports",
    "report_revisions",
    "conversations",
    "messages",
    "conversation_state_revisions",
    "conversation_summary_revisions",
    "review_tasks",
    "dependency_edges",
    "derived_status",
    "report_citations",
    "evolution_citations",
    "message_citations",
    "recall_hits",
    "generation_runs",
    "generation_run_models",
    "output_generations",
)

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
    """version / recorded_at / created_by_job_id / schema_version."""
    return [
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("recorded_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_job_id", _UUID, nullable=True),
        sa.Column("schema_version", sa.Integer, server_default="1", nullable=False),
    ]


def _i_scope() -> list[sa.Column]:
    return [
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
    ]


def _hybrid_scope() -> list[sa.Column]:
    """owner NOT NULL + nullable industry (kind-level scoping)."""
    return [
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=True),
        sa.Column("id", _UUID, nullable=False),
    ]


def upgrade() -> None:
    _create_job_tables()
    _create_knowledge_tables()
    _create_association_tables()
    _create_generation_tables()
    _create_coverage_tables()
    _create_publication_tables()
    _create_conversation_tables()
    _create_governance_tables()
    _create_deferred_fks()
    _create_indexes()
    _apply_rls()


def _create_job_tables() -> None:
    # -- jobs family (O/I hybrids) -------------------------------------------
    op.create_table(
        "jobs",
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("input", _JSONB, nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("available_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("attempt", sa.Integer, server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer, server_default="3", nullable=False),
        sa.Column("lease_token", sa.Text, nullable=True),
        sa.Column("lease_until", _TS, nullable=True),
        sa.Column("heartbeat_at", _TS, nullable=True),
        sa.Column("cancel_requested_at", _TS, nullable=True),
        sa.Column("progress", _JSONB, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error", _JSONB, nullable=True),
        sa.Column("output_ref", sa.Text, nullable=True),
        *_hybrid_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_jobs_owner_id")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name="uq_jobs_owner_industry_id"
        ),
        sa.UniqueConstraint(
            "owner_id", "kind", "idempotency_key", name="uq_jobs_idempotency"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_jobs_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_jobs_owner_id_industries"),
        ),
    )
    op.create_table(
        "job_steps",
        sa.Column("job_id", _UUID, nullable=False),
        sa.Column("step_key", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("input_hash", sa.Text, nullable=False),
        sa.Column("output_ref", sa.Text, nullable=True),
        sa.Column("attempt", sa.Integer, server_default="0", nullable=False),
        sa.Column("started_at", _TS, nullable=True),
        sa.Column("completed_at", _TS, nullable=True),
        *_hybrid_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_steps")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_job_steps_owner_id")),
        sa.UniqueConstraint(
            "job_id", "step_key", "input_hash", name="uq_job_steps_step_input"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_job_steps_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_job_steps_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_job_steps_owner_id_jobs"),
        ),
    )
    op.create_table(
        "job_events",
        sa.Column("job_id", _UUID, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("data", _JSONB, nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=True),
        sa.PrimaryKeyConstraint("job_id", "seq", name=op.f("pk_job_events")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_job_events_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_job_events_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_job_events_owner_id_jobs"),
        ),
    )
    op.create_table(
        "model_runs",
        sa.Column("job_id", _UUID, nullable=False),
        sa.Column("step_key", sa.Text, nullable=False),
        sa.Column("nooa_session_ref", sa.Text, nullable=True),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("model_route", sa.Text, nullable=False),
        sa.Column("provider_model", sa.Text, nullable=True),
        sa.Column("nooa_commit", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("settings_hash", sa.Text, nullable=False),
        sa.Column("input_manifest", _JSONB, nullable=False),
        sa.Column("usage", _JSONB, nullable=True),
        sa.Column("result_status", sa.Text, nullable=False),
        sa.Column("started_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", _TS, nullable=True),
        *_hybrid_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_runs")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_model_runs_owner_id")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name="uq_model_runs_owner_industry_id"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_model_runs_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_model_runs_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_model_runs_owner_id_jobs"),
        ),
    )


def _create_knowledge_tables() -> None:
    op.create_table(
        "entities",
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("canonical_name", sa.Text, nullable=False),
        sa.Column("identifiers", _JSONB, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("attributes", _JSONB, server_default=sa.text("'{}'"), nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_entities_owner_id")
        ),
        sa.CheckConstraint(
            "kind IN ('company', 'product', 'person', 'institution', 'method',"
            " 'dataset', 'component')",
            name=op.f("ck_entities_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_entities_owner_id_industries"),
        ),
    )
    op.create_table(
        "entity_aliases",
        sa.Column("entity_id", _UUID, nullable=False),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("normalized_alias", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("qualifier", sa.Text, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_aliases")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_entity_aliases_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_entity_aliases_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
            name=op.f("fk_entity_aliases_owner_id_entities"),
        ),
    )
    op.create_table(
        "source_families",
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("origin_url", sa.Text, nullable=True),
        sa.Column("origin_entity_id", _UUID, nullable=True),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_families")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_source_families_owner_id")
        ),
        sa.CheckConstraint(
            "basis IN ('explicit_reference', 'exact_reprint', 'inferred')",
            name=op.f("ck_source_families_basis"),
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'uncertain')",
            name=op.f("ck_source_families_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_source_families_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "origin_entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
            name=op.f("fk_source_families_owner_id_entities"),
        ),
    )
    op.create_table(
        "industry_documents",
        sa.Column("document_id", _UUID, nullable=False),
        sa.Column("current_parse_id", _UUID, nullable=False),
        sa.Column("relevance", sa.Text, nullable=False),
        sa.Column("association_reason", sa.Text, nullable=False),
        sa.Column("associated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("active", sa.Boolean, server_default=sa.text("true"), nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_industry_documents")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_industry_documents_owner_id"),
        ),
        sa.UniqueConstraint(
            "industry_id", "document_id", name="uq_industry_documents_industry_document"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_industry_documents_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "document_id"],
            ["documents.owner_id", "documents.id"],
            name=op.f("fk_industry_documents_owner_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "current_parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name=op.f("fk_industry_documents_owner_id_parsed_artifacts"),
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "document_topics",
        sa.Column("industry_document_id", _UUID, nullable=False),
        sa.Column("topic_id", _UUID, nullable=False),
        sa.Column("topic_revision_id", _UUID, nullable=False),
        sa.Column("relevance", sa.Text, nullable=False),
        sa.Column(
            "supporting_block_ids", _TEXT_ARR,
            server_default=sa.text("'{}'"), nullable=False,
        ),
        sa.Column("decision_origin", sa.Text, nullable=False),
        sa.Column(
            "locked_by_user", sa.Boolean, server_default=sa.text("false"),
            nullable=False,
        ),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_topics")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_document_topics_owner_id")
        ),
        sa.UniqueConstraint(
            "industry_document_id", "topic_id", name="uq_document_topics_document_topic"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_document_topics_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "industry_document_id"],
            [
                "industry_documents.owner_id",
                "industry_documents.industry_id",
                "industry_documents.id",
            ],
            name=op.f("fk_document_topics_owner_id_industry_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
            name=op.f("fk_document_topics_owner_id_topics"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_revision_id"],
            ["topic_revisions.owner_id", "topic_revisions.industry_id",
             "topic_revisions.id"],
            name=op.f("fk_document_topics_owner_id_topic_revisions"),
        ),
    )
    op.create_table(
        "claims",
        sa.Column("current_revision_id", _UUID, nullable=True),
        sa.Column("state", sa.Text, nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
        sa.UniqueConstraint("owner_id", "industry_id", "id",
                            name=op.f("uq_claims_owner_id")),
        sa.CheckConstraint(
            "state IN ('active', 'disputed', 'corrected', 'retracted')",
            name=op.f("ck_claims_state"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_claims_owner_id_industries"),
        ),
    )
    op.create_table(
        "claim_revisions",
        sa.Column("claim_id", _UUID, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("asserted_by_entity_id", _UUID, nullable=True),
        sa.Column("predicate", sa.Text, nullable=False),
        sa.Column("object", _JSONB, nullable=False),
        sa.Column("conditions", _JSONB, nullable=False),
        sa.Column("valid_time", _JSONB, nullable=True),
        sa.Column("assessment", _JSONB, nullable=False),
        sa.Column("input_manifest", _JSONB, nullable=False),
        *_version_common(),
        *_i_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_claim_revisions_owner_id"),
        ),
        sa.UniqueConstraint(
            "claim_id", "version", name="uq_claim_revisions_claim_version"
        ),
        sa.CheckConstraint(
            "kind IN ('source_statement', 'inference')",
            name=op.f("ck_claim_revisions_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_claim_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_id"],
            ["claims.owner_id", "claims.industry_id", "claims.id"],
            name=op.f("fk_claim_revisions_owner_id_claims"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "asserted_by_entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
            name=op.f("fk_claim_revisions_owner_id_entities"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_claim_revisions_owner_id_jobs"),
        ),
    )
    op.create_table(
        "evidence",
        sa.Column("claim_revision_id", _UUID, nullable=False),
        sa.Column("parsed_artifact_id", _UUID, nullable=False),
        sa.Column("block_id", sa.Text, nullable=False),
        sa.Column("start_char", sa.Integer, nullable=False),
        sa.Column("end_char", sa.Integer, nullable=False),
        sa.Column("exact_quote", sa.Text, nullable=False),
        sa.Column("quote_sha256", sa.String(64), nullable=False),
        sa.Column("relation", sa.Text, nullable=False),
        sa.Column(
            "locator_verified_at", _TS, server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("semantic_support_status", sa.Text, nullable=False),
        sa.Column("source_family_id", _UUID, nullable=True),
        sa.Column("extraction_run_id", _UUID, nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_evidence_owner_id")
        ),
        sa.CheckConstraint(
            "relation IN ('supports', 'refutes', 'context')",
            name=op.f("ck_evidence_relation"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_evidence_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_revision_id"],
            ["claim_revisions.owner_id", "claim_revisions.industry_id",
             "claim_revisions.id"],
            name=op.f("fk_evidence_owner_id_claim_revisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "parsed_artifact_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name=op.f("fk_evidence_owner_id_parsed_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "source_family_id"],
            ["source_families.owner_id", "source_families.industry_id",
             "source_families.id"],
            name=op.f("fk_evidence_owner_id_source_families"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "extraction_run_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_evidence_owner_id_jobs"),
        ),
    )
    op.create_table(
        "events",
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("current_revision_id", _UUID, nullable=True),
        sa.Column("lifecycle", sa.Text, nullable=False),
        sa.Column("merged_into_id", _UUID, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_events_owner_id")
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'merged', 'retracted')",
            name=op.f("ck_events_lifecycle"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_events_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "merged_into_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name=op.f("fk_events_owner_id_events"),
        ),
    )
    op.create_table(
        "event_revisions",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("occurred_time", _JSONB, nullable=False),
        sa.Column("published_time", _JSONB, nullable=False),
        sa.Column("effective_time", _JSONB, nullable=False),
        sa.Column("occurred_start", _TS, nullable=True),
        sa.Column("occurred_end", _TS, nullable=True),
        sa.Column("first_discovered_at", _TS, nullable=False),
        sa.Column("identity_key", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("input_manifest", _JSONB, nullable=False),
        *_version_common(),
        *_i_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_event_revisions_owner_id")
        ),
        sa.UniqueConstraint(
            "event_id", "version", name="uq_event_revisions_event_version"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name=op.f("fk_event_revisions_owner_id_events"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_event_revisions_owner_id_jobs"),
        ),
    )
    op.create_table(
        "event_topics",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("topic_id", _UUID, nullable=False),
        sa.Column("current_revision_id", _UUID, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_topics")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_event_topics_owner_id")
        ),
        sa.UniqueConstraint(
            "event_id", "topic_id", name="uq_event_topics_event_topic"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_topics_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name=op.f("fk_event_topics_owner_id_events"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
            name=op.f("fk_event_topics_owner_id_topics"),
        ),
    )
    op.create_table(
        "event_topic_revisions",
        sa.Column("event_topic_id", _UUID, nullable=False),
        sa.Column("topic_revision_id", _UUID, nullable=False),
        sa.Column("relevance", sa.Text, nullable=False),
        sa.Column("importance", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("interpretation", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column(
            "locked_by_user", sa.Boolean, server_default=sa.text("false"),
            nullable=False,
        ),
        *_version_common(),
        *_i_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_topic_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_event_topic_revisions_owner_id"),
        ),
        sa.UniqueConstraint(
            "event_topic_id", "version", name="uq_event_topic_revisions_topic_version"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_topic_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_topic_id"],
            ["event_topics.owner_id", "event_topics.industry_id", "event_topics.id"],
            name=op.f("fk_event_topic_revisions_owner_id_event_topics"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_revision_id"],
            ["topic_revisions.owner_id", "topic_revisions.industry_id",
             "topic_revisions.id"],
            name=op.f("fk_event_topic_revisions_owner_id_topic_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_event_topic_revisions_owner_id_jobs"),
        ),
    )
    op.create_table(
        "event_relations",
        sa.Column("from_event_id", _UUID, nullable=False),
        sa.Column("to_event_id", _UUID, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("current_revision_id", _UUID, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_relations")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_event_relations_owner_id")
        ),
        sa.CheckConstraint(
            "from_event_id <> to_event_id", name=op.f("ck_event_relations_no_self_loop")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_relations_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "from_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_relations_from_event",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "to_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_relations_to_event",
        ),
    )
    op.create_table(
        "event_relation_revisions",
        sa.Column("relation_id", _UUID, nullable=False),
        sa.Column("from_event_revision_id", _UUID, nullable=False),
        sa.Column("to_event_revision_id", _UUID, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("basis", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        *_version_common(),
        *_i_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_relation_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_event_relation_revisions_owner_id"),
        ),
        sa.UniqueConstraint(
            "relation_id", "version", name="uq_event_relation_revisions_rel_version"
        ),
        sa.CheckConstraint(
            "type IN ('updates', 'validates', 'refutes', 'applies', 'extends',"
            " 'parallel', 'replaces')",
            name=op.f("ck_event_relation_revisions_type"),
        ),
        sa.CheckConstraint(
            "basis IN ('explicit', 'inferred')",
            name=op.f("ck_event_relation_revisions_basis"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_relation_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "relation_id"],
            ["event_relations.owner_id", "event_relations.industry_id",
             "event_relations.id"],
            name=op.f("fk_event_relation_revisions_owner_id_event_relations"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "from_event_revision_id"],
            ["event_revisions.owner_id", "event_revisions.industry_id",
             "event_revisions.id"],
            name="fk_event_relation_revisions_from",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "to_event_revision_id"],
            ["event_revisions.owner_id", "event_revisions.industry_id",
             "event_revisions.id"],
            name="fk_event_relation_revisions_to",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_event_relation_revisions_owner_id_jobs"),
        ),
    )
    op.create_table(
        "watches",
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("resolution_report_id", _UUID, nullable=True),
        sa.Column("last_checked_at", _TS, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watches")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_watches_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_watches_owner_id_industries"),
        ),
    )
    op.create_table(
        "overrides",
        sa.Column("object_type", sa.Text, nullable=False),
        sa.Column("object_id", _UUID, nullable=False),
        sa.Column("field_path", sa.Text, nullable=False),
        sa.Column("value", _JSONB, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("revoked_at", _TS, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_overrides")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_overrides_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_overrides_owner_id_industries"),
        ),
    )
    op.create_table(
        "event_read_states",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("is_read", sa.Boolean, server_default=sa.text("false"),
                  nullable=False),
        sa.Column("changed_at", _TS, server_default=sa.text("now()"), nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_read_states")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_event_read_states_owner_id"),
        ),
        sa.UniqueConstraint(
            "industry_id", "event_id", name="uq_event_read_states_industry_event"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_read_states_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name=op.f("fk_event_read_states_owner_id_events"),
        ),
    )


def _create_association_tables() -> None:
    """Spec 03 §9 physical association + history tables (pure links: PK, no
    common mutability columns)."""
    _link_table(
        "topic_revision_entities",
        [("topic_revision_id", "topic_revisions"), ("entity_id", "entities")],
        extra_columns=[sa.Column("role", sa.Text, nullable=True)],
    )
    op.create_table(
        "claim_revision_entities",
        sa.Column("claim_revision_id", _UUID, nullable=False),
        sa.Column("entity_id", _UUID, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint(
            "claim_revision_id", "entity_id", name=op.f("pk_claim_revision_entities")
        ),
        sa.CheckConstraint(
            "role IN ('subject', 'asserted_by', 'object')",
            name=op.f("ck_claim_revision_entities_role"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_claim_revision_entities_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_revision_id"],
            ["claim_revisions.owner_id", "claim_revisions.industry_id",
             "claim_revisions.id"],
            name=op.f("fk_claim_revision_entities_owner_id_claim_revisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
            name=op.f("fk_claim_revision_entities_owner_id_entities"),
        ),
    )
    op.create_table(
        "event_revision_claims",
        sa.Column("event_revision_id", _UUID, nullable=False),
        sa.Column("claim_revision_id", _UUID, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint(
            "event_revision_id", "claim_revision_id",
            name=op.f("pk_event_revision_claims"),
        ),
        sa.CheckConstraint(
            "role IN ('primary', 'supporting')",
            name=op.f("ck_event_revision_claims_role"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_revision_claims_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_revision_id"],
            ["event_revisions.owner_id", "event_revisions.industry_id",
             "event_revisions.id"],
            name=op.f("fk_event_revision_claims_owner_id_event_revisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_revision_id"],
            ["claim_revisions.owner_id", "claim_revisions.industry_id",
             "claim_revisions.id"],
            name=op.f("fk_event_revision_claims_owner_id_claim_revisions"),
            ondelete="RESTRICT",
        ),
    )
    _link_table(
        "event_revision_entities",
        [("event_revision_id", "event_revisions"), ("entity_id", "entities")],
        extra_columns=[sa.Column("role", sa.Text, nullable=False)],
        restrict_first=True,
    )
    _link_table(
        "event_topic_revision_claims",
        [("event_topic_revision_id", "event_topic_revisions"),
         ("claim_revision_id", "claim_revisions")],
        extra_columns=[sa.Column("ordinal", sa.Integer, nullable=False)],
        restrict_second=True,
    )
    _link_table(
        "relation_revision_evidence",
        [("relation_revision_id", "event_relation_revisions"),
         ("evidence_id", "evidence")],
        extra_columns=[sa.Column("ordinal", sa.Integer, nullable=False)],
        restrict_second=True,
    )
    _link_table("watch_topics", [("watch_id", "watches"), ("topic_id", "topics")])
    _link_table("watch_entities", [("watch_id", "watches"), ("entity_id", "entities")])

    # -- history tables (append-only) ----------------------------------------
    op.create_table(
        "document_association_history",
        sa.Column("industry_document_id", _UUID, nullable=False),
        sa.Column("topic_id", _UUID, nullable=False),
        sa.Column("topic_revision_id", _UUID, nullable=False),
        sa.Column("relevance", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column(
            "locked_by_user", sa.Boolean, server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("recorded_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("supersedes_id", _UUID, nullable=True),
        *_i_scope()[:2],
        sa.Column("id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_association_history")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_document_association_history_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_document_association_history_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "industry_document_id"],
            ["industry_documents.owner_id", "industry_documents.industry_id",
             "industry_documents.id"],
            name=op.f("fk_document_association_history_owner_id_industry_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
            name=op.f("fk_document_association_history_owner_id_topics"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_revision_id"],
            ["topic_revisions.owner_id", "topic_revisions.industry_id",
             "topic_revisions.id"],
            name=op.f("fk_document_association_history_owner_id_topic_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "supersedes_id"],
            ["document_association_history.owner_id",
             "document_association_history.industry_id",
             "document_association_history.id"],
            name="fk_document_association_history_supersedes",
        ),
    )
    op.create_table(
        "event_association_history",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("topic_id", _UUID, nullable=False),
        sa.Column("topic_revision_id", _UUID, nullable=False),
        sa.Column("relevance", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column(
            "locked_by_user", sa.Boolean, server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("recorded_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("supersedes_id", _UUID, nullable=True),
        *_i_scope()[:2],
        sa.Column("id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_association_history")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_event_association_history_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_association_history_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name=op.f("fk_event_association_history_owner_id_events"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
            name=op.f("fk_event_association_history_owner_id_topics"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_revision_id"],
            ["topic_revisions.owner_id", "topic_revisions.industry_id",
             "topic_revisions.id"],
            name=op.f("fk_event_association_history_owner_id_topic_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "supersedes_id"],
            ["event_association_history.owner_id",
             "event_association_history.industry_id",
             "event_association_history.id"],
            name="fk_event_association_history_supersedes",
        ),
    )
    op.create_table(
        "event_merge_operations",
        sa.Column("source_event_id", _UUID, nullable=False),
        sa.Column("target_event_id", _UUID, nullable=False),
        sa.Column("source_revision_before", _UUID, nullable=False),
        sa.Column("target_revision_before", _UUID, nullable=False),
        sa.Column("membership_snapshot", _JSONB, nullable=False),
        sa.Column("operation_status", sa.Text, nullable=False),
        sa.Column("applied_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("undone_at", _TS, nullable=True),
        *_i_scope()[:2],
        sa.Column("id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_merge_operations")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_event_merge_operations_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_merge_operations_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "source_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_merge_operations_source_event",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "target_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_merge_operations_target_event",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "source_revision_before"],
            ["event_revisions.owner_id", "event_revisions.industry_id",
             "event_revisions.id"],
            name="fk_event_merge_operations_source_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "target_revision_before"],
            ["event_revisions.owner_id", "event_revisions.industry_id",
             "event_revisions.id"],
            name="fk_event_merge_operations_target_revision",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "event_lifecycle_history",
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("lifecycle", sa.Text, nullable=False),
        sa.Column("recorded_at", _TS, server_default=sa.text("now()"), nullable=False),
        *_i_scope()[:2],
        sa.Column("id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_lifecycle_history")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_event_lifecycle_history_owner_id")
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'merged', 'retracted')",
            name=op.f("ck_event_lifecycle_history_lifecycle"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_event_lifecycle_history_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name=op.f("fk_event_lifecycle_history_owner_id_events"),
        ),
    )


def _link_table(
    name: str,
    links: list[tuple[str, str]],
    extra_columns: list[sa.Column] | None = None,
    restrict_first: bool = False,
    restrict_second: bool = False,
) -> None:
    """Uniform spec-03-§9 pure link table: composite PK over the two link
    columns, I scope, composite FKs to industries and both parents."""
    cols = [sa.Column(col, _UUID, nullable=False) for col, _ in links]
    cols += (extra_columns or [])
    cols += [
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
    ]
    pk_cols = [col for col, _ in links]
    constraints: list[sa.Constraint] = [
        sa.PrimaryKeyConstraint(*pk_cols, name=op.f(f"pk_{name}")),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f(f"fk_{name}_owner_id_industries"),
        ),
    ]
    for i, (col, parent) in enumerate(links):
        restrict = (restrict_first and i == 0) or (restrict_second and i == 1)
        constraints.append(
            sa.ForeignKeyConstraint(
                ["owner_id", "industry_id", col],
                [f"{parent}.owner_id", f"{parent}.industry_id", f"{parent}.id"],
                name=op.f(f"fk_{name}_owner_id_{parent}"),
                ondelete="RESTRICT" if restrict else None,
            )
        )
    op.create_table(name, *cols, *constraints)


def _create_generation_tables() -> None:
    op.create_table(
        "generation_runs",
        sa.Column("job_id", _UUID, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("step_key", sa.Text, nullable=False),
        sa.Column("trace_session_id", sa.Text, nullable=False),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("root_span_id", sa.Text, nullable=True),
        sa.Column("step_span_id", sa.Text, nullable=True),
        sa.Column("artifact_ref", sa.Text, nullable=True),
        sa.Column("trace_state", sa.Text, nullable=False),
        sa.Column("viewer_import_state", sa.Text, nullable=False),
        sa.Column("model_route_display", sa.Text, nullable=True),
        sa.Column("nooa_commit", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("config_hash", sa.Text, nullable=False),
        sa.Column("input_manifest", _JSONB, nullable=False),
        sa.Column("started_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", _TS, nullable=True),
        sa.Column("retention_until", _TS, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_runs")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_generation_runs_owner_id")
        ),
        sa.CheckConstraint(
            "trace_state IN ('recording', 'pending', 'available', 'missing',"
            " 'expired', 'redacted')",
            name=op.f("ck_generation_runs_trace_state"),
        ),
        sa.CheckConstraint(
            "viewer_import_state IN ('not_requested', 'pending', 'ready', 'failed')",
            name=op.f("ck_generation_runs_viewer_import_state"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_generation_runs_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_generation_runs_owner_id_jobs"),
        ),
    )
    _link_table(
        "generation_run_models",
        [("generation_run_id", "generation_runs"), ("model_run_id", "model_runs")],
        extra_columns=[sa.Column("attempt", sa.Integer, nullable=False)],
    )
    op.create_table(
        "output_generations",
        sa.Column("report_revision_id", _UUID, nullable=True),
        sa.Column("evolution_revision_id", _UUID, nullable=True),
        sa.Column("message_id", _UUID, nullable=True),
        sa.Column("event_revision_id", _UUID, nullable=True),
        sa.Column("event_topic_revision_id", _UUID, nullable=True),
        sa.Column("generation_run_id", _UUID, nullable=False),
        sa.Column("output_path", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_output_generations")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_output_generations_owner_id"),
        ),
        sa.CheckConstraint(
            "role IN ('producer', 'verifier', 'upstream')",
            name=op.f("ck_output_generations_role"),
        ),
        sa.CheckConstraint(
            "num_nonnulls(report_revision_id, evolution_revision_id, message_id,"
            " event_revision_id, event_topic_revision_id) = 1",
            name=op.f("ck_output_generations_single_target"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_output_generations_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "report_revision_id"],
            ["report_revisions.owner_id", "report_revisions.industry_id",
             "report_revisions.id"],
            name=op.f("fk_output_generations_owner_id_report_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "evolution_revision_id"],
            ["evolution_revisions.owner_id", "evolution_revisions.industry_id",
             "evolution_revisions.id"],
            name=op.f("fk_output_generations_owner_id_evolution_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
            name=op.f("fk_output_generations_owner_id_messages"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_revision_id"],
            ["event_revisions.owner_id", "event_revisions.industry_id",
             "event_revisions.id"],
            name=op.f("fk_output_generations_owner_id_event_revisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_topic_revision_id"],
            ["event_topic_revisions.owner_id", "event_topic_revisions.industry_id",
             "event_topic_revisions.id"],
            name=op.f("fk_output_generations_owner_id_event_topic_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "generation_run_id"],
            ["generation_runs.owner_id", "generation_runs.industry_id",
             "generation_runs.id"],
            name=op.f("fk_output_generations_owner_id_generation_runs"),
        ),
    )


def _create_coverage_tables() -> None:
    op.create_table(
        "coverage_batches",
        sa.Column("feed_id", _UUID, nullable=True),
        sa.Column("topic_id", _UUID, nullable=True),
        sa.Column("phase", sa.Text, nullable=False),
        sa.Column("window_start", _TS, nullable=False),
        sa.Column("window_end", _TS, nullable=False),
        sa.Column("expected_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("done_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("unknown_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("watermark", _TS, nullable=True),
        sa.Column("config_revision", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        *_hybrid_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coverage_batches")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_coverage_batches_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_coverage_batches_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_coverage_batches_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "feed_id"],
            ["owner_feeds.owner_id", "owner_feeds.id"],
            name=op.f("fk_coverage_batches_owner_id_owner_feeds"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
            name=op.f("fk_coverage_batches_owner_id_topics"),
        ),
    )
    op.create_table(
        "recall_hits",
        sa.Column("recall_job_id", _UUID, nullable=False),
        sa.Column("parse_id", _UUID, nullable=False),
        sa.Column("chunk_id", _UUID, nullable=True),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("rank", sa.Integer, nullable=True),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("decision", sa.Text, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recall_hits")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_recall_hits_owner_id")
        ),
        # NULL chunk_id rows dedupe at the service layer (doc-level recalls).
        sa.UniqueConstraint(
            "recall_job_id", "parse_id", "chunk_id", "channel",
            name="uq_recall_hits_job_parse_chunk_channel",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_recall_hits_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "recall_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_recall_hits_owner_id_jobs"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name=op.f("fk_recall_hits_owner_id_parsed_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "chunk_id"],
            ["chunks.owner_id", "chunks.id"],
            name=op.f("fk_recall_hits_owner_id_chunks"),
        ),
    )
    op.create_table(
        "api_idempotency",
        sa.Column("route", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("response_status", sa.Integer, nullable=False),
        sa.Column("response_body", _JSONB, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_idempotency")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_api_idempotency_owner_id")),
        sa.UniqueConstraint(
            "owner_id", "route", "key", name="uq_api_idempotency_route_key"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_api_idempotency_owner_id_users")
        ),
    )


def _create_publication_tables() -> None:
    """Evolution/report publication chains (spec 03 §6)."""
    op.create_table(
        "evolutions",
        sa.Column("topic_id", _UUID, nullable=False),
        sa.Column("current_revision_id", _UUID, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evolutions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_evolutions_owner_id")
        ),
        sa.UniqueConstraint("topic_id", name="uq_evolutions_topic"),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_evolutions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
            name=op.f("fk_evolutions_owner_id_topics"),
        ),
    )
    op.create_table(
        "evolution_revisions",
        sa.Column("evolution_id", _UUID, nullable=False),
        sa.Column("topic_revision_id", _UUID, nullable=False),
        sa.Column("as_of", _TS, nullable=False),
        sa.Column("period_start", _TS, nullable=True),
        sa.Column("period_end", _TS, nullable=True),
        sa.Column("stages", _JSONB, nullable=False),
        sa.Column("nodes", _JSONB, nullable=False),
        sa.Column("edges", _JSONB, nullable=False),
        sa.Column(
            "open_questions", _TEXT_ARR, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("coverage", _JSONB, nullable=False),
        sa.Column("input_manifest", _JSONB, nullable=False),
        sa.Column("supersedes_id", _UUID, nullable=True),
        *_version_common(),
        *_i_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evolution_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_evolution_revisions_owner_id"),
        ),
        sa.UniqueConstraint(
            "evolution_id", "version", name="uq_evolution_revisions_evolution_version"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_evolution_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "evolution_id"],
            ["evolutions.owner_id", "evolutions.industry_id", "evolutions.id"],
            name=op.f("fk_evolution_revisions_owner_id_evolutions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_revision_id"],
            ["topic_revisions.owner_id", "topic_revisions.industry_id",
             "topic_revisions.id"],
            name=op.f("fk_evolution_revisions_owner_id_topic_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_evolution_revisions_owner_id_jobs"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "supersedes_id"],
            ["evolution_revisions.owner_id", "evolution_revisions.industry_id",
             "evolution_revisions.id"],
            name=op.f("fk_evolution_revisions_owner_id_evolution_revisions"),
        ),
    )
    op.create_table(
        "reports",
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("current_revision_id", _UUID, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_reports_owner_id")
        ),
        sa.CheckConstraint(
            "type IN ('daily', 'topic', 'investigation')",
            name=op.f("ck_reports_type"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_reports_owner_id_industries"),
        ),
    )
    op.create_table(
        "report_revisions",
        sa.Column("report_id", _UUID, nullable=False),
        sa.Column("content", _JSONB, nullable=False),
        sa.Column("citations", _JSONB, nullable=False),
        sa.Column("as_of", _TS, nullable=False),
        sa.Column("coverage", _JSONB, nullable=False),
        sa.Column("input_manifest", _JSONB, nullable=False),
        *_version_common(),
        *_i_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_report_revisions_owner_id"),
        ),
        sa.UniqueConstraint(
            "report_id", "version", name="uq_report_revisions_report_version"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_report_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "report_id"],
            ["reports.owner_id", "reports.industry_id", "reports.id"],
            name=op.f("fk_report_revisions_owner_id_reports"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_report_revisions_owner_id_jobs"),
        ),
    )


def _create_conversation_tables() -> None:
    op.create_table(
        "conversations",
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("archived_at", _TS, nullable=True),
        sa.Column("state_version", sa.Integer, server_default="0", nullable=False),
        sa.Column("last_committed_message_id", _UUID, nullable=True),
        sa.Column("current_state_revision_id", _UUID, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_conversations_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_conversations_owner_id_industries"),
        ),
    )
    op.create_table(
        "messages",
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("job_id", _UUID, nullable=True),
        sa.Column(
            "citation_manifest", _JSONB, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("as_of", _TS, nullable=True),
        sa.Column("parent_message_id", _UUID, nullable=True),
        sa.Column("turn_index", sa.Integer, nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_messages_owner_id")
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')", name=op.f("ck_messages_role")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_messages_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "conversation_id"],
            ["conversations.owner_id", "conversations.industry_id",
             "conversations.id"],
            name=op.f("fk_messages_owner_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_messages_owner_id_jobs"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "parent_message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
            name=op.f("fk_messages_owner_id_messages"),
        ),
    )
    op.create_table(
        "conversation_summary_revisions",
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("through_message_id", _UUID, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column(
            "preserved_constraint_ids", _TEXT_ARR, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "reference_ids", _UUID_ARR, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "input_message_ids", _UUID_ARR, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("generation_run_id", _UUID, nullable=False),
        sa.Column("validation_status", sa.Text, nullable=False),
        sa.Column("recorded_at", _TS, server_default=sa.text("now()"), nullable=False),
        *_i_scope()[:2],
        sa.Column("id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_conversation_summary_revisions")
        ),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_conversation_summary_revisions_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_conversation_summary_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "conversation_id"],
            ["conversations.owner_id", "conversations.industry_id",
             "conversations.id"],
            name=op.f("fk_conversation_summary_revisions_owner_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "through_message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
            name=op.f("fk_conversation_summary_revisions_owner_id_messages"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "generation_run_id"],
            ["generation_runs.owner_id", "generation_runs.industry_id",
             "generation_runs.id"],
            name=op.f("fk_conversation_summary_revisions_owner_id_generation_runs"),
        ),
    )
    op.create_table(
        "conversation_state_revisions",
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("last_message_id", _UUID, nullable=False),
        sa.Column("resolved_question", sa.Text, nullable=False),
        sa.Column("constraints", _JSONB, nullable=False),
        sa.Column(
            "active_topic_ids", _UUID_ARR, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "reference_ids", _UUID_ARR, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "open_questions", _TEXT_ARR, server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("summary_revision_id", _UUID, nullable=True),
        *_version_common(),
        *_i_scope(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_state_revisions")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id",
            name=op.f("uq_conversation_state_revisions_owner_id"),
        ),
        sa.UniqueConstraint(
            "conversation_id", "version",
            name="uq_conversation_state_revisions_conversation_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_conversation_state_revisions_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "conversation_id"],
            ["conversations.owner_id", "conversations.industry_id",
             "conversations.id"],
            name=op.f("fk_conversation_state_revisions_owner_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "last_message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
            name=op.f("fk_conversation_state_revisions_owner_id_messages"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "summary_revision_id"],
            ["conversation_summary_revisions.owner_id",
             "conversation_summary_revisions.industry_id",
             "conversation_summary_revisions.id"],
            name="fk_conversation_state_revisions_summary",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_conversation_state_revisions_owner_id_jobs"),
        ),
    )
    # publication_citations split (spec 03 §9: 首版选分表避免多态FK).
    # Parent revision FKs use the default NO ACTION; the §9 RESTRICT mandate
    # covers referenced parses/claim revisions/event revisions/evidence rows,
    # which the evidence/claim_revision FKs below carry.
    _citation_table("report_citations", "report_revisions", "report_revision_id")
    _citation_table(
        "evolution_citations", "evolution_revisions", "evolution_revision_id"
    )
    _citation_table("message_citations", "messages", "message_id")


def _citation_table(
    name: str, parent_table: str, parent_col: str
) -> None:
    """Uniform citation link: PK(parent, evidence), label/claim/ordinal."""
    op.create_table(
        name,
        sa.Column(parent_col, _UUID, nullable=False),
        sa.Column("evidence_id", _UUID, nullable=False),
        sa.Column("citation_label", sa.Text, nullable=False),
        sa.Column("claim_revision_id", _UUID, nullable=True),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("industry_id", _UUID, nullable=False),
        sa.PrimaryKeyConstraint(parent_col, "evidence_id", name=op.f(f"pk_{name}")),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f(f"fk_{name}_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", parent_col],
            [f"{parent_table}.owner_id", f"{parent_table}.industry_id",
             f"{parent_table}.id"],
            name=op.f(f"fk_{name}_owner_id_{parent_table}"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "evidence_id"],
            ["evidence.owner_id", "evidence.industry_id", "evidence.id"],
            name=op.f(f"fk_{name}_owner_id_evidence"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_revision_id"],
            ["claim_revisions.owner_id", "claim_revisions.industry_id",
             "claim_revisions.id"],
            name=op.f(f"fk_{name}_owner_id_claim_revisions"),
            ondelete="RESTRICT",
        ),
    )


def _create_governance_tables() -> None:
    op.create_table(
        "review_tasks",
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("proposal", _JSONB, nullable=False),
        sa.Column("expected_versions", _JSONB, nullable=False),
        sa.Column("decision", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("decided_at", _TS, nullable=True),
        sa.Column("applied_job_id", _UUID, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_tasks")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_review_tasks_owner_id")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'obsolete',"
            " 'applied', 'failed')",
            name=op.f("ck_review_tasks_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_review_tasks_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "applied_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_review_tasks_owner_id_jobs"),
        ),
    )
    op.create_table(
        "audit_log",
        sa.Column("industry_id", _UUID, nullable=True),
        sa.Column("actor_type", sa.Text, nullable=False),
        sa.Column("actor_id", _UUID, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target_type", sa.Text, nullable=False),
        sa.Column("target_id", _UUID, nullable=False),
        sa.Column("before_version", sa.BigInteger, nullable=True),
        sa.Column("after_version", sa.BigInteger, nullable=True),
        sa.Column(
            "details", _JSONB, server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("owner_id", "id", name=op.f("uq_audit_log_owner_id")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_audit_log_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_audit_log_owner_id_industries"),
        ),
    )
    op.create_table(
        "dependency_edges",
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("source_revision_id", _UUID, nullable=False),
        sa.Column("derived_type", sa.Text, nullable=False),
        sa.Column("derived_revision_id", _UUID, nullable=False),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dependency_edges")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_dependency_edges_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_dependency_edges_owner_id_industries"),
        ),
    )
    op.create_table(
        "derived_status",
        sa.Column("object_type", sa.Text, nullable=False),
        sa.Column("revision_id", _UUID, nullable=False),
        sa.Column("stale_since", _TS, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("refresh_job_id", _UUID, nullable=True),
        *_i_scope(),
        *_common(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_derived_status")),
        sa.UniqueConstraint(
            "owner_id", "industry_id", "id", name=op.f("uq_derived_status_owner_id")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
            name=op.f("fk_derived_status_owner_id_industries"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "industry_id", "refresh_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
            name=op.f("fk_derived_status_owner_id_jobs"),
        ),
    )


def _create_deferred_fks() -> None:
    """Circular/forward FKs + the FKs 0001 deferred to the jobs/knowledge
    migration. use_alter constraints never render, so these must be explicit
    ALTER TABLE ADD CONSTRAINT statements."""
    deferred = [
        # 0001 promised / broken-by-use_alter FKs, repaired here.
        ("industries", ["owner_id", "current_revision_id"],
         "industry_revisions", ["owner_id", "id"]),
        ("topics", ["owner_id", "industry_id", "current_revision_id"],
         "topic_revisions", ["owner_id", "industry_id", "id"]),
        ("documents", ["owner_id", "current_capture_id"],
         "captures", ["owner_id", "id"]),
        ("industry_revisions", ["owner_id", "created_by_job_id"],
         "jobs", ["owner_id", "id"]),
        ("topic_revisions", ["owner_id", "industry_id", "created_by_job_id"],
         "jobs", ["owner_id", "industry_id", "id"]),
        ("source_runs", ["owner_id", "job_id"], "jobs", ["owner_id", "id"]),
        ("processing_decisions", ["owner_id", "model_run_id"],
         "model_runs", ["owner_id", "id"]),
        ("processing_decisions", ["owner_id", "industry_id", "override_id"],
         "overrides", ["owner_id", "industry_id", "id"]),
        # 0002 circular current_* pointers and forward references.
        ("claims", ["owner_id", "industry_id", "current_revision_id"],
         "claim_revisions", ["owner_id", "industry_id", "id"]),
        ("events", ["owner_id", "industry_id", "current_revision_id"],
         "event_revisions", ["owner_id", "industry_id", "id"]),
        ("event_topics", ["owner_id", "industry_id", "current_revision_id"],
         "event_topic_revisions", ["owner_id", "industry_id", "id"]),
        ("event_relations", ["owner_id", "industry_id", "current_revision_id"],
         "event_relation_revisions", ["owner_id", "industry_id", "id"]),
        ("watches", ["owner_id", "industry_id", "resolution_report_id"],
         "reports", ["owner_id", "industry_id", "id"]),
        ("evolutions", ["owner_id", "industry_id", "current_revision_id"],
         "evolution_revisions", ["owner_id", "industry_id", "id"]),
        ("reports", ["owner_id", "industry_id", "current_revision_id"],
         "report_revisions", ["owner_id", "industry_id", "id"]),
        ("conversations", ["owner_id", "industry_id", "last_committed_message_id"],
         "messages", ["owner_id", "industry_id", "id"]),
        ("conversations", ["owner_id", "industry_id", "current_state_revision_id"],
         "conversation_state_revisions", ["owner_id", "industry_id", "id"]),
    ]
    for table, local, ref, refcols in deferred:
        op.create_foreign_key(
            f"fk_{table}_owner_id_{ref}", table, ref, local, refcols
        )


def _create_indexes() -> None:
    """Spec 03 §8: every O/I table indexed on owner_id, every I table on
    (owner_id, industry_id), key FKs indexed."""
    btree = [
        ("ix_jobs_owner_industry", "jobs", ["owner_id", "industry_id"]),
        ("ix_jobs_state_available_at", "jobs", ["state", "available_at"]),
        ("ix_job_steps_owner_industry", "job_steps", ["owner_id", "industry_id"]),
        ("ix_job_steps_owner_job", "job_steps", ["owner_id", "job_id"]),
        ("ix_job_events_owner_job", "job_events", ["owner_id", "job_id"]),
        ("ix_model_runs_owner_industry", "model_runs", ["owner_id", "industry_id"]),
        ("ix_model_runs_owner_job", "model_runs", ["owner_id", "job_id"]),
        ("ix_entities_owner_industry", "entities", ["owner_id", "industry_id"]),
        ("ix_entities_owner_name", "entities",
         ["owner_id", "industry_id", "canonical_name"]),
        ("ix_entity_aliases_owner_industry", "entity_aliases",
         ["owner_id", "industry_id"]),
        ("ix_entity_aliases_owner_entity", "entity_aliases",
         ["owner_id", "industry_id", "entity_id"]),
        ("ix_entity_aliases_owner_norm", "entity_aliases",
         ["owner_id", "industry_id", "normalized_alias"]),
        ("ix_source_families_owner_industry", "source_families",
         ["owner_id", "industry_id"]),
        ("ix_industry_documents_owner_industry", "industry_documents",
         ["owner_id", "industry_id"]),
        ("ix_industry_documents_owner_document", "industry_documents",
         ["owner_id", "document_id"]),
        ("ix_document_topics_owner_industry", "document_topics",
         ["owner_id", "industry_id"]),
        ("ix_document_topics_owner_document", "document_topics",
         ["owner_id", "industry_document_id"]),
        ("ix_claims_owner_industry", "claims", ["owner_id", "industry_id"]),
        ("ix_claim_revisions_owner_industry", "claim_revisions",
         ["owner_id", "industry_id"]),
        ("ix_claim_revisions_owner_claim", "claim_revisions",
         ["owner_id", "industry_id", "claim_id"]),
        ("ix_evidence_owner_industry", "evidence", ["owner_id", "industry_id"]),
        ("ix_evidence_owner_claim_revision", "evidence",
         ["owner_id", "claim_revision_id"]),
        ("ix_evidence_owner_parse", "evidence", ["owner_id", "parsed_artifact_id"]),
        ("ix_events_owner_industry", "events", ["owner_id", "industry_id"]),
        ("ix_event_revisions_owner_industry", "event_revisions",
         ["owner_id", "industry_id"]),
        ("ix_event_revisions_owner_event_recorded", "event_revisions",
         ["owner_id", "industry_id", "event_id", "recorded_at"]),
        ("ix_event_revisions_owner_occurred", "event_revisions",
         ["owner_id", "industry_id", "occurred_start"]),
        ("ix_event_revisions_owner_identity", "event_revisions",
         ["owner_id", "industry_id", "identity_key"]),
        ("ix_event_topics_owner_industry", "event_topics",
         ["owner_id", "industry_id"]),
        ("ix_event_topics_owner_event", "event_topics",
         ["owner_id", "industry_id", "event_id"]),
        ("ix_event_topic_revisions_owner_industry", "event_topic_revisions",
         ["owner_id", "industry_id"]),
        ("ix_event_topic_revisions_owner_parent", "event_topic_revisions",
         ["owner_id", "industry_id", "event_topic_id"]),
        ("ix_event_relations_owner_industry", "event_relations",
         ["owner_id", "industry_id"]),
        ("ix_event_relations_owner_from", "event_relations",
         ["owner_id", "industry_id", "from_event_id"]),
        ("ix_event_relation_revisions_owner_industry", "event_relation_revisions",
         ["owner_id", "industry_id"]),
        ("ix_event_relation_revisions_owner_relation", "event_relation_revisions",
         ["owner_id", "industry_id", "relation_id"]),
        ("ix_watches_owner_industry", "watches", ["owner_id", "industry_id"]),
        ("ix_overrides_owner_industry", "overrides", ["owner_id", "industry_id"]),
        ("ix_event_read_states_owner_industry", "event_read_states",
         ["owner_id", "industry_id"]),
        ("ix_topic_revision_entities_owner_industry", "topic_revision_entities",
         ["owner_id", "industry_id"]),
        ("ix_claim_revision_entities_owner_industry", "claim_revision_entities",
         ["owner_id", "industry_id"]),
        ("ix_event_revision_claims_owner_industry", "event_revision_claims",
         ["owner_id", "industry_id"]),
        ("ix_event_revision_entities_owner_industry", "event_revision_entities",
         ["owner_id", "industry_id"]),
        ("ix_event_topic_revision_claims_owner_industry",
         "event_topic_revision_claims", ["owner_id", "industry_id"]),
        ("ix_relation_revision_evidence_owner_industry", "relation_revision_evidence",
         ["owner_id", "industry_id"]),
        ("ix_watch_topics_owner_industry", "watch_topics",
         ["owner_id", "industry_id"]),
        ("ix_watch_entities_owner_industry", "watch_entities",
         ["owner_id", "industry_id"]),
        ("ix_generation_runs_owner_industry", "generation_runs",
         ["owner_id", "industry_id"]),
        ("ix_generation_runs_owner_job", "generation_runs",
         ["owner_id", "industry_id", "job_id"]),
        ("ix_generation_run_models_owner_industry", "generation_run_models",
         ["owner_id", "industry_id"]),
        ("ix_coverage_batches_owner_industry", "coverage_batches",
         ["owner_id", "industry_id"]),
        ("ix_recall_hits_owner_industry", "recall_hits",
         ["owner_id", "industry_id"]),
        ("ix_recall_hits_owner_job", "recall_hits",
         ["owner_id", "industry_id", "recall_job_id"]),
        ("ix_api_idempotency_owner_expires", "api_idempotency",
         ["owner_id", "expires_at"]),
        ("ix_evolutions_owner_industry", "evolutions",
         ["owner_id", "industry_id"]),
        ("ix_evolution_revisions_owner_industry", "evolution_revisions",
         ["owner_id", "industry_id"]),
        ("ix_evolution_revisions_owner_evolution", "evolution_revisions",
         ["owner_id", "industry_id", "evolution_id"]),
        ("ix_reports_owner_industry", "reports", ["owner_id", "industry_id"]),
        ("ix_report_revisions_owner_industry", "report_revisions",
         ["owner_id", "industry_id"]),
        ("ix_report_revisions_owner_report", "report_revisions",
         ["owner_id", "industry_id", "report_id"]),
        ("ix_conversations_owner_industry", "conversations",
         ["owner_id", "industry_id"]),
        ("ix_messages_owner_industry", "messages", ["owner_id", "industry_id"]),
        ("ix_messages_owner_conversation", "messages",
         ["owner_id", "industry_id", "conversation_id", "turn_index"]),
        ("ix_conversation_state_revisions_owner_industry",
         "conversation_state_revisions", ["owner_id", "industry_id"]),
        ("ix_conversation_state_revisions_owner_conversation",
         "conversation_state_revisions",
         ["owner_id", "industry_id", "conversation_id"]),
        ("ix_conversation_summary_revisions_owner_industry",
         "conversation_summary_revisions", ["owner_id", "industry_id"]),
        ("ix_conversation_summary_revisions_owner_conversation",
         "conversation_summary_revisions",
         ["owner_id", "industry_id", "conversation_id"]),
        ("ix_review_tasks_owner_industry", "review_tasks",
         ["owner_id", "industry_id"]),
        ("ix_review_tasks_owner_status", "review_tasks",
         ["owner_id", "industry_id", "status"]),
        ("ix_audit_log_owner_id", "audit_log", ["owner_id"]),
        ("ix_audit_log_owner_target", "audit_log",
         ["owner_id", "target_type", "target_id"]),
        ("ix_dependency_edges_owner_industry", "dependency_edges",
         ["owner_id", "industry_id"]),
        ("ix_dependency_edges_owner_source", "dependency_edges",
         ["owner_id", "industry_id", "source_type", "source_revision_id"]),
        ("ix_derived_status_owner_industry", "derived_status",
         ["owner_id", "industry_id"]),
        ("ix_derived_status_owner_object", "derived_status",
         ["owner_id", "industry_id", "object_type", "revision_id"]),
        ("ix_report_citations_owner_industry", "report_citations",
         ["owner_id", "industry_id"]),
        ("ix_evolution_citations_owner_industry", "evolution_citations",
         ["owner_id", "industry_id"]),
        ("ix_message_citations_owner_industry", "message_citations",
         ["owner_id", "industry_id"]),
        ("ix_output_generations_owner_industry", "output_generations",
         ["owner_id", "industry_id"]),
        ("ix_output_generations_owner_run", "output_generations",
         ["owner_id", "industry_id", "generation_run_id"]),
        ("ix_document_association_history_owner_industry",
         "document_association_history", ["owner_id", "industry_id"]),
        ("ix_document_association_history_owner_document",
         "document_association_history",
         ["owner_id", "industry_id", "industry_document_id"]),
        ("ix_event_association_history_owner_industry",
         "event_association_history", ["owner_id", "industry_id"]),
        ("ix_event_association_history_owner_event", "event_association_history",
         ["owner_id", "industry_id", "event_id"]),
        ("ix_event_merge_operations_owner_industry", "event_merge_operations",
         ["owner_id", "industry_id"]),
        ("ix_event_lifecycle_history_owner_industry", "event_lifecycle_history",
         ["owner_id", "industry_id"]),
        ("ix_event_lifecycle_history_owner_event", "event_lifecycle_history",
         ["owner_id", "industry_id", "event_id"]),
    ]
    for name, table, columns in btree:
        colspec = ", ".join(columns)
        op.execute(f"CREATE INDEX {name} ON {table} ({colspec})")


def _apply_rls() -> None:
    """Same policy shape as 0001: ENABLE + FORCE RLS, TO intel_app policies
    bound to transaction-local GUCs (owner-only predicate for the O/I
    hybrids; owner+industry predicate for I tables)."""
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


# Reverse dependency order. The ALTER-added FKs on 0001 tables are dropped
# explicitly first; dropping the new tables CASCADE would also remove them,
# but explicit drops keep 0001 tables untouched-by-CASCADE semantics clear.
_DROPPED_TABLES = [
    "event_lifecycle_history",
    "event_merge_operations",
    "event_association_history",
    "document_association_history",
    "derived_status",
    "dependency_edges",
    "audit_log",
    "review_tasks",
    "message_citations",
    "conversation_state_revisions",
    "conversation_summary_revisions",
    "messages",
    "conversations",
    "evolution_citations",
    "report_citations",
    "report_revisions",
    "reports",
    "evolution_revisions",
    "evolutions",
    "api_idempotency",
    "recall_hits",
    "coverage_batches",
    "output_generations",
    "generation_run_models",
    "generation_runs",
    "watch_entities",
    "watch_topics",
    "relation_revision_evidence",
    "event_topic_revision_claims",
    "event_revision_entities",
    "event_revision_claims",
    "claim_revision_entities",
    "topic_revision_entities",
    "event_read_states",
    "overrides",
    "watches",
    "event_relation_revisions",
    "event_relations",
    "event_topic_revisions",
    "event_topics",
    "event_revisions",
    "events",
    "evidence",
    "claim_revisions",
    "claims",
    "document_topics",
    "industry_documents",
    "source_families",
    "entity_aliases",
    "entities",
    "model_runs",
    "job_events",
    "job_steps",
    "jobs",
]


def downgrade() -> None:
    for fk in [
        "fk_industries_owner_id_industry_revisions",
        "fk_topics_owner_id_topic_revisions",
        "fk_documents_owner_id_captures",
        "fk_industry_revisions_owner_id_jobs",
        "fk_topic_revisions_owner_id_jobs",
        "fk_source_runs_owner_id_jobs",
        "fk_processing_decisions_owner_id_model_runs",
        "fk_processing_decisions_owner_id_overrides",
    ]:
        table = {
            "fk_industries_owner_id_industry_revisions": "industries",
            "fk_topics_owner_id_topic_revisions": "topics",
            "fk_documents_owner_id_captures": "documents",
            "fk_industry_revisions_owner_id_jobs": "industry_revisions",
            "fk_topic_revisions_owner_id_jobs": "topic_revisions",
            "fk_source_runs_owner_id_jobs": "source_runs",
            "fk_processing_decisions_owner_id_model_runs": "processing_decisions",
            "fk_processing_decisions_owner_id_overrides": "processing_decisions",
        }[fk]
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk}")
    for table in _DROPPED_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
