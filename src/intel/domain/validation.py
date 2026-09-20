"""Deterministic claim/quote validation (spec 05 §1 steps 1-4, EVI-01/02).

The model proposes; Python verifies. Nothing from an ExtractionProposal
reaches the database before passing here, and nothing may be "fixed" by
rewriting the quote to look like the source (失败时保留 extraction_failed
或 proposal — the model never rewrites quotes into the text).

Checks, in order (05 §1):

1. parse scope ownership — the caller supplies the parse's OWN blocks;
   a block_id from another parse simply does not exist here (跨 parse
   quote → rejected as block_missing);
2. block exists + exact code-point match of the quote inside that block;
   multiple occurrences are ambiguous — the model must supply more
   context (a longer quote), the validator never picks arbitrarily;
3. length floor and numeric support: every number in the claim text must
   appear in at least one evidence quote (literal hit ≠ semantic
   support, but numeric drift is already fatal);
4. statement kind guard: a source_statement without an attribution
   degrades to inference (厂商主张 needs a 主体) — flagged, not rejected.

Semantic re-judgement (EVI-02 second-pass) is the workflow's LLM step on
top of these gates; its verdict lands on the evidence row's
semantic_support_status, never bypassing this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from intel.contracts.models import ClaimProposal, SourceBlock

__all__ = [
    "MIN_QUOTE_CHARS",
    "QuoteLocation",
    "RejectedClaim",
    "ValidatedClaim",
    "ValidatedExtraction",
    "ValidationCode",
    "locate_quote",
    "validate_claim",
    "validate_proposal",
]

#: Shorter quotes are unverifiable context, not evidence (05 §1 长度检查).
MIN_QUOTE_CHARS = 8

_NUMBER_RE = re.compile(
    r"\d+(?:[.,]\d+)*\s*(?:%|ppm|nm|µm|um|mm|cm|m|km|s|ms|ns|us|µs|Hz|kHz|MHz|GHz|"
    r"V|mV|kV|W|mW|kW|A|mA|Pa|kPa|MPa|bar|K|°C|°F|eV|keV|MeV|GB|MB|TB|Gbps|Mbps)?",
)


class ValidationCode(StrEnum):
    """Why a candidate claim was rejected (spec 07 §6 error classes)."""

    NO_EVIDENCE = "no_evidence"
    BLOCK_MISSING = "block_missing"
    QUOTE_NOT_FOUND = "quote_not_found"
    QUOTE_AMBIGUOUS = "quote_ambiguous"
    QUOTE_TOO_SHORT = "quote_too_short"
    NUMERIC_UNSUPPORTED = "numeric_unsupported"


@dataclass(frozen=True, slots=True)
class QuoteLocation:
    """One verified code-point span of a quote within its block."""

    block_id: str
    start_char: int
    end_char: int
    exact_quote: str


@dataclass(frozen=True, slots=True)
class RejectedClaim:
    index: int
    code: ValidationCode
    detail: str


@dataclass(slots=True)
class ValidatedClaim:
    claim: ClaimProposal
    locations: list[QuoteLocation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidatedExtraction:
    accepted: list[ValidatedClaim] = field(default_factory=list)
    rejected: list[RejectedClaim] = field(default_factory=list)

    @property
    def all_rejected(self) -> bool:
        return bool(self.rejected) and not self.accepted


def locate_quote(block_text: str, quote: str) -> list[tuple[int, int]]:
    """All exact code-point (start, end) spans of quote in block_text.

    Empty quote never matches; occurrences do not overlap (equal spans
    step by len(quote)). Python string indices ARE code points, so this
    is byte-safe for CJK.
    """
    if not quote:
        return []
    spans: list[tuple[int, int]] = []
    start = block_text.find(quote)
    while start != -1:
        spans.append((start, start + len(quote)))
        start = block_text.find(quote, start + len(quote))
    return spans


def _numbers_in(text: str) -> set[str]:
    return {m.group(0).replace(" ", "") for m in _NUMBER_RE.finditer(text)}


def _numeric_support(claim: ClaimProposal, quotes: Iterable[str]) -> bool:
    """Every number in the claim must appear verbatim in some quote.

    Unit-bearing values compare as-is (value+unit must co-occur); a claim
    saying 40% against a quote saying 40 (no unit) is unsupported — the
    unit is part of the fact (05 §1 数字单位检查).
    """
    claim_numbers = _numbers_in(claim.text)
    if not claim_numbers:
        return True
    quoted = set[str]().union(*(_numbers_in(q) for q in quotes)) if quotes else set()
    return claim_numbers <= quoted


def validate_claim(
    claim: ClaimProposal,
    blocks: Mapping[str, SourceBlock],
    *,
    min_quote_chars: int = MIN_QUOTE_CHARS,
) -> ValidatedClaim | RejectedClaim:
    """Validate one candidate claim against the parse's own blocks."""
    if not claim.evidence:
        return RejectedClaim(0, ValidationCode.NO_EVIDENCE, "claim carries no evidence")

    locations: list[QuoteLocation] = []
    for evidence in claim.evidence:
        block = blocks.get(evidence.block_id)
        if block is None:
            return RejectedClaim(
                0,
                ValidationCode.BLOCK_MISSING,
                f"block {evidence.block_id!r} not in this parse"
                " (cross-parse quotes are rejected)",
            )
        quote = evidence.exact_quote
        if len(quote.strip()) < min_quote_chars:
            return RejectedClaim(
                0,
                ValidationCode.QUOTE_TOO_SHORT,
                f"quote shorter than {min_quote_chars} chars in block"
                f" {evidence.block_id!r}",
            )
        spans = locate_quote(block.text, quote)
        if not spans:
            return RejectedClaim(
                0,
                ValidationCode.QUOTE_NOT_FOUND,
                f"quote not found in block {evidence.block_id!r}"
                " (never rewrite the quote to match)",
            )
        if len(spans) > 1:
            return RejectedClaim(
                0,
                ValidationCode.QUOTE_AMBIGUOUS,
                f"quote occurs {len(spans)}x in block {evidence.block_id!r};"
                " supply more surrounding context",
            )
        start, end = spans[0]
        locations.append(
            QuoteLocation(
                block_id=evidence.block_id,
                start_char=start,
                end_char=end,
                exact_quote=quote,
            )
        )

    if not _numeric_support(claim, (loc.exact_quote for loc in locations)):
        return RejectedClaim(
            0,
            ValidationCode.NUMERIC_UNSUPPORTED,
            "claim cites numbers absent from every evidence quote",
        )

    warnings: list[str] = []
    if claim.kind == "source_statement" and not (claim.attribution or "").strip():
        warnings.append(
            "source_statement without attribution downgraded to inference"
            " (05 §1: 厂商主张需要主体)"
        )
    return ValidatedClaim(claim=claim, locations=locations, warnings=warnings)


def validate_proposal(
    claims: Iterable[ClaimProposal],
    blocks: Mapping[str, SourceBlock],
    *,
    min_quote_chars: int = MIN_QUOTE_CHARS,
) -> ValidatedExtraction:
    """Validate every candidate claim; accepted and rejected both survive.

    The rejected list is data (extraction_failed evidence), never an
    exception — a partially-good document keeps its good claims.
    """
    result = ValidatedExtraction()
    for index, claim in enumerate(claims):
        outcome = validate_claim(claim, blocks, min_quote_chars=min_quote_chars)
        if isinstance(outcome, RejectedClaim):
            result.rejected.append(RejectedClaim(index, outcome.code, outcome.detail))
        else:
            result.accepted.append(outcome)
    return result
