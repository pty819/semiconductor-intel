"""Unit tests: deterministic claim/quote validation (spec 05 §1, EVI-01).

Offline, pure functions. The load-bearing behaviors:

- 伪造 quote (fabricated text) → rejected, never "repaired";
- 跨 parse quote (block id from another parse) → block_missing;
- block 不存在 → block_missing;
- repeated occurrence inside one block → ambiguous, more context required;
- too-short quote → rejected;
- numeric drift (claim 40% vs quote "40"; claim 5nm vs quote 3nm) →
  numeric_unsupported;
- source_statement without attribution → warning + inference downgrade,
  NOT a rejection (partial documents keep their good claims);
- accepted + rejected coexist (validate_proposal never throws).
"""

from __future__ import annotations

from intel.contracts.models import (
    ClaimProposal,
    EvidenceProposal,
    SourceBlock,
)
from intel.domain.validation import (
    MIN_QUOTE_CHARS,
    ValidationCode,
    locate_quote,
    validate_claim,
    validate_proposal,
)

BLOCKS = {
    "b001": SourceBlock(
        block_id="b001",
        text="公司在 2026 年第三季度推出新一代刻蚀设备，吞吐量提升 40%，"
        "适用于 5nm 以下制程。",
    ),
    "b002": SourceBlock(
        block_id="b002",
        text="测试重复句子出现在这里。测试重复句子出现在这里。这是完全独立的第二段落内容。",
    ),
}


def _claim(**overrides) -> ClaimProposal:
    payload: dict = {
        "text": "厂商宣称吞吐量提升 40%",
        "kind": "source_statement",
        "attribution": "设备厂商",
        "conditions": ["适用于 5nm 以下制程"],
        "evidence": [
            EvidenceProposal(
                block_id="b001",
                exact_quote="吞吐量提升 40%",
                relation="supports",
            )
        ],
    }
    payload.update(overrides)
    return ClaimProposal(**payload)


class TestLocateQuote:
    def test_exact_code_point_span(self) -> None:
        text = "前缀吞吐量提升 40%后缀"
        assert locate_quote(text, "吞吐量提升 40%") == [(2, 11)]

    def test_multiple_occurrences_all_reported(self) -> None:
        spans = locate_quote("abcXabcX", "abcX")
        assert spans == [(0, 4), (4, 8)]

    def test_missing_and_empty(self) -> None:
        assert locate_quote("abc", "zzz") == []
        assert locate_quote("abc", "") == []


class TestEvi01FabricatedOrForeignQuotes:
    def test_fabricated_quote_rejected(self) -> None:
        outcome = validate_claim(
            _claim(
                evidence=[
                    EvidenceProposal(
                        block_id="b001",
                        exact_quote="完全没出现过的引文内容",
                        relation="supports",
                    )
                ]
            ),
            BLOCKS,
        )
        assert getattr(outcome, "code", None) == ValidationCode.QUOTE_NOT_FOUND

    def test_cross_parse_block_id_rejected(self) -> None:
        # A block_id that belongs to a DIFFERENT parse simply does not
        # exist in this parse's block map.
        outcome = validate_claim(
            _claim(
                evidence=[
                    EvidenceProposal(
                        block_id="z999",
                        exact_quote="吞吐量提升 40%",
                        relation="supports",
                    )
                ]
            ),
            BLOCKS,
        )
        assert getattr(outcome, "code", None) == ValidationCode.BLOCK_MISSING

    def test_partial_edit_of_real_text_rejected(self) -> None:
        # "Close enough" is not exact: a model-edited quote fails.
        outcome = validate_claim(
            _claim(
                evidence=[
                    EvidenceProposal(
                        block_id="b001",
                        exact_quote="吞吐量提升了 40%",  # 多了一个"了"
                        relation="supports",
                    )
                ]
            ),
            BLOCKS,
        )
        assert getattr(outcome, "code", None) == ValidationCode.QUOTE_NOT_FOUND

    def test_repeated_occurrence_is_ambiguous(self) -> None:
        outcome = validate_claim(
            _claim(
                evidence=[
                    EvidenceProposal(
                        block_id="b002",
                        exact_quote="测试重复句子出现在这里。",
                        relation="supports",
                    )
                ]
            ),
            BLOCKS,
        )
        assert getattr(outcome, "code", None) == ValidationCode.QUOTE_AMBIGUOUS

    def test_longer_context_disambiguates(self) -> None:
        outcome = validate_claim(
            _claim(
                text="文档末尾包含独立说明段落",
                evidence=[
                    EvidenceProposal(
                        block_id="b002",
                        exact_quote="这是完全独立的第二段落内容。",  # unique in block
                        relation="context",
                    )
                ],
            ),
            BLOCKS,
        )
        assert not getattr(outcome, "code", None)

    def test_too_short_quote_rejected(self) -> None:
        assert len("40%") < MIN_QUOTE_CHARS
        outcome = validate_claim(
            _claim(
                evidence=[
                    EvidenceProposal(
                        block_id="b001", exact_quote="40%", relation="supports"
                    )
                ]
            ),
            BLOCKS,
        )
        assert getattr(outcome, "code", None) == ValidationCode.QUOTE_TOO_SHORT


