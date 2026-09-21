# Semiconductor-Intel v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `semiconductor-intel-design` v1.3 设计包实现完整的半导体行业情报系统（采集→判别→证据/事件→时间轴/演进→问答/报告），代码全部本地完成后统一联调。

**Architecture:** 单体多进程：FastAPI API + scheduler + pipeline/research worker + Postgres 18（NAS podman 容器，pg_textsearch/pg_jieba/pgvector/pgvectorscale）。NOOA 以固定提交路径依赖接入，Agent 只产提案、Python 验证提交。模型路由经 grok2api（OpenAI 兼容）。

**Tech Stack:** Python 3.12 + uv、FastAPI、SQLAlchemy 2 async + Alembic、PostgreSQL 18（BM25: pg_textsearch + pg_jieba；向量: pgvector + pgvectorscale diskann）、React+TS+Vite+TanStack Query、NOOA @ d4d46f7。

**Spec:** `<local design-pack checkout>/`（README + docs/01–16 + contracts/）。本计划的任务引用规格章节时，以规格的精确字段/路由表为准，执行者须同时读规格对应节。

## Global Constraints

- NOOA 固定提交 `d4d46f78ae0eeaed7d18a466196601e8d16bc101`，本地路径 `<local NOOA checkout>`，以 uv path 依赖（editable）接入；**不修改 NOOA 源码**。
- Python `>=3.12,<3.14`；包管理只用 uv；前端 Node LTS + pnpm。
- 用户隔离：所有 O/I 表 RLS ENABLE+FORCE，应用角色非 owner；owner_id 一律来自 session；复合 scope 外键（规格 docs/03 §1）。
- 模型产候选、业务代码验证提交；Pydantic 类型正确≠证据真实（规格 README 约束 9）。
- 完整增量采集：采集前不按主题/标题/top-k 丢弃文档（规格 README 约束 3）。
- 数据库：NAS `liyifan@your-nas-host`（aarch64 Armbian, podman 5.7.0）新起 `postgres:18` 容器 `semiconductor-intel-pg`，发布宿主 `5432`；扩展 pg_textsearch/pg_jieba/pgvectorscale(+pgvector CASCADE) 随镜像构建。**不触碰 NAS 上既有 postgres-server（pgduckdb）**。
- LLM：grok2api `http://your-nas-host:8000/v1`（OpenAI 兼容）；API key 经 env `GROK2API_KEY` 注入，不入库不入 git。
- 用户指令：**先把代码全部写完再联调**；所有依赖真实 DB/真实模型的测试标记 `@pytest.mark.integration`/`live`，默认跳过；离线测试（unit/contract，FakeLLM）必须通过。
- 实现代码放 `<repo>/`，git 仓库，每任务一提交。
- 检索选型已锁定（设计 D13/D14）：pg_textsearch+pg_jieba（BM25）、pgvector+pgvectorscale（diskann，exact 为默认基线）。
- 中间件三层（设计 D16）与模型路由三档（D17）按 docs/06 v1.3 与 docs/14 §3.1 实现。
- 优先试运行领域：Etching 三主题（RF/射频、材料选择、OES 监控算法）（设计 D15）。

## File Structure（规格 docs/02 §4 展开）

