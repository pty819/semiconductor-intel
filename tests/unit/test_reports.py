"""Unit tests: report citation coverage + staleness (05 §8; Task 14)."""

from __future__ import annotations

from intel.services.reports import (
    ReportKind,
    validate_citation_coverage,
)

ALLOWED = frozenset({"ev-1", "ev-2"})


def _sections(*statements) -> list[dict]:
    return [{"title": "进展", "statements": list(statements)}]


class TestCitationCoverage:
    def test_full_coverage_is_publishable(self):
        report = validate_citation_coverage(
            kind=ReportKind.DAILY,
            title="日报",
            sections=_sections(
                {"text": "E-500 正式商用", "citations": ["ev-1"]},
                {"text": "产能提升", "citations": ["ev-2", "ev-1"]},
            ),
            allowed_citation_ids=ALLOWED,
        )
        assert report.citation_coverage == 1.0
        assert report.publishable
        assert len(report.sections[0]["statements"]) == 2

    def test_uncited_statement_dropped_not_published(self):
        report = validate_citation_coverage(
            kind=ReportKind.TOPIC,
            title="专题",
            sections=_sections(
                {"text": "有据陈述", "citations": ["ev-1"]},
                {"text": "无据断言"},
            ),
            allowed_citation_ids=ALLOWED,
        )
        assert report.citation_coverage == 0.5
        assert not report.publishable
        assert report.unsupported_statements == ["无据断言"]
        assert len(report.sections[0]["statements"]) == 1

    def test_out_of_packet_citation_is_unsupported(self):
        # 引用不在 EvidencePacket 允许集合内 → 不算支持（引用语义不匹配）。
        report = validate_citation_coverage(
            kind=ReportKind.INVESTIGATION,
            title="调查",
            sections=_sections({"text": "外部记忆陈述", "citations": ["ev-99"]}),
            allowed_citation_ids=ALLOWED,
        )
        assert report.citation_coverage == 0.0
        assert report.unsupported_statements == ["外部记忆陈述"]

    def test_empty_report_vacuously_covered(self):
        report = validate_citation_coverage(
            kind=ReportKind.DAILY,
            title="空日报",
            sections=[],
            allowed_citation_ids=ALLOWED,
        )
        assert report.citation_coverage == 1.0 and report.publishable

    def test_stale_banner_is_data_not_mutation(self):
        report = validate_citation_coverage(
            kind=ReportKind.TOPIC,
            title="旧专题",
            sections=_sections({"text": "x", "citations": ["ev-1"]}),
            allowed_citation_ids=ALLOWED,
        )
        report.stale = True
        report.stale_reason = "new material after 2026-09-20T12:00Z"
        assert report.publishable  # 内容仍完整；stale 只是横幅
        assert report.stale and report.stale_reason
