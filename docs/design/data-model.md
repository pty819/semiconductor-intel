# 数据模型与多租户隔离

迁移文件在 `migrations/versions/`（Alembic，异步引擎）：

| 迁移 | 内容 |
|---|---|
| `0001_core_tables` | 认证/工作区/来源/文档池核心表 + RLS ENABLE+FORCE |
| `0002_knowledge_tables` | Claim/Evidence/Event/时间轴/会话/任务/生成记录（含 use_alter 循环外键的显式 ALTER） |
| `0003_index_generation` | 检索索引代际表、Chunk 的 bm25/trigram/diskann 索引 |
| `0004_intel_app_grants` | `intel_app` 角色 DML 授权、默认特权、`GRANT intel_app TO current_user` |

ORM 按 spec 03 分组在 `src/intel/db/models/`：`auth`、`workspace`、
`sources`、`pool`（文档池/Chunk）、`knowledge`、`conversation`、`jobs`、
`generation`、`retrieval`。

## RLS：唯一的多租户边界

隔离不写在 WHERE 子句里，写在数据库里：

- 所有 owner 维度的业务表 `ENABLE ROW LEVEL SECURITY` **并** `FORCE`
  （表 owner 也受策略约束）。
- 应用连接使用 `intel_app` 角色：`NOLOGIN`、非表 owner、不 `BYPASSRLS`，
  只有 0004 授予的 DML 权限。
- 每个事务开头执行 `SET LOCAL ROLE intel_app`
  （`db/rls.py: set_app_role`），再把 `app.owner_id` /
  `app.industry_id` 绑定为事务本地 GUC（`set_scope` / `bind_app`）。
- 策略谓词读 GUC：不绑定 scope 的查询看到零行（fail-closed）。

```python
# 典型 opener（api/deps.py 与 workers/stores.py 同一纪律）
conn = await engine.connect()
await conn.begin()
await set_app_role(conn)      # SET LOCAL ROLE intel_app
await set_scope(conn, scope)  # SET LOCAL app.owner_id = '…'
```

`SET LOCAL` 保证角色与 GUC 都随事务结束还原，连接归还连接池后不残留
上一个用户的身份。调度器/迁移等管理动作用 superuser 连接，明确绕过
RLS（`jobs` 认领必须跨 owner 扫描）。

## 复合 scope 外键

凡同时挂在 owner 与行业维度上的行（如 processing_decisions），外键用
复合 `(owner_id, industry_id)` 指向父表的复合唯一键，杜绝"跨行业挂错
父行"这类应用层 bug 在数据库层面发生。

核心表关系（简化 ER 图，省略列）：

```{mermaid}
erDiagram
    users ||--o{ sessions : "登录"
    users ||--o{ industries : "owner"
    industries ||--o{ topics : "主题"
    industries ||--o{ owner_feeds : "来源"
    owner_feeds ||--o{ discovery_items : "发现"
    discovery_items ||--o{ captures : "抓取"
    captures ||--o{ documents : "canonical"
    captures ||--o{ parsed_artifacts : "解析"
    parsed_artifacts ||--o{ chunks : "索引"
    parsed_artifacts ||--o{ processing_decisions : "判别"
    parsed_artifacts ||--o{ evidence : "引文定位"
    evidence }o--|| claim_revisions : "支撑"
    claim_revisions }o--o{ event_revisions : "事件成员"
    event_revisions }o--|| events : "不可变修订"
    events ||--o{ dependency_edges : "演进/stale"
    users ||--o{ conversations : "会话"
    conversations ||--o{ messages : "串行轮次"
    industries ||--o{ report_builds : "报告"
    evidence ||--o{ report_citations : "引用"
    industries ||--o{ jobs : "scope"
    jobs ||--o{ job_events : "outbox"
```

要点：`event_revisions` 与 `claim_revisions` 只追加（不可变修订）；
`jobs.job_events` 是同事务 outbox；`evidence → parsed_artifacts`
的引用链保证任何结论可回放当时的快照。

## 不可变修订（核心表族）

- `claim_revisions` / `event_revisions` / `conversation_summary_revisions`
  只追加：更正产生新修订行，旧行文本永不改写（TIM-03 可证）。
- 修订行带 `recorded_at`；事件卡查询用窗口函数取每事件最新修订，
  排序带 `EventRevision.id` 做平局裁决。
- 复核（reviews）通过 `expected_versions` 乐观并发控制；`row_version`
  在每个写路径 `WHERE row_version = :expected` 递增。

## 时间四元组

事件修订携带 `occurred_* / published_* / effective_*` 三组
`TimeValue`（精度 `day|month|year|unknown`，区间用 start/end 表达）加
`first_discovered_at`。时间轴把 occurred 与 discovery 当作**两个独立
排序轴**（`sort=occurred|discovered`），as_of 过滤独立于时间范围控件
（TIM-02：今天发现的去年事件，在 as_of=上月的视图里不可见）。

## 幂等与游标

- 任务幂等：`jobs` 表 `UNIQUE(owner_id, kind, idempotency_key)`，
  键的组成由 `KIND_SPECS` 集中声明（见 {doc}`jobs`）。
- API 幂等：`Idempotency-Key` 头 → `repositories/idempotency.py`
  的请求记录，重放返回首次响应。
- 游标：offset 用 HMAC 签名（`api/pagination.py`），拒绝伪造；
  时间轴/事件流用业务键 `(effective_sort_key, event_id)` 做 keyset 分页。
