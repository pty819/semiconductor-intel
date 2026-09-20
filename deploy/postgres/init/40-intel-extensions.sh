#!/bin/bash
# 40-intel-extensions.sh — first-boot initialization for semiconductor-intel-pg.
#
# The postgres entrypoint runs /docker-entrypoint-initdb.d/*.sh exactly once,
# on an empty PGDATA, as the database superuser. Re-running the container on
# an existing volume skips this file (extensions already present).
#
# Extension versions installed by the Containerfile (verified 2026-09-20):
#   vector        0.8.6   https://github.com/pgvector/pgvector
#   vectorscale   0.9.1   https://github.com/timescale/pgvectorscale
#   pg_textsearch 1.4.0   https://github.com/timescale/pg_textsearch
#   pg_jieba      1.1.1   https://github.com/jaiminpan/pg_jieba
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
-- pgvector first: pgvectorscale depends on it (CASCADE would also pull it in,
-- per the pgvectorscale README, but pre-creating makes the pin explicit).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE EXTENSION IF NOT EXISTS pg_jieba;

-- Custom text search config `jieba` for pg_textsearch BM25 indexes
-- (referenced later as WITH (text_config='jieba')).
--
-- The parser object registered by pg_jieba is named `jieba` — NOT `pg_jieba`
-- (verified in pg_jieba.sql:
--   https://github.com/jaiminpan/pg_jieba/blob/master/pg_jieba.sql
--  which runs `CREATE TEXT SEARCH PARSER jieba (...)`; pg_jieba also ships
--  ready-made configs jiebacfg/jiebamp/jiebahmm/jiebaqry, but we want the
--  design-D13 mapping: selected POS tags -> `simple` dictionary).
CREATE TEXT SEARCH CONFIGURATION jieba (PARSER = jieba);

-- Token types n,v,a,i,e,l all exist in pg_jieba's lexer table (verified in
-- jieba_token.h lex_descr[]:
--   https://github.com/jaiminpan/pg_jieba/blob/master/jieba_token.h )
ALTER TEXT SEARCH CONFIGURATION jieba ADD MAPPING FOR n,v,a,i,e,l WITH simple;
EOSQL
