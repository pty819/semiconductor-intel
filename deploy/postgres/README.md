# Postgres Deployment (`semiconductor-intel-pg`)

Postgres 18 container for semiconductor-intel, built and run on ANY
podman host over ssh — the Containerfile is arch-agnostic (all four
extensions build from source on both aarch64 and x86_64). Tested
targets:

| Host | Notes |
|---|---|
| NAS `liyifan@192.168.1.21` | Armbian 26.8.3 aarch64, podman 5.7.0, 7.7 GB RAM. Host port 5432 is taken by a host-level service → publish 5433. Build is slow (Rust on weak ARM). |
| x86 `192.168.1.82` | Faster builds; use the default 5432 unless something listens. |

It carries the four retrieval extensions selected in design D13/D14:
BM25 full-text search over jieba-segmented Chinese (pg_textsearch +
pg_jieba) and vector search (pgvector + pgvectorscale diskann). Note:
images are native-arch — an x86-built image does not run on the aarch64
NAS; build on the host that will run the container (or add `--arch`
cross-building later if needed).

Per the task ruling these artifacts are **written now, executed during 联调**
(`先把代码写完`): nothing here has been built or run against the NAS yet.

## Files

| File | Role |
|---|---|
| `Containerfile` | postgres:18 base + source builds of the four extensions, build-dep cleanup in the same layer, initdb script baked in, `shared_preload_libraries` baked into CMD |
| `init/40-intel-extensions.sh` | Runs once on first boot (empty PGDATA): `CREATE EXTENSION` x4 + `CREATE TEXT SEARCH CONFIGURATION jieba (PARSER = jieba)` + POS mapping |
| `setup.sh` | End-to-end deploy: remote dirs → scp build context → `podman build` → `podman run` → wait-for-ready → PASS/FAIL smoke checks |

Run with (see `usage` inside for all knobs):

```bash
# NAS (aarch64), host port 5433:
HOST_PORT=5433 PG_PASSWORD=... ./deploy/postgres/setup.sh
# x86 server:
HOST=liyifan@192.168.1.82 PG_PASSWORD=... ./deploy/postgres/setup.sh
```

## Verified extension versions, licenses, sources

All pins and syntax below were verified against upstream on 2026-09-20.

| Extension | Pin | License | Upstream | Verified syntax we rely on |
|---|---|---|---|---|
| pgvector | `v0.8.6` | PostgreSQL License | https://github.com/pgvector/pgvector | `CREATE EXTENSION vector`; KNN: `ORDER BY emb <=> '[...]' LIMIT n` |
| pgvectorscale | tag `0.9.1` | PostgreSQL License | https://github.com/timescale/pgvectorscale | `CREATE EXTENSION vectorscale CASCADE`; `CREATE INDEX ... USING diskann (emb vector_cosine_ops)`; pgrx pinned `=0.16.1` (its Cargo.toml at 0.9.1) |
| pg_textsearch | `v1.4.0` | PostgreSQL License | https://github.com/timescale/pg_textsearch | `CREATE EXTENSION pg_textsearch`; `CREATE INDEX ... USING bm25 (col) WITH (text_config='jieba')`; score operator `col <@> 'query'` — **returns negative BM25 scores** ("returns negative BM25 scores for ascending index scans, so lower scores rank first"), use as `ORDER BY ... <@> ...`; **requires `shared_preload_libraries='pg_textsearch'`** |
| pg_jieba | 1.1.1 @ `d0ffac8` (last master commit 2022-11-22; repo has no newer release) | BSD-3-Clause | https://github.com/jaiminpan/pg_jieba | parser object is named **`jieba`** (not `pg_jieba` — see its `pg_jieba.sql`); `to_tsvector('jieba', ...)`; token types `n,v,a,i,e,l` all exist (`jieba_token.h`) |

Two facts that differ from the original task sketch, both verified:

