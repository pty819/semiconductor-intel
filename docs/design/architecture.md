# 系统架构

## 业务能力地图

从"外部信息"到"可信结论"的业务能力分解：

```{mermaid}
flowchart TB
    subgraph ACQ["采集域"]
        A1[来源管理<br/>feeds/sources]
        A2[增量发现<br/>discover]
        A3[抓取快照<br/>fetch]
        A4[解析质控<br/>parse]
    end
    subgraph KBS["知识域"]
        K1[行业/主题判别<br/>route]
        K2[证据抽取<br/>extract]
        K3[事件构建<br/>event_build]
        K4[合并/更正<br/>merge + review]
    end
    subgraph SERVE["服务域"]
        S1[检索召回<br/>search/recall]
        S2[时间轴/演进<br/>timeline/evolution]
        S3[会话问答<br/>conversations]
        S4[报告/复核<br/>reports/reviews]
    end
    subgraph GOV["治理域"]
        G1[任务队列<br/>jobs]
        G2[多租户隔离<br/>RLS]
        G3[失控防护<br/>budget/gateway]
        G4[全链路追踪<br/>tracing]
    end

    ACQ --> KBS --> SERVE
    GOV -.承载全部域.-> ACQ & KBS & SERVE
```

治理域不是一层"模块"而是横切约束：任何业务写入都必须同时满足
队列幂等、RLS scope、模型输出校验、留痕可追踪四件事。

## 总体形态

单体多进程：一个 FastAPI 服务、一个调度器、若干按角色划分的后台 worker，
共享同一个 PostgreSQL 18 数据库。没有消息队列——任务队列就是数据库里的
`jobs` 表（见 {doc}`jobs`），跨进程协作全部通过表与事务完成。

```{mermaid}
flowchart LR
    subgraph browser["浏览器"]
        FE["Vue 3 SPA"]
    end
    subgraph compose["docker compose / 主机进程"]
        RP["reverse-proxy (nginx)\n静态资源 + /api 转发"]
        API["api (uvicorn)\n/api/v1 + SSE"]
        SCH["scheduler\n定时入队"]
        PW["pipeline-worker\n采集/索引/知识"]
        FW["fetch-worker\n抓取"]
        RR["research-runner\n问答/调查/报告"]
    end
    PG[("PostgreSQL 18\ntables + RLS\nBM25 + vector")]
    LLM["OpenAI-v1 兼容\nLLM 端点"]
    WEB["外部网站 / RSS\n(采集目标)"]

    FE <--> RP --> API
    SCH --> PG
    PW <--> PG
    FW <--> PG
    RR <--> PG
    API <--> PG
    FW --> WEB
    RR --> LLM
    PW --> LLM
```

## 进程与职责

| 进程 | 入口 | 职责 |
|---|---|---|
| `api` | `uvicorn intel.api.app:app` | 全部 REST 路由、SSE 事件流、会话鉴权 |
| `scheduler` | `intel scheduler` | 周期扫 `owner_feeds.next_poll_at`、日报窗口、watch 节拍，入队任务 |
| `pipeline-worker` | `intel worker --role pipeline` | discover / source_poll / parse / index / route / extract / event_build / apply_review / watch_check |
| `fetch-worker` | `intel worker --role fetch` | fetch（网络密集，独立并发池 `INTEL_FETCH_CONCURRENCY`） |
| `research-runner` | `intel worker --role research` | archive_answer / investigate / report_build（LLM 密集，`INTEL_LLM_CONCURRENCY`） |

Worker 的 kind→role 映射与装配在 `src/intel/workers/composition.py`
（组合根）：每个角色用过滤器声明自己认领的 kind，任务认领时按 kind 过滤，
因此同一数据库上可以随意增减 worker 副本。

## 分层

代码架构：模块依赖严格自上而下（箭头=允许的 import 方向）：

```{mermaid}
flowchart TD
    API["api<br/>路由/依赖注入/SSE/错误信封"]
    WF["workflows<br/>后台任务处理器"]
    NOOA["nooa_adapter<br/>agents/middleware/gateway/tracing"]
    SVC["services<br/>用例：jobs/knowledge/timeline/evolution/reviews"]
    RETR["retrieval<br/>chunker/bm25/vector/recall"]
    SRC["sources + parsing<br/>发现/抓取/SSRF/解析/质量"]
    REPO["repositories<br/>每聚合一个，scope 强制"]
    DB["db<br/>ORM 模型 + RLS helper"]
    DOM["domain<br/>纯业务规则，零 IO"]
    CT["contracts<br/>DTO 唯一事实源"]
    COMP["workers/composition<br/>组合根：只装配不业务"]

    API --> SVC
    API --> REPO
    WF --> NOOA
    WF --> SVC
    WF --> RETR
    WF --> SRC
    SVC --> REPO
    RETR --> REPO
    REPO --> DB
    DB --> DOM
    NOOA --> DOM
    SVC --> DOM
    WF --> DOM
    API --> CT
    SVC --> CT
    REPO --> CT
    COMP -.装配.-> API & WF & NOOA & SVC & RETR & REPO
```

