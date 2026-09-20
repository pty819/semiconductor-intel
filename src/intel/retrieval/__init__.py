"""Retrieval layer (spec 04 §5/§6, 03 §8): chunker, index writer, and the
four-channel local recall service.

Modules:

- :mod:`intel.retrieval.chunker` — pure blocks → chunks (04 §5).
- :mod:`intel.retrieval.indexer` — the ``index`` job handler: chunk rows +
  embeddings + :class:`IndexManifest` (14 §2).
- :mod:`intel.retrieval.bm25` — pg_textsearch ``<@>`` channel (jieba).
- :mod:`intel.retrieval.vector` — exact KNN channel + the REC-08 diskann
  recall-gate harness.
- :mod:`intel.retrieval.recall` — :class:`RecallService` orchestrating the
  four channels with RRF ordering and batch cursors (04 §6 B).
"""
