"""Evolution construction: edge gates, cycles, insufficiency, staleness (05 §6).

The six-step workflow's deterministic skeleton — the model composes
stages, Python enforces the graph contract:

- **EVO-01**: an edge without a basis is not an edge — ``basis`` must be
  ``explicit`` or ``inferred`` with non-empty evidence references. Time
  proximity alone never justifies ``validates``/``refutes``/``applies``
  (05 §6: 不能仅根据发布时间建立关系), so inferred edges of those types
  additionally require evidence refs.
- **EVO-02**: the ``updates``/``replaces``/``extends`` chain must stay
  acyclic; a proposal closing a cycle is rejected whole, not trimmed.
- ``parallel`` is undirected: endpoints are normalized (sorted) so A‖B
  and B‖A dedupe, and parallel edges never enter cycle detection — they
  are not squeezed into a directed DAG.
- **Insufficiency is a status**: with no verified edges (or coverage under
  the floor) the build records ``insufficient_evidence`` — 资料不足 —
  and the timeline keeps living beside it.
- **Staleness is data**: corrections/new material mark related
  evolutions stale; a single debounced rebuild job per topic consumes the
  marks (default 5 minutes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

__all__ = [
    "CYCLE_RELATIONS",
    "CycleRejected",
    "EdgeDraft",
    "EvolutionBuildResult",
    "StalePolicy",
    "build_evolution",
    "detect_cycles",
    "normalize_parallel",
    "validate_edges",
]

#: Directed relations forming the evolution chain (05 §6 step 5).
CYCLE_RELATIONS = frozenset({"updates", "replaces", "extends"})

#: Relations that timing alone can never justify.
_EVIDENCE_REQUIRED = frozenset({"validates", "refutes", "applies"})


class CycleRejected(Exception):
    """The proposed updates/replaces/extends chain closes a cycle (EVO-02)."""

    def __init__(self, cycle: list[str]) -> None:
        super().__init__(f"relation cycle: {' -> '.join(cycle)}")
        self.cycle = cycle


@dataclass(frozen=True, slots=True)
class EdgeDraft:
    """One candidate edge from the composition step."""

    from_node: str
    to_node: str
    relation: str
    basis: str = ""  # "explicit" | "inferred" | "" (missing)
    rationale: str = ""
    evidence_refs: tuple[str, ...] = ()

    @property
    def normalized(self) -> tuple[str, str, str]:
        if self.relation == "parallel":
            low, high = sorted((self.from_node, self.to_node))
            return (low, high, self.relation)
        return (self.from_node, self.to_node, self.relation)


def validate_edges(
    drafts: list[EdgeDraft],
) -> tuple[list[EdgeDraft], list[dict[str, str]]]:
    """Apply the EVO-01 gates; returns (kept edges, dropped reasons)."""
    kept: list[EdgeDraft] = []
    dropped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for draft in drafts:
        if draft.from_node == draft.to_node:
            dropped.append(
                {"edge": f"{draft.from_node}->{draft.to_node}", "reason": "self_loop"}
            )
            continue
        if draft.basis not in ("explicit", "inferred"):
            dropped.append(
                {"edge": f"{draft.from_node}->{draft.to_node}", "reason": "no_basis"}
            )
            continue
        if (
            draft.basis == "inferred"
            and draft.relation in _EVIDENCE_REQUIRED
            and not draft.evidence_refs
        ):
            dropped.append(
                {
                    "edge": f"{draft.from_node}->{draft.to_node}",
                    "reason": "inferred_relation_without_evidence"
                    " (timing alone never justifies"
                    f" {draft.relation})",
                }
            )
            continue
        key = draft.normalized
        if key in seen:
            dropped.append(
                {"edge": f"{key[0]}-{key[1]}", "reason": "duplicate_parallel"}
            )
            continue
        seen.add(key)
        kept.append(draft)
    return kept, dropped


def detect_cycles(edges: list[EdgeDraft]) -> None:
    """Reject the proposal if the directed chain closes a cycle (EVO-02).

    Parallel edges are undirected and excluded; everything else with a
    chain relation forms the directed graph. DFS back-edge detection.
    """
    graph: dict[str, list[str]] = {}
    for edge in edges:
        if edge.relation in CYCLE_RELATIONS:
            graph.setdefault(edge.from_node, []).append(edge.to_node)

    state: dict[str, int] = {}  # 0=unvisited, 1=in-stack, 2=done
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for neighbor in graph.get(node, ()):
            if state.get(neighbor, 0) == 1:
                cycle = stack[stack.index(neighbor) :] + [neighbor]
                raise CycleRejected(cycle)
            if state.get(neighbor, 0) == 0:
                visit(neighbor)
        stack.pop()
        state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            visit(node)


def normalize_parallel(from_node: str, to_node: str) -> tuple[str, str]:
    """Canonical undirected endpoints for a parallel edge."""
    low, high = sorted((from_node, to_node))
    return (low, high)


@dataclass(slots=True)
class EvolutionBuildResult:
    status: str  # "ok" | "insufficient_evidence"
    stages: list[dict[str, Any]] = field(default_factory=list)
    edges: list[EdgeDraft] = field(default_factory=list)
    dropped_edges: list[dict[str, str]] = field(default_factory=list)
    input_manifest: dict[str, Any] = field(default_factory=dict)
    revision_id: UUID = field(default_factory=uuid4)


def build_evolution(
    *,
    stages: list[dict[str, Any]],
    edge_drafts: list[EdgeDraft],
    input_manifest: dict[str, Any],
    coverage_ratio: float = 1.0,
    min_coverage: float = 0.0,
) -> EvolutionBuildResult:
    """Compose one immutable evolution build (05 §6 steps 3-6).

    Insufficiency (资料不足) is recorded as a status — the timeline keeps
    existing; the evolution view says why it cannot draw the line.
    """
    edges, dropped = validate_edges(edge_drafts)
    detect_cycles(edges)
    verified = [edge for edge in edges if edge.basis == "explicit"]
    insufficient = (not verified and not edges) or coverage_ratio < min_coverage
    return EvolutionBuildResult(
        status="insufficient_evidence" if insufficient else "ok",
        stages=stages,
        edges=edges,
        dropped_edges=dropped,
        input_manifest=input_manifest,
    )


@dataclass(frozen=True, slots=True)
class StalePolicy:
    """Debounced single-job rebuild policy (05 §6, 默认 5 分钟)."""

    debounce: timedelta = timedelta(minutes=5)
    one_job_per_topic: bool = True

    def should_rebuild(
        self,
        *,
        marked_at: datetime,
        now: datetime,
        running_jobs_for_topic: int = 0,
    ) -> bool:
        if self.one_job_per_topic and running_jobs_for_topic > 0:
            return False
        return (now - marked_at) >= self.debounce
