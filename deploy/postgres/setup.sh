#!/usr/bin/env bash
# setup.sh — build and start the semiconductor-intel Postgres container on
# ANY podman host over ssh. Arch-agnostic Containerfile; tested targets:
#   - the NAS  (liyifan@192.168.1.21, Armbian aarch64, podman 5.7.0)
#   - the x86 server (192.168.1.82) — faster builds, same image semantics
# Select with HOST=... (or the legacy NAS_HOST=... spelling).
#
# Running it ssh's to the host, builds the image (30-60 min on the NAS,
# much faster on x86 — dominated by the pgvectorscale Rust build) and
# starts the container. On the NAS nothing existing is touched:
#   postgres-server (pgduckdb, publishes no ports), redis, open-webui,
#   the LLM gateway (0.0.0.0:8000) — hands off all four.
#
# Rollback (removes the container; second line also wipes DB data):
#   ssh <host> 'podman rm -f semiconductor-intel-pg'
#   ssh <host> 'rm -rf ~/semiconductor-intel/pgdata/*'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST="${HOST:-${NAS_HOST:-liyifan@192.168.1.21}}"
CONTAINER_NAME="${CONTAINER_NAME:-semiconductor-intel-pg}"
IMAGE_TAG="${IMAGE_TAG:-semiconductor-intel-pg:18}"
HOST_PORT="${HOST_PORT:-5432}"
REMOTE_BASE="${REMOTE_BASE:-~/semiconductor-intel}"
# Memory guard. The default (NAS) host has 7.7 GB total and already runs
# postgres-server, redis, open-webui and the LLM gateway — 5g leaves
# headroom for them and the OS. Raise freely on bigger x86 hosts;
# MEM_LIMIT=0 disables the cap.
MEM_LIMIT="${MEM_LIMIT:-5g}"

usage() {
    cat <<'USAGE'
Usage: PG_PASSWORD=... ./setup.sh [-p <password>] [HOST=...]

  -p <password>   Postgres superuser password (alternative to PG_PASSWORD env)

Environment overrides:
  HOST (liyifan@192.168.1.21; NAS_HOST accepted as the legacy spelling)
  CONTAINER_NAME (semiconductor-intel-pg)  IMAGE_TAG (semiconductor-intel-pg:18)
  HOST_PORT (5432)   REMOTE_BASE (~/semiconductor-intel)   MEM_LIMIT (5g)

Examples:
  # NAS (aarch64) — host port 5433 (5432 is taken by a host-level service):
  HOST_PORT=5433 PG_PASSWORD=... ./setup.sh
  # x86 server .82:
  HOST=liyifan@192.168.1.82 PG_PASSWORD=... ./setup.sh
USAGE
}

PG_PASSWORD="${PG_PASSWORD:-}"
while getopts ":p:h" opt; do
    case "$opt" in
        p) PG_PASSWORD="$OPTARG" ;;
        h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
if [[ -z "$PG_PASSWORD" ]]; then
    echo "ERROR: Postgres password required (PG_PASSWORD env or -p)" >&2
    usage >&2
    exit 2
fi

[[ -f "$SCRIPT_DIR/Containerfile" ]] || { echo "ERROR: $SCRIPT_DIR/Containerfile missing" >&2; exit 2; }
[[ -f "$SCRIPT_DIR/init/40-intel-extensions.sh" ]] || { echo "ERROR: $SCRIPT_DIR/init/40-intel-extensions.sh missing" >&2; exit 2; }

# Run SQL on the container via stdin (keeps Chinese text out of nested quoting).
psql_stdin() {
    ssh "$HOST" "podman exec -i $CONTAINER_NAME psql -U postgres -d postgres -v ON_ERROR_STOP=1 -qAt"
}

echo "== 1/6 prepare remote directories =="
ssh "$HOST" "mkdir -p $REMOTE_BASE/{pgdata,objects,traces,build/init}"

echo "== 2/6 copy build context (Containerfile + init script) =="
scp -q "$SCRIPT_DIR/Containerfile" "$HOST:$REMOTE_BASE/build/Containerfile"
scp -q "$SCRIPT_DIR/init/40-intel-extensions.sh" "$HOST:$REMOTE_BASE/build/init/40-intel-extensions.sh"

port_state="$(ssh "$HOST" "ss -ltn 2>/dev/null | grep -c ':$HOST_PORT ' || true")"
if [[ "$port_state" != "0" ]]; then
    echo "WARN: something already listens on host port $HOST_PORT; the -p mapping below may fail"
fi

echo "== 3/6 podman build on $HOST (pgvectorscale Rust/LTO dominates the time)"
ssh "$HOST" "podman build -t $IMAGE_TAG $REMOTE_BASE/build"

echo "== 4/6 run container =="
# Volume layout note: postgres:18 changed PGDATA to /var/lib/postgresql/18/docker
# with VOLUME /var/lib/postgresql (docker-library/postgres 18/trixie Dockerfile),
# so the host pgdata dir is mounted over /var/lib/postgresql and the image's
# default PGDATA is kept. POSTGRES_PASSWORD travels via the remote command line
# (printf %q); it is briefly visible in `ps` on the NAS — acceptable on the LAN,
# swap in a podman secret if that bothers you.
# The image CMD already carries: -c shared_preload_libraries=pg_textsearch
# (required by pg_textsearch; see Containerfile).
ssh "$HOST" "podman run -d --name $CONTAINER_NAME --replace \
    -p $HOST_PORT:5432 \
    --memory=$MEM_LIMIT \
    -e POSTGRES_PASSWORD=$(printf '%q' "$PG_PASSWORD") \
    -v $REMOTE_BASE/pgdata:/var/lib/postgresql:Z \
    -v $REMOTE_BASE/objects:/var/lib/intel/objects:Z \
    $IMAGE_TAG"

