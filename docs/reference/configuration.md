# 配置参考

全部配置经 pydantic-settings 读取，环境变量 `INTEL_` 前缀优先，
兼容无前缀拼写（LLM 两项另兼容历史 `GROK2API_*` 名）。源码：
{mod}`intel.settings`。

## 核心

| Setting | Env | 默认 | 说明 |
|---|---|---|---|
| `database_url` | `INTEL_DATABASE_URL` | 本地 postgres | `postgresql+asyncpg://…` |
| `object_store_root` | `INTEL_OBJECT_STORE_ROOT` | `var/objects` | 抓取原始字节 |
| `job_trace_dir` | `INTEL_JOB_TRACE_DIR` | `var/traces` | 每 job JSONL：`job-{id}.jsonl` |
| `session_pepper` | `INTEL_SESSION_PEPPER` | `dev-only-…` | 会话签名椒盐；生产必须换 |

## 模型提供方（通用 OpenAI-v1）

| Setting | Env（别名） | 默认 | 说明 |
|---|---|---|---|
| `llm_base_url` | `INTEL_LLM_BASE_URL`（`LLM_BASE_URL`、`INTEL_GROK2API_BASE_URL`、`GROK2API_BASE_URL`） | `http://192.168.1.82:8080/v1` | 任意 chat-completions 兼容端点 |
| `llm_api_key` | `INTEL_LLM_API_KEY`（`LLM_API_KEY`、`GROK2API_KEY` 族） | 空 | |
| `route_aliases` | — | `L1/L2/L3 → 同名` | unifiedllm registry 路由别名重映射 |

## 运行旋钮（spec 10 §5 防失控）

| Setting | Env | 默认 | 说明 |
|---|---|---|---|
| `llm_concurrency` | `INTEL_LLM_CONCURRENCY` | 4 | research 侧并发 |
| `fetch_concurrency` | `INTEL_FETCH_CONCURRENCY` | 8 | 抓取并发 |
| `search_backfill_days` | `INTEL_SEARCH_BACKFILL_DAYS` | 90 | 搜索回填窗口 |
| `llm_max_calls_per_job` | — | 400 | MW-01 预算腿 |
| `cell_output_max_chars` | — | 100000 | MW-03 CodeAct 输出上限 |

## 解析质量阈值（spec 04 §4）

| Setting | 默认 | 语义 |
|---|---|---|
| `min_text_ratio` / `max_text_ratio` | 0.01 / 0.95 | 可用文本/字节比异常区间 |
| `boilerplate_max` | 0.6 | 样板占比上限 |
| `min_text_chars` | 200 | 低于此且低文本比 → PAR-03 硬失败 |
| `link_density_max` | 0.5 | 链接文本占比（推荐列表页特征） |

## 检索

| Setting | 默认 | 语义 |
|---|---|---|
| `bm25_k1` / `bm25_b` | 1.5 / 0.75 | pg_textsearch BM25 参数（四处交叉钉住） |
| `diskann_query_search_list_size` | 100 | diskann 查询参数 |
| `diskann_query_rescore` | 50 | diskann 查询参数 |

## 调查网关（spec 14 §1）

| Setting | Env | 默认 | 说明 |
|---|---|---|---|
| `gateway_secret` | `INTEL_GATEWAY_SECRET` | 空（**fail-closed**） | HMAC 工具令牌密钥；空则 research runner 拒启 |
| `gateway_token_ttl_seconds` | — | 3600 | 令牌有效期 |

## CLI

```
intel api            # uvicorn 入口（compose 用）
intel scheduler      # 定时入队
intel worker --role pipeline|fetch|research|all
intel migrate        # alembic upgrade head
intel create-user / reset-password / import-fixtures
```
