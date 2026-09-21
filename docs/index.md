# semiconductor-intel 设计文档

半导体行业情报系统：把新闻、公告、论文、PDF 等来源采集入库，经 NOOA 智能体
驱动的抽取与知识管线（**模型产候选、业务代码验证提交**），沉淀为带证据、
可追溯、可时间旅行的事件时间轴，并提供会话问答、报告与人工复核。

- **后端**：Python 3.12+ / FastAPI / SQLAlchemy 2 (async) / PostgreSQL 18
  （pg_textsearch + pg_jieba 全文检索，pgvector + pgvectorscale 向量检索）
- **智能体**：[NOOA](https://github.com/NVIDIA-NeMo/labs-OO-Agents) 框架
  （git 依赖，固定 commit `d4d46f7`）
- **前端**：Vue 3.5 + Vite + Pinia + Vue Router（`web/`）
- **部署**：docker compose（api / scheduler / 双 worker / research-runner /
  nginx 反代），数据库外置

```{toctree}
:maxdepth: 2
:caption: 设计

design/architecture
design/data-model
design/ingest
design/knowledge
design/retrieval
design/jobs
design/nooa
design/api
design/qa
design/frontend
design/security
design/deployment
design/testing
```

```{toctree}
:maxdepth: 2
:caption: 参考

reference/configuration
reference/api-source
```

## 快速开始（开发环境）

```bash
uv sync --group docs          # 运行时依赖 + 文档工具链
uv run pytest                 # 676 个离线测试（integration/live 默认跳过）
uv run sphinx-build -b html docs docs/_build/html   # 构建本文档
```

需要 PostgreSQL 18 与上述扩展的真实数据库的测试标记为
`@pytest.mark.integration`，需要真实 LLM 的标记为 `@pytest.mark.live`，
两者默认不参与 `pytest`。

## 文档索引

| 页面 | 内容 |
|---|---|
| {doc}`design/architecture` | 系统架构、进程模型、目录结构 |
| {doc}`design/data-model` | 数据模型、RLS 多租户隔离、迁移策略 |
| {doc}`design/ingest` | 采集管线：discover→fetch→parse→index/route |
| {doc}`design/knowledge` | 知识层：证据、事件身份、合并与撤销 |
| {doc}`design/retrieval` | 检索：BM25、向量、四通道召回 |
| {doc}`design/jobs` | 任务系统：fencing 租约、幂等键、at-least-once |
| {doc}`design/nooa` | NOOA 适配：智能体、中间件、工具网关、追踪 |
| {doc}`design/api` | API 设计：错误信封、游标分页、SSE、幂等 |
| {doc}`design/qa` | 会话问答、报告、复核生命周期 |
| {doc}`design/frontend` | Vue 前端与证据抽屉 |
| {doc}`design/security` | 安全模型：RLS、网关令牌、SSRF、秘密扫描 |
| {doc}`design/deployment` | 部署：compose、镜像、迁移、运行手册 |
| {doc}`design/testing` | 测试策略与 golden 评测 |
| {doc}`reference/configuration` | 全量配置项参考 |
| {doc}`reference/api-source` | 源码 API 参考（autodoc） |