```text
semiconductor-intel/
  pyproject.toml / uv.lock
  .env.example  alembic.ini
  src/intel/
    settings.py            # pydantic-settings 分层配置
    contracts/             # 从设计包 contracts/models.py 移入并维护（单一事实源）
    domain/                # 纯业务规则：time.py url.py identity.py merge.py validation.py
    db/
      base.py meta.py rls.py   # Base、UUID/JSONB 类型、set_config('app.*') helper
      models/              # 按 03 分组：auth.py workspace.py sources.py pool.py knowledge.py
                           #  conversation.py jobs.py generation.py
    repositories/          # scope 强制：base.py + 每聚合一个 repo
    services/              # use cases：identity workspace sources acquisition parsing
                           #  retrieval routing knowledge evolution research reviews jobs reports
    api/
      app.py deps.py errors.py sse.py
      routes/              # auth.py industries.py topics.py feeds.py sources.py documents.py
                           #  evidence.py entities.py events.py timeline.py evolution.py
                           #  watches.py reviews.py conversations.py reports.py search.py
                           #  coverage.py jobs.py generation_runs.py health.py
    sources/
      adapters/            # rss.py atom.py api_list.py html_list.py sitemap.py page_monitor.py
      fetcher.py browser.py urlnorm.py ssrf.py
    parsing/               # parser.py html.py pdf.py textnorm.py blocksdiff.py quality.py
    retrieval/             # chunker.py indexer.py bm25.py vector.py recall.py
    nooa_adapter/
      factory.py agents.py   # RoutingAgent/ExtractionAgent/RelationAgent/EvolutionAgent/
                             # AnswerAgent/InvestigationAgent/QueryPlannerAgent
      middleware.py        # agent_call/llm_call/execute_python 三层
      tracing.py gateway.py registry/
    workflows/             # ingest.py route.py extract.py event_build.py evolution.py
                           #  report.py answer.py investigate.py recall.py review.py
    workers/               # scheduler.py runner.py leases.py
    storage/               # objects.py（FileObjectStore 持久卷实现）
    cli.py                 # create-user / reset-password / import-fixtures
  migrations/versions/     # Alembic
  web/                     # React app（vite）
  deploy/                  # nas/Containerfile nas/setup-nas.sh compose.yaml Dockerfile.api
  tests/{unit,contract,integration,e2e,eval}/
  fixtures/                # golden set 合成样本
  docs/superpowers/plans/  # 本计划
```

---

### Task 0: 仓库与依赖骨架

**Files:** Create `pyproject.toml`、`.gitignore`、`.env.example`、`src/intel/__init__.py`、`tests/unit/__init__.py`、`README.md`

**Interfaces:** Produces 可安装包 `intel`、`uv run pytest` 可跑空测试。

- [ ] **Step 1** `git init` 后写 `pyproject.toml`：

```toml
[project]
name = "intel"
version = "0.1.0"
requires-python = ">=3.12,<3.14"
dependencies = [
  "fastapi>=0.115", "uvicorn[standard]", "sqlalchemy[asyncio]>=2.0.36", "asyncpg",
  "alembic", "pydantic>=2.9", "pydantic-settings", "httpx", "feedparser",
  "beautifulsoup4", "lxml", "pypdf", "python-multipart", "argon2-cffi",
  "msgpack", "structlog",
]
[dependency-groups]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy", "types-feedparser"]

[tool.uv.sources]
nooa = { path = "<local NOOA checkout>", editable = true }

[tool.pytest.ini_options]
markers = ["integration: needs real Postgres", "live: needs network/provider"]
addopts = "-m 'not integration and not live'"
```

注意：`nooa` 本体依赖由其 pyproject 声明（含 msgpack 等）；path editable 保证 pin 在当前 checkout（d4d46f7）。
- [ ] **Step 2** `uv sync` 成功；`uv run python -c "import nooa, intel"` 通过。
- [ ] **Step 3** 空测试 `tests/unit/test_smoke.py::test_imports` 通过；`git add -A && git commit -m "chore: repo skeleton"`。

### Task 1: contracts 移入 + settings + 领域纯函数（time/url）

**Files:** Create `src/intel/contracts/models.py`（拷贝设计包并保持逐字段一致）、`src/intel/settings.py`、`src/intel/domain/time.py`、`src/intel/domain/urlnorm.py`；Test `tests/unit/test_domain.py`

**Interfaces:** Produces `normalize_url(url)->str`、`time_value_bounds(tv:TimeValue)->tuple[datetime|None,datetime|None]`（规格 03 §5 语义：[start,end)，day=本地零点转 UTC，unknown 返回 (None,None)）、`Settings`（字段见规格 10 §5：`database_url`、`object_store_root`、`session_pepper`、`grok2api_base_url`、`grok2api_key`、`llm_concurrency=4`、`fetch_concurrency=8`、`search_backfill_days=90`、`bm25_k1/b`、`diskann_query_search_list_size/query_rescore`、`route_aliases: dict[str,str]` L1/L2/L3）。

- [ ] **Step 1** 失败测试：URL 规范化（去 fragment、小写 host、默认端口、跟踪参数白名单删除、保留语义参数——用规格 04 §3 的例：`?utm_source=x` 删、`?lang=zh` 留）；TimeValue 边界（unknown→None；day 精度 [00:00,次日00:00)；range 左闭右开；月精度不伪造日）。
- [ ] **Step 2** 实现至通过；`ruff check` 干净。
- [ ] **Step 3** 提交 `feat: contracts + domain time/url`。

