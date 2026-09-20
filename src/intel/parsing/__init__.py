"""Parsing subpackage: Parser + HTML/PDF/text extraction, block diffs,
quality checks (Task 8; spec 04 §4, 14 §2, 03 §3)."""

from intel.parsing.blocksdiff import (
    BLOCK_DIFF_ALGORITHM,
    DiffResult,
    diff_blocks,
)
from intel.parsing.dto import (
    BUILTIN_PARSER_VERSION_ID,
    PARSER_KEY,
    PARSER_VERSION,
    Block,
    ExtractedDocument,
    ParsedArtifact,
    ParserInput,
)
from intel.parsing.parser import Parser
from intel.parsing.quality import (
    Assessment,
    QualitySignals,
    QualityThresholds,
    assess_quality,
    detect_language,
)
from intel.parsing.textnorm import clean_block_text, decode_bytes, normalize_text

__all__ = [
    "BLOCK_DIFF_ALGORITHM",
    "BUILTIN_PARSER_VERSION_ID",
    "PARSER_KEY",
    "PARSER_VERSION",
    "Assessment",
    "Block",
    "DiffResult",
    "ExtractedDocument",
    "ParsedArtifact",
    "Parser",
    "ParserInput",
    "QualitySignals",
    "QualityThresholds",
    "assess_quality",
    "clean_block_text",
    "decode_bytes",
    "detect_language",
    "diff_blocks",
    "normalize_text",
]
