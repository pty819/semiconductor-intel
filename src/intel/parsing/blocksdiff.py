"""Block-level diff between two parses (Task 8; spec 04 §4, PAR-01).

``diff_blocks(from_artifact, to_artifact, algorithm_version)``:

- the KIND is decided by whole-document normalized-text equality
  (:func:`document_text_hash` — re-segmentation-proof, see textnorm)
  plus parser-version identity, NOT by block boundaries: a parser
  upgrade that merges/splits unchanged paragraphs is ``parser_change``
  with block-level detail for inspection, never a content event;
- LCS over per-block text hashes (NFC) populates ``changed_blocks``:
  everything off the stable spine is removed/added, and
  positionally-paired replacements above the similarity threshold
  become ``changed`` entries carrying block refs from BOTH parses;
- ``field_changes`` records metadata-level changes (title/author/date);
- kind rules (documented, event pipelines key off the kind):
  text equal + versions differ → ``parser_change`` (PAR-01); text
  equal + same version + field changes → ``content_change`` (a genuine
  server-side metadata edit under the same parser); text equal + same
  version + no field changes → ``parser_change`` (segmentation only);
  text changed + field changes → ``mixed``; text changed only →
  ``content_change``.

The workflow persists the result into ``document_diffs`` under
UNIQUE(from, to, algorithm).
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from intel.parsing.textnorm import canonical_document_text

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from intel.parsing.dto import ParsedArtifact

#: Bump when the diff algorithm itself changes (stored per row, 03 §3).
BLOCK_DIFF_ALGORITHM = "blockslcs@1"

#: Metadata fields diffed as field_changes (04 §4 关键元数据).
FIELD_KEYS = ("title", "author", "date", "published_at")

#: Similarity over which a removed+added pair reads as one edited block.
DEFAULT_SIMILARITY_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class DiffResult:
    kind: str  # content_change | parser_change | mixed
    changed_blocks: list[dict] = field(default_factory=list)
    field_changes: list[dict] = field(default_factory=list)
    algorithm_version: str = BLOCK_DIFF_ALGORITHM


def block_text_hash(block: dict) -> str:
    """Content identity of one block: NFC text digest (kind-agnostic so
    a parser relabeling blocks does not mint content changes)."""
    normalized = unicodedata.normalize("NFC", block.get("text", ""))
    return hashlib.sha256(normalized.encode()).hexdigest()


def document_text_hash(artifact: ParsedArtifact) -> str:
    """Whole-document normalized-text digest — the PAR-01 原文未变 test.

    Computed from the blocks (not the stored field) with the
    re-segmentation-proof canonicalization from textnorm, so merging or
    splitting paragraphs cannot flip the kind."""
    texts = [
        block.text if hasattr(block, "text") else block.get("text", "")
        for block in artifact.blocks
    ]
    return hashlib.sha256(
        canonical_document_text(texts).encode()
    ).hexdigest()


def _as_dict(block) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return dict(vars(block))


def diff_blocks(
    from_artifact: ParsedArtifact,
    to_artifact: ParsedArtifact,
    algorithm_version: str = BLOCK_DIFF_ALGORITHM,
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> DiffResult:
    old_hashes = [
        block_text_hash(_as_dict(block)) for block in from_artifact.blocks
    ]
    new_hashes = [
        block_text_hash(_as_dict(block)) for block in to_artifact.blocks
    ]

    changed: list[dict] = []
    matcher = SequenceMatcher(
        None, old_hashes, new_hashes, autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            changed.extend(
                _entry("removed", from_artifact.blocks[index], "from_")
                for index in range(i1, i2)
            )
        elif tag == "insert":
            changed.extend(
                _entry("added", to_artifact.blocks[index], "to_")
                for index in range(j1, j2)
            )
        else:  # replace: pair positionally, split off weak pairs
            old_range = list(range(i1, i2))
            new_range = list(range(j1, j2))
            for offset in range(max(len(old_range), len(new_range))):
                if offset < len(old_range) and offset < len(new_range):
                    old_block = from_artifact.blocks[old_range[offset]]
                    new_block = to_artifact.blocks[new_range[offset]]
                    ratio = SequenceMatcher(
                        None, old_block.text, new_block.text
                    ).ratio()
                    if ratio >= similarity_threshold:
                        changed.append(_pair_entry(old_block, new_block))
                        continue
                if offset < len(old_range):
                    changed.append(
                        _entry(
                            "removed",
                            from_artifact.blocks[old_range[offset]],
                            "from_",
                        )
                    )
                if offset < len(new_range):
                    changed.append(
                        _entry(
                            "added",
                            to_artifact.blocks[new_range[offset]],
                            "to_",
                        )
                    )

    field_changes = _field_changes(
        from_artifact.metadata, to_artifact.metadata
    )
    return DiffResult(
        kind=_classify(
            document_text_hash(from_artifact),
            document_text_hash(to_artifact),
            field_changes,
            from_artifact.parser_version,
            to_artifact.parser_version,
        ),
        changed_blocks=changed,
        field_changes=field_changes,
        algorithm_version=algorithm_version,
    )


def _classify(
    old_doc_hash: str,
    new_doc_hash: str,
    field_changes: list[dict],
    from_version: str,
    to_version: str,
) -> str:
    """Event-signal semantics (kind only; changed_blocks is detail):

    - Whole-document text equal + different parser version →
      ``parser_change``: re-segmentation or better metadata extraction by
      a new parser is not a 新闻内容更新事件 (PAR-01).
    - Text equal + SAME parser + field changes → ``content_change``: the
      same parser re-read the document and only metadata moved — a
      genuine server-side metadata edit, event-worthy.
    - Text equal + same parser + no field changes → ``parser_change``
      (pure segmentation detail).
    - Text changed + field changes → ``mixed``; text changed only →
      ``content_change``.
    """
    content_unchanged = old_doc_hash == new_doc_hash
    if content_unchanged:
        if from_version == to_version:
            return "content_change" if field_changes else "parser_change"
        return "parser_change"
    return "mixed" if field_changes else "content_change"


def _entry(change: str, block, prefix: str) -> dict:
    data = _block_ref(block, prefix=prefix)
    data["change"] = change
    return data


def _pair_entry(old_block, new_block) -> dict:
    old_ref = _block_ref(old_block, prefix="from_")
    new_ref = _block_ref(new_block, prefix="to_")
    return {"change": "changed", **old_ref, **new_ref}


def _block_ref(block, prefix: str) -> dict:
    block_dict = _as_dict(block)
    ref = {
        f"{prefix}block_id": block_dict["block_id"],
        f"{prefix}kind": block_dict["kind"],
        f"{prefix}text_hash": block_text_hash(block_dict),
    }
    return ref


def _field_changes(old: dict, new: dict) -> list[dict]:
    changes: list[dict] = []
    for key in FIELD_KEYS:
        old_value = old.get(key)
        new_value = new.get(key)
        if old_value != new_value:
            changes.append({"field": key, "from": old_value, "to": new_value})
    return changes