### Task 2: DB base + RLS helper + Alembic 初始迁移（auth/workspace/pool 表）

**Files:** Create `src/intel/db/{base,rls}.py`、`src/intel/db/models/{auth,workspace,sources,pool}.py`、`migrations/`；Test `tests/integration/test_rls.py`

**Interfaces:** Produces `Base`（DeclarativeBase，公共列 mixin：`id:UUID pk default uuid4, created_at, updated_at, row_version:BigInteger default 1`）、`set_scope(conn, owner_id, industry_id|None)`（`set_config('app.owner_id',..., True)`，缺 owner 的写路径 fail closed）、迁移建表清单（字段以规格 03 §2/§3 为准，逐表对照）：users、auth_sessions、industries、industry_revisions、topics、topic_revisions、source_templates、parser_versions、owner_feeds、industry_sources、source_runs、discovery_items、blobs、documents、document_origins、captures、fetch_observations、parsed_artifacts、document_diffs、chunks、processing_decisions。全部 O/I 表 RLS ENABLE+FORCE、复合 FK（I→I 带 owner_id+industry_id）、索引按 03 §8。

- [ ] **Step 1** 写 `alembic init` 并手写 `0001_core_tables.py`（不 autogenerate；UNIQUE/RLS/复合FK 显式 op.execute）。
- [ ] **Step 2** 集成测试（标记 `@pytest.mark.integration`，默认跳过；连接串 env `INTEL_TEST_DATABASE_URL`）：建库跑迁移，`SELECT` for owner A with app.owner_id=B 返回 0 行、跨 owner INSERT 抛错（ISO-03）；无 owner GUC 时写路径抛 `ScopeMissing`。
- [ ] **Step 3** 提交 `feat: schema core tables + RLS`。

### Task 3: 知识/对话/任务/生成表迁移

**Files:** Create `src/intel/db/models/{knowledge,conversation,jobs,generation}.py`、`migrations/0002_knowledge_*.py`…

**Interfaces:** 按规格 03 §4/§6/§7/§9 建齐：claims、claim_revisions、evidence、source_families、events、event_revisions、event_topics(+revisions)、event_relations(+revisions)、watches、overrides、event_read_states、industry_documents、document_topics、entities、entity_aliases；evolutions(+revisions)、reports(+revisions)、conversations（含 state_version/last_committed_message_id/current_state_revision_id）、messages（含 parent_message_id/turn_index）、conversation_state_revisions、conversation_summary_revisions、review_tasks、audit_log、dependency_edges、derived_status；jobs、job_steps、job_events、model_runs、coverage_batches、recall_hits、api_idempotency、generation_runs、generation_run_models、output_generations。物理关联表按 03 §9 全量落地（topic_revision_entities…publication_citations×3 分表）。合并操作表 event_merge_operations/event_lifecycle_history。

- [ ] **Step 1** 迁移 + 模型对齐 03 逐表核对（写一个 `tests/unit/test_schema_manifest.py` 用静态清单断言表/列存在，防漂移）。
- [ ] **Step 2** 提交 `feat: knowledge/conversation/jobs schema`。

### Task 4: identity 服务 + 账号 CLI

**Files:** Create `src/intel/services/identity.py`、`src/intel/cli.py`；Test `tests/unit/test_identity.py`（argon2 hash/verify、session 轮换、password_version 失效）

**Interfaces:** Produces `create_user(login,password)`、`authenticate(login,password)->Principal|None`（限速计数）、`issue_session(principal)->(cookie_value,csrf)`（token 只存 hash、登录轮换）、`reset_password(login,new_password)`（吊销全部 session）。CLI：`uv run intel create-user alice '...'`、`intel reset-password`。

- [ ] **Step 1–4** TDD 三件套（失败测试→实现→通过→提交 `feat: identity + cli`）。

### Task 5: scope repositories + workspace/sources/acquisition 服务与 API

**Files:** Create `src/intel/repositories/base.py`、`src/intel/services/{workspace,sources,acquisition}.py`、`src/intel/api/{app,deps,errors}.py`、`routes/{auth,industries,topics,feeds,sources}.py`；Test `tests/unit/test_workspace_service.py`（Fake repo）、`tests/contract/`（稍后 Task 15 统一 OpenAPI 校验）

