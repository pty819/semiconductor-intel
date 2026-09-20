"""Knowledge read repository: timeline/evidence/entities/watches (Task 13).

Reads for the spec-08 §3 routes over the Task-3 schema. The timeline
query implements 05 §5 IN SQL with the same semantics the offline
service pins (tests/unit/test_timeline_asof.py):

- ``as_of`` selects, per event, the latest revision RECORDED at-or-before
  the timestamp (TIM-03) AND requires ``first_discovered_at <= as_of``
  (TIM-02: 今天发现的去年事件在历史视图不可见);
- the range filter intersects the revision's [occurred_start,
  occurred_end) span with the window (TIM-01; instants carry
  occurred_end NULL and compare by occurred_start);
- unknown dates (occurred_start NULL) never match ranged filters; the
  separate 未知 group is included only on request and sorts last;
- pagination is keyset on (unknown_group, occurred_start, event_id) —
  the stable (effective_sort_key, event_id) cursor.

Write paths (entities, watches) go through the same RLS-bound
connection; event/claim writes stay in services/knowledge.py's commit
flow. Join-dependent EventCard fields (interpretations, citations,
generation_refs) are v1-empty pending the Task-14 generation wiring —
documented deviation, ledgered.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.contracts.models import TimeValue
from intel.db.models.conversation import ReviewTask
from intel.db.models.knowledge import (
    Claim,
    ClaimRevision,
    Entity,
    EntityAlias,
    Event,
    EventLifecycleHistory,
    EventMergeOperation,
    EventRevision,
    EventRevisionClaim,
    Evidence,
    SourceFamily,
    Watch,
)
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository
from intel.services.timeline import decode_cursor, encode_cursor

__all__ = [
    "KnowledgeReadRepository",
    "SqlAlchemyKnowledgeRepository",
    "SqlAlchemyKnowledgeStore",
]

_UNKNOWN_TIME = {"precision": "unknown"}


def _validated_time(
    raw: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[datetime | None, datetime | None]]:
    """Serialized TimeValue → (validated JSONB payload, occurred projection).

    Precision rules are TimeValue's own validators (03 §5); structurally
    invalid payloads downgrade to ``unknown`` instead of crashing the
    commit. The projection follows :func:`intel.domain.time.occurred_span`
    — exactly what the ORM's ``occurred_start``/``occurred_end`` columns
    mean.
    """
    from intel.domain.time import occurred_span

    payload = dict(raw) if raw else dict(_UNKNOWN_TIME)
    try:
        tv = TimeValue.model_validate(payload)
    except ValueError:
        tv = TimeValue(precision="unknown")
        payload = dict(_UNKNOWN_TIME)
    return payload, occurred_span(tv)


def _late_discovery(
    occurred_start: datetime | None,
    occurred_end: datetime | None,
    first_discovered: datetime | None,
) -> bool:
    """TIM-02 迟到标记: discovery landed after the occurred span closed.

    Instants (``occurred_end`` NULL) get a one-day grace so a same-day
    discovery of an announcement is not "late"; unknown dates never carry
    the marker (nothing to compare against).
    """
    if occurred_start is None or first_discovered is None:
        return False
    horizon = occurred_end or (occurred_start + timedelta(days=1))
    return first_discovered > horizon


def _quote_sha256(exact_quote: str) -> str:
    return hashlib.sha256(exact_quote.encode()).hexdigest()


class KnowledgeReadRepository:
    """Protocol surface the Task-13 routes consume."""


class SqlAlchemyKnowledgeRepository(ScopedRepository, KnowledgeReadRepository):
    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    # -- timeline -------------------------------------------------------

    async def list_event_cards(
        self,
        *,
        topic_ids: list[UUID] | None = None,
        event_types: list[str] | None = None,
        window_from: datetime | None = None,
        window_to: datetime | None = None,
        as_of: datetime | None = None,
        include_unknown: bool = False,
        cursor: str | None = None,
        limit: int = 50,
        sort: str = "occurred",
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Timeline page per 05 §5 (see module docstring for semantics).

        ``sort`` selects the 05 §5 axis: ``occurred`` (default — known
        dates ascending, the 未知 group last, user-hideable) or
        ``discovered`` (``first_discovered_at`` ascending; every event is
        discovery-dated, so the unknown-occurred group does not apply and
        is always included).
        """
        industry_id = await self._bind()
        if sort not in ("occurred", "discovered"):
            raise ValueError(f"unknown timeline sort {sort!r}")

        # Latest revision at-or-before as_of per event (id tie-break keeps
        # the pick deterministic on a shared recorded_at timestamp).
        latest = (
            select(
                EventRevision.id.label("revision_id"),
                EventRevision.event_id.label("event_id"),
                EventRevision.title.label("title"),
                EventRevision.summary.label("summary"),
                EventRevision.occurred_start.label("occurred_start"),
                EventRevision.occurred_end.label("occurred_end"),
                EventRevision.occurred_time.label("occurred_time"),
                EventRevision.published_time.label("published_time"),
                EventRevision.effective_time.label("effective_time"),
                EventRevision.first_discovered_at.label("first_discovered"),
                func.row_number()
                .over(
                    partition_by=EventRevision.event_id,
                    order_by=(
                        EventRevision.recorded_at.desc(),
                        EventRevision.id.desc(),
                    ),
                )
                .label("rn"),
            )
            .where(EventRevision.recorded_at <= (as_of or func.now()))
            .subquery()
        )
        latest = select(latest).where(latest.c.rn == 1).subquery()

        stmt = (
            select(
                Event.id.label("event_id"),
                Event.event_type.label("event_type"),
                Event.lifecycle.label("lifecycle"),
                Event.merged_into_id.label("merged_into_id"),
                Event.row_version.label("row_version"),
                latest.c.revision_id.label("revision_id"),
                latest.c.title.label("title"),
                latest.c.summary.label("summary"),
                latest.c.occurred_start.label("occurred_start"),
                latest.c.occurred_end.label("occurred_end"),
                latest.c.occurred_time.label("occurred_time"),
                latest.c.published_time.label("published_time"),
                latest.c.effective_time.label("effective_time"),
                latest.c.first_discovered.label("first_discovered"),
            )
            .select_from(Event)
            .join(latest, latest.c.event_id == Event.id)
            .where(
                Event.owner_id == self.owner_id,
                Event.industry_id == industry_id,
            )
        )
        if event_types:
            stmt = stmt.where(Event.event_type.in_(event_types))
        if as_of is not None:
            # TIM-02: discovery time gates historical views.
            stmt = stmt.where(latest.c.first_discovered <= as_of)
        if window_from is not None and window_to is not None:
            # TIM-01 half-open intersection; instants (end NULL) by start.
            # NULL occurred_start fails these predicates, so unknown dates
            # never match ranged filters (05 §5) — no extra clause needed.
            stmt = stmt.where(
                latest.c.occurred_start < window_to,
                func.coalesce(latest.c.occurred_end, latest.c.occurred_start)
                > window_from,
            )
        elif not include_unknown and sort == "occurred":
            # No window on the occurred axis: the 未知 group is
            # default-on, user-hideable. The discovered axis always
            # includes it — every event is discovery-dated there.
            stmt = stmt.where(latest.c.occurred_start.isnot(None))

        from sqlalchemy import tuple_

        if sort == "discovered":
            # Stable keyset on (first_discovered, event_id).
            stmt = stmt.order_by(
                latest.c.first_discovered.asc().nulls_last(),
                Event.id.asc(),
            ).limit(bindparam("limit"))
            if cursor is not None:
                (group, start_iso), event_id = decode_cursor(cursor)
                discovered = datetime.fromisoformat(start_iso or "0001-01-01T00:00:00")
                stmt = stmt.where(
                    tuple_(latest.c.first_discovered, Event.id) > (discovered, event_id)
                )
        else:
            # Stable keyset: known dates ascending, unknown group LAST, id.
            stmt = stmt.order_by(
                latest.c.occurred_start.is_(None),  # False (known) first
                latest.c.occurred_start.asc().nulls_last(),
                Event.id.asc(),
            ).limit(bindparam("limit"))
            if cursor is not None:
                (group, start_iso), event_id = decode_cursor(cursor)
                if group == 1:  # resume inside the unknown group
                    stmt = stmt.where(
                        latest.c.occurred_start.is_(None), Event.id > event_id
                    )
                else:
                    # (start, id) keyset strictly after the cursor position.
                    stmt = stmt.where(
                        tuple_(latest.c.occurred_start, Event.id)
                        > (
                            datetime.fromisoformat(start_iso),
                            event_id,
                        ),
                        latest.c.occurred_start.isnot(None),
                    )

        rows = (
            await self.conn.execute(
                stmt, {"limit": limit + 1, "industry_id": industry_id}
            )
        ).mappings()
        page = list(rows)[:limit]
        has_more = len(rows) > limit
        cards = [self._card_from_row(row) for row in page]
        next_cursor = None
        if has_more and page:
            row = page[-1]
            if sort == "discovered":
                key = (0, row["first_discovered"].isoformat())
            else:
                start = row["occurred_start"]
                key = (1, "") if start is None else (0, start.isoformat())
            next_cursor = encode_cursor(key, row["event_id"])
        return cards, next_cursor

    def _card_from_row(self, row: Any) -> dict[str, Any]:
        occurred = row["occurred_time"] or {}
        return {
            "id": row["event_id"],
            "revision_id": row["revision_id"],
            "row_version": row["row_version"],
            "title": row["title"],
            "summary": row["summary"],
            "event_type": row["event_type"],
            "occurred_time": occurred,
            "published_time": row["published_time"] or {},
            "effective_time": row["effective_time"] or {},
            "first_discovered_at": row["first_discovered"],
            "interpretations": [],  # v1: join pending Task-14 wiring
            "citations": [],
            "document_count": 0,
            "conflict_state": "unknown",
            "late_discovery": _late_discovery(
                row["occurred_start"], row["occurred_end"], row["first_discovered"]
            ),
            "lifecycle": row["lifecycle"],
            "merged_into_id": row["merged_into_id"],
            "generation_refs": [],
        }

    # -- evidence ---------------------------------------------------------

    async def get_event_card(self, event_id: UUID) -> dict[str, Any] | None:
        cards, _ = await self.list_event_cards(limit=1000)
        for card in cards:
            if card["id"] == event_id:
                return card
        return None

    async def list_evolutions(self, *, topic_id: UUID) -> list[dict[str, Any]]:
        from intel.db.models.conversation import Evolution, EvolutionRevision

        industry_id = await self._bind()
        stmt = (
            select(Evolution, EvolutionRevision)
            .join(
                EvolutionRevision,
                EvolutionRevision.evolution_id == Evolution.id,
            )
            .where(
                Evolution.owner_id == self.owner_id,
                Evolution.industry_id == industry_id,
                Evolution.topic_id == topic_id,
            )
            .order_by(EvolutionRevision.version.desc())
        )
        rows = (await self.conn.execute(stmt)).all()
        return [
            {
                "id": evolution.id,
                "topic_id": evolution.topic_id,
                "revision_id": revision.id,
                "version": revision.version,
                "status": "ok",
            }
            for evolution, revision in rows
        ]

    async def get_evolution(self, evolution_id: UUID) -> dict[str, Any] | None:
        from intel.db.models.conversation import Evolution, EvolutionRevision

        industry_id = await self._bind()
        stmt = (
            select(Evolution, EvolutionRevision)
            .join(
                EvolutionRevision,
                EvolutionRevision.evolution_id == Evolution.id,
            )
            .where(
                Evolution.owner_id == self.owner_id,
                Evolution.industry_id == industry_id,
                Evolution.id == evolution_id,
            )
            .order_by(EvolutionRevision.version.desc())
            .limit(1)
        )
        row = (await self.conn.execute(stmt)).first()
        if row is None:
            return None
        evolution, revision = row
        return {
            "id": evolution.id,
            "topic_id": evolution.topic_id,
            "revision_id": revision.id,
            "version": revision.version,
            "status": "ok",
        }

    async def list_evidence_for_claim(
        self, claim_revision_id: UUID, *, document_id: UUID | None = None
    ) -> list[dict[str, Any]]:
        await self._bind()
        stmt = (
            select(Evidence)
            .where(
                Evidence.owner_id == self.owner_id,
                Evidence.claim_revision_id == claim_revision_id,
            )
            .order_by(Evidence.created_at.asc())
        )
        rows = (await self.conn.execute(stmt)).scalars()
        return [
            {
                "id": row.id,
                "claim_revision_id": row.claim_revision_id,
                "block_id": row.block_id,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "exact_quote": row.exact_quote,
                "locator_status": "verified",
                "semantic_support": row.semantic_support_status,
            }
            for row in rows
        ]

    # -- entities -----------------------------------------------------------

    async def list_entities(self, *, kind: str | None = None) -> list[dict]:
        industry_id = await self._bind()
        stmt = select(Entity).where(
            Entity.owner_id == self.owner_id,
            Entity.industry_id == industry_id,
        )
        if kind:
            stmt = stmt.where(Entity.kind == kind)
        rows = (await self.conn.execute(stmt)).scalars()
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "canonical_name": row.canonical_name,
                "identifiers": row.identifiers,
            }
            for row in rows
        ]

    async def create_entity(
        self, *, kind: str, canonical_name: str, identifiers: dict | None = None
    ) -> dict:
        industry_id = await self._bind()
        entity_id = uuid4()
        await self.conn.execute(
            pg_insert(Entity)
            .values(
                id=entity_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                kind=kind,
                canonical_name=canonical_name,
                identifiers=identifiers or {},
                attributes={},
            )
            .on_conflict_do_nothing()
        )
        return {"id": entity_id, "kind": kind, "canonical_name": canonical_name}

    async def add_alias(
        self, entity_id: UUID, *, alias: str, language: str = "zh"
    ) -> UUID:
        await self._bind()
        alias_id = uuid4()
        await self.conn.execute(
            pg_insert(EntityAlias).values(
                id=alias_id,
                owner_id=self.owner_id,
                industry_id=self.scope.require_industry_id(),
                entity_id=entity_id,
                alias=alias,
                normalized_alias=alias.casefold(),
                language=language,
            )
        )
        return alias_id

    # -- watches -------------------------------------------------------------

    async def list_watches(self) -> list[dict]:
        industry_id = await self._bind()
        rows = (
            await self.conn.execute(
                select(Watch).where(
                    Watch.owner_id == self.owner_id,
                    Watch.industry_id == industry_id,
                )
            )
        ).scalars()
        return [
            {
                "id": row.id,
                "title": row.title,
                "question": row.question,
                "status": row.status,
            }
            for row in rows
        ]

    async def create_watch(self, *, title: str, question: str) -> dict:
        industry_id = await self._bind()
        watch_id = uuid4()
        await self.conn.execute(
            pg_insert(Watch).values(
                id=watch_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                title=title,
                question=question,
                status="active",
                input_manifest={},
            )
        )
        return {
            "id": watch_id,
            "title": title,
            "question": question,
            "status": "active",
        }

    async def set_watch_status(self, watch_id: UUID, *, status: str) -> None:
        await self._bind()
        await self.conn.execute(
            update(Watch)
            .where(
                Watch.owner_id == self.owner_id,
                Watch.id == watch_id,
            )
            .values(status=status, row_version=Watch.row_version + 1)
        )


