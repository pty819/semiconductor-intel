"""Integration tests: retrieval channels against real Postgres (04 §6, 03 §8).

Skipped unless ``INTEL_TEST_DATABASE_URL`` points at a maintenance database
(the test_jobs.py harness pattern): the suite creates a dedicated database,
runs alembic head — migration 0003 must therefore install pg_trgm, the
jieba text search configuration, the BM25 index and the trigram GIN index
on a bare database — and exercises, owner-scoped as intel_app:

- jieba-tokenized BM25 via ``<@>``: a Chinese semiconductor term matches,
  non-matching documents do not, scores come back NEGATIVE (pg_textsearch
  semantics verified in Task 9) and ``positive_bm25_score`` flips them;
- the trigram alias channel on ``normalized_terms``;
- exact KNN ordering via ``<=>`` with the vector literal cast.

The diskann-vs-exact golden-set comparison (REC-08) stays offline: the
harness is ``intel.retrieval.vector.recall_at_k`` over two result sets.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from intel.db.rls import set_scope
from intel.retrieval.bm25 import (
    bm25_search_stmt,
    build_bm25_query,
    positive_bm25_score,
)
from intel.retrieval.recall import RawHit, alias_search_stmt
from intel.retrieval.vector import knn_search_stmt

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("INTEL_TEST_DATABASE_URL"),
        reason="INTEL_TEST_DATABASE_URL not set (needs reachable Postgres"
        " with the Task 9 extension image)",
    ),
]


async def _create_database(admin_url: str) -> str:
    url = make_url(admin_url)
    dbname = f"intel_retrieval_test_{uuid4().hex[:8]}"
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}" TEMPLATE template1'))
    finally:
        await engine.dispose()
    return str(url.set(database=dbname))


async def _drop_database(admin_url: str, dbname: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def _run_migrations(db_url: str) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
async def db_url() -> AsyncIterator[str]:
    admin_url = os.environ["INTEL_TEST_DATABASE_URL"]
    url = await _create_database(admin_url)
    try:
        await asyncio.to_thread(_run_migrations, url)
        yield url
    finally:
        await _drop_database(admin_url, make_url(url).database)


@pytest.fixture(scope="module")
async def engine(db_url: str):
    engine = create_async_engine(db_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO intel_app"))
        await conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA"
                " public TO intel_app"
            )
        )
    yield engine
    await engine.dispose()


@pytest.fixture()
async def scoped_ids(engine) -> AsyncIterator[tuple[UUID, UUID]]:
    """Seed owner + industry + one bound document chain, owner-scoped."""
    owner, industry, document, capture, blob, parse = (uuid4() for _ in range(6))
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO users (id, login, password_hash, timezone,"
                " password_version) VALUES (:id, :login, 'x', 'UTC', 1)"
            ),
            {"id": str(owner), "login": f"u-{owner.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO industries (id, owner_id, name, status,"
                " created_at, updated_at, row_version)"
                " VALUES (:id, :owner, 'semis', 'active', now(), now(), 1)"
            ),
            {"id": str(industry), "owner": str(owner)},
        )
        await conn.execute(text("SET ROLE intel_app"))
        await set_scope(conn, owner, industry)
        await conn.execute(
            text(
                "INSERT INTO blobs (id, owner_id, object_key, sha256,"
                " media_type, byte_size, retention_class)"
                " VALUES (:id, :owner, 'k', :sha, 'text/html', 10, 'raw')"
            ),
            {"id": str(blob), "owner": str(owner), "sha": uuid4().hex * 2},
        )
        await conn.execute(
            text(
                "INSERT INTO documents (id, owner_id, canonical_url,"
                " identity_namespace, identity_value, visibility_scope_key,"
                " origin_kind)"
                " VALUES (:id, :owner, 'https://example.com/a', 'url',"
                " 'https://example.com/a', 'public', 'feed')"
            ),
            {"id": str(document), "owner": str(owner)},
        )
        await conn.execute(
            text(
                "INSERT INTO captures (id, owner_id, document_id, raw_blob_id,"
                " response_status, effective_url, content_hash,"
                " content_type, retrieval_scope, access_policy)"
                " VALUES (:id, :owner, :document, :blob, 200,"
                " 'https://example.com/a', :content, 'text/html',"
                " 'fulltext', 'public')"
            ),
            {
                "id": str(capture),
                "owner": str(owner),
                "document": str(document),
                "blob": str(blob),
                "content": uuid4().hex,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO parsed_artifacts (id, owner_id, capture_id,"
                " parser_version_id, normalized_blob_id, text_hash, blocks,"
                " metadata, parse_status, coverage, quality_flags)"
                " VALUES (:id, :owner, :capture, :parser, :norm, :thash,"
                " '[]'::jsonb, '{}'::jsonb, 'ok', '{}'::jsonb, '{}')"
            ),
            {
                "id": str(parse),
                "owner": str(owner),
                "capture": str(capture),
                "parser": str(uuid4()),
                "norm": str(blob),
                "thash": uuid4().hex,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO industry_documents (id, owner_id, industry_id,"
                " document_id, current_parse_id, relevance,"
                " association_reason, active)"
                " VALUES (:id, :owner, :industry, :document, :parse,"
                " 'background', 'seed', true)"
            ),
            {
                "id": str(uuid4()),
                "owner": str(owner),
                "industry": str(industry),
                "document": str(document),
                "parse": str(parse),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO chunks (id, owner_id, parsed_artifact_id,"
                ' ordinal, block_ids, "text", language, normalized_terms)'
                " VALUES (:id, :owner, :parse, 0, '{\"b000\"}',"
                " :body, 'zh', :terms)"
            ),
            {
                "id": str(uuid4()),
                "owner": str(owner),
                "parse": str(parse),
                "body": "半导体刻蚀工艺进展：等离子刻蚀设备在 3nm 节点的用量持续上升。",
                "terms": "半导体 刻蚀 工艺 进展 等离子 刻蚀 设备 3nm"
                " 节点 用量 持续 上升",
            },
        )
    yield owner, industry
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(owner)})


class TestBm25Channel:
    async def test_jieba_term_matches_with_negative_score(
        self, engine, scoped_ids
    ) -> None:
        owner, industry = scoped_ids
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, owner, industry)
            rows = (
                await conn.execute(
                    bm25_search_stmt(),
                    {
                        "owner_id": owner,
                        "industry_id": industry,
                        "bm25_q": build_bm25_query(["刻蚀"]),
                        "limit": 10,
                    },
                )
            ).mappings()
            hits = [dict(r) for r in rows]
        assert hits, "jieba BM25 must match the 刻蚀 chunk"
        assert all(hit["bm25_score"] < 0 for hit in hits)
        assert all(positive_bm25_score(hit["bm25_score"]) > 0 for hit in hits)


class TestAliasChannel:
    async def test_trigram_alias_match(self, engine, scoped_ids) -> None:
        owner, industry = scoped_ids
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, owner, industry)
            rows = (
                await conn.execute(
                    alias_search_stmt(),
                    {
                        "owner_id": owner,
                        "industry_id": industry,
                        "alias": "刻蚀设备",
                        "limit": 10,
                    },
                )
            ).mappings()
            hits = [dict(r) for r in rows]
        assert hits
        assert hits[0]["alias_score"] > 0


class TestExactKnn:
    async def test_nearest_vector_first(self, engine, scoped_ids) -> None:
        owner, industry = scoped_ids
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, owner, industry)
            # Two embedded chunks; the query vector sits exactly on A.
            for ordinal, vec in ((1, "[1,0,0]"), (2, "[0,1,0]")):
                await conn.execute(
                    text(
                        "INSERT INTO chunks (id, owner_id,"
                        ' parsed_artifact_id, ordinal, block_ids, "text",'
                        " language, normalized_terms, embedding)"
                        " VALUES (:id, :owner,"
                        " (SELECT current_parse_id FROM industry_documents"
                        "  WHERE owner_id = :owner AND industry_id"
                        "  = :industry LIMIT 1),"
                        f" {ordinal}, '{{\"b00{ordinal}\"}}', 'vec', 'en',"
                        " 'vec', :vec::vector)"
                    ),
                    {
                        "id": str(uuid4()),
                        "owner": str(owner),
                        "industry": str(industry),
                        "vec": vec,
                    },
                )
            rows = (
                await conn.execute(
                    knn_search_stmt(),
                    {
                        "owner_id": owner,
                        "industry_id": industry,
                        "qvec": "[1,0,0]",
                        "limit": 10,
                    },
                )
            ).mappings()
            hits = [dict(r) for r in rows]
        assert hits
        assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)


class TestRawHitShape:
    def test_rawhit_is_channel_agnostic(self) -> None:
        hit = RawHit(
            channel="bm25",
            industry_document_id=uuid4(),
            parse_id=uuid4(),
            chunk_id=uuid4(),
            block_ids=("b000",),
            score=1.5,
            reason="test",
        )
        assert hit.channel == "bm25"
