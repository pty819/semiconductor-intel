"""Unit tests: timeline as_of / interval / cursor + evolution gates (05 §5/§6).

Offline acceptance cases:

- TIM-01: month-precision events intersect any window overlapping the
  month; unknown dates never match ranged filters and sort as their own
  group; half-open end boundary excludes to==end;
- TIM-02: an event discovered today is invisible at as_of=last month;
- TIM-03: after a correction, an as_of before the correction still reads
  the OLD revision's text (revisions are immutable);
- cursor pagination is stable and strictly-after;
- EVO-01: no basis ⇒ no edge; inferred validates/refutes/applies without
  evidence refs ⇒ dropped (timing alone never justifies them);
- EVO-02: updates/replaces/extends cycles rejected whole; parallel edges
  are undirected (normalized, deduped, cycle-free by construction);
- insufficient_evidence status; stale debounce single-job policy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from intel.contracts.models import TimeValue
from intel.services.evolution import (
    CYCLE_RELATIONS,
    CycleRejected,
    EdgeDraft,
    StalePolicy,
    build_evolution,
    detect_cycles,
    normalize_parallel,
    validate_edges,
)
from intel.services.timeline import (
    decode_cursor,
    effective_sort_key,
    encode_cursor,
    intersects,
    page_events,
    select_revision,
    visible_at,
)

T = datetime(2026, 9, 20, tzinfo=UTC)


def _month(year: int, month: int) -> TimeValue:
    start = datetime(year, month, 1, tzinfo=UTC)
    end_month = month + 1 if month < 12 else 1
    end_year = year if month < 12 else year + 1
    end = datetime(end_year, end_month, 1, tzinfo=UTC)
    return TimeValue(start=start, end=end, precision="month")


UNKNOWN = TimeValue(precision="unknown")


class TestTim01Intervals:
    def test_month_precision_covers_whole_month(self) -> None:
        event = _month(2026, 8)
        # Windows anywhere inside August intersect.
        assert intersects(
            event, datetime(2026, 8, 5, tzinfo=UTC), datetime(2026, 8, 6, tzinfo=UTC)
        )
        # Overlapping the month's edge intersects too.
        assert intersects(
            event, datetime(2026, 7, 15, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
        )
        assert intersects(
            event, datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        )
        # Disjoint windows do not.
        assert not intersects(
            event, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
        )
        assert not intersects(
            event, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC)
        )

    def test_half_open_end_exclusive(self) -> None:
        event = _month(2026, 8)
        # [start, end): a window BEGINNING exactly at end does not match.
        assert not intersects(
            event, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
        )

    def test_instant_containment(self) -> None:
        event = TimeValue(start=T, precision="instant")
        assert intersects(event, T, T + timedelta(days=1))
        assert not intersects(event, T + timedelta(seconds=1), T + timedelta(days=1))
        assert intersects(event, T - timedelta(days=1), T + timedelta(days=1))

    def test_unknown_never_matches_ranged_filters(self) -> None:
        assert not intersects(
            UNKNOWN, datetime(1900, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
        )

    def test_unknown_sorts_as_its_own_group_last(self) -> None:
        keys = [
            effective_sort_key(UNKNOWN),
            effective_sort_key(_month(2026, 8)),
            effective_sort_key(TimeValue(start=T, precision="instant")),
        ]
        assert keys[0] > keys[1] and keys[0] > keys[2]
        assert keys[1] < keys[2]  # August < September instant


class TestTim02AsOfDiscovery:
    def test_late_discovery_hidden_from_historical_view(self) -> None:
        occurred_last_year = _month(2025, 6)
        discovered_today = T
        as_of_last_month = T - timedelta(days=30)
        assert occurred_last_year.start < as_of_last_month  # 事件本身在过去
        assert not visible_at(discovered_today, as_of_last_month)
        assert visible_at(discovered_today, T)  # 当前时间线可见（迟到标记）


class TestTim03RevisionImmutability:
    def test_as_of_before_correction_reads_old_text(self) -> None:
        from intel.services.timeline import RevisionLike

        old = RevisionLike(
            id=uuid4(), recorded_at=T - timedelta(days=10), text="吞吐量提升 40%"
        )
        correction = RevisionLike(
            id=uuid4(), recorded_at=T, text="吞吐量提升 14%（更正）"
        )
        assert select_revision([old, correction], T - timedelta(days=1)) is old
        assert select_revision([old, correction], T) is correction
        assert select_revision([old, correction], T - timedelta(days=30)) is None


class TestCursorPagination:
    def test_roundtrip_and_strictly_after(self) -> None:
        key = effective_sort_key(_month(2026, 8))
        event_id = uuid4()
        assert decode_cursor(encode_cursor(key, event_id)) == (key, event_id)

        # Fixed ids: b < c by string order, pinning the equal-key tie-break.
        id_a = UUID("00000000-0000-0000-0000-00000000000a")
        id_b = UUID("00000000-0000-0000-0000-00000000000b")
        id_c = UUID("00000000-0000-0000-0000-00000000000c")
        id_u = UUID("00000000-0000-0000-0000-00000000000d")
        entries = [
            ((0, "2026-08"), id_a, "a"),
            ((0, "2026-09"), id_b, "b"),
            ((0, "2026-09"), id_c, "c"),
            ((1, ""), id_u, "unknown-group"),
        ]
        page, next_cursor = page_events(entries, cursor=None, limit=2)
        assert [item for item in page] == ["a", "b"]
        resumed_key, resumed_id = decode_cursor(next_cursor)
        page2, _ = page_events(entries, cursor=(resumed_key, resumed_id), limit=10)
        # Same sort_key breaks on event id — no skip, no repeat.
        assert [item for item in page2] == ["c", "unknown-group"]

    def test_last_page_has_no_cursor(self) -> None:
        entries = [((0, "2026-08"), uuid4(), "only")]
        page, next_cursor = page_events(entries, cursor=None, limit=5)
        assert page == ["only"] and next_cursor is None


class TestEvo01Edges:
    def test_no_basis_no_edge(self) -> None:
        kept, dropped = validate_edges(
            [EdgeDraft(from_node="n1", to_node="n2", relation="updates")]
        )
        assert kept == []
        assert dropped[0]["reason"] == "no_basis"

    def test_explicit_edge_kept(self) -> None:
        kept, _ = validate_edges(
            [
                EdgeDraft(
                    from_node="n1",
                    to_node="n2",
                    relation="updates",
                    basis="explicit",
                    rationale="论文引用",
                )
            ]
        )
        assert len(kept) == 1

    def test_inferred_semantic_relation_needs_evidence(self) -> None:
        # 仅凭发布时间相邻不能建立 validates/refutes/applies。
        kept, dropped = validate_edges(
            [
                EdgeDraft(
                    from_node="n1",
                    to_node="n2",
                    relation="validates",
                    basis="inferred",
                    rationale="先于发布",
                ),
                EdgeDraft(
                    from_node="n1",
                    to_node="n3",
                    relation="validates",
                    basis="inferred",
                    evidence_refs=("ev-1",),
                ),
            ]
        )
        assert [e.to_node for e in kept] == ["n3"]
        assert "timing alone" in dropped[0]["reason"]

    def test_self_loop_dropped(self) -> None:
        kept, dropped = validate_edges(
            [
                EdgeDraft(
                    from_node="n1", to_node="n1", relation="extends", basis="explicit"
                )
            ]
        )
        assert kept == [] and dropped[0]["reason"] == "self_loop"

    def test_parallel_normalized_and_deduped(self) -> None:
        assert normalize_parallel("b", "a") == ("a", "b")
        kept, dropped = validate_edges(
            [
                EdgeDraft(
                    from_node="b",
                    to_node="a",
                    relation="parallel",
                    basis="inferred",
                    evidence_refs=("ev",),
                ),
                EdgeDraft(
                    from_node="a",
                    to_node="b",
                    relation="parallel",
                    basis="inferred",
                    evidence_refs=("ev",),
                ),
            ]
        )
        assert len(kept) == 1 and dropped[0]["reason"] == "duplicate_parallel"


class TestEvo02Cycles:
    def test_directed_cycle_rejected(self) -> None:
        with pytest.raises(CycleRejected):
            detect_cycles(
                [
                    EdgeDraft(
                        from_node="a", to_node="b", relation="updates", basis="explicit"
                    ),
                    EdgeDraft(
                        from_node="b",
                        to_node="c",
                        relation="replaces",
                        basis="explicit",
                    ),
                    EdgeDraft(
                        from_node="c", to_node="a", relation="extends", basis="explicit"
                    ),
                ]
            )

    def test_parallel_edges_never_create_cycles(self) -> None:
        detect_cycles(
            [
                EdgeDraft(
                    from_node="a", to_node="b", relation="parallel", basis="explicit"
                ),
                EdgeDraft(
                    from_node="b", to_node="a", relation="parallel", basis="explicit"
                ),
            ]
        )  # no raise

    def test_chain_relations_only(self) -> None:
        assert CYCLE_RELATIONS == frozenset({"updates", "replaces", "extends"})
        detect_cycles(
            [
                EdgeDraft(
                    from_node="a",
                    to_node="b",
                    relation="validates",
                    basis="explicit",
                    evidence_refs=("ev",),
                )
            ]
        )  # non-chain relations do not participate


class TestInsufficiencyAndStale:
    def test_no_verified_edges_is_insufficient(self) -> None:
        result = build_evolution(
            stages=[],
            edge_drafts=[],
            input_manifest={"spec": "evo"},
        )
        assert result.status == "insufficient_evidence"

    def test_coverage_floor(self) -> None:
        result = build_evolution(
            stages=[{"title": "阶段一"}],
            edge_drafts=[
                EdgeDraft(
                    from_node="a", to_node="b", relation="updates", basis="explicit"
                )
            ],
            input_manifest={},
            coverage_ratio=0.3,
            min_coverage=0.5,
        )
        assert result.status == "insufficient_evidence"

    def test_ok_build_keeps_dropped_reasons(self) -> None:
        result = build_evolution(
            stages=[{"title": "阶段一"}],
            edge_drafts=[
                EdgeDraft(
                    from_node="a", to_node="b", relation="updates", basis="explicit"
                ),
                EdgeDraft(from_node="x", to_node="y", relation="updates"),
            ],
            input_manifest={"event_ids": ["e1"]},
        )
        assert result.status == "ok"
        assert len(result.edges) == 1
        assert result.dropped_edges[0]["reason"] == "no_basis"

    def test_stale_debounce_single_job(self) -> None:
        policy = StalePolicy()
        assert not policy.should_rebuild(
            marked_at=T, now=T + timedelta(minutes=4), running_jobs_for_topic=0
        )
        assert policy.should_rebuild(
            marked_at=T,
            now=T + timedelta(minutes=5),
            running_jobs_for_topic=0,
        )
        assert not policy.should_rebuild(
            marked_at=T, now=T + timedelta(hours=1), running_jobs_for_topic=1
        )


def test_uuid_typing_of_revision_ids() -> None:
    result = build_evolution(stages=[], edge_drafts=[], input_manifest={})
    assert isinstance(result.revision_id, UUID)


class TestRoutesRegistered:
    def test_knowledge_routers_declare_the_05_5_query_surface(self) -> None:
        from fastapi.routing import APIRoute

        from intel.api.routes import knowledge

        timeline = next(
            route
            for route in knowledge.timeline_router.routes
            if isinstance(route, APIRoute) and route.path.endswith("/timeline")
        )
        names = set(timeline.endpoint.__annotations__)
        for expected in (
            "topic_ids",
            "event_types",
            "window_from",
            "window_to",
            "as_of",
            "include_unknown",
            "cursor",
        ):
            assert expected in names, expected

    def test_all_routers_include_knowledge(self) -> None:
        from intel.api.routes import all_routers, knowledge

        included = {id(router) for router in all_routers}
        for router in (
            knowledge.timeline_router,
            knowledge.entities_router,
            knowledge.watches_router,
            knowledge.evolutions_router,
        ):
            assert id(router) in included
