"""Parse quality checks → quality_flags + parse_status (spec 04 §4, PAR-03).

Quality thresholds come from :class:`intel.settings.Settings` so the
numbers are operational knobs, not code constants. The assessment turns
extraction signals into:

- ``quality_flags`` — machine-readable soft/hard findings;
- ``parse_status`` — ok / partial / failed (03 §3 vocabulary);
- ``retrieval_scope`` — metadata / abstract / partial / fulltext (04 §4).

Never silently succeed on empty or garbage: a hard breach (no usable
text, login/challenge page, extreme text ratio) lands on ``failed``;
soft findings (missing metadata, ratio anomalies, coverage gaps,
recommend-list shape) land on ``partial`` with flags (PAR-03).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from intel.settings import Settings

#: Quality flag vocabulary (stable machine names).
FLAG_NO_TEXT = "no_text"
FLAG_TEXT_RATIO_LOW = "text_ratio_low"
FLAG_TEXT_RATIO_HIGH = "text_ratio_high"
FLAG_HIGH_BOILERPLATE = "high_boilerplate"
FLAG_LOW_STRUCTURE_HIT = "low_structure_hit"
FLAG_MISSING_TITLE = "missing_title"
FLAG_MISSING_DATE = "missing_date"
FLAG_TABLE_NOT_EXTRACTED = "table_not_extracted"
FLAG_IMAGE_NOT_READ = "image_not_read"
FLAG_RECOMMEND_LIST = "recommend_list_page"
FLAG_LOGIN_PAGE = "login_page"
FLAG_CHALLENGE_PAGE = "challenge_page"
FLAG_ENCODING_REPLACEMENT = "encoding_replacement"
FLAG_LANGUAGE_MIXED = "language_mixed"

_STRUCTURE_HIT_FLOOR = 0.5
_LIST_TITLE_MARKERS = (
    "推荐", "recommended", "related", "news list", "list of", "最新",
)


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    """The parse quality knobs (Settings carries the defaults)."""

    min_text_ratio: float = 0.01
    max_text_ratio: float = 0.95
    boilerplate_max: float = 0.6
    #: Below this many usable characters the text is an abstract at best,
    #: and a low ratio becomes a hard failure rather than a soft flag.
    min_text_chars: int = 200
    #: Link-text share over which a page reads as a recommendation list.
    link_density_max: float = 0.5

    @classmethod
    def from_settings(cls, settings: Settings) -> QualityThresholds:
        return cls(
            min_text_ratio=settings.min_text_ratio,
            max_text_ratio=settings.max_text_ratio,
            boilerplate_max=settings.boilerplate_max,
            min_text_chars=settings.min_text_chars,
            link_density_max=settings.link_density_max,
        )


@dataclass(slots=True)
class QualitySignals:
    """What extraction learned about one document, pre-assessment."""

    media_type: str = ""
    byte_size: int = 0
    text_chars: int = 0
    block_count: int = 0
    boilerplate_ratio: float = 0.0
    structure_hit_rate: float = 1.0
    link_text_chars: int = 0
    has_title: bool = False
    has_date: bool = False
    has_author: bool = False
    table_status: str = "none"
    image_not_read: bool = False
    #: Zero text is explained by coverage (image-only pages): a documented
    #: gap lands on partial, an unexplained one fails (PAR-03).
    text_gap_explained: bool = False
    encoding_replaced: bool = False
    language_mixed: bool = False
    title: str | None = None
    login_detected: bool = False
    challenge_detected: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Assessment:
    parse_status: str
    retrieval_scope: str
    quality_flags: list[str]


def assess_quality(
    signals: QualitySignals, thresholds: QualityThresholds
) -> Assessment:
    flags = list(signals.flags)
    hard = False

    if signals.login_detected:
        flags.append(FLAG_LOGIN_PAGE)
        hard = True
    if signals.challenge_detected:
        flags.append(FLAG_CHALLENGE_PAGE)
        hard = True
    if signals.encoding_replaced:
        flags.append(FLAG_ENCODING_REPLACEMENT)
    if signals.language_mixed:
        flags.append(FLAG_LANGUAGE_MIXED)

    usable = signals.text_chars
    if signals.block_count == 0 or usable == 0:
        flags.append(FLAG_NO_TEXT)
        if not signals.text_gap_explained:
            hard = True
    else:
        ratio = usable / signals.byte_size if signals.byte_size else 1.0
        if ratio < thresholds.min_text_ratio:
            flags.append(FLAG_TEXT_RATIO_LOW)
            if usable < thresholds.min_text_chars:
                hard = True  # almost nothing usable vs the bytes fetched
        elif (
            ratio > thresholds.max_text_ratio
            and not signals.media_type.startswith("text/plain")
        ):
            # Markup containers should never yield ~1.0 text/bytes.
            flags.append(FLAG_TEXT_RATIO_HIGH)

    if signals.boilerplate_ratio > thresholds.boilerplate_max:
        flags.append(FLAG_HIGH_BOILERPLATE)
    if signals.structure_hit_rate < _STRUCTURE_HIT_FLOOR:
        flags.append(FLAG_LOW_STRUCTURE_HIT)

    total_chars = max(usable, 1)
    link_density = signals.link_text_chars / total_chars
    title_is_list = bool(signals.title) and any(
        marker in (signals.title or "").lower()
        for marker in _LIST_TITLE_MARKERS
    )
    if link_density > thresholds.link_density_max or (
        title_is_list and link_density > thresholds.link_density_max / 2
    ):
        # PAR-03: 推荐列表不当正常正文 — partial + flag, never silent ok.
        flags.append(FLAG_RECOMMEND_LIST)

    if not signals.has_title:
        flags.append(FLAG_MISSING_TITLE)
    if not signals.has_date:
        flags.append(FLAG_MISSING_DATE)
    if signals.table_status == "not_extracted":
        flags.append(FLAG_TABLE_NOT_EXTRACTED)
    if signals.image_not_read:
        flags.append(FLAG_IMAGE_NOT_READ)

    if hard:
        parse_status = "failed"
    elif flags:
        parse_status = "partial"
    else:
        parse_status = "ok"

    if parse_status == "failed":
        scope = "metadata"
    elif usable < thresholds.min_text_chars:
        scope = "abstract"
    elif parse_status == "partial":
        scope = "partial"
    else:
        scope = "fulltext"

    return Assessment(parse_status, scope, _dedupe(flags))


def _dedupe(flags: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for flag in flags:
        if flag not in seen:
            seen.add(flag)
            ordered.append(flag)
    return ordered


# --------------------------------------------------------------------------
# language: script-ratio heuristic, no dependencies
# --------------------------------------------------------------------------

_SAMPLE_LIMIT = 4000


def detect_language(text: str) -> str:
    """Script-ratio language guess: zh / ja / ko / ru / en / mixed /
    unknown. Astral-plane chars count as single code points (03 §3)."""
    sample = text[:_SAMPLE_LIMIT]
    counts = {"han": 0, "kana": 0, "hangul": 0, "cyrillic": 0, "latin": 0}
    total = 0
    for char in unicodedata.normalize("NFC", sample):
        if not char.isalpha():
            continue
        total += 1
        if _is_han(char):
            counts["han"] += 1
        elif _is_kana(char):
            counts["kana"] += 1
        elif _is_hangul(char):
            counts["hangul"] += 1
        elif _is_cyrillic(char):
            counts["cyrillic"] += 1
        elif char.isascii():
            counts["latin"] += 1
    if total == 0:
        return "unknown"
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_count = ranked[0]
    if top_count / total < 0.3:
        return "mixed"
    if top_name == "han":
        return "zh"
    if top_name == "kana":
        return "ja"
    if top_name == "hangul":
        return "ko"
    if top_name == "cyrillic":
        return "ru"
    return "en"


#: Scripts whose co-occurrence signals a genuinely mixed document
#: (han+kana is Japanese, not "mixed").
_MIXED_PAIRS = ({"han", "latin"}, {"kana", "latin"}, {"han", "cyrillic"})


def is_language_mixed(text: str) -> bool:
    """True when two major scripts share the document without one
    dominating — surfaced as a soft quality flag."""
    sample = text[:_SAMPLE_LIMIT]
    counts = {"han": 0, "kana": 0, "hangul": 0, "cyrillic": 0, "latin": 0}
    total = 0
    for char in sample:
        if not char.isalpha():
            continue
        total += 1
        if _is_han(char):
            counts["han"] += 1
        elif _is_kana(char):
            counts["kana"] += 1
        elif _is_hangul(char):
            counts["hangul"] += 1
        elif _is_cyrillic(char):
            counts["cyrillic"] += 1
        elif char.isascii():
            counts["latin"] += 1
    if total == 0:
        return False
    shares = {name: count / total for name, count in counts.items()}
    scripts = {
        name for name, share in shares.items()
        if share > 0.3 and name in {"han", "kana", "hangul", "cyrillic",
                                    "latin"}
    }
    return any(set(pair) <= scripts for pair in _MIXED_PAIRS)


def _is_han(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff" or "\u3400" <= char <= "\u4dbf"


def _is_kana(char: str) -> bool:
    return "\u3040" <= char <= "\u30ff" or "\u31f0" <= char <= "\u31ff"


def _is_hangul(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3" or "\u1100" <= char <= "\u11ff"


def _is_cyrillic(char: str) -> bool:
    return "\u0400" <= char <= "\u04ff"
