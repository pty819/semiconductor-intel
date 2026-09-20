"""Design DTOs, Pydantic v2. Not ORM models or a running application.

All examples are synthetic. Authorization, quote verification, and state changes
must be enforced by application services in addition to these structural checks.
"""
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeValue(DTO):
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    precision: Literal["instant", "day", "month", "year", "range", "unknown"]
    timezone: str | None = None
    original_text: str | None = None
    basis: Literal["explicit", "inferred", "unknown"] = "explicit"

    @model_validator(mode="after")
    def interval(self):
        if self.precision == "unknown":
            if self.start is not None or self.end is not None:
                raise ValueError("Unknown date cannot contain fabricated bounds")
        elif self.start is None:
            raise ValueError("Known date requires start")
        elif self.precision == "instant":
            if self.end is not None:
                raise ValueError("Instant has start only")
        elif self.end is None or self.end <= self.start:
            raise ValueError("Non-instant time is a nonempty half-open interval")
        return self


class VersionCommand(DTO):
    expected_version: int = Field(ge=1)


class IndustrySettings(DTO):
    pool_scope: Literal["all_public", "selected_feeds"] = "all_public"
    backfill_days: int = Field(default=90, ge=0, le=3650)
    daily_report: bool = True


class BusinessProfile(DTO):
    products: list[str] = Field(default_factory=list)
    target_customers: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    research_goals: list[str] = Field(default_factory=list)
    important_changes: list[str] = Field(default_factory=list)


class IndustryCreate(DTO):
    name: str = Field(min_length=1, max_length=160)
    description: str
    included_scope: list[str] = Field(default_factory=list)
    excluded_scope: list[str] = Field(default_factory=list)
    profile: BusinessProfile | None = None
    settings: IndustrySettings = Field(default_factory=IndustrySettings)


class IndustryPatch(VersionCommand):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    included_scope: list[str] | None = None
    excluded_scope: list[str] | None = None
    profile: BusinessProfile | None = None
    settings: IndustrySettings | None = None


class IndustryView(IndustryCreate):
    id: UUID
    revision_id: UUID
    row_version: int
    status: Literal["draft", "active", "paused", "archived"]


class LifecycleCommand(VersionCommand):
    action: Literal["activate", "pause", "archive", "restore"]


class TopicCreate(DTO):
    name: str = Field(min_length=1, max_length=160)
    description: str
    positive_examples: list[str] = Field(default_factory=list)
    negative_examples: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    entity_ids: list[UUID] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    analysis_template: str = "technical"
    priority: int = Field(default=0, ge=0, le=10)


class TopicPatch(VersionCommand):
    name: str | None = None
    description: str | None = None
    positive_examples: list[str] | None = None
    negative_examples: list[str] | None = None
    aliases: list[str] | None = None
    entity_ids: list[UUID] | None = None
    questions: list[str] | None = None
    analysis_template: str | None = None
    priority: int | None = Field(default=None, ge=0, le=10)
    status: Literal["active", "paused", "archived"] | None = None


class TopicView(TopicCreate):
    id: UUID
    industry_id: UUID
    revision_id: UUID
    row_version: int
    status: Literal["active", "paused", "archived"]


class WindowRequest(DTO):
    from_time: AwareDatetime | None = None
    to_time: AwareDatetime | None = None
    as_of: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.from_time and self.to_time and self.from_time >= self.to_time:
            raise ValueError("from_time must precede to_time")
        return self


class SourceTemplate(DTO):
    id: str
    name: str
    homepage: str
    canonical_seed: str
    kind: str
    tags: list[str]
    access_notes: str


class FeedCreate(DTO):
    template_id: str | None = None
    seed_url: str
    adapter_type: Literal["rss", "atom", "api", "html_list", "sitemap", "page_monitor"]
    interval_seconds: int = Field(default=21600, ge=60)
    user_enabled: bool = True
    credential_ref: str | None = None


class FeedPatch(VersionCommand):
    interval_seconds: int | None = Field(default=None, ge=60)
    user_enabled: bool | None = None
    parser_version_id: UUID | None = None
    status: Literal["active", "paused"] | None = None


class FeedView(FeedCreate):
    id: UUID
    row_version: int
    status: str
    next_poll_at: AwareDatetime | None = None


class SourceSubscriptionCreate(DTO):
    feed_id: UUID
    backfill_from: AwareDatetime | None = None


class SourceSubscriptionPatch(VersionCommand):
    status: Literal["active", "paused"]


class SourceSubscriptionView(SourceSubscriptionCreate):
    id: UUID
    industry_id: UUID
    row_version: int
    status: Literal["active", "paused"]


