"""Unit tests: event identity strong keys + merge gates (spec 05 §2).

Offline acceptance cases from doc 05 §7 plus the identity table:

- EVT-01: ten reposts of one announcement share one strong key → one
  event, one source family (the family grouping itself is asserted at
  the service level in test_event_service.py; here: one key, one
  auto-merge group);
- EVT-02: same company, same day, two different products do NOT merge;
  same-day two attribute changes do not merge either;
- EVT-03: 预告/内测/正式商用 are three distinct events of one product
  (release phase is a key component);
- paper_version: v1 vs v2 and 发表 vs 撤稿 are different events;
- weak evidence (missing required component) ⇒ no key ⇒ new independent
  event, possible_duplicate only as a recorded proposal;
- 'other' never gets a generic key;
- optional components participate: a known region does not merge into
  an unregionalized record.
"""

from __future__ import annotations

import pytest

from intel.domain.identity import (
    StrongKey,
    build_identity_key,
    can_auto_merge,
    duplicate_candidates,
    key_string,
)

ANNOUNCEMENT = {
    "announcement_ref": "https://corp.example.com/pr/2026-09-01",
    "counterparty": "台积电",
    "action": "设备采购协议",
}


def _key(event_type: str, **fields):
    return build_identity_key(event_type, fields)


class TestEvt01SameAnnouncement:
    def test_ten_reposts_produce_one_key(self) -> None:
        # Ten documents each propose the same commercial announcement —
        # maybe with different whitespace/casing from extraction noise.
        keys = [
            _key(
                "commercial_announcement",
                **{k: f"  {v} " for k, v in ANNOUNCEMENT.items()},
            )
            for _ in range(10)
        ]
        assert all(k is not None for k in keys)
        first = keys[0]
        assert all(can_auto_merge(first, k) for k in keys[1:])
        assert len({key_string(k) for k in keys}) == 1

    def test_duplicate_candidates_find_the_existing_event(self) -> None:
        existing = {"event-1": _key("commercial_announcement", **ANNOUNCEMENT)}
        new = _key("commercial_announcement", **ANNOUNCEMENT)
        assert duplicate_candidates(new, existing) == ["event-1"]


class TestEvt02SameDayDifferentObjects:
    def test_two_products_same_day_do_not_merge(self) -> None:
        camera = _key(
            "product_release",
            release_ref="https://corp.example.com/news/camera",
            product="影像传感器 X100",
            phase="ga",
        )
        lidar = _key(
            "product_release",
            release_ref="https://corp.example.com/news/lidar",
            product="激光雷达 L200",
            phase="ga",
        )
        assert not can_auto_merge(camera, lidar)

    def test_same_day_two_attribute_changes_do_not_merge(self) -> None:
        rf = _key(
            "product_change",
            product="刻蚀机 E-500",
            attribute="RF 频率",
            change_ref="changelog#2026-09-01-rf",
        )
        pressure = _key(
            "product_change",
            product="刻蚀机 E-500",
            attribute="腔压",
            change_ref="changelog#2026-09-01-pressure",
        )
        assert not can_auto_merge(rf, pressure)


class TestEvt03ReleasePhases:
    def test_announced_beta_ga_are_three_events(self) -> None:
        announced = _key(
            "product_release",
            release_ref="https://corp.example.com/products/etch-e500",
            product="刻蚀机 E-500",
            phase="announced",
        )
        beta = _key(
            "product_release",
            release_ref="https://corp.example.com/products/etch-e500",
            product="刻蚀机 E-500",
            phase="beta",
        )
        ga = _key(
            "product_release",
            release_ref="https://corp.example.com/products/etch-e500",
            product="刻蚀机 E-500",
            phase="ga",
        )
        assert announced and beta and ga
        assert not can_auto_merge(announced, beta)
        assert not can_auto_merge(beta, ga)
        assert not can_auto_merge(announced, ga)

    def test_regions_do_not_merge(self) -> None:
        domestic = _key(
            "product_release",
            release_ref="https://corp.example.com/products/etch-e500",
            product="刻蚀机 E-500",
            phase="ga",
            region="中国大陆",
        )
        unregionalized = _key(
            "product_release",
            release_ref="https://corp.example.com/products/etch-e500",
            product="刻蚀机 E-500",
            phase="ga",
        )
        # Known region ≠ absent region — the optional component still gates.
        assert not can_auto_merge(domestic, unregionalized)


class TestPaperVersion:
    def test_v1_v2_are_different_events(self) -> None:
        v1 = _key(
            "paper_version",
            work_id="doi:10.1234/etch.2026.001",
            version="1",
            action="publish",
        )
        v2 = _key(
            "paper_version",
            work_id="doi:10.1234/etch.2026.001",
            version="2",
            action="publish",
        )
        assert not can_auto_merge(v1, v2)

    def test_publish_vs_retract_are_different_events(self) -> None:
        published = _key(
            "paper_version",
            work_id="doi:10.1234/etch.2026.001",
            version="1",
            action="publish",
        )
        retracted = _key(
            "paper_version",
            work_id="doi:10.1234/etch.2026.001",
            version="1",
            action="retract",
        )
        assert not can_auto_merge(published, retracted)


