"""Unit tests: SQLAlchemy KnowledgeStore adapter (protocol + bind/SQL shape)."""

from __future__ import annotations

from uuid import uuid4

from intel.repositories.base import IndustryScope
from intel.repositories.knowledge import SqlAlchemyKnowledgeStore
from intel.services.knowledge import KnowledgeStore

OWNER = uuid4()
INDUSTRY = uuid4()
SCOPE = IndustryScope(owner_id=OWNER, industry_id=INDUSTRY)


class FakeResult:
    def __init__(self, value: str | None = None) -> None:
        self._value = value
        self.rowcount = 1

    def first(self):
        return None

    def one_or_none(self):
        if self._value is None:
            return None
        return (self._value,)

    def mappings(self):
        return []

    def scalars(self):
        return []

    def all(self):
        return []


class FakeConn:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        compiled = str(stmt)
        if "current_setting" in compiled:
            return FakeResult(str(OWNER))
        return FakeResult()


class TestSqlAlchemyKnowledgeStore:
    def test_implements_protocol(self) -> None:
        store = SqlAlchemyKnowledgeStore(FakeConn(), SCOPE)
        assert isinstance(store, KnowledgeStore)

    async def test_find_and_insert_bind_rls_and_do_not_crash(self) -> None:
        conn = FakeConn()
        store = SqlAlchemyKnowledgeStore(conn, SCOPE)
        assert await store.find_source_family(SCOPE, "https://origin.example") is None
        family_id = await store.insert_source_family(
            SCOPE, origin_ref="https://origin.example", label="origin"
        )
        assert family_id is not None
        claim_id, revision_id = await store.insert_claim_with_revision(
            SCOPE,
            text="陈述",
            kind="source_statement",
            attribution="官方",
            predicate="states",
            object_value={"text": "陈述"},
            conditions={"listed": [], "unknown": []},
            assessment={"independence": "unknown"},
            input_manifest={"parse": "1"},
        )
        assert claim_id != revision_id
        evidence_id = await store.insert_evidence(
            SCOPE,
            claim_revision_id=revision_id,
            parsed_artifact_id=uuid4(),
            block_id="b001",
            start_char=0,
            end_char=2,
            exact_quote="陈述",
            relation="supports",
            semantic_support_status="pending",
            source_family_id=family_id,
            extraction_run_id=uuid4(),
        )
        assert evidence_id
        event_id, event_rev = await store.insert_event_with_revision(
            SCOPE,
            event_type="commercial_announcement",
            title="采购",
            summary="摘要",
            identity_key="k",
            rationale="strong key",
            input_manifest={"spec": "evt"},
        )
        await store.link_event_claims(
            SCOPE, event_id=event_id, claim_revision_ids=[revision_id]
        )
        assert conn.statements  # RLS bind + writes
        del event_rev
