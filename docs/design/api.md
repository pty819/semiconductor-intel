# API 设计

FastAPI 单体，前缀 `/api/v1`，cookie 会话鉴权。设计关键词：
**统一错误信封、签名游标、请求幂等、SSE 事件流、RLS scope 依赖注入**。

## 版式约定

- 所有列表端点 `?cursor=&page_size=`；游标是 **HMAC 签名的 offset**
  （`api/pagination.py`），key 来自会话派生——伪造/跨会话重用直接 400。
  时间轴/事件流等有序集合改用业务 keyset 游标，不用 offset。
- 写端点接受 `Idempotency-Key` 头：首请求记录响应，重放返回**首次**
  响应（`api/idempotency.py`），前端因此可以安全重试。
- 写路径**不做乐观更新**（前端同规）：版本冲突显式 409，保留用户输入。

## 统一错误信封

`api/errors.py` 把 `ServiceError` 族映射为：

```json
{"error": {"code": "not_found", "http": 404, "message": "…", "details": {}}}
```

- 事件/演进等读取未命中 → 标准 `not_found` 404（曾修复过返回
  `200 + null` body 的信封漂移）；
- `expected_version` 不匹配 → 409（复核、事件合并、文档更正）；
- `parent_message_id` 指向未就绪轮次 → 409（会话串行提交）；
- 允许的错误码集合 = 规格 08 §6 + 4 个经 T15 契约测试批准的扩展码。

## 会话与 scope

```{mermaid}
flowchart LR
    CK["session cookie"] --> P["Principal"]
    P --> S["IndustryScope<br/>(owner_id, industry_id)"]
    S --> CN["get_conn<br/>connect + begin"]
    CN --> ROLE["SET LOCAL ROLE intel_app"]
    ROLE --> GUC["SET LOCAL app.owner_id / app.industry_id"]
    GUC --> REPO["仓储实例（已绑 scope）"]
    REPO --> H["路由处理函数"]
```

`api/deps.py` 的依赖链把安全做成默认值：

```
session cookie → Principal → IndustryScope(owner_id, industry_id)
                      │
get_conn: engine.connect() → begin() → SET LOCAL ROLE intel_app
                      │
set_scope: SET LOCAL app.owner_id / app.industry_id
```

路由函数拿到的仓储全部已绑 scope——**不可能写出漏掉 owner 过滤的
查询**。用户/会话表不启用 RLS（登录必须先于角色切换工作）。

## SSE（/jobs/{id}/events）

断线重连与游标续播：

```{mermaid}
sequenceDiagram
    autonumber
    participant C as 客户端 (RunCenter)
    participant A as api
    participant DB as job_events
    C->>A: GET /jobs/{id}/events
    A->>DB: 握手（job 存在? 游标过期?）[请求事务]
    A-->>C: text/event-stream
    loop 逐批
        A->>DB: 短事务读新事件（seq > cursor）
        A-->>C: id: seq + data + 心跳注释
    end
    Note over C: 连接断开（记下最后 seq）
    C->>A: 重连, 头带 Last-Event-ID: seq
    A->>DB: 从 seq+1 重放（不丢事件）
    alt 游标超出保留窗口
        A-->>C: 409 event_cursor_expired → 客户端全量回退
    end
```

`api/sse.py`（spec 07 §7）：

- **Last-Event-ID 重放**：断线重连从 `job_events` 的 seq 续播，不丢事件；
- 游标超出保留窗口 → `409 event_cursor_expired`（客户端回退全量拉取）；
- 心跳注释行保活，代理不超时；
- 连接纪律：握手（job 存在性、游标过期判定）在请求事务上完成，
  **轮询循环用独立短事务逐批开合**（`get_job_event_opener`），
  绝不跨 `sleep` 持有事务——否则默认连接池（5+10）会被少量长流占死。

## 路由清单

`api/routes/` 按聚合划分：`auth`（登录/登出/密码）、`industries`、
`topics`、`feeds`、`sources`、`documents`、`evidence`、`entities`、
`events`、`timeline`、`evolution`、`watches`、`reviews`、
`conversations`、`reports`、`search`、`coverage`、`jobs`、
`generation_runs`。全部路由与设计包 openapi 的 path 集合 diff 由
contract 测试钉住（8 个显式豁免均有书面理由，见 {doc}`testing`）。

文档与证据读取语义（前端同源遵守）：

- 证据定位**必须**用 `evidence.parse_id` 指定的快照 + `[start_char,
  end_char)` 字符区间，快照不匹配直接报错，**禁止**在最新正文里
  模糊重搜旧引用；
- `documents/{id}/revisions` 是文档自身的修订史；证据修订按文档
  scope 隔离。
