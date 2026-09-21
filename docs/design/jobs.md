# 任务系统

没有外部消息队列。`jobs` 表就是队列，PostgreSQL 的事务与行锁就是
投递语义（spec 07）。这换来了与业务数据完全一致的备份/迁移/观测故事，
代价是吞吐上限——对本系统的规模（行业情报，非互联网并发）是正确取舍。

## 幂等键（KIND_SPECS）

`services/jobs.py` 的 `KIND_SPECS` 是 16 种任务键组成的唯一事实源：

```python
KindSpec("discover", ("owner", "feed", "schedule_slot", "config_version"), 5)
KindSpec("fetch",    ("owner", "discovery_item", "refresh_epoch"), 5)
KindSpec("parse",    ("owner", "capture", "parser_version"))
KindSpec("index",    ("owner", "parse", "chunker_version", "embedding_version"))
KindSpec("route",    ("owner", "industry", "parse", "industry_revision", "analysis_version"))
KindSpec("extract",  ("industry", "parse", "extraction_version"))
KindSpec("event_build", ("industry", "extraction_commit", "event_policy_version"))
KindSpec("report_build", ("industry", "report_type", "period", "input_manifest_hash"))
KindSpec("archive_answer", ("industry", "message_id"))
KindSpec("investigate", ("industry", "message_id", "request_version"))
KindSpec("apply_review", ("industry", "review_id", "decision_version"))
KindSpec("watch_check", ("industry", "watch", "schedule_slot"))
KindSpec("source_poll", ("owner", "feed", "mode", "idempotency_key"), 5)
# 以及 topic_recall / evolution_build / reclassify（已声明，未注册执行器）
```

- 键按声明**精确**组包：多传少传部件直接 `ValidationFailed`
  （build_key 里显式 diff 报错）。
- `UNIQUE(owner_id, kind, idempotency_key)`：重复入队返回既有 job，
  重试风暴与派生链重复天然免疫。
- "强制重跑"不改键——发新的 `refresh_epoch` / run nonce。

## At-least-once + 业务侧幂等

执行语义是 at-least-once：任何一步都可能重放。因此每个 handler 的
写入路径都以幂等键开头（重复认领直接短路成功），保证重放无害。
派生链（{doc}`ingest`）的每一跳都在**父任务同一事务**里入队：
入队失败则父任务提交一起回滚，链条不会断头也不会双发。

## Fencing 租约

```{mermaid}
sequenceDiagram
    autonumber
    participant R as runner
    participant DB as jobs 表
    R->>DB: FOR UPDATE SKIP LOCKED LIMIT 1 认领
    DB-->>R: job + lease_token + lease_until=now()+90s
    loop 每 30s
        R->>DB: 心跳续租 WHERE lease_token=:t
    end
    R->>DB: 步进提交 WHERE lease_token=:t AND lease_until>now()
    alt 匹配零行
        Note over R,DB: LeaseLost：僵尸进程写入被拒（fencing）
    end
    R->>DB: 终态 + job_events(outbox, 同事务)
```

- 认领：`FOR UPDATE SKIP LOCKED LIMIT 1`，写 `lease_token`（随机）、
  `lease_until = now() + 90s`、`attempt += 1`。
- 心跳：runner 每 30s 用 `lease_token` 续租。
- 步进提交：`UPDATE … WHERE lease_token = :t AND lease_until > now()`，
  匹配零行 = `LeaseLost`（ fencing：僵尸进程的写入被拒绝）；
  僵尸 finalize 也有守卫（不得覆盖持锁中的任务）。
- `max_attempts` 耗尽（网络类 kind 上限 5 次，spec 07 §6）→ `failed`
  或 `waiting_review`。

## 状态机与事件流

```{mermaid}
stateDiagram-v2
    [*] --> queued
    queued --> running: 认领(租约)
    running --> succeeded
    running --> partial
    running --> failed: 重试耗尽
    running --> waiting_review: 需复核
    running --> retry_wait: 可重试失败
    retry_wait --> queued: 退避到期
    waiting_review --> running: 复核通过
    queued --> cancelled
    running --> cancelled: 步进边界生效
    retry_wait --> cancelled
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
```

- 状态迁移表显式声明（非法迁移直接拒绝）；
- 每次状态变化/步进写 `job_events`（单调 seq，PK(job_id, seq)），
  **outbox 模式：与业务写同事务**；
- 取消在步进边界生效：middleware 层（MW-02）在每次 agent/LLM 调用前
  检查 job 是否仍 active（见 {doc}`nooa`）；
- SSE `GET /jobs/{id}/events` 按 `Last-Event-ID` 重放 + 心跳注释 +
  游标过期 409（见 {doc}`api`）。

## Runner 与组合根

`workers/runner.py` 逐 kind 执行注册的 handler；`workers/composition.py`
声明 kind→role 过滤与全部依赖装配（SQL store、NOOA runtime、追踪、
usage sink）。研究类任务需要 `INTEL_GATEWAY_SECRET`，为空时 fail-closed
拒绝启动（{doc}`security`）。`watch_check` 目前是 ack 占位；
`evolution_build` / `topic_recall` / `reclassify` 已声明键但未注册
执行器——都是显式登记的延后项，不是遗漏。
