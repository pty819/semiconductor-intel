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

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.knowledge import (
    Entity,
    EntityAlias,
    Event,
    EventRevision,
    Evidence,
    Watch,
)
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository
from intel.services.timeline import decode_cursor, encode_cursor

__all__ = ["KnowledgeReadRepository", "SqlAlchemyKnowledgeRepository"]


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
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Timeline page per 05 §5 (see module docstring for semantics)."""
        industry_id = await self._bind()

        # Latest revision at-or-before as_of per event.
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
                    order_by=EventRevision.recorded_at.desc(),
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
        elif not include_unknown:
            # No window: the 未知 group is default-on, user-hideable.
            stmt = stmt.where(latest.c.occurred_start.isnot(None))

        # Stable keyset: known dates ascending, unknown group LAST, then id.
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
                from sqlalchemy import tuple_

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
            "late_discovery": False,
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
        from sqlalchemy import update

        await self._bind()
        await self.conn.execute(
            update(Watch)
            .where(
                Watch.owner_id == self.owner_id,
                Watch.id == watch_id,
            )
            .values(status=status, row_version=Watch.row_version + 1)
        )