class TestNumericChecks:
    def test_unit_drift_rejected(self) -> None:
        # claim 40%，quote 只说 40（无单位）→ 不支持。
        outcome = validate_claim(
            _claim(
                evidence=[
                    EvidenceProposal(
                        block_id="b001",
                        exact_quote="吞吐量提升约 40 的水平线，未注明单位",
                        relation="supports",
                    )
                ]
            ),
            {
                "b001": SourceBlock(
                    block_id="b001",
                    text="官方说明原文为：吞吐量提升约 40 的水平线，未注明单位，需谨慎解读。",
                )
            },
        )
        assert getattr(outcome, "code", None) == ValidationCode.NUMERIC_UNSUPPORTED

    def test_wrong_number_rejected(self) -> None:
        outcome = validate_claim(_claim(text="适用于 3nm 以下制程"), BLOCKS)
        assert getattr(outcome, "code", None) == ValidationCode.NUMERIC_UNSUPPORTED

    def test_numbers_across_any_evidence_quote(self) -> None:
        claim = _claim(
            text="吞吐量提升 40%，适用于 5nm 以下制程",
            evidence=[
                EvidenceProposal(
                    block_id="b001",
                    exact_quote="适用于 5nm 以下制程",
                    relation="supports",
                ),
                # second evidence is the 40% quote (default)
                EvidenceProposal(
                    block_id="b001",
                    exact_quote="吞吐量提升 40%",
                    relation="supports",
                ),
            ],
        )
        outcome = validate_claim(claim, BLOCKS)
        assert not getattr(outcome, "code", None)
        assert len(outcome.locations) == 2

    def test_no_numbers_in_claim_passes(self) -> None:
        outcome = validate_claim(_claim(text="厂商宣称吞吐量显著提升"), BLOCKS)
        assert not getattr(outcome, "code", None)


class TestStatementKind:
    def test_source_statement_without_attribution_warns(self) -> None:
        outcome = validate_claim(_claim(attribution=None), BLOCKS)
        assert not getattr(outcome, "code", None)
        assert any("inference" in w for w in outcome.warnings)

    def test_attribution_keeps_kind(self) -> None:
        outcome = validate_claim(_claim(), BLOCKS)
        assert outcome.warnings == []


class TestProposalLevel:
    def test_good_and_bad_claims_coexist(self) -> None:
        result = validate_proposal(
            [
                _claim(),
                _claim(
                    evidence=[
                        EvidenceProposal(
                            block_id="b001",
                            exact_quote="这段引文在任何块中都不存在",
                            relation="supports",
                        )
                    ]
                ),
            ],
            BLOCKS,
        )
        assert len(result.accepted) == 1
        assert [r.code for r in result.rejected] == [ValidationCode.QUOTE_NOT_FOUND]
        assert not result.all_rejected

    def test_all_rejected_flag(self) -> None:
        result = validate_proposal(
            [
                _claim(
                    evidence=[
                        EvidenceProposal(
                            block_id="b001",
                            exact_quote="这段引文在任何块中都不存在",
                            relation="supports",
                        )
                    ]
                )
            ],
            BLOCKS,
        )
        assert result.all_rejected