class SqlAlchemyKnowledgeStore(ScopedRepository):
    """Production KnowledgeStore over the Task-3 schema (RLS-bound)."""

    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    async def find_source_family(
        self, scope: IndustryScope, origin_ref: str
    ) -> UUID | None:
        industry_id = await self._bind()
        stmt = (
            select(SourceFamily.id)
            .where(
                SourceFamily.owner_id == self.owner_id,
                SourceFamily.industry_id == industry_id,
                SourceFamily.origin_url == origin_ref,
            )
            .limit(1)
        )
        row = (await self.conn.execute(stmt)).first()
        return None if row is None else row[0]

    async def insert_source_family(
        self, scope: IndustryScope, *, origin_ref: str, label: str
    ) -> UUID:
        industry_id = await self._bind()
        family_id = uuid4()
        await self.conn.execute(
            pg_insert(SourceFamily).values(
                id=family_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                label=label,
                origin_url=origin_ref,
                basis="explicit_reference",
                status="confirmed",
            )
        )
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
        industry_id = await self._bind()
        claim_id, revision_id = uuid4(), uuid4()
        await self.conn.execute(
            pg_insert(Claim).values(
                id=claim_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                state="active",
                current_revision_id=None,
            )
        )
        await self.conn.execute(
            pg_insert(ClaimRevision).values(
                id=revision_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                claim_id=claim_id,
                version=1,
                text=text,
                kind=kind,
                predicate=predicate,
                object_value=object_value,
                conditions=conditions,
                assessment=assessment,
                input_manifest=input_manifest,
            )
        )
        await self.conn.execute(
            update(Claim)
            .where(Claim.id == claim_id, Claim.owner_id == self.owner_id)
            .values(current_revision_id=revision_id)
        )
        del attribution
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
        industry_id = await self._bind()
        evidence_id = uuid4()
        await self.conn.execute(
            pg_insert(Evidence).values(
                id=evidence_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                claim_revision_id=claim_revision_id,
                parsed_artifact_id=parsed_artifact_id,
                block_id=block_id,
                start_char=start_char,
                end_char=end_char,
                exact_quote=exact_quote,
                quote_sha256=_quote_sha256(exact_quote),
                relation=relation,
                semantic_support_status=semantic_support_status,
                source_family_id=source_family_id,
                extraction_run_id=extraction_run_id,
            )
        )
        return evidence_id

    async def find_event_by_key(
        self, scope: IndustryScope, canonical_key: str
    ) -> UUID | None:
        industry_id = await self._bind()
        stmt = (
            select(Event.id)
            .join(EventRevision, Event.current_revision_id == EventRevision.id)
            .where(
                Event.owner_id == self.owner_id,
                Event.industry_id == industry_id,
                Event.lifecycle == "active",
                EventRevision.identity_key == canonical_key,
            )
            .limit(1)
        )
        row = (await self.conn.execute(stmt)).first()
        return None if row is None else row[0]

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
        industry_id = await self._bind()
        event_id, revision_id = uuid4(), uuid4()
        now = datetime.now(UTC)
        occurred_json, (occurred_start, occurred_end) = _validated_time(occurred)
        published_json, _ = _validated_time(published)
        effective_json, _ = _validated_time(effective)
        await self.conn.execute(
            pg_insert(Event).values(
                id=event_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                event_type=event_type,
                lifecycle="active",
                current_revision_id=None,
            )
        )
        await self.conn.execute(
            pg_insert(EventRevision).values(
                id=revision_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                event_id=event_id,
                version=1,
                title=title,
                summary=summary,
                occurred_time=occurred_json,
                published_time=published_json,
                effective_time=effective_json,
                # Projection columns of occurred_time (03 §8): instants
                # carry NULL end (compare by start); unknown stays NULL.
                occurred_start=occurred_start,
                occurred_end=occurred_end,
                first_discovered_at=first_discovered_at or now,
                identity_key=identity_key,
                status="ok",
                rationale=rationale,
                input_manifest=input_manifest,
            )
        )
        await self.conn.execute(
            update(Event)
            .where(Event.id == event_id, Event.owner_id == self.owner_id)
            .values(current_revision_id=revision_id)
        )
        return event_id, revision_id

    async def link_event_claims(
        self, scope: IndustryScope, *, event_id: UUID, claim_revision_ids: list[UUID]
    ) -> None:
        industry_id = await self._bind()
        row = (
            await self.conn.execute(
                select(Event.current_revision_id).where(
                    Event.id == event_id,
                    Event.owner_id == self.owner_id,
                    Event.industry_id == industry_id,
                )
            )
        ).first()
        if row is None or row[0] is None:
            return
        revision_id = row[0]
        for claim_revision_id in claim_revision_ids:
            await self.conn.execute(
                pg_insert(EventRevisionClaim)
                .values(
                    owner_id=self.owner_id,
                    industry_id=industry_id,
                    event_revision_id=revision_id,
                    claim_revision_id=claim_revision_id,
                    role="supporting",
                )
                .on_conflict_do_nothing()
            )

    async def record_possible_duplicate(
        self,
        scope: IndustryScope,
        *,
        event_id: UUID,
        candidate_event_id: UUID | None,
        reason: str,
        input_manifest: dict,
    ) -> UUID:
        industry_id = await self._bind()
        duplicate_id = uuid4()
        await self.conn.execute(
            pg_insert(ReviewTask).values(
                id=duplicate_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                type="possible_duplicate",
                status="pending",
                proposal={
                    "event_id": str(event_id),
                    "candidate_event_id": (
                        str(candidate_event_id) if candidate_event_id else None
                    ),
                    "reason": reason,
                    "input_manifest": input_manifest,
                },
                expected_versions={},
            )
        )
        return duplicate_id

    async def event_row_version(self, scope: IndustryScope, event_id: UUID) -> int:
        industry_id = await self._bind()
        row = (
            await self.conn.execute(
                select(Event.row_version).where(
                    Event.id == event_id,
                    Event.owner_id == self.owner_id,
                    Event.industry_id == industry_id,
                )
            )
        ).first()
        if row is None:
            raise KeyError(event_id)
        return int(row[0])

    async def _current_revision(self, event_id: UUID, industry_id: UUID) -> UUID:
        row = (
            await self.conn.execute(
                select(Event.current_revision_id).where(
                    Event.id == event_id,
                    Event.owner_id == self.owner_id,
                    Event.industry_id == industry_id,
                )
            )
        ).first()
        if row is None or row[0] is None:
            raise KeyError(event_id)
        return row[0]

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
        industry_id = await self._bind()
        canonical_rev = await self._current_revision(canonical_id, industry_id)
        merged_rev = await self._current_revision(merged_id, industry_id)
        merge_id = uuid4()
        snapshot = {
            **membership_snapshot,
            "review_id": str(review_id),
            "rationale": rationale,
            "canonical_row_version": await self.event_row_version(scope, canonical_id),
            "merged_row_version": await self.event_row_version(scope, merged_id),
        }
        await self.conn.execute(
            pg_insert(EventMergeOperation).values(
                id=merge_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                source_event_id=merged_id,
                target_event_id=canonical_id,
                source_revision_before=merged_rev,
                target_revision_before=canonical_rev,
                membership_snapshot=snapshot,
                operation_status="applied",
            )
        )
        return merge_id

    async def mark_merged(
        self, scope: IndustryScope, *, event_id: UUID, merged_into_id: UUID
    ) -> None:
        industry_id = await self._bind()
        await self.conn.execute(
            update(Event)
            .where(
                Event.id == event_id,
                Event.owner_id == self.owner_id,
                Event.industry_id == industry_id,
            )
            .values(
                merged_into_id=merged_into_id,
                lifecycle="merged",
                row_version=Event.row_version + 1,
            )
        )
        await self.conn.execute(
            pg_insert(EventLifecycleHistory).values(
                id=uuid4(),
                owner_id=self.owner_id,
                industry_id=industry_id,
                event_id=event_id,
                lifecycle="merged",
            )
        )

    async def post_merge_modifications(
        self, scope: IndustryScope, merge_id: UUID
    ) -> int:
        industry_id = await self._bind()
        row = (
            await self.conn.execute(
                select(EventMergeOperation).where(
                    EventMergeOperation.id == merge_id,
                    EventMergeOperation.owner_id == self.owner_id,
                    EventMergeOperation.industry_id == industry_id,
                )
            )
        ).first()
        if row is None:
            return 0
        merge = row[0]
        snapshot = merge.membership_snapshot or {}
        canonical = await self.event_row_version(scope, merge.target_event_id)
        merged = await self.event_row_version(scope, merge.source_event_id)
        extra = 0
        if canonical > int(snapshot.get("canonical_row_version", canonical)):
            extra += canonical - int(snapshot["canonical_row_version"])
        # mark_merged bumps the merged event by 1; anything beyond is a later edit.
        expected_merged = int(snapshot.get("merged_row_version", merged)) + 1
        if merged > expected_merged:
            extra += merged - expected_merged
        return extra

    async def undo_merge(
        self, scope: IndustryScope, *, merge_id: UUID, compensation: dict | None
    ) -> None:
        industry_id = await self._bind()
        row = (
            await self.conn.execute(
                select(EventMergeOperation).where(
                    EventMergeOperation.id == merge_id,
                    EventMergeOperation.owner_id == self.owner_id,
                )
            )
        ).first()
        if row is None:
            return
        merge = row[0]
        if compensation is not None:
            snapshot = dict(merge.membership_snapshot or {})
            snapshot["_compensation"] = compensation
            await self.conn.execute(
                update(EventMergeOperation)
                .where(EventMergeOperation.id == merge_id)
                .values(membership_snapshot=snapshot)
            )
            return
        await self.conn.execute(
            update(Event)
            .where(
                Event.id == merge.source_event_id,
                Event.owner_id == self.owner_id,
                Event.industry_id == industry_id,
            )
            .values(
                merged_into_id=None,
                lifecycle="active",
                row_version=Event.row_version + 1,
            )
        )
        await self.conn.execute(
            update(EventMergeOperation)
            .where(EventMergeOperation.id == merge_id)
            .values(
                operation_status="undone",
                undone_at=datetime.now(UTC),
            )
        )
        await self.conn.execute(
            pg_insert(EventLifecycleHistory).values(
                id=uuid4(),
                owner_id=self.owner_id,
                industry_id=industry_id,
                event_id=merge.source_event_id,
                lifecycle="active",
            )
        )