class TestWeakEvidence:
    def test_missing_required_component_gives_no_key(self) -> None:
        # counterparty missing: 同公司同日两笔交易 is exactly the case a
        # missing gate must NOT auto-merge on the rest.
        fields = dict(ANNOUNCEMENT)
        del fields["counterparty"]
        assert _key("commercial_announcement", **fields) is None

    def test_blank_required_component_gives_no_key(self) -> None:
        assert (
            _key("product_release", release_ref="  ", product="p", phase="ga") is None
        )

    def test_other_type_never_keys(self) -> None:
        assert _key("other", anything="x") is None

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            _key("merger", a="1")


class TestKeyShape:
    def test_canonical_string_roundtrip(self) -> None:
        key = _key("commercial_announcement", **ANNOUNCEMENT)
        parsed = StrongKey(event_type=key.event_type, components=key.components)
        assert key_string(parsed) == key.canonical
        assert key.canonical.startswith("commercial_announcement|")
        assert "counterparty=台积电" in key.canonical

    def test_case_and_whitespace_normalized(self) -> None:
        a = _key(
            "commercial_announcement",
            announcement_ref="HTTPS://Corp.Example.COM/PR/2026-09-01",
            counterparty="TSMC",
            action="Supply Agreement",
        )
        b = _key(
            "commercial_announcement",
            announcement_ref="https://corp.example.com/pr/2026-09-01",
            counterparty="tsmc",
            action="supply agreement",
        )
        assert a and b and can_auto_merge(a, b)

    def test_optional_component_present_serializes(self) -> None:
        key = _key(
            "product_release",
            release_ref="r",
            product="p",
            phase="ga",
            version="2.0",
        )
        assert "version=2.0" in key.canonical
        assert key.get("version") == "2.0"
        assert key.get("region") == ""


# ---------------------------------------------------------------------------
# Service-level acceptance cases (05 §3/§4) over the in-memory store double
# ---------------------------------------------------------------------------

from uuid import uuid4

from intel.contracts.models import (
    ClaimProposal,
    EvidenceProposal,
    SourceBlock,
)
from intel.domain.validation import validate_claim
from intel.repositories.base import IndustryScope
from intel.services.knowledge import (
    EventProposalInput,
    InMemoryKnowledgeStore,
    MergeConflict,
    UndoConflict,
    apply_event_merge,
    commit_extraction,
    resolve_event,
    undo_event_merge,
)

OWNER_ID = uuid4()
INDUSTRY_ID = uuid4()
SCOPE = IndustryScope(owner_id=OWNER_ID, industry_id=INDUSTRY_ID)

_BLOCKS = {
    "b001": SourceBlock(
        block_id="b001",
        text="官方公告：公司与台积电签署设备采购协议，价值 5 亿美元。",
    )
}


def _validated_claim():
    proposal = ClaimProposal(
        text="公司与台积电签署设备采购协议，价值 5 亿美元",
        kind="source_statement",
        attribution="公司官方",
        conditions=[],
        evidence=[
            EvidenceProposal(
                block_id="b001",
                exact_quote="公司与台积电签署设备采购协议，价值 5 亿美元",
                relation="supports",
            )
        ],
    )
    outcome = validate_claim(proposal, _BLOCKS)
    assert not getattr(outcome, "code", None)
    return outcome


class TestEvt01Service:
    async def test_ten_reposts_one_family_one_event(self) -> None:
        """EVT-01: 10 篇同公告转载 → 1 个事件、1 个 source_family."""
        store = InMemoryKnowledgeStore()
        origin = "https://corp.example.com/pr/2026-09-01"

        event_ids = set()
        family_ids = set()
        for _ in range(10):
            commit = await commit_extraction(
                store,
                SCOPE,
                validated_claims=[_validated_claim()],
                rejected=[],
                parse_id=uuid4(),
                extraction_run_id=uuid4(),
                input_manifest={"spec": "evt-01"},
                origin_ref=origin,
            )
            family_ids.update(commit.source_family_ids)
            resolution = await resolve_event(
                store,
                SCOPE,
                EventProposalInput(
                    event_type="commercial_announcement",
                    title="设备采购协议",
                    identity_fields={
                        "announcement_ref": origin,
                        "counterparty": "台积电",
                        "action": "设备采购协议",
                    },
                    claim_revision_ids=commit.claim_revision_ids,
                ),
                input_manifest={"spec": "evt-01"},
            )
            event_ids.add(resolution.event_id)

        assert len(event_ids) == 1  # 一个事件
        assert len(family_ids) == 1  # 一个 source_family
        event_id = next(iter(event_ids))
        # 10 documents' claims all link to that one event.
        assert len(store.event_claims[event_id]) == 10
        # ... and share the single family (independence is NOT claimed).
        family_id = next(iter(family_ids))
        assert (
            sum(
                1
                for row in store.evidence.values()
                if row["source_family_id"] == family_id
            )
            == 10
        )

    async def test_distinct_origins_create_distinct_families(self) -> None:
        store = InMemoryKnowledgeStore()
        for origin in (
            "https://a.example.com/pr/1",
            "https://b.example.com/pr/1",
        ):
            await commit_extraction(
                store,
                SCOPE,
                validated_claims=[_validated_claim()],
                rejected=[],
                parse_id=uuid4(),
                extraction_run_id=uuid4(),
                input_manifest={},
                origin_ref=origin,
            )
        assert len(store.families) == 2


