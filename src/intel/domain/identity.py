"""Event identity: strong keys per event_type + boolean merge gates (05 §2).

业务身份逻辑不进模型 prompt：the model proposes events with structured
identity fields; this registry decides merge-versus-new deterministically.

A strong key exists only when every REQUIRED component of the event_type
is present. Missing components ⇒ no key ⇒ a new independent event, with
an optional possible_duplicate proposal the caller may record — 近似语义
相似只能找候选，从不自动合并.

Auto-merge is a strict boolean gate: two keys merge only when the
event_type matches AND every component matches exactly. There is
deliberately no similarity threshold — "0.95 就合并" is forbidden by the
spec, and the gates it names (对象、版本、阶段、地区、时间冲突) are
component equality/difference, not scores.

Component sets (05 §2 table):

======================  ====================================================
event_type              key components (★ = required)
======================  ====================================================
paper_version           ★work_id ★version ★action
product_release         ★release_ref ★product ★phase  version  region
product_change          ★product ★attribute ★change_ref  effective_window
commercial_announce.    ★announcement_ref ★counterparty ★action
research_result         ★work_id ★result_key  conditions
correction/retraction   ★correction_ref ★target_ref
other                   (never keyed — candidates + human review only)
======================  ====================================================

The blocked mis-merges fall out of the components: v1/v2 and
publish/retract differ (version, action); 预告/内测/正式 differ (phase);
regions differ; same-day two products differ (product); same-day two
parameter changes differ (attribute); same-company same-day two deals
differ (counterparty); different experiment setups differ (conditions).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "EVENT_IDENTITY_COMPONENTS",
    "EVENT_TYPES",
    "StrongKey",
    "build_identity_key",
    "can_auto_merge",
    "duplicate_candidates",
    "key_string",
]

#: event_type → (required components, optional components). Order is the
#: canonical serialization order of the key.
EVENT_IDENTITY_COMPONENTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "paper_version": (("work_id", "version", "action"), ()),
    "product_release": (
        ("release_ref", "product", "phase"),
        ("version", "region"),
    ),
    "product_change": (
        ("product", "attribute", "change_ref"),
        ("effective_window",),
    ),
    "commercial_announcement": (
        ("announcement_ref", "counterparty", "action"),
        (),
    ),
    "research_result": (("work_id", "result_key"), ("conditions",)),
    "correction": (("correction_ref", "target_ref"), ()),
    "retraction": (("correction_ref", "target_ref"), ()),
    # 'other' is intentionally absent: no generic unique key is ever built.
}

EVENT_TYPES = frozenset(EVENT_IDENTITY_COMPONENTS) | {"other"}


@dataclass(frozen=True, slots=True)
class StrongKey:
    """The boolean identity of one event under its event_type."""

    event_type: str
    components: tuple[tuple[str, str], ...]

    @property
    def canonical(self) -> str:
        return key_string(self)

    def get(self, component: str) -> str | None:
        for name, value in self.components:
            if name == component:
                return value
        return None


def _normalize(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def build_identity_key(event_type: str, fields: Mapping[str, str]) -> StrongKey | None:
    """Build the strong key for one proposal, or None when under-evidenced.

    Optional components participate when present; a missing optional
    component serializes as the empty string, so absent==absent still
    merges while present≠absent does not (a known region never silently
    merges into an unregionalized record).
    """
    if event_type == "other":
        return None
    spec = EVENT_IDENTITY_COMPONENTS.get(event_type)
    if spec is None:
        raise ValueError(f"unknown event_type {event_type!r}")
    required, optional = spec
    components: list[tuple[str, str]] = []
    for name in required:
        raw = fields.get(name)
        if raw is None or not str(raw).strip():
            return None  # 弱证据: no strong identity ⇒ new event
        components.append((name, _normalize(str(raw))))
    for name in optional:
        components.append((name, _normalize(str(fields.get(name, "")))))
    return StrongKey(event_type=event_type, components=tuple(components))


def key_string(key: StrongKey) -> str:
    """Canonical 'type|k=v|...' serialization (DB identity_key column)."""
    body = "|".join(f"{name}={value}" for name, value in key.components)
    return f"{key.event_type}|{body}"


def can_auto_merge(a: StrongKey, b: StrongKey) -> bool:
    """Boolean gate: identical type and every component equal.

    Different values in ANY component (including optional ones) block the
    merge — the conflict signals the spec names (对象、版本、阶段、地区、
    时间) are exactly the components.
    """
    return a.event_type == b.event_type and a.components == b.components


def duplicate_candidates(
    key: StrongKey, existing: Mapping[str, StrongKey]
) -> list[str]:
    """Exact-key matches among existing events (event_id → key).

    Only exact matches auto-relate; near matches (same product, different
    phase; same work, different version) are deliberately NOT candidates
    for automatic linkage — they are different events by definition.
    Similarity-based candidate finding, if ever added, lives in the
    service layer behind human review (05 §2 近似语义相似只能找候选).
    """
    return [
        event_id for event_id, other in existing.items() if can_auto_merge(key, other)
    ]
