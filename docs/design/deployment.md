# 部署

```{mermaid}
flowchart LR
    U["用户"] -->|"http(s) :80"| RP["reverse-proxy<br/>nginx: Vue 静态 + /api →"]
    RP --> API["api :8000<br/>uvicorn"]
    subgraph workers["后台进程 (同一镜像 Dockerfile.api)"]
        SCH["scheduler"]
        PW["pipeline-worker"]
        FW["fetch-worker"]
        RR["research-runner"]
    end
    API & SCH & PW & FW & RR <--> PG[("PostgreSQL 18<br/>(外置, 0001→0004 迁移)<br/>BM25 + vector")]
    PW & RR & API -.objects/traces 卷.-> VOL[["named volumes"]]
    RR & PW --> LLM["OpenAI-v1 端点<br/>(routes.yaml 三档)"]
    FW --> NET["外部网站/RSS"]
```

## 拓扑

`deploy/compose.yaml` 六服务 + 两个命名卷（`objects`、`traces`）：

| 服务 | 镜像 | 说明 |
|---|---|---|
| reverse-proxy | Dockerfile.web（node 构建 + nginx） | 托管 Vue 静态资源，`/api` 反代到 api |
| api | Dockerfile.api | uvicorn，暴露 8000（仅内网） |
| scheduler | Dockerfile.api | `intel scheduler` |
| pipeline-worker | Dockerfile.api | `intel worker --role pipeline` |
| fetch-worker | Dockerfile.api | `intel worker --role fetch` |
| research-runner | Dockerfile.api | `intel worker --role research` |
| postgres（profile `local-db`） | 官方 postgres:18 + 插件 | 默认**不启动** |

数据库默认外置（当前为 82 主机 `192.168.1.82:5432` 的
`semiconductor-intel-pg` 容器，内含 pg_textsearch / pg_jieba /
pgvector + pgvectorscale，均已 smoke 验证）。镜像按目标主机**原生
架构**构建（x86 在 x86 上建，arm 在 arm 上建），不做跨架构模拟。

## Dockerfile.api 要点

```dockerfile
FROM python:3.12-slim-bookworm
# git 是 uv 解析 NOOA git 依赖（pin d4d46f7）所需
RUN apt-get install … curl ca-certificates build-essential git
COPY pyproject.toml uv.lock README.md alembic.ini /app/
COPY src migrations /app/
RUN uv sync --frozen --no-dev
```

- `--frozen`：构建期不解析，完全按 `uv.lock` 安装（NOOA 亦被锁）；
- 无宿主路径依赖：NOOA 从 GitHub 拉取，任何机器/CI 可构建；
- 旧方案（把本地 NOOA checkout 作为额外 build context 拷进镜像 +
  sed 改 lock 路径）已被 git 依赖方案整体替代。

## 发布流程

```bash
export PG_PASSWORD=…
docker compose -f deploy/compose.yaml config      # 校验
docker compose -f deploy/compose.yaml up --build  # 起 API/worker 栈

# 首次/升级：跑迁移（0001→0004，0004 授予 intel_app DML）
uv run intel migrate          # 或 alembic upgrade head

# 生产环境变量（.env / compose environment）
INTEL_DATABASE_URL=postgresql+asyncpg://postgres:…@192.168.1.82:5432/postgres
INTEL_LLM_BASE_URL=http://192.168.1.82:8080/v1   # 任意 OpenAI-v1 兼容端点
LLM_API_KEY=…
INTEL_GATEWAY_SECRET=…        # research-runner 必需，空则 fail-closed
INTEL_SESSION_PEPPER=…        # 生产必须换掉 dev 默认
```

## 配置样例：模型路由

`src/intel/nooa_adapter/registry/routes.yaml`（unifiedllm registry 加载）：

```yaml
routes:
  L1: { provider: openai, model: "openai/model-l1", base_url: "…/v1",
        api_key_env: LLM_API_KEY, reasoning: none }
  L2: { provider: openai, model: "openai/model-l2", base_url: "…/v1",
        api_key_env: LLM_API_KEY }
  L3: { provider: openai, model: "openai/model-l3", base_url: "…/v1",
        api_key_env: LLM_API_KEY, reasoning: high }
```

模型 id 是**显式占位**：联调时对端点 `GET /v1/models` 实测后替换。
设置侧 `Settings.route_aliases` 可把 L1/L2/L3 别名重映射到 registry
里的其他路由。

## 试运行（Etching 领域）

1. 建用户/工作区/行业（CLI：`intel create-user` 等）；
2. 导入 Etching 三主题（RF/射频、材料选择、OES 监控算法）种子；
3. 配 RSS/列表页来源 → 调度器自动发现入队，或 `source_poll` 手动触发；
4. RunCenter 观察 discover→…→event_build 链路；
5. Conversations 页以归档模式问答验证 QA-01；Reports 验证引文覆盖。

## 联调门清单

- [ ] 82 数据库跑迁移至 head；
- [ ] 真实 `/v1/models` → 填 routes.yaml 三个模型 id；
- [ ] `INTEL_GATEWAY_SECRET` / `INTEL_SESSION_PEPPER` 生产值；
- [ ] Vue 由 mock 切换真实 API（`stores/session.ts`）；
- [ ] E2E：一条来源从 discover 走到时间轴可见；
- [ ] （可选）`--profile local-db` 起本地 postgres 自证闭环。
