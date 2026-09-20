"""Conversation coordination + answer evidence packets (16 §6, 14 §5).

The multi-turn contract, as data structures and a coordinator that is
pure over injected stores (offline-testable; the API layer wraps it):

- **Serial turns**: every MessageCreate carries ``parent_message_id`` =
  the last committed assistant message. A turn submitted against any
  other parent than the conversation's ``last_committed_message_id`` is
  CHAT-04-rejected (409-class) — concurrent sends cannot interleave.
- **state_version**: optimistic check on submit; a moved version means
  the client read stale state (多轮网页读最新state后发送).
- **Constraints**: ``{id, text, kind, source_message_id, status}``
  accumulate across turns; a later turn may relax what an earlier turn
  constrained, and the history keeps both.
- **EvidencePacket** (14 §5): scope-bound immutable answer materials —
  fixed claim/event revisions, exact quotes with block context,
  ``allowed_citation_ids`` (the ONLY citable ids), and
  ``actually_read_blocks`` (what the answering pass really read). In
  archive mode with no usable evidence the packet is empty and the
  answer is ``insufficient_evidence`` with ZERO tool calls (QA-01) —
  missing evidence never becomes a web search in archive mode.

The QueryPlanner resolution step (CHAT-01 pronouns → referenced ids)
is an agent call the coordinator schedules; tests inject a fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

__all__ = [
    "CHAT_TURN_CONFLICT",
    "ConversationConflict",
    "ConversationCoordinator",
    "ConversationRecord",
    "EvidencePacket",
    "MessageIn",
    "StateVersionStale",
    "build_evidence_packet",
]


class ConversationConflict(Exception):
    """409-class conversation turn rejection."""

    code: str = "conversation_conflict"


class StateVersionStale(ConversationConflict):
    """Client submitted against a moved state_version (16 §6 step 1)."""

    code = "state_version_stale"


class ParentMismatch(ConversationConflict):
    """CHAT-04: turn's parent is not the last committed message."""

    code = "parent_message_mismatch"


CHAT_TURN_CONFLICT = "chat_turn_conflict"


@dataclass(slots=True)
class MessageIn:
    """One submitted user turn (MessageCreate projection)."""

    text: str
    parent_message_id: UUID | None
    state_version: int
    constraints: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class CommittedTurn:
    user_message_id: UUID
    assistant_message_id: UUID
    state_version: int
    turn_index: int
    referenced_ids: list[str] = field(default_factory=list)


class Mode(StrEnum):
    ARCHIVE = "archive"
    ONLINE = "online"


@dataclass(slots=True)
class ConversationRecord:
    """The coordinator's mutable view of one conversation."""

    owner_id: UUID
    industry_id: UUID
    state_version: int = 1
    last_committed_message_id: UUID | None = None
    turn_counter: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    # CHAT-04 test seam: set False to simulate a concurrent writer holding
    # the serial lock.
    lock_free: bool = True


class ConversationCoordinator:
    """16 §6 steps 1-2 over one conversation record (pure; no I/O)."""

    def check_turn(self, conversation: ConversationRecord, turn: MessageIn) -> None:
        """Step 1: scope + state_version + serial parent. Raises 409-class."""
        if turn.state_version != conversation.state_version:
            raise StateVersionStale(
                f"state_version {turn.state_version} != current"
                f" {conversation.state_version}; re-read the conversation"
            )
        if not conversation.lock_free:
            raise ParentMismatch("another turn holds the serial commit slot")
        if turn.parent_message_id != conversation.last_committed_message_id:
            raise ParentMismatch(
                f"parent_message_id {turn.parent_message_id} is not the last"
                " committed assistant message"
                f" ({conversation.last_committed_message_id})"
            )

    def commit_turn(
        self,
        conversation: ConversationRecord,
        turn: MessageIn,
        *,
        assistant_text: str,
        referenced_ids: list[str] | None = None,
    ) -> CommittedTurn:
        """Steps 5-6: append the message pair, bump state, keep constraints.

        The conversation_summary compression (CHAT-06) keeps the ORIGINAL
        messages and appends a summary — history is never rewritten.
        """
        self.check_turn(conversation, turn)
        conversation.turn_counter += 1
        user_message_id = uuid4()
        assistant_message_id = uuid4()
        for constraint in turn.constraints:
            conversation.constraints.append(
                {
                    "id": str(constraint.get("id") or uuid4()),
                    "text": str(constraint.get("text", "")),
                    "kind": str(constraint.get("kind", "scope")),
                    "source_message_id": str(user_message_id),
                    "status": str(constraint.get("status", "active")),
                }
            )
        conversation.messages.append(
            {
                "id": str(user_message_id),
                "role": "user",
                "text": turn.text,
                "turn_index": conversation.turn_counter,
                "parent_message_id": (
                    str(turn.parent_message_id) if turn.parent_message_id else None
                ),
            }
        )
        conversation.messages.append(
            {
                "id": str(assistant_message_id),
                "role": "assistant",
                "text": assistant_text,
                "turn_index": conversation.turn_counter,
                "parent_message_id": str(user_message_id),
                "referenced_ids": referenced_ids or [],
            }
        )
        conversation.last_committed_message_id = assistant_message_id
        conversation.state_version += 1
        return CommittedTurn(
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            state_version=conversation.state_version,
            turn_index=conversation.turn_counter,
            referenced_ids=referenced_ids or [],
        )

    def context_snapshot(
        self, conversation: ConversationRecord, *, recent_turns: int = 6
    ) -> dict[str, Any]:
        """16 §6 context priority: system → constraints → summary → recent."""
        recent = conversation.messages[-(recent_turns * 2) :]
        return {
            "state_version": conversation.state_version,
            "constraints": list(conversation.constraints),
            "summaries": list(conversation.summaries),
            "recent_messages": recent,
            "last_committed_message_id": (
                str(conversation.last_committed_message_id)
                if conversation.last_committed_message_id
                else None
            ),
        }

    def append_summary(self, conversation: ConversationRecord, summary: str) -> None:
        """CHAT-06: compression keeps originals; summaries only append."""
        conversation.summaries.append(summary)


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """14 §5 answer materials — the only citable universe for one answer."""

    packet_id: UUID
    as_of: datetime
    question: str
    mode: Mode
    claims: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    allowed_citation_ids: frozenset[str] = frozenset()
    actually_read_blocks: frozenset[str] = frozenset()
    coverage: dict[str, Any] = field(default_factory=dict)
    conflicts: tuple[dict[str, Any], ...] = ()


def build_evidence_packet(
    *,
    question: str,
    mode: Mode,
    claims: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    read_blocks: list[str] | None = None,
) -> EvidencePacket:
    """Assemble one packet; citation universe == supplied evidence ids.

    Archive mode with no claims/events/evidence yields the EMPTY packet —
    the answer flow must then return insufficient_evidence WITHOUT any
    tool call (QA-01): missing evidence never silently becomes a web
    search.
    """
    claims = tuple(claims or ())
    events = tuple(events or ())
    evidence = tuple(evidence or ())
    allowed = frozenset(
        str(item["id"]) for item in evidence if item.get("id") is not None
    )
    return EvidencePacket(
        packet_id=uuid4(),
        as_of=datetime.now(UTC),
        question=question,
        mode=mode,
        claims=claims,
        events=events,
        evidence=evidence,
        allowed_citation_ids=allowed,
        actually_read_blocks=frozenset(read_blocks or ()),
        coverage=dict(coverage or {}),
        conflicts=tuple(conflicts or ()),
    )