**Interfaces:** `IndustryScope(owner_id,industry_id,job_id|None,capabilities)` 只由 API 依赖构造（`deps.py: get_principal/get_scope`，URL 中 industry_id 先验 ownership，404 统一）。路由按规格 08 §2 表逐行实现（auth/login|logout|me、industries CRUD+lifecycle、topics CRUD+replay、source-templates、feeds CRUD+poll+runs、industries/{id}/sources）。服务层语义按 01 §5：industry draft/active/paused/archived 状态机、topic 修订版本化、feed 的 interval/user_enabled、订阅 backfill_from。错误包络按 08 §6 稳定错误码。

- [ ] **Step 1** 失败测试：industry 生命周期非法迁移抛 `invalid_state_transition`；B 猜 A 的 industry → scope 校验 404。
- [ ] **Step 2** 实现；`uvicorn intel.api.app:app` 可空转（ `/health/live` 200）。
- [ ] **Step 3** 提交 `feat: identity/workspace/sources api`。

### Task 6: jobs 核心（队列/租约/fencing/scheduler tick）

**Files:** Create `src/intel/workers/{leases,scheduler,runner}.py`、`src/intel/services/jobs.py`；Test `tests/integration/test_jobs.py`

**Interfaces:** 按规格 07 全量：`enqueue(kind,input,idempotency_key,industry_id|None)`（UNIQUE 冲突返回既有 job）、`claim()->Job|None`（`FOR UPDATE SKIP LOCKED LIMIT 1`，写 lease_token/lease_until=90s/attempt）、heartbeat 30s、step 提交带 `WHERE lease_token=:t AND lease_until>now()`（fencing，JOB-02）、`job_events` 序号单调（PK(job_id,seq)，outbox 同事务）、取消在 step 边界生效、重试分类表（07 §6 常量）。幂等键组成按 07 §3 表逐 kind 落地。scheduler tick：查 `owner_feeds.next_poll_at<=now()` 发 discover、查 due 的 daily report、watch 周期。

- [ ] **Step 1** 集成测试：双 worker 并发 claim 只一人成功（JOB-01）；lease 过期后旧 worker 提交被拒（JOB-02）；crash 后重跑不重复业务行（JOB-03 语义：idempotent commit）。
- [ ] **Step 2** 提交 `feat: durable job queue`。

### Task 7: 采集适配器 + 抓取 + SSRF 防护

**Files:** Create `src/intel/sources/urlnorm.py`（并入 Task 1）、`adapters/{rss,atom,api_list,html_list,sitemap,page_monitor}.py`、`fetcher.py`、`browser.py`、`ssrf.py`；Test `tests/unit/test_adapters.py`（fixture XML/HTML 样本）、`tests/unit/test_ssrf.py`

**Interfaces:** Protocol 按规格 02 §6：`discover(plan:FeedPlan,cursor:str|None)->DiscoveryPage`（items 带 URL/title_hint/date_hint/provider_id?；无 next 且未 exhausted→partial）、`Fetcher.fetch(FetchRequest)->CaptureResult`（条件请求 ETag/Last-Modified、no_change 关联旧 capture、header 白名单、响应大小/时限/重定向上限）。SSRF（SEC-02）：仅 http/https、拒回环/私网/链路本地/169.254.169.254/非预期端口、每次 DNS 解析与重定向重校验、连接地址与校验一致（rebinding）、解压大小限制。枚举算法按 04 §3：游标事务、72h 重叠窗口、列表翻动用 ID 优先、429 按 Retry-After。浏览器渲染用 playwright 固定导航（goto→wait_for→取 DOM），作为 Fetcher 的 fallback 分支。

- [ ] **Step 1** 失败测试：RSS fixture 第 51 条被枚举（ING-01）；翻页期间插入新条目重叠扫描补齐（ING-03）；304/未变 200/变化 200 三态（ING-04）；SSRF 拒 IPv6 映射私网、redirect 到 169.254、rebinding。
- [ ] **Step 2** 实现至通过；提交 `feat: discovery adapters + fetcher + ssrf`。

### Task 8: 解析器 + blocks diff + 质量检查

**Files:** Create `src/intel/parsing/{parser,html,pdf,textnorm,blocksdiff,quality}.py`；Test `tests/unit/test_parsing.py`

