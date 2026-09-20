"""Parsing DTOs (spec 14 §2): ParserInput / ParsedArtifact / Block /
ExtractedDocument.

Kept beside the parsers (not in ``contracts``) because these are
service-internal shapes, same ruling as ``sources.dto``. The parser has
no DB access — ``ParserInput`` carries capture metadata + raw bytes only,
and ``ParsedArtifact`` is plain data the workflow persists.

Blocks are the ordered array of spec 03 §3: ``{block_id, kind, text,
page?, section_path[], bbox?, source_locator?}``; ``block_id`` is unique
and immutable within one artifact (ordinal + content digest), and text
is NFC-normalized so char offsets are Unicode code points, 左闭右开.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: Block kinds (03 §3 examples; tables keep header+row groups, PAR-02).
BlockKind = Literal["heading", "paragraph", "table", "caption"]

#: parse_status vocabulary (03 §3).
ParseStatus = Literal["ok", "partial", "failed"]

#: retrieval_scope vocabulary (03 §3 / 04 §4).
RetrievalScope = Literal["metadata", "abstract", "partial", "fulltext"]

#: This parser implementation's release label (04 §4: parser 首版人工发布,
#: bumped when extraction behavior changes so re-parses mint new artifacts).
PARSER_KEY = "builtin"
PARSER_VERSION = "builtin@1"

#: Deterministic id for the built-in parser's parser_versions row; the
#: release process must seed the matching G-scope row (03 §2) before the
#: SQL store is used in production. In-memory tests skip the FK.
BUILTIN_PARSER_VERSION_ID: UUID = uuid.uuid5(
    UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"),  # NAMESPACE_URL
    "https://intel.example.com/parser/builtin@1",
)


class ParsingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParserInput(ParsingModel):
    """What the parser sees of one capture (14 §2: parser 无 DB 权限)."""

    capture_id: UUID
    media_type: str
    raw: bytes
    parser_version: str = PARSER_VERSION


class Block(ParsingModel):
    block_id: str
    kind: BlockKind
    text: str
    page: int | None = None
    section_path: list[str] = Field(default_factory=list)
    #: PDF best-effort, nullable (04 §4); [x0, y0, x1, y1] user-space.
    bbox: list[float] | None = None
    #: HTML: selector path; PDF: none; plain text: line/offset marker.
    source_locator: str | None = None


class ParsedArtifact(ParsingModel):
    """Parser output contract (14 §2). Block 永久绑定本次 parse."""

    capture_id: UUID
    parser_version: str = PARSER_VERSION
    metadata: dict[str, Any] = Field(default_factory=dict)
    blocks: list[Block] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    parse_status: ParseStatus = "failed"
    quality_flags: list[str] = Field(default_factory=list)
    text_hash: str = ""
    retrieval_scope: RetrievalScope = "metadata"


@dataclass(slots=True)
class ExtractedDocument:
    """Media-specific extraction output before quality assessment."""

    blocks: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    table_meta: list[dict] = field(default_factory=list)
    #: Signals the quality layer needs (ratios, detection hits).
    text_chars: int = 0
    boilerplate_ratio: float = 0.0
    structure_hit_rate: float = 1.0
    link_text_chars: int = 0
    login_detected: bool = False
    challenge_detected: bool = False
    encoding_replaced: bool = False
    full_text: str = ""