class PollRequest(DTO):
    mode: Literal["incremental", "trial", "backfill"] = "incremental"
    from_time: AwareDatetime | None = None


class Coverage(DTO):
    status: Literal["complete", "partial", "pending", "unknown"]
    expected: int | None = Field(default=None, ge=0)
    processed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    gaps: list[str] = Field(default_factory=list)
    watermark: AwareDatetime | None = None


class InputManifest(DTO):
    """Cross-object provenance for derived outputs. Schema-versioned."""

    schema_version: int = 1
    config_refs: list[str] = Field(default_factory=list)
    document_parse_ids: list[UUID] = Field(default_factory=list)
    claim_revision_ids: list[UUID] = Field(default_factory=list)
    event_revision_ids: list[UUID] = Field(default_factory=list)
    model_run_ids: list[UUID] = Field(default_factory=list)


class SourceRunView(DTO):
    id: UUID
    feed_id: UUID
    outcome: Literal["success", "no_change", "partial", "failed"]
    coverage: Coverage
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    error_code: str | None = None


class DocumentView(DTO):
    id: UUID
    industry_id: UUID
    row_version: int
    title: str
    source_url: str
    current_parse_id: UUID | None = None
    retrieval_scope: Literal["metadata", "abstract", "partial", "fulltext"]
    parse_status: Literal["ok", "partial", "failed", "pending"]
    relevance: Literal["direct", "background", "uncertain"]
    quality_flags: list[str]
    first_seen_at: AwareDatetime


class DocumentRevisionView(DTO):
    id: UUID
    document_id: UUID
    capture_id: UUID
    parser_version_id: UUID
    parsed_at: AwareDatetime
    coverage: Coverage


class DiffView(DTO):
    from_parse_id: UUID
    to_parse_id: UUID
    kind: Literal["content_change", "parser_change", "mixed"]
    algorithm_version: str
    changed_blocks: list[dict[str, Any]]
    field_changes: list[dict[str, Any]]


class ImportURL(DTO):
    url: str


class SourceBlock(DTO):
    block_id: str
    text: str
    kind: Literal["paragraph", "heading", "table", "caption"] = "paragraph"
    page: int | None = Field(default=None, ge=1)


class ExtractionInput(DTO):
    parse_id: UUID
    retrieval_scope: Literal["metadata", "abstract", "partial", "fulltext"]
    blocks: list[SourceBlock]


class EvidenceProposal(DTO):
    block_id: str
    exact_quote: str = Field(min_length=1)
    relation: Literal["supports", "refutes", "context"]


class ClaimProposal(DTO):
    text: str = Field(min_length=1)
    kind: Literal["source_statement", "inference"]
    attribution: str | None = None
    conditions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceProposal] = Field(min_length=1)


class ExtractionProposal(DTO):
    claims: list[ClaimProposal]
    limitations: list[str] = Field(default_factory=list)


class AssessmentDimensions(DTO):
    """Discrete quality vocabulary shared by claims, events and answers.

    Uncalibrated probabilities are not representable in this structure.
    """

    extraction_quality: Literal["high", "medium", "low", "unknown"] = "unknown"
    source_reliability: Literal[
        "primary_official", "independent_media", "aggregator", "unknown"
    ] = "unknown"
    independence: Literal["independent", "shared_source", "unknown"] = "unknown"
    evidence_sufficiency: Literal[
        "sufficient", "partial", "insufficient", "unknown"
    ] = "unknown"
    conflict_state: Literal[
        "none_known", "disputed", "corrected", "retracted", "unknown"
    ] = "unknown"
    reason: str = ""


class Citation(DTO):
    id: str
    claim_revision_id: UUID | None = None
    evidence_id: UUID
    parse_id: UUID
    document_id: UUID


class GenerationRef(DTO):
    run_id: UUID
    output_path: str = Field(pattern=r"^(|/.*)$")
    role: Literal["producer", "verifier", "upstream"] = "producer"


class GenerationRunView(DTO):
    id: UUID
    job_id: UUID
    attempt: int = Field(ge=1)
    step_key: str
    trace_state: Literal["recording", "pending", "available", "missing", "expired", "redacted"]
    model_route_display: str | None = None
    nooa_commit: str
    prompt_version: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    summary: list[str]
    evidence_validation: Literal["passed", "partial", "failed", "not_applicable"]
    viewer_available: bool


class GenerationViewerLink(DTO):
    available: bool
    url: str | None = None
    reason: str | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def link_state(self):
        if self.available and not self.url:
            raise ValueError("Available viewer requires an authorized URL")
        if not self.available and self.url is not None:
            raise ValueError("Unavailable viewer must not reveal a URL")
        return self