**Interfaces:** `Parser.parse(ParserInput)->ParsedArtifact`：blocks 顺序数组 `{block_id,kind,text,page?,section_path[],bbox?,source_locator?}`（03 §3）、`coverage`（table/image_not_read/ocr 标注）、`retrieval_scope` 判定 metadata/abstract/partial/fulltext、登录页/挑战页识别不当正文（ING-06）、表格按行组保留 header（PAR-02）。diff：`document_diffs` kind=content_change/parser_change/mixed（PAR-01：parser 升级同原文不制造事件）。质量阈值：selector 命中、长度突变、模板占比 → parse_status=partial/failed（PAR-03）。

- [ ] **Step 1** 失败测试：HTML fixture 正文抽取+表格结构；PDF 页码；同原文换 parser 版本 → parser_change；title 正常正文为推荐列表 → partial。
- [ ] **Step 2** 提交 `feat: parsing + diffs`。

### Task 9: NAS Postgres 容器（infra，联调阶段执行）

**Files:** Create `deploy/nas/Containerfile`、`deploy/nas/setup-nas.sh`、`deploy/nas/README.md`

**Interfaces:** 在 `liyifan@your-nas-host`（aarch64）执行：

```bash
# setup-nas.sh 概要（脚本内逐步 set -euo pipefail）
ssh liyifan@your-nas-host mkdir -p ~/semiconductor-intel/{pgdata,objects,traces}
# Containerfile: FROM docker.io/library/postgres:18
#   apt-get install -y build-essential cmake git postgresql-server-dev-18(注:镜像内为 postgresql-dev-18)
#   git clone pg_textsearch@v1.4.0 → make && make install（写入 shared_preload_libraries 模板）
#   git clone pg_jieba → cmake 构建 cppjieba + 扩展（aarch64 源码编译）
#   安装 pgvector（pgvectorscale CASCADE 会装，但预装更稳：PGDG apt 或源码）
#   git clone pgvectorscale → cargo/pgrx 不可用时用官方预编译 deb arm64；无则源码 rustup 构建
#   ENV POSTGRES_INITDB_ARGS="--data-checksums"
podman build -t semiconductor-intel-pg:18 deploy/nas/（在 NAS 上 build）
podman run -d --name semiconductor-intel-pg --replace \
  -p 5432:5432 -e POSTGRES_PASSWORD="${PG_PASSWORD}" \
  -v ~/semiconductor-intel/pgdata:/var/lib/postgresql/data:Z \
  -v ~/semiconductor-intel/objects:/var/lib/intel/objects:Z \
  semiconductor-intel-pg:18
# 容器内: CREATE EXTENSION vector; CREATE EXTENSION vectorscale;
#         CREATE EXTENSION pg_textsearch; CREATE EXTENSION pg_jieba;
#         CREATE TEXT SEARCH CONFIGURATION jieba (PARSER=pg_jieba);
#         ALTER TEXT SEARCH CONFIGURATION jieba ADD MAPPING FOR n,v,a,i,e,l WITH simple;
#         smoke: SELECT to_tsvector('jieba','先进封装 刻蚀'); SELECT '刻蚀 高深宽比'::text <@> ...;
```

- [ ] **Step 1** 写 Containerfile + 脚本（版本 pin：pg_textsearch v1.4.0、pg_jieba master commit、pgvector 0.8.x、pgvectorscale 0.9.x）。
- [ ] **Step 2** 在 NAS 构建并起容器；`psql -h your-nas-host -U postgres -c '\dx'` 四扩展在场；jieba 分词 smoke 出词、`<@>` 返回行。
- [ ] **Step 3** 提交 `feat: nas postgres container`。**注意：用户要求先写完代码——本任务排在代码任务之后执行亦可，脚本先就位。**

### Task 10: 检索层（chunker/BM25/向量/召回）

**Files:** Create `src/intel/retrieval/{chunker,indexer,bm25,vector,recall}.py`；Test `tests/unit/test_chunker.py`、`tests/integration/test_retrieval.py`