echo "== 5/6 wait for postgres =="
# First boot runs initdb + /docker-entrypoint-initdb.d/40-intel-extensions.sh;
# pg_isready can answer "ready" against the temporary initdb server before the
# init script finishes, so poll for the four extensions as the true-ready gate.
deadline=$((SECONDS + 300))
until ssh "$HOST" "podman exec $CONTAINER_NAME pg_isready -U postgres -d postgres" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        echo "FAIL: postgres not accepting connections within 300s; inspect with:"
        echo "  ssh $NAS_HOST 'podman logs $CONTAINER_NAME'"
        exit 1
    fi
    sleep 5
done

extension_count() {
    psql_stdin 2>/dev/null <<'SQL' || echo 0
SELECT count(*) FROM pg_extension WHERE extname IN ('vector', 'vectorscale', 'pg_textsearch', 'pg_jieba');
SQL
}
deadline=$((SECONDS + 300))
until [[ "$(extension_count)" == "4" ]]; do
    if (( SECONDS >= deadline )); then
        echo "FAIL: extensions did not appear within 300s; inspect with:"
        echo "  ssh $NAS_HOST 'podman logs $CONTAINER_NAME'"
        exit 1
    fi
    sleep 5
done
echo "PASS: postgres up with 4 extensions loaded"

echo "== 6/6 smoke checks =="
FAILURES=0
check() { # check <label> <expected substring> <actual>
    if [[ "$3" == *"$2"* ]]; then
        echo "PASS: $1"
    else
        echo "FAIL: $1 — expected '$2' in output: $3"
        FAILURES=$((FAILURES + 1))
    fi
}

# 6a. all four extensions present
out="$(psql_stdin 2>&1 <<'SQL' || true
SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension
WHERE extname IN ('vector', 'vectorscale', 'pg_textsearch', 'pg_jieba');
SQL
)"
check "extensions installed" "pg_jieba,pg_textsearch,vector,vectorscale" "$out"

# 6b. jieba config (created by 40-intel-extensions.sh) tokenizes Chinese
out="$(psql_stdin 2>&1 <<'SQL' || true
SELECT position('刻蚀' in to_tsvector('jieba', '先进封装 刻蚀 高深宽比')::text) > 0;
SQL
)"
check "to_tsvector('jieba', ...) segments Chinese" "t" "$out"

# 6c. pg_textsearch BM25 over the jieba config.
# Index + score-operator syntax per the pg_textsearch README (verified):
#   CREATE INDEX ... USING bm25 (col) WITH (text_config='...');
#   SELECT ... ORDER BY col <@> 'query' LIMIT n;
# <@> returns NEGATIVE BM25 scores (README: "returns negative BM25 scores for
# ascending index scans, so lower scores rank first") — a matching document
# therefore scores < 0, hence the assertion below.
out="$(psql_stdin 2>&1 <<'SQL' || true
DROP TABLE IF EXISTS _smoke_bm25;
CREATE TABLE _smoke_bm25 (id int PRIMARY KEY, body text);
INSERT INTO _smoke_bm25 VALUES (1, '先进封装技术需要高深宽比刻蚀工艺');
CREATE INDEX _smoke_bm25_idx ON _smoke_bm25 USING bm25 (body) WITH (text_config='jieba');
SELECT (body <@> '刻蚀') < 0 FROM _smoke_bm25 WHERE id = 1;
SQL
)"
check "pg_textsearch bm25 index + <@> score" "t" "$out"

# 6d. pgvectorscale diskann + pgvector KNN.
# Syntax per the pgvectorscale README (verified):
#   CREATE INDEX ... USING diskann (emb vector_cosine_ops);
#   SELECT ... ORDER BY emb <=> $1 LIMIT n;
out="$(psql_stdin 2>&1 <<'SQL' || true
DROP TABLE IF EXISTS _smoke_vec;
CREATE TABLE _smoke_vec (id int PRIMARY KEY, emb vector(4));
INSERT INTO _smoke_vec VALUES (1, '[0.1, 0.2, 0.3, 0.4]');
CREATE INDEX _smoke_vec_idx ON _smoke_vec USING diskann (emb vector_cosine_ops);
SELECT count(*) FROM (SELECT id FROM _smoke_vec ORDER BY emb <=> '[0.1, 0.2, 0.3, 0.4]' LIMIT 1) AS hit;
SQL
)"
check "diskann index + <=> KNN query" "1" "$out"

# smoke cleanup (guarded — a failed block may have left a table behind)
psql_stdin >/dev/null 2>&1 <<'SQL' || true
DROP TABLE IF EXISTS _smoke_bm25;
DROP TABLE IF EXISTS _smoke_vec;
SQL

if (( FAILURES > 0 )); then
    echo "RESULT: $FAILURES smoke check(s) FAILED"
    exit 1
fi
echo "RESULT: all smoke checks PASSED"
echo "Connect with: postgresql+asyncpg://postgres:<PG_PASSWORD>@${HOST#*@}:$HOST_PORT/postgres"