class EvidenceView(DTO):
    id: UUID
    claim_revision_id: UUID
    parse_id: UUID
    document_id: UUID
    block_id: str
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=1)
    exact_quote: str
    context_before: str
    context_after: str
    page: int | None = None
    locator_status: Literal["verified", "invalid"]
    semantic_support: Literal["supported", "uncertain", "rejected"]
    retrieval_scope: Literal["metadata", "abstract", "partial", "fulltext"]
    source_url: str
    artifact_url: str


class EntityCreate(DTO):
    name: str
    kind: Literal["company", "product", "person", "institution", "method", "dataset", "component"]
    aliases: list[str] = Field(default_factory=list)
    identifiers: dict[str, str] = Field(default_factory=dict)


class EntityPatch(VersionCommand):
    name: str | None = None
    aliases: list[str] | None = None
    identifiers: dict[str, str] | None = None


class EntityView(EntityCreate):
    id: UUID
    row_version: int


class TopicInterpretation(DTO):
    topic_id: UUID
    topic_revision_id: UUID
    relevance: Literal["direct", "background", "uncertain"]
    importance: Literal["high", "normal", "low"]
    rationale: str
    interpretation: str
    citation_ids: list[str]
    generation_refs: list[GenerationRef] = Field(default_factory=list)


class EventCard(DTO):
    id: UUID
    revision_id: UUID
    row_version: int
    title: str
    summary: str
    event_type: str
    occurred_time: TimeValue
    published_time: TimeValue
    effective_time: TimeValue
    first_discovered_at: AwareDatetime
    interpretations: list[TopicInterpretation]
    citations: list[Citation]
    document_count: int = Field(ge=0)
    source_family_count_known: int | None = Field(default=None, ge=0)
    conflict_state: Literal["none_known", "disputed", "unknown"]
    late_discovery: bool = False
    lifecycle: Literal["active", "merged", "retracted"] = "active"
    merged_into_id: UUID | None = None
    generation_refs: list[GenerationRef] = Field(default_factory=list)


class ClaimErrorCorrection(DTO):
    kind: Literal["claim_error"] = "claim_error"
    claim_revision_id: UUID
    corrected_text: str
    conditions: list[str] = Field(default_factory=list)


class EventTimeCorrection(DTO):
    kind: Literal["event_time"] = "event_time"
    event_revision_id: UUID
    occurred_time: TimeValue
    published_time: TimeValue | None = None


class DuplicateMergeCorrection(DTO):
    kind: Literal["duplicate"] = "duplicate"
    source_event_id: UUID
    target_event_id: UUID


class TopicAssociationCorrection(DTO):
    kind: Literal["topic_association"] = "topic_association"
    industry_document_id: UUID
    topic_id: UUID
    relevance: Literal["direct", "background", "uncertain", "unrelated"]


class RelationErrorCorrection(DTO):
    kind: Literal["relation_error"] = "relation_error"
    relation_id: UUID
    status: Literal["retracted", "revised"] = "retracted"
    note: str = ""


CorrectionValue = Annotated[
    ClaimErrorCorrection
    | EventTimeCorrection
    | DuplicateMergeCorrection
    | TopicAssociationCorrection
    | RelationErrorCorrection,
    Field(discriminator="kind"),
]


class CorrectionCommand(VersionCommand):
    correction: CorrectionValue
    rationale: str
    related_ids: list[UUID] = Field(default_factory=list)


class DocumentTopicCommand(VersionCommand):
    topic_id: UUID
    relevance: Literal["direct", "background", "uncertain", "unrelated"]
    rationale: str
    lock: bool = True


class ReadStateCommand(DTO):
    read: bool


class EvolutionNode(DTO):
    event_id: UUID
    event_revision_id: UUID
    title: str
    citation_ids: list[str]
    generation_refs: list[GenerationRef] = Field(default_factory=list)


class EvolutionEdge(DTO):
    from_event_id: UUID
    to_event_id: UUID
    relation: Literal["updates", "validates", "refutes", "applies", "extends", "parallel", "replaces"]
    basis: Literal["explicit", "inferred"]
    rationale: str
    citation_ids: list[str] = Field(min_length=1)
    generation_refs: list[GenerationRef] = Field(default_factory=list)


class EvolutionStage(DTO):
    title: str
    summary: str
    event_revision_ids: list[UUID]
    citation_ids: list[str]
    generation_refs: list[GenerationRef] = Field(default_factory=list)