**Interfaces:** chunker：标题层级+段落，~800 tok/最大 1200，100 tok 重叠，表格行组带 header，块带 block_ids，`chunker_version` 记录（04 §5）。indexer：写 chunks 表 + `IndexManifest`；`index_generation` 表语义：词典/分词配置/embedding 变更 → 新 generation 双写回填后切换（03 §8）。bm25.py：DDL `CREATE INDEX ... USING bm25`（等 pg_textsearch 语法核实后落：`text_config='jieba'`、k1/b 从 settings）；查询构造 `<@>` + owner/industry 过滤（RLS 之上仍带 scope 条件，ISO-03 变体）。vector.py：embedding 经 L1 route 客户端写入（缺 embedding 不阻塞归档，REC-04）；默认 exact 排序（`ORDER BY embedding <=> $1`）；diskann 索引建为非默认 generation，切换需 REC-08 评测。recall.py：四通道（语义/FTS 术语 jiebaqry/精确别名 trigram/正例邻近）union 按文档去重，RRF k=60 仅定顺序，`recall_hits` 逐通道落库，batch 100 + 游标持久化（04 §6）。

- [ ] **Step 1** 失败测试：整篇仅一段相关能命中（REC-01，合成索引 fixture）；超一页候选续批（REC-03）；top-k 不删除文档仅排序（断言无删除副作用）。
- [ ] **Step 2** 提交 `feat: retrieval stack`。

### Task 11: NOOA 适配层（factory/agents/middleware/tracing/gateway）

**Files:** Create `src/intel/nooa_adapter/{factory,agents,middleware,tracing,gateway}.py`、`src/intel/nooa_adapter/registry/routes.yaml`；Test `tests/unit/test_middleware.py`（FakeLLM）、`tests/unit/test_agents.py`

**Interfaces:** 按 06/14/15/16：factory 建 7 个 Agent 类（Routing/Extraction/Relation/Evolution/Answer/Investigation/QueryPlanner），`@strategy` + 方法级 `llm=route_alias`（L1/L2/L3 从 settings.route_aliases 读，registry yaml 指向 grok2api base_url，key 从 env）。middleware.py（D16）：

```python
def install_intel_middleware(agent, *, scope_ctx, usage_sink, budget):
    async def llm_guard(ctx: LLMCallContext, nxt: LLMCallNext) -> LLMCallContext:
        scan_for_secrets(ctx.messages)          # MW-01: 命中即 raise MiddlewareBlocked -> job 失败并审计
        budget.check_llm_call(scope_ctx.job_id)  # 每 job 计数限流
        resp = await nxt(ctx)
        usage_sink.record(scope_ctx, resp.usage) # -> model_runs 行
        return resp
    async def agent_guard(ctx: AgentCallContext, nxt: AgentCallNext) -> AgentCallContext:
        if scope_ctx.cancelled_or_lease_lost(): raise MiddlewareBlocked("job inactive")  # MW-02
        return await nxt(ctx)
    async def cell_guard(ctx: ExecutePythonContext, nxt) -> ExecutePythonContext:
        out = await nxt(ctx); enforce_cell_limits(out); return out          # MW-03
    agent.event_manager.intercept("llm_call", llm_guard)
    agent.event_manager.intercept("agent_call", agent_guard)
    agent.event_manager.intercept("execute_python", cell_guard)
```

tracing.py：job 进程入口 `enable_tracing(jsonl(per-job dir))` + `session_scope(trace_session_id)` + finally flush（15 §2 样式）；generation_runs 写入 producer/verifier 关联（15 §3）。gateway.py：`search_archive/read_evidence/read_document_blocks/fetch_public/search_web` 五工具，token 校验 owner/industry/job/actions/expiry（14 §1）。investigate 的 CodeActV2 候选配置（16 §2）：`execution_backend="sandbox"`、network=False、require=True；TokenBudgetSummarizer 按 16 §4 接线（threshold=context_budget(llm,0.65)，preserve_recent=16，target_chars=6000，aclose 排空）。

- [ ] **Step 1** 失败测试（FakeLLM）：secret 注入 prompt → MW-01 阻断且 fake.call_count 不增；usage_sink 收到记录；取消后 agent_call 阻断。
- [ ] **Step 2** 提交 `feat: nooa adapter + middleware`。

### Task 12: 路由/抽取/事件工作流（模型提案→验证→提交）

**Files:** Create `src/intel/workflows/{route,extract,event_build}.py`、`src/intel/domain/{validation,identity}.py`、`src/intel/services/knowledge.py`；Test `tests/unit/test_validation.py`、`tests/unit/test_event_identity.py`