目录（与上图一一对应）：

```
src/intel/
├── api/            # FastAPI：路由、deps（会话/RLS/分页/幂等）、SSE、错误信封
├── contracts/      # DTO 唯一事实源（models.py 自设计包逐字节拷贝）
├── domain/         # 纯业务规则：时间/URL 归一、事件身份、证据校验（无 IO）
├── services/       # 用例层：任务队列语义、知识提交、时间轴/演进、复核、报告
├── repositories/   # 每聚合一个仓储；scope 强制、SQL 适配器
├── db/             # ORM 模型（按 03 分组）、RLS helper、类型
├── workflows/      # 后台任务处理器：ingest/route/extract/event_build/answer/…
├── workers/        # 组合根、runner、租约、scheduler、SQL store 适配
├── retrieval/      # 分块、BM25/向量索引、四通道召回
├── sources/        # 发现适配器、抓取器、浏览器渲染、SSRF 防护
├── parsing/        # 解析、正文抽取、质量判定、块差异
├── nooa_adapter/   # NOOA 智能体/中间件/工具网关/追踪的接线层
└── storage/        # 对象存储（本地卷实现）
```

分层纪律：

- **domain 不做 IO**。证据校验（EVI-01）、事件身份（EVT-01/02/03）、
  时间轴 as_of 语义全部是纯函数，离线可测。
- **模型产候选、业务代码验证提交**。所有写入路径中，凡是从模型输出来的
  字段（引文、块引用、claim id、时间表达、主题判决）必须先经过
  Python 侧的成员校验/精确匹配才允许落库；校验不过的字段被丢弃并计数。
- **组合根只做装配**。`composition.py` 把 SQL store、NOOA 智能体、中间件、
  追踪会话接成每个后台任务可执行的闭包；不含业务逻辑。

## 关键数据流

一条公告从采集到出现在时间轴上：

```{mermaid}
flowchart TD
    D[discover<br/>发现新条目] --> F[fetch<br/>抓取原文/快照]
    F --> P[parse<br/>正文抽取+质量判定]
    P --> I[index<br/>分块+BM25/向量索引]
    P --> R[route<br/>行业判别 direct/background/uncertain/unrelated]
    R --> E[extract<br/>claim/evidence 抽取+引文校验]
    E --> B[event_build<br/>事件身份解析+合并]
    B --> T[时间轴 / 演进 / 问答 可见]
```

派生链的完整时序（每跳一个事务，事务内派生下一跳）：

```{mermaid}
sequenceDiagram
    autonumber
    participant W as worker
    participant DB as jobs 表（同事务写入）
    W->>DB: parse 成功提交
    W->>DB: 同事务入队 index + route（幂等键）
    Note over DB: 任一入队失败 → 整个事务回滚，parse 重试
    W->>DB: route 判决落库（处理决策行）
    W->>DB: 同事务入队 extract
    W->>DB: extract 校验通过的声明落库（claim/evidence）
    W->>DB: 仅当声明落库时——同事务入队 event_build
    W->>DB: event_build 解析身份 + 时间 → 事件可见
```

链条中任何一跳失败：任务按重试分类表退避重试，已提交的上一跳不受影响。

## 设计原则速查

1. **可追溯**：证据必须按 `parse_id + 字符区间` 精确回定位到抓取快照；
   禁止在最新正文里"模糊重搜"旧引用。
2. **增量完整**：采集前不按主题/标题/top-k 丢弃文档；检索只做排序不做删除。
3. **租户隔离**：所有业务表 `ENABLE + FORCE RLS`，应用角色 `intel_app`
   NOLOGIN 且非 owner，事务开头 `SET LOCAL ROLE intel_app` + 绑定
   `app.owner_id` GUC。
4. **失控防护**：每任务 LLM 调用数上限、每 job 幂等键、秘密扫描、
   CodeAct 沙箱、工具网关 fail-closed（见 {doc}`nooa`、{doc}`security`）。
5. **离线可测**：默认测试套件 676 个用例不依赖真实数据库与真实模型；
   集成/真实模型测试用 marker 显式分组。
