"""Knowledge commit service: claims/evidence/events in transactions (05 §1-§4).

Pure decision logic lives in :mod:`intel.domain.validation` and
:mod:`intel.domain.identity`; this module owns the TRANSACTIONAL shapes
the workflows drive:

- :func:`commit_extraction` — one validated proposal → claim_revisions +
  evidence + source_family rows, all inside the caller's store
  transaction (14 §2 commit_extraction signature). Source families are
  keyed by origin reference: EVT-01's ten reposts of one announcement
  dedupe to ONE family — the event card then says 10 documents sharing a
  source, never "10 independent verifications".
- :func:`resolve_event` — proposal → strong key → auto-merge into the
  existing event (boolean gate) or new event (+ possible_duplicate
  proposal when weakly evidenced).
- :func:`apply_event_merge` / :func:`undo_event_merge` — the 05 §4
  manual merge lifecycle: expected row_versions checked under the
  transaction's locks, membership snapshot saved, undo restores unless
  post-merge modifications exist — then ``review_conflict`` and a
  compensation proposal, never an overwrite (EVT-04).

The store protocol is intentionally narrow; the in-memory double backs
the offline tests and the SQLAlchemy adapter lands with the Task 13/14
read/write API wiring.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from intel.contracts.models import TimeValue
from intel.domain.identity import StrongKey, build_identity_key, key_string
from intel.domain.validation import ValidatedClaim
from intel.repositories.base import IndustryScope

__all__ = [
    "EventProposalInput",
    "ExtractionCommit",
    "InMemoryKnowledgeStore",
    "KnowledgeError",
    "KnowledgeStore",
    "MergeConflict",
    "apply_event_merge",
    "commit_extraction",
    "resolve_event",
    "undo_event_merge",
]


class KnowledgeError(Exception):
    """Transactional contract violation (surfaces as 409-class API errors)."""

    code: str = "knowledge_error"


class MergeConflict(KnowledgeError):
    """Expected row_versions did not match the locked rows (05 §4)."""

    code = "version_conflict"


class UndoConflict(KnowledgeError):
    """Post-merge modification blocks a clean undo (EVT-04)."""

    code = "review_conflict"


@dataclass(slots=True)
class EventProposalInput:
    """One candidate event from the extraction workflow."""

    event_type: str
    title: str
    summary: str = ""
    identity_fields: dict[str, str] = field(default_factory=dict)
    claim_revision_ids: list[UUID] = field(default_factory=list)
    evidence_ids: list[UUID] = field(default_factory=list)
    occurred_time: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionCommit:
    claim_revision_ids: list[UUID] = field(default_factory=list)
    evidence_ids: list[UUID] = field(default_factory=list)
    source_family_ids: list[UUID] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)


@runtime_checkable
class KnowledgeStore(Protocol):
    """What the knowledge service needs from storage (one transaction)."""

    async def find_source_family(
        self, scope: IndustryScope, origin_ref: str
    ) -> UUID | None: ...

    async def insert_source_family(
        self, scope: IndustryScope, *, origin_ref: str, label: str
    ) -> UUID: ...

    async def insert_claim_with_revision(
        self,
        scope: IndustryScope,
        *,
        text: str,
        kind: str,
        attribution: str | None,
        predicate: str,
        object_value: dict,
        conditions: dict,
        assessment: dict,
        input_manifest: dict,
    ) -> tuple[UUID, UUID]:
        """Returns (claim_id, claim_revision_id)."""

    async def insert_evidence(
        self,
        scope: IndustryScope,
        *,
        claim_revision_id: UUID,
        parsed_artifact_id: UUID,
        block_id: str,
        start_char: int,
        end_char: int,
        exact_quote: str,
        relation: str,
        semantic_support_status: str,
        source_family_id: UUID | None,
        extraction_run_id: UUID,
    ) -> UUID: ...

    async def find_event_by_key(
        self, scope: IndustryScope, canonical_key: str
    ) -> UUID | None: ...

    async def insert_event_with_revision(
        self,
        scope: IndustryScope,
        *,
        event_type: str,
        title: str,
        summary: str,
        identity_key: str | None,
        rationale: str,
        input_manifest: dict,
        occurred: Mapping[str, Any] | None = None,
        published: Mapping[str, Any] | None = None,
        effective: Mapping[str, Any] | None = None,
        first_discovered_at: datetime | None = None,
    ) -> tuple[UUID, UUID]:
        """``occurred``/``published``/``effective`` are serialized
        TimeValues (contracts.TimeValue.model_dump — precision validated
        per 03 §5, ``unknown`` allowed); adapters project
        ``occurred_start``/``occurred_end`` exactly as the ORM defines
        them. ``first_discovered_at`` defaults to now when omitted."""

    async def link_event_claims(
        self, scope: IndustryScope, *, event_id: UUID, claim_revision_ids: list[UUID]
    ) -> None: ...

    async def record_possible_duplicate(
        self,
        scope: IndustryScope,
        *,
        event_id: UUID,
        candidate_event_id: UUID | None,
        reason: str,
        input_manifest: dict,
    ) -> UUID: ...

    async def event_row_version(self, scope: IndustryScope, event_id: UUID) -> int: ...

    async def save_merge(
        self,
        scope: IndustryScope,
        *,
        review_id: UUID,
        canonical_id: UUID,
        merged_id: UUID,
        membership_snapshot: dict,
        rationale: str,
    ) -> UUID:
        """Locks both events; caller has already checked row_versions."""

    async def mark_merged(
        self, scope: IndustryScope, *, event_id: UUID, merged_into_id: UUID
    ) -> None: ...

    async def post_merge_modifications(
        self, scope: IndustryScope, merge_id: UUID
    ) -> int:
        """Human edits landed on the merged pair after the merge."""

    async def undo_merge(
        self, scope: IndustryScope, *, merge_id: UUID, compensation: dict | None
    ) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _quote_sha256(exact_quote: str) -> str:
    import hashlib

    return hashlib.sha256(exact_quote.encode()).hexdigest()


#: EVI-02 semantic-support statuses commit_extraction persists: ``pending``
#: is the unwired default (literal quote hit is not semantic support);
#: anything a wired judge returns outside this set downgrades to
#: ``uncertain`` (code validates — the model never chooses the storage
#: vocabulary).
_SEMANTIC_STATUSES = frozenset({"pending", "supports", "refutes", "uncertain"})


async def commit_extraction(
    store: KnowledgeStore,
    scope: IndustryScope,
    *,
    validated_claims: Sequence[ValidatedClaim],
    rejected: Sequence[Mapping[str, str]],
    parse_id: UUID,
    extraction_run_id: UUID,
    input_manifest: dict,
    origin_ref: str | None,
    semantic_status_by_index: Mapping[int, str] | None = None,
) -> ExtractionCommit:
    """Persist one parse's validated extraction inside one transaction.

    Source-family dedup: all evidence from reposts of one announcement
    (same origin_ref) shares ONE source_family row — independence is a
    per-claim fact derived from families, never a document count (05 §3).

    ``semantic_status_by_index`` carries the optional second-pass EVI-02
    verdict per accepted claim (index into ``validated_claims``); claims
    without an entry stay ``pending`` — a literal quote hit is not yet
    semantic support. The workflow RETURNS these statuses from its judge
    seam; nothing mutates the frozen validated DTOs.
    """
    commit = ExtractionCommit(rejected=[dict(item) for item in rejected])
    family_id: UUID | None = None
    if origin_ref:
        family_id = await store.find_source_family(scope, origin_ref)
        if family_id is None:
            family_id = await store.insert_source_family(
                scope,
                origin_ref=origin_ref,
                label=f"origin:{origin_ref[:120]}",
            )
            commit.source_family_ids.append(family_id)

    for index, validated in enumerate(validated_claims):
        claim = validated.claim
        kind = (
            "inference"
            if claim.kind == "source_statement"
            and not (claim.attribution or "").strip()
            else claim.kind
        )
        status = "pending"
        if semantic_status_by_index is not None:
            raw = str(semantic_status_by_index.get(index, "pending"))
            status = raw if raw in _SEMANTIC_STATUSES else "uncertain"
        claim_id, revision_id = await store.insert_claim_with_revision(
            scope,
            text=claim.text,
            kind=kind,
            attribution=claim.attribution,
            predicate="states",
            object_value={"text": claim.text},
            conditions={
                "listed": list(claim.conditions),
                "unknown": [],
            },
            assessment={"independence": "unknown"},
            input_manifest=input_manifest,
        )
        commit.claim_revision_ids.append(revision_id)
        for location, evidence in zip(validated.locations, claim.evidence, strict=True):
            evidence_id = await store.insert_evidence(
                scope,
                claim_revision_id=revision_id,
                parsed_artifact_id=parse_id,
                block_id=location.block_id,
                start_char=location.start_char,
                end_char=location.end_char,
                exact_quote=location.exact_quote,
                relation=evidence.relation,
                semantic_support_status=status,
                source_family_id=family_id,
                extraction_run_id=extraction_run_id,
            )
            commit.evidence_ids.append(evidence_id)
        del claim_id
    return commit


@dataclass(slots=True)
class EventResolution:
    event_id: UUID
    created: bool
    revision_id: UUID
    merged_into_existing: bool
    possible_duplicate_recorded: bool = False


async def resolve_event(
    store: KnowledgeStore,
    scope: IndustryScope,
    proposal: EventProposalInput,
    *,
    input_manifest: dict,
    similar_candidate_event_id: UUID | None = None,
) -> EventResolution:
    """One proposal → auto-merge or new event (05 §2 boolean gates only).

    A strong key that matches an existing event links claims/evidence to
    it (reposts enrich the event; they do not create a revision). No
    strong key ⇒ independent new event, optionally flagged against a
    similarity-suggested candidate as possible_duplicate (候选 only —
    近似语义从不自动合并).
    """
    key: StrongKey | None = build_identity_key(
        proposal.event_type, proposal.identity_fields
    )
    canonical = key_string(key) if key is not None else None

    if canonical is not None:
        existing_id = await store.find_event_by_key(scope, canonical)
        if existing_id is not None:
            await store.link_event_claims(
                scope,
                event_id=existing_id,
                claim_revision_ids=list(proposal.claim_revision_ids),
            )
            return EventResolution(
                event_id=existing_id,
                created=False,
                revision_id=UUID(int=0),  # linked, no new revision
                merged_into_existing=True,
            )

    event_id, revision_id = await store.insert_event_with_revision(
        scope,
        event_type=proposal.event_type,
        title=proposal.title,
        summary=proposal.summary,
        identity_key=canonical,
        rationale=(
            "strong identity key: " + canonical
            if canonical
            else "no strong identity key; independent event (05 §2)"
        ),
        input_manifest=input_manifest,
        occurred=dict(proposal.occurred_time) or None,
        published=None,
        effective=None,
    )
    await store.link_event_claims(
        scope, event_id=event_id, claim_revision_ids=list(proposal.claim_revision_ids)
    )
    flagged = False
    if canonical is None and similar_candidate_event_id is not None:
        await store.record_possible_duplicate(
            scope,
            event_id=event_id,
            candidate_event_id=similar_candidate_event_id,
            reason="weak identity; similarity candidate only",
            input_manifest=input_manifest,
        )
        flagged = True
    return EventResolution(
        event_id=event_id,
        created=True,
        revision_id=revision_id,
        merged_into_existing=False,
        possible_duplicate_recorded=flagged,
    )


async def apply_event_merge(
    store: KnowledgeStore,
    scope: IndustryScope,
    *,
    canonical_id: UUID,
    merged_id: UUID,
    expected_row_versions: Mapping[UUID, int],
    review_id: UUID,
    rationale: str,
    membership_snapshot: dict,
) -> UUID:
    """Merge two events under expected row_versions (05 §4).

    The snapshot preserves undo; the old id stays resolvable via
    merged_into. Throws :class:`MergeConflict` before any write when the
    versions moved — the client re-reads and retries.
    """
    for event_id, expected in expected_row_versions.items():
        actual = await store.event_row_version(scope, event_id)
        if actual != expected:
            raise MergeConflict(
                f"event {event_id} moved: expected row_version"
                f" {expected}, found {actual}"
            )
    if canonical_id == merged_id:
        raise KnowledgeError("cannot merge an event into itself")
    merge_id = await store.save_merge(
        scope,
        review_id=review_id,
        canonical_id=canonical_id,
        merged_id=merged_id,
        membership_snapshot=membership_snapshot,
        rationale=rationale,
    )
    await store.mark_merged(scope, event_id=merged_id, merged_into_id=canonical_id)
    return merge_id


async def undo_event_merge(
    store: KnowledgeStore, scope: IndustryScope, *, merge_id: UUID
) -> None:
    """Undo a merge — unless it was modified afterwards (EVT-04).

    Post-merge human modifications win: undo then raises
    :class:`UndoConflict` and the store keeps a compensation proposal
    for review instead of overwriting the newer edits (05 §4: 不能覆盖
    后续修改).
    """
    modifications = await store.post_merge_modifications(scope, merge_id)
    if modifications:
        compensation = {
            "merge_id": str(merge_id),
            "post_merge_modifications": modifications,
            "action": "manual re-split required",
        }
        await store.undo_merge(scope, merge_id=merge_id, compensation=compensation)
        raise UndoConflict(
            f"{modifications} modification(s) landed after the merge;"
            " compensation proposal recorded"
        )
    await store.undo_merge(scope, merge_id=merge_id, compensation=None)


# ---------------------------------------------------------------------------
# In-memory double (offline tests; the shape the SQLAlchemy adapter follows)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InMemoryKnowledgeStore:
    families: dict[str, UUID] = field(default_factory=dict)
    claims: dict[UUID, dict] = field(default_factory=dict)
    claim_revisions: dict[UUID, dict] = field(default_factory=dict)
    evidence: dict[UUID, dict] = field(default_factory=dict)
    events: dict[UUID, dict] = field(default_factory=dict)
    event_revisions: dict[UUID, dict] = field(default_factory=dict)
    event_keys: dict[str, UUID] = field(default_factory=dict)
    event_claims: dict[UUID, list[UUID]] = field(default_factory=dict)
    duplicates: list[dict] = field(default_factory=list)
    merges: dict[UUID, dict] = field(default_factory=dict)
    post_merge_edit_count: dict[UUID, int] = field(default_factory=dict)
    undone: list[UUID] = field(default_factory=list)

    async def find_source_family(
        self, scope: IndustryScope, origin_ref: str
    ) -> UUID | None:
        return self.families.get(origin_ref)

    async def insert_source_family(
        self, scope: IndustryScope, *, origin_ref: str, label: str
    ) -> UUID:
        family_id = uuid4()
        self.families[origin_ref] = family_id
        return family_id

    async def insert_claim_with_revision(
        self,
        scope: IndustryScope,
        *,
        text: str,
        kind: str,
        attribution: str | None,
        predicate: str,
        object_value: dict,
        conditions: dict,
        assessment: dict,
        input_manifest: dict,
    ) -> tuple[UUID, UUID]:
        claim_id, revision_id = uuid4(), uuid4()
        self.claims[claim_id] = {"row_version": 1, "state": "active"}
        self.claim_revisions[revision_id] = {
            "claim_id": claim_id,
            "version": 1,
            "text": text,
            "kind": kind,
            "attribution": attribution,
            "input_manifest": input_manifest,
        }
        return claim_id, revision_id

    async def insert_evidence(
        self,
        scope: IndustryScope,
        *,
        claim_revision_id: UUID,
        parsed_artifact_id: UUID,
        block_id: str,
        start_char: int,
        end_char: int,
        exact_quote: str,
        relation: str,
        semantic_support_status: str,
        source_family_id: UUID | None,
        extraction_run_id: UUID,
    ) -> UUID:
        evidence_id = uuid4()
        self.evidence[evidence_id] = {
            "claim_revision_id": claim_revision_id,
            "parsed_artifact_id": parsed_artifact_id,
            "block_id": block_id,
            "start_char": start_char,
            "end_char": end_char,
            "exact_quote": exact_quote,
            "quote_sha256": _quote_sha256(exact_quote),
            "relation": relation,
            "semantic_support_status": semantic_support_status,
            "source_family_id": source_family_id,
            "extraction_run_id": extraction_run_id,
        }
        return evidence_id

    async def find_event_by_key(
        self, scope: IndustryScope, canonical_key: str
    ) -> UUID | None:
        return self.event_keys.get(canonical_key)

    async def insert_event_with_revision(
        self,
        scope: IndustryScope,
        *,
        event_type: str,
        title: str,
        summary: str,
        identity_key: str | None,
        rationale: str,
        input_manifest: dict,
        occurred: Mapping[str, Any] | None = None,
        published: Mapping[str, Any] | None = None,
        effective: Mapping[str, Any] | None = None,
        first_discovered_at: datetime | None = None,
    ) -> tuple[UUID, UUID]:
        from intel.domain.time import occurred_span

        def _validated(
            raw: Mapping[str, Any] | None,
        ) -> tuple[dict[str, Any], tuple[datetime | None, datetime | None]]:
            payload = dict(raw) if raw else {"precision": "unknown"}
            try:
                tv = TimeValue.model_validate(payload)
            except ValueError:
                # Downgrade-to-unknown, never crash the commit (03 §5).
                tv = TimeValue(precision="unknown")
            return (
                tv.model_dump(mode="json"),
                occurred_span(tv),
            )

        event_id, revision_id = uuid4(), uuid4()
        occurred_json, (start, end) = _validated(occurred)
        published_json, _ = _validated(published)
        effective_json, _ = _validated(effective)
        discovered = first_discovered_at or _now()
        self.events[event_id] = {
            "event_type": event_type,
            "row_version": 1,
            "merged_into_id": None,
            "current_revision_id": revision_id,
        }
        self.event_revisions[revision_id] = {
            "event_id": event_id,
            "version": 1,
            "title": title,
            "summary": summary,
            "identity_key": identity_key,
            "rationale": rationale,
            "occurred_time": occurred_json,
            "published_time": published_json,
            "effective_time": effective_json,
            "occurred_start": start,
            "occurred_end": end,
            "first_discovered_at": discovered,
        }
        if identity_key is not None:
            self.event_keys[identity_key] = event_id
        return event_id, revision_id

    async def link_event_claims(
        self,
        scope: IndustryScope,
        *,
        event_id: UUID,
        claim_revision_ids: list[UUID],
    ) -> None:
        self.event_claims.setdefault(event_id, []).extend(claim_revision_ids)

    async def record_possible_duplicate(
        self,
        scope: IndustryScope,
        *,
        event_id: UUID,
        candidate_event_id: UUID | None,
        reason: str,
        input_manifest: dict,
    ) -> UUID:
        duplicate_id = uuid4()
        self.duplicates.append(
            {
                "id": duplicate_id,
                "event_id": event_id,
                "candidate_event_id": candidate_event_id,
                "reason": reason,
            }
        )
        return duplicate_id

    async def event_row_version(self, scope: IndustryScope, event_id: UUID) -> int:
        return self.events[event_id]["row_version"]

    async def save_merge(
        self,
        scope: IndustryScope,
        *,
        review_id: UUID,
        canonical_id: UUID,
        merged_id: UUID,
        membership_snapshot: dict,
        rationale: str,
    ) -> UUID:
        merge_id = uuid4()
        self.merges[merge_id] = {
            "review_id": review_id,
            "canonical_id": canonical_id,
            "merged_id": merged_id,
            "membership_snapshot": membership_snapshot,
            "rationale": rationale,
            "compensation": None,
            "undone": False,
        }
        return merge_id

    async def mark_merged(
        self, scope: IndustryScope, *, event_id: UUID, merged_into_id: UUID
    ) -> None:
        self.events[event_id]["merged_into_id"] = merged_into_id
        self.events[event_id]["row_version"] += 1
        self.post_merge_edit_count.setdefault(merged_into_id, 0)

    async def post_merge_modifications(
        self, scope: IndustryScope, merge_id: UUID
    ) -> int:
        merge = self.merges[merge_id]
        return self.post_merge_edit_count.get(merge["canonical_id"], 0)

    async def undo_merge(
        self, scope: IndustryScope, *, merge_id: UUID, compensation: dict | None
    ) -> None:
        merge = self.merges[merge_id]
        merge["compensation"] = compensation
        if compensation is None:
            merge["undone"] = True
            merged_id = merge["merged_id"]
            self.events[merged_id]["merged_into_id"] = None
            self.events[merged_id]["row_version"] += 1
            self.undone.append(merge_id)
