"""Report composition + citation coverage validation (05 §8, 08 §4).

A report is only publishable when every claim-level statement carries a
citation from its evidence packet (引用校验 coverage=100%): uncited
assertions are dropped to the report's own “未支持陈述” list, never
silently published. Snapshots are immutable revisions; recomposition on
new material marks prior revisions stale without deleting them (旧版本
仍可打开).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

__all__ = [
    "ReportComposition",
    "ReportKind",
    "validate_citation_coverage",
]


class ReportKind:
    DAILY = "daily"
    TOPIC = "topic"
    INVESTIGATION = "investigation"


@dataclass(slots=True)
class ReportComposition:
    """One validated report snapshot ready to persist as a revision."""

    kind: str
    title: str
    sections: list[dict[str, Any]] = field(default_factory=list)
    unsupported_statements: list[str] = field(default_factory=list)
    citation_coverage: float = 0.0
    stale: bool = False
    stale_reason: str = ""
    revision_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def publishable(self) -> bool:
        """coverage==1.0 AND nothing unsupported slipped into sections."""
        return self.citation_coverage == 1.0 and not self.unsupported_statements


def validate_citation_coverage(
    *,
    kind: str,
    title: str,
    sections: list[dict[str, Any]],
    allowed_citation_ids: frozenset[str] | set[str],
) -> ReportComposition:
    """Split statements into cited/unsupported; compute coverage.

    A statement is supported iff its ``citations`` list is non-empty AND
    every cited id is in the packet's allowed set — an out-of-packet
    citation is unsupported (引用语义不匹配: the fix loop drops or
    re-cites, never trusts).
    """
    allowed = frozenset(allowed_citation_ids)
    kept: list[dict[str, Any]] = []
    unsupported: list[str] = []
    total = 0
    cited = 0
    for section in sections:
        kept_statements = []
        for statement in section.get("statements", []):
            total += 1
            citations = list(statement.get("citations", []))
            if citations and set(citations) <= allowed:
                cited += 1
                kept_statements.append(statement)
            else:
                unsupported.append(str(statement.get("text", "")))
        kept.append({**section, "statements": kept_statements})
    coverage = (cited / total) if total else 1.0
    return ReportComposition(
        kind=kind,
        title=title,
        sections=kept,
        unsupported_statements=unsupported,
        citation_coverage=coverage,
    )