**Interfaces:** route：每 parse 对全部 active industries 判别（direct/background/uncertain/unrelated + 理由 + 块引用），processing_decisions 落库（03 §3），uncertain 进待判断入口。extract（05 §1 五步）：quote 在 block 内 code-point 精确匹配且唯一（重复出现→要求更多上下文拒绝）、长度/单位/否定检查、二次语义判别（EVI-02）、source_statement/inference 区分、事务提交 claim_revision/evidence/model_run 关联；失败留 extraction_failed 不重写 quote。identity.py（05 §2 表）：七种 event_type 的强身份键解析器注册表（paper_version: work_id+version+action；product_release: 发布记录+产品+phase；…），布尔门自动合并（无冲突才并），弱证据→新事件+possible_duplicate 提案；EVT-01/02/03 用例可离线跑。merge/undo 事务（05 §4）：expected row_versions、membership_snapshot、merge→undo 冲突→409 补偿提案（EVT-04）。

- [ ] **Step 1** 失败测试：伪造 quote/跨 parse/block 不存在 → 拒绝（EVI-01）；10 篇同公告 → 1 事件 1 source_family（EVT-01）；同日两产品不并（EVT-02）；预告/内测/正式三分（EVT-03）。
- [ ] **Step 2** 提交 `feat: extraction validation + event identity`。

### Task 13: 时间轴/演进/关系服务 + API

**Files:** Create `src/intel/services/{evolution,timeline}.py`、`routes/{events,timeline,evolution,entities,evidence,watches}.py`；Test `tests/unit/test_timeline_asof.py`

**Interfaces:** timeline 查询参数与稳定游标（05 §5：(effective_sort_key,event_id) 游标、未知日期独立组、as_of 过滤 recorded_at/绑定/关系版本——TIM-02：今天发现的去年事件在 as_of=上月 不可见）。evolution 构建六步（05 §6）：input_manifest 固定输入、边 basis=explicit/inferred、环检测、`parallel` 无向规范化、insufficient_evidence 状态、stale 传导（dependency_edges+derived_status，debounce 5min 单 job）。EventCard/EvidenceView/EvolutionView DTO 按 08 §3（含 generation_refs）。

- [ ] **Step 1** 失败测试：月精度/未知/区间交集（TIM-01）；更正后旧 revision 文本不变（TIM-03）；无依据不建边（EVO-01）；环拒绝（EVO-02）。
- [ ] **Step 2** 提交 `feat: timeline + evolution`。

### Task 14: 会话问答/报告/复核 API + SSE

**Files:** Create `src/intel/services/{research,reviews,reports}.py`、`workflows/{answer,investigate,report,review}.py`、`src/intel/api/sse.py`、`routes/{conversations,reviews,reports,search,coverage,jobs,generation_runs,documents}.py`；Test `tests/unit/test_conversation_context.py`

**Interfaces:** ConversationCoordinator/QueryPlanner 按 16 §6 六步：state_version 校验、parent_message_id 串行（409）、约束结构 `{id,text,kind,source_message_id,status}`、上下文优先级（16 §6 列表）、conversation_summary_revisions 压缩保留原消息（CHAT-06）。archive/online 两模式：EvidencePacket（14 §5：allowed_citation_ids、actually_read_blocks、answer-local evidence）、引用语义不匹配一次修复后删减（QA-01 零 search_web 调用）。reports：daily/topic/investigation、citation coverage=100% 校验、stale banner。reviews：approve/reject/undo + expected_versions + apply_review 幂等。SSE：`/jobs/{id}/events` Last-Event-ID 恢复、409 event_cursor_expired、heartbeat 注释（07 §7）。

- [ ] **Step 1** 失败测试：第二轮代词解析回上一轮引用 ID（CHAT-01 合成）；并发发送 409（CHAT-04）；归档模式无证据 → insufficient_evidence 且零工具调用（QA-01）；SSE 断线重连重放。
- [ ] **Step 2** 提交 `feat: conversations + qa + reports + sse`。

### Task 15: OpenAPI/contract 校验 + golden set 离线评测骨架

**Files:** Create `tests/contract/test_openapi.py`、`tests/eval/README.md`、`fixtures/golden/`（Etching RF/材料选择/OES 各≥5 合成样本）、`tools/run_golden.py`