class EvolutionView(DTO):
    revision_id: UUID | None = None
    topic_revision_id: UUID
    as_of: AwareDatetime
    status: Literal["ready", "insufficient_evidence", "building", "failed"]
    stale: bool
    stages: list[EvolutionStage]
    nodes: list[EvolutionNode]
    edges: list[EvolutionEdge]
    citations: list[Citation]
    open_questions: list[str]
    coverage: Coverage


class WatchCreate(DTO):
    title: str
    question: str
    topic_ids: list[UUID] = Field(default_factory=list)
    entity_ids: list[UUID] = Field(default_factory=list)


class WatchPatch(VersionCommand):
    title: str | None = None
    question: str | None = None
    topic_ids: list[UUID] | None = None
    status: Literal["open", "resolved", "paused"] | None = None


class WatchView(WatchCreate):
    id: UUID
    row_version: int
    status: Literal["open", "resolved", "paused"]


class ReviewView(DTO):
    id: UUID
    row_version: int
    type: str
    status: Literal["pending", "accepted", "rejected", "obsolete", "applied", "failed"]
    proposal: dict[str, Any]
    expected_versions: dict[str, int]


class ReviewDecision(VersionCommand):
    action: Literal["approve", "reject", "undo"]
    reason: str


class ConversationCreate(DTO):
    title: str = "新对话"


class ConversationView(ConversationCreate):
    id: UUID
    industry_id: UUID
    state_version: int = Field(ge=1)
    last_committed_message_id: UUID | None = None


class MessageCreate(DTO):
    text: str = Field(min_length=1, max_length=50000)
    mode: Literal["archive", "online"] = "archive"
    topic_ids: list[UUID] = Field(default_factory=list)
    as_of: AwareDatetime | None = None
    parent_message_id: UUID | None = None

    @model_validator(mode="after")
    def mode_time(self):
        if self.mode == "online" and self.as_of is not None:
            raise ValueError("Historical knowledge cutoff is supported only in archive mode")
        return self


class AnswerBlock(DTO):
    text: str
    kind: Literal["fact", "inference", "unknown"]
    citation_ids: list[str] = Field(default_factory=list)
    generation_refs: list[GenerationRef] = Field(default_factory=list)


class MessageView(DTO):
    id: UUID
    parent_message_id: UUID | None = None
    turn_index: int = Field(ge=1)
    role: Literal["user", "assistant"]
    status: Literal["pending", "ready", "partial", "failed", "cancelled"]
    blocks: list[AnswerBlock]
    citations: list[Citation]
    as_of: AwareDatetime | None = None
    coverage: Coverage | None = None
    job_id: UUID | None = None


class ConversationConstraint(DTO):
    id: UUID
    text: str
    kind: Literal["scope", "time", "entity", "preference", "comparison", "other"]
    source_message_id: UUID
    status: Literal["active", "superseded"]


class ConversationContext(DTO):
    conversation_id: UUID
    state_version: int = Field(ge=1)
    resolved_question: str
    recent_messages: list[MessageView]
    constraints: list[ConversationConstraint]
    ordered_references: list[Citation]
    open_questions: list[str]
    summary: str | None = None
    summary_through_message_id: UUID | None = None


class ReportCreate(WindowRequest):
    type: Literal["daily", "topic", "investigation"]
    title: str
    topic_ids: list[UUID] = Field(default_factory=list)


class ReportView(DTO):
    id: UUID
    revision_id: UUID
    title: str
    as_of: AwareDatetime
    blocks: list[AnswerBlock]
    citations: list[Citation]
    coverage: Coverage
    stale: bool


class SearchRequest(WindowRequest):
    query: str = Field(min_length=1)
    topic_ids: list[UUID] = Field(default_factory=list)
    entity_ids: list[UUID] = Field(default_factory=list)
    cursor: str | None = None
    page_size: int = Field(default=50, ge=1, le=200)


class SearchHit(DTO):
    object_id: UUID
    object_type: Literal["document", "claim", "event"]
    title: str
    excerpt: str
    channels: list[str]
    citation_ids: list[str]


class JobAccepted(DTO):
    job_id: UUID
    state: str
    events_url: str
    result_url: str


class JobView(DTO):
    id: UUID
    industry_id: UUID | None = None
    kind: str
    state: Literal["queued", "running", "retry_wait", "waiting_review", "succeeded", "partial", "failed", "cancelled"]
    stage: str | None = None
    done: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    result_url: str | None = None


class JobCommand(DTO):
    reason: str = "user_requested"


class ErrorDetail(DTO):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(DTO):
    error: ErrorDetail


class LoginRequest(DTO):
    login: str
    password: str


class UserView(DTO):
    id: UUID
    login: str
    timezone: str
    csrf_token: str


class HealthView(DTO):
    status: Literal["ok", "unavailable"]


class Ack(DTO):
    ok: bool
