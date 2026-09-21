# 会话问答、报告与复核

规格 14/16 的落地：带证据包的归档问答、在线调查、引文覆盖率强制的
报告、可撤销的复核生命周期。

## 会话模型（CHAT）

`ConversationCoordinator` 强制串行提交（spec 16 §6）：

- `begin_turn` 插入 `status="pending"` 的用户消息；串行守卫拒绝在
  存在 pending 轮时开新轮 → 并发发送得到 409（CHAT-04）；
- 回答提交（`insert_answer_message`，与用户行同一事务）：
  - 用户行 `pending → ready`，
  - 助手状态经**唯一映射** `assistant_message_status` 落库：
    `ok → ready`，`insufficient_evidence → partial`
    （契约 Literal 之外的字符串会被拒，避免脏值流入 MessageView）；
- 指代解析（CHAT-01）：`resolve_followup_question` 在组包前把"它/
  该机型"这类追问改写成自包含问题，并解析出引用的实体/constraint
  id。生产路径上由 `make_query_planner_agent`（L2）执行，上下文读取
  用一个短事务（排除本轮 pending 行），LLM 调用在任何事务之外；
  解析出的 `referenced_ids` 随消息 manifest 与任务进度持久，供下一轮
  解析链式引用；
- 上下文优先级按 spec 16 §6：最近轮次 > 被引用对象 > 摘要；
  `conversation_summary_revisions` 压缩历史但**保留原消息**
  （CHAT-06）。

## 归档问答（QA-01）

```{mermaid}
flowchart TD
    Q["用户问题"] --> FP["resolve_followup_question (L2)<br/>指代改写 + referenced_ids 解析"]
    FP --> PKG["EvidencePacket 组包<br/>allowed_citation_ids + actually_read_blocks"]
    PKG --> HAS{"包内有可用证据?"}
    HAS -- "无" --> IE["insufficient_evidence<br/>零工具调用、零 search_web<br/>(网关层排除 online 动作)"]
    HAS -- 有 --> GEN["compose_answer (L2)"]
    GEN --> MATCH{"引文语义匹配?"}
    MATCH -- "不匹配(一次机会)" --> FIX["修复轮: 重定位引文"]
    FIX --> MATCH
    MATCH -- "仍不匹配" --> TRIM["删减引用<br/>绝不保留错误引用"]
    MATCH -- 匹配 --> DONE["提交回答 (ready)"]
    IE --> DONE2["提交 (partial)"]
```

`archive_answer` 只允许读已归档内容：

1. EvidencePacket 组包（spec 14 §5）：`allowed_citation_ids` 白名单、
   `actually_read_blocks`（真正读过的块才允许引用）、answer-local
   evidence 命名空间；
2. 无证据可用 → 直接 `insufficient_evidence`，**零工具调用、零
   search_web**——这不是提示词约束，是网关层排除了 online 动作；
3. 引文语义不匹配 → 一次修复轮；仍不匹配 → 删减引用而非保留错误引用；
4. 引用错误计入消息质量元数据。

## 在线调查（investigate）

CodeActV2 沙箱执行（`network=False`），经工具网关（{doc}`nooa`）
访问五个受令牌约束的工具；TokenBudgetSummarizer 控制长调查上下文；
任务取消在调用边界生效。

## 报告（reports）

- `report_build` 幂等键 `(industry, report_type, period,
  input_manifest_hash)`：输入清单哈希相同 = 确定性重放；失败任务靠
  新 `refresh_epoch` 强制重跑；
- **引文覆盖率 = 100% 硬校验**（spec 05 §8）：报告正文每条结论必须
  挂 evidence 引用，覆盖不足的任务失败，不带病出稿；
- 数据源过期（merge/更正触发 `derived_status.stale_since`，见
  {doc}`knowledge`）→ 报告带 stale banner，前端置灰并提示重建。

## 复核（reviews）

| 动作 | 语义 |
|---|---|
| approve / reject | 写决定行，`expected_versions` 乐观并发，冲突 409 |
| undo | 显式补偿；`apply_review` 幂等键含 `decision_version`，重放安全 |
| waiting_review | 任务终态之一：复核通过前结果不生效 |

事件合并、topic-decisions、文档修订的更正走统一 decide/undo 入口；
`apply_decision` 对更正类 review 的实际提交步骤（把更正写回 claim）
是登记的延后项——生命周期与幂等已就位，提交动作待补。