**Interfaces:** contract 测试：从 `src/intel/contracts/models.py` 生成 schema 与设计包 `contracts/schemas.json` 关键子集对比（CorrectionCommand 联合、TimeValue 不变量、GenerationViewerLink）；路由清单 vs 设计包 openapi.json path 集合 diff 为空（允许显式豁免清单）。golden runner：FakeLLM 脚本化跑 extract/route 流水，输出 nooa-bench 风格 trajectory JSON + 行为报告占位（11 §3）。

- [ ] **Step 1** 失败测试：path diff 非空 → 列出缺失路由。
- [ ] **Step 2** 提交 `feat: contract checks + golden harness`。

### Task 16: 前端（React+Vite）

**Files:** Create `web/`（`pnpm create vite web --template react-ts`）、`web/src/{api,schemas,stores}/`、`pages/{Login,Industries,Workspace,Documents,Topic,Timeline,Evolution,EventDetail,Conversations,Reports,Reviews,RunCenter,Settings}.tsx`、`components/{EvidenceDrawer,GenerationProcess,EventCard,TimelineControls,CoverageBadge}.tsx`

**Interfaces:** 页面矩阵按 09 §2 表；证据抽屉四层下钻（引用→claim→原文高亮按 parse_id→快照版本，禁止在最新正文重搜旧引用）；“依据/生成过程”双入口；问答 archive/online 切换与 SSE 进度；时间范围与 as_of 分开控件；写路径不乐观确认（版本冲突保留输入）；中文界面。API 客户端由设计包 openapi 生成 types。

- [ ] **Step 1** `pnpm i && pnpm build` 通过（mock 数据驱动页面渲染）。
- [ ] **Step 2** 提交 `feat: web frontend`。

### Task 17: 部署编排 + 配置样例 + 收尾

**Files:** Create `deploy/compose.yaml`、`deploy/Dockerfile.api`、`.env.example` 补全、`README.md` 运行手册（本地 dev、NAS DB、grok2api route、seed 导入、试运行步骤）

**Interfaces:** compose：reverse-proxy/api/scheduler/pipeline-worker/fetch-worker/research-runner/postgres（复用 NAS 或本地）；`INTEL_DATABASE_URL=postgresql+asyncpg://intel:${PG_PASSWORD}@your-nas-host:5432/intel`；grok2api route 样例：

```yaml
# registry/routes.yaml（经 unifiedllm registry 加载）
routes:
  L1: { provider: openai_compatible, model: "grok-3-mini", base_url: "http://your-nas-host:8000/v1", api_key_env: GROK2API_KEY, reasoning: none }
  L2: { provider: openai_compatible, model: "grok-3",      base_url: "http://your-nas-host:8000/v1", api_key_env: GROK2API_KEY }
  L3: { provider: openai_compatible, model: "grok-4",      base_url: "http://your-nas-host:8000/v1", api_key_env: GROK2API_KEY, reasoning: high }
```

（真实可用模型名以 grok2api `/v1/models` 实测为准，联调时修正。）

- [ ] **Step 1** `docker compose -f deploy/compose.yaml config` 校验通过；README 完整。
- [ ] **Step 2** 提交 `feat: deploy orchestration`；向用户汇报：代码完成度清单 + 待联调项（NAS 容器、真实 route、E2E）。

---

## Self-Review

- **规格覆盖**：01 需求→Task 5/13/14；02 架构→Task 0/17；03 数据→Task 2/3；04 采集→Task 7/8/10；05 知识→Task 12/13；06/15/16 NOOA→Task 11/14；07 任务→Task 6；08 API→Task 5/13/14/15；09 前端→Task 16；10 运维→Task 9/17；11 测试→各任务+Task 15；12 实施→整体顺序对齐 M0–M7；13 决策→全局约束；14 内部契约→Task 10/11/12。
- **占位符扫描**：无 TBD；“以规格为准”仅用于大表字段清单（规格随计划执行者可读），关键逻辑均给代码或精确语义。
- **类型一致性**：`IndustryScope`/`Principal`（Task 5 定义，11/12/14 消费）、`InputManifest`/`AssessmentDimensions`（contracts，Task 12/14 消费）、middleware 签名与 NOOA `runtime/middleware.py` 实测一致（LLMCallContext/Next）。