- The PG18 server-dev package on the postgres:18 image's trixie-pgdg repo is
  **`postgresql-server-dev-18`** (checked the actual
  [repo package index](https://apt.postgresql.org/pub/repos/apt/dists/trixie-pgdg/main/binary-arm64/Packages.gz)),
  not `postgresql-dev-18`.
- `pg_jieba` does **not** need `shared_preload_libraries` for our usage: its
  README states only the `pg_jieba.*` GUCs (custom dictionaries:
  `pg_jieba.user_dict` etc.) require preloading. We use the built-in
  dictionaries, so only `pg_textsearch` is preloaded. If a custom user dict
  is added later, change the image CMD to
  `shared_preload_libraries=pg_textsearch,pg_jieba`.

## Build-time expectations (aarch64, 7.7 GB host)

- pgvector / pg_textsearch: C builds, minutes each.
- pg_jieba: cmake + cppjieba (submodule), a few minutes.
- **pgvectorscale: Rust/pgrx with `lto = "fat"`, `codegen-units = 1` —
  expect roughly 20-40 min on this box.** The Containerfile pins
  `cargo-pgrx 0.16.1` and caps `CARGO_BUILD_JOBS=2` as a RAM guard.
- Total image build: ~30-60 min; transient build deps + Rust toolchain are
  purged in the same layer, final image is roughly base + ~100 MB of
  extension binaries.
- Fallback: pgvectorscale 0.9.1 ships prebuilt artifacts including
  `pgvectorscale-0.9.1-pg18-arm64.zip`
  (https://github.com/timescale/pgvectorscale/releases/tag/0.9.1). We still
  build from source because the zip's install layout is not documented;
  if the source build proves too heavy on the NAS, that zip is the escape
  hatch (`# VERIFY AT FIRST RUN`: layout unverified).

## Host layout & port

- Container `semiconductor-intel-pg`, host port `5432` → container 5432
  (host 5432 verified free; the NAS's existing `postgres-server` publishes
  no ports).
- postgres:18 changed its layout (`PGDATA=/var/lib/postgresql/18/docker`,
  `VOLUME /var/lib/postgresql`), so the host bind is
  `~/semiconductor-intel/pgdata:/var/lib/postgresql:Z` and the image-default
  PGDATA is kept; data lands at `~/semiconductor-intel/pgdata/18/docker` on
  the host.
- `~/semiconductor-intel/objects` → `/var/lib/intel/objects:Z` for fetched
  PDFs/objects; `~/semiconductor-intel/traces` reserved for later tasks.
- `--memory=5g` guard (host has 7.7 GB and four other containers).
- `POSTGRES_INITDB_ARGS=--data-checksums` is baked into the image (first
  initdb only).

## Neighbors on the NAS — hands off

`postgres-server` (pgduckdb image, no published ports), `redis`,
`open-webui`, `grok2api` (`0.0.0.0:8000`) all predate this project.
`setup-nas.sh` creates exactly one new container and touches none of these.
The app's model traffic goes through the existing grok2api at
`http://192.168.1.21:8000` — unrelated to this database container.

## How the app connects

```bash
export INTEL_DATABASE_URL='postgresql+asyncpg://postgres:<PG_PASSWORD>@192.168.1.21:5432/postgres'
```

Note for 联调: Postgres extensions are **per-database**. The init script runs
only against the default `postgres` database (first-boot initdb). If the app
later migrates to a dedicated `intel` database, re-run the same DDL there,
e.g.:

```bash
ssh liyifan@192.168.1.21 'podman exec -i semiconductor-intel-pg psql -U postgres -d intel' \
    < <(sed -n "/<<'EOSQL'/,/^EOSQL/p" deploy/nas/init/40-intel-extensions.sh | sed '1d;$d')
```

## Backup pointer

Data lives in the bind mount `~/semiconductor-intel/pgdata` plus
`~/semiconductor-intel/objects`. Logical backups:

```bash
ssh liyifan@192.168.1.21 'podman exec -t semiconductor-intel-pg pg_dumpall -U postgres' \
    > ~/backups/semiconductor-intel-$(date +%F).sql
```

(Verify the restore path during 联调; pg_dumpall output of a jieba/bm25
schema recreates the DDL — the extensions themselves come from the image.)

## Rollback

```bash
ssh liyifan@192.168.1.21 'podman rm -f semiconductor-intel-pg'        # container only
ssh liyifan@192.168.1.21 'rm -rf ~/semiconductor-intel/pgdata/*'      # + destroy data
ssh liyifan@192.168.1.21 'podman rmi semiconductor-intel-pg:18'       # + image
```