class TestWeakEvidenceProposals:
    async def test_no_key_new_event_with_duplicate_proposal(self) -> None:
        store = InMemoryKnowledgeStore()
        first = await resolve_event(
            store,
            SCOPE,
            EventProposalInput(
                event_type="commercial_announcement",
                title="某交易",
                identity_fields={"counterparty": "台积电"},  # 缺 announcement_ref
            ),
            input_manifest={},
        )
        assert first.created

        second = await resolve_event(
            store,
            SCOPE,
            EventProposalInput(
                event_type="commercial_announcement",
                title="某交易（疑似重复）",
                identity_fields={"counterparty": "台积电"},
            ),
            input_manifest={},
            similar_candidate_event_id=first.event_id,
        )
        assert second.created and second.event_id != first.event_id
        assert second.possible_duplicate_recorded
        assert len(store.duplicates) == 1


class TestEvt04MergeUndo:
    async def _merged_pair(self, store):
        canonical = await resolve_event(
            store,
            SCOPE,
            EventProposalInput(
                event_type="product_release",
                title="正式发布",
                identity_fields={
                    "release_ref": "r",
                    "product": "p",
                    "phase": "ga",
                },
            ),
            input_manifest={},
        )
        duplicate = await resolve_event(
            store,
            SCOPE,
            EventProposalInput(
                event_type="product_release",
                title="正式发布公告（重复来源）",
                identity_fields={},
            ),
            input_manifest={},
        )
        versions = {
            canonical.event_id: store.events[canonical.event_id]["row_version"],
            duplicate.event_id: store.events[duplicate.event_id]["row_version"],
        }
        return canonical, duplicate, versions

    async def test_merge_checks_expected_versions(self) -> None:
        store = InMemoryKnowledgeStore()
        canonical, duplicate, versions = await self._merged_pair(store)
        stale = dict(versions)
        stale[canonical.event_id] = versions[canonical.event_id] - 1
        with pytest.raises(MergeConflict):
            await apply_event_merge(
                store,
                SCOPE,
                canonical_id=canonical.event_id,
                merged_id=duplicate.event_id,
                expected_row_versions=stale,
                review_id=uuid4(),
                rationale="dup",
                membership_snapshot={"claims": []},
            )
        assert store.events[duplicate.event_id]["merged_into_id"] is None

    async def test_clean_undo_restores_membership(self) -> None:
        store = InMemoryKnowledgeStore()
        canonical, duplicate, versions = await self._merged_pair(store)
        merge_id = await apply_event_merge(
            store,
            SCOPE,
            canonical_id=canonical.event_id,
            merged_id=duplicate.event_id,
            expected_row_versions=versions,
            review_id=uuid4(),
            rationale="dup",
            membership_snapshot={"claims": [str(uuid4())]},
        )
        assert store.events[duplicate.event_id]["merged_into_id"] == canonical.event_id
        await undo_event_merge(store, SCOPE, merge_id=merge_id)
        assert store.events[duplicate.event_id]["merged_into_id"] is None
        assert merge_id in store.undone

    async def test_undo_after_modification_conflicts_with_compensation(self) -> None:
        store = InMemoryKnowledgeStore()
        canonical, duplicate, versions = await self._merged_pair(store)
        merge_id = await apply_event_merge(
            store,
            SCOPE,
            canonical_id=canonical.event_id,
            merged_id=duplicate.event_id,
            expected_row_versions=versions,
            review_id=uuid4(),
            rationale="dup",
            membership_snapshot={"claims": []},
        )
        # 合并后产生新的人工修改 → undo 不能覆盖 (EVT-04)。
        store.post_merge_edit_count[canonical.event_id] = 2
        with pytest.raises(UndoConflict):
            await undo_event_merge(store, SCOPE, merge_id=merge_id)
        # Not undone — and the compensation proposal was recorded.
        assert merge_id not in store.undone
        assert store.events[duplicate.event_id]["merged_into_id"] == canonical.event_id
        assert store.merges[merge_id]["compensation"] is not None
