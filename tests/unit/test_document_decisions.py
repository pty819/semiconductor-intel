"""Document topic-decisions, revision scoping, and report/message bodies."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from intel.contracts.models import DocumentTopicCommand
from intel.repositories.conversations import message_to_view
from intel.repositories.documents import (
    SqlAlchemyDocumentRepository,
    list_revisions_stmt,
)
from intel.repositories.reports import report_revision_to_view
from intel.services.errors import VersionConflict


class TestMessageAndReportBodies:
    def test_message_content_becomes_a_block(self) -> None:
        row = SimpleNamespace(
            id=uuid4(),
            parent_message_id=None,
            turn_index=1,
            role="user",
            status="pending",
            content="均匀性如何",
            citation_manifest={},
            as_of=None,
            job_id=None,
        )
        view = message_to_view(row)
        assert view.blocks[0].text == "均匀性如何"
        assert view.blocks[0].kind == "unknown"

    def test_assistant_manifest_blocks_and_citations_round_trip(self) -> None:
        evidence_id = uuid4()
        parse_id = uuid4()
        document_id = uuid4()
        row = SimpleNamespace(
            id=uuid4(),
            parent_message_id=uuid4(),
            turn_index=1,
            role="assistant",
            status="ready",
            content="ignored when blocks present",
            citation_manifest={
                "blocks": [
                    {
                        "text": "E-500 商用",
                        "kind": "fact",
                        "citation_ids": ["ev-1"],
                    }
                ],
                "citations": [
                    {
                        "id": "ev-1",
                        "evidence_id": str(evidence_id),
                        "parse_id": str(parse_id),
                        "document_id": str(document_id),
                    }
                ],
            },
            as_of=None,
            job_id=uuid4(),
        )
        view = message_to_view(row)
        assert [b.text for b in view.blocks] == ["E-500 商用"]
        assert view.citations[0].id == "ev-1"

    def test_report_sections_map_to_blocks(self) -> None:
        report = SimpleNamespace(id=uuid4(), title="日报", type="daily")
        revision = SimpleNamespace(
            id=uuid4(),
            as_of=datetime(2026, 9, 20, tzinfo=UTC),
            content=[
                {
                    "title": "进展",
                    "statements": [
                        {"text": "有据陈述", "citations": ["ev-1"]},
                    ],
                }
            ],
            citations=[],
            coverage={"status": "complete", "processed": 1, "failed": 0, "gaps": []},
            input_manifest={"stale": False},
        )
        view = report_revision_to_view(report, revision)
        assert view["blocks"][0]["text"] == "有据陈述"
        assert view["blocks"][0]["citation_ids"] == ["ev-1"]


class TestRevisionSqlIsDocumentScoped:
    def test_list_revisions_joins_capture_document_and_binding(self) -> None:
        document_id = uuid4()
        industry_id = uuid4()
        owner_id = uuid4()
        compiled = str(
            list_revisions_stmt(
                owner_id=owner_id, industry_id=industry_id, document_id=document_id
            ).compile(dialect=postgresql.dialect())
        )
        lowered = compiled.lower()
        assert "parsed_artifacts" in lowered
        assert "captures" in lowered
        assert "industry_documents" in lowered
        assert "document_id" in lowered


class RecordingConn:
    def __init__(self, *, row_version: int = 3, update_count: int = 1) -> None:
        self.row_version = row_version
        self.update_count = update_count
        self.statements: list[str] = []
        self.industry_document_id = uuid4()
        self.topic_revision_id = uuid4()

    async def execute(self, stmt, params=None):
        text = str(stmt)
        self.statements.append(text)
        lowered = text.lower()
        if "current_setting" in lowered:
            return SimpleNamespace(one_or_none=lambda: ("owner",), rowcount=1)
        if "set_config" in lowered:
            return SimpleNamespace(rowcount=1)
        if (
            lowered.lstrip().startswith("update")
            or "update industry_documents" in lowered
        ):
            return SimpleNamespace(rowcount=self.update_count)
        if "industry_documents" in lowered and "insert" not in lowered:
            return _ScalarResult(
                SimpleNamespace(
                    id=self.industry_document_id,
                    document_id=uuid4(),
                    row_version=self.row_version,
                    association_reason="",
                )
            )
        if "from topics" in lowered:
            return _ScalarResult(
                SimpleNamespace(id=uuid4(), current_revision_id=self.topic_revision_id)
            )
        return _ScalarResult(None)


class _ScalarResult:
    def __init__(self, row) -> None:
        self._row = row
        self.rowcount = 1

    def scalars(self):
        return self

    def first(self):
        return self._row


class TestTopicDecisionOptimisticConcurrency:
    async def test_stale_expected_version_raises_version_conflict(self) -> None:
        conn = RecordingConn(row_version=3, update_count=0)
        repo = SqlAlchemyDocumentRepository(
            conn,  # type: ignore[arg-type]
            __import__(
                "intel.repositories.base", fromlist=["IndustryScope"]
            ).IndustryScope(owner_id=uuid4(), industry_id=uuid4()),
        )
        body = DocumentTopicCommand(
            expected_version=1,
            topic_id=uuid4(),
            relevance="direct",
            rationale="人工锁定",
            lock=True,
        )
        with pytest.raises(VersionConflict) as caught:
            await repo.apply_topic_decision(uuid4(), body)
        assert caught.value.details["current_version"] == 3

    async def test_matching_version_writes_document_topic(self) -> None:
        conn = RecordingConn(row_version=1, update_count=1)
        from intel.repositories.base import IndustryScope

        repo = SqlAlchemyDocumentRepository(
            conn,  # type: ignore[arg-type]
            IndustryScope(owner_id=uuid4(), industry_id=uuid4()),
        )
        body = DocumentTopicCommand(
            expected_version=1,
            topic_id=uuid4(),
            relevance="direct",
            rationale="人工锁定",
            lock=True,
        )
        result = await repo.apply_topic_decision(uuid4(), body)
        joined = "\n".join(conn.statements).lower()
        assert "document_topics" in joined
        assert "industry_documents" in joined
        assert result["relevance"] == "direct"
        assert result["row_version"] == 2
