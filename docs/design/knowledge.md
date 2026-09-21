# 知识层：证据、事件身份、合并

规格 05 的落地。原则只有一句：**每一条进入知识库的陈述都要能沿
"事件 → claim → evidence → 引文 → parse 快照"这条链逐层点开**。

## 证据抽取（extract）

引文校验门（EVI-01）——模型输出落库前的全部关卡：

```{mermaid}
flowchart TD
    Q["模型产出: claim + quote + block_id"] --> M{"quote 在声称的<br/>block 内码点级精确匹配?"}
    M -- "多处命中" --> AMB["quote_ambiguous<br/>要求更多上下文, 绝不替模型挑"]
    M -- "block 属于其他 parse" --> MISS["block_missing"]
    M -- 唯一命中 --> LEN["长度/单位/否定/数值规则检查"]
    LEN -- 不通过 --> FAIL["extraction_failed<br/>不改写引文, 不换块重搜"]
    LEN -- 通过 --> SRC{"source_statement<br/>还是 inference?"}
    SRC -- "来源陈述缺出处" --> WARN["降级告警"]
    SRC --> SEM["EVI-02 语义判别缝<br/>(semantic_status_by_index)"]
    WARN --> SEM
    SEM --> COMMIT["commit_extraction<br/>claim/evidence/model_run 关联提交<br/>origin_ref → source family"]
```

EVI-01（纯函数，`domain/validation.py`，完全离线可测）：

- 引文必须在其声称的 block 内做**码点级精确匹配**；
  - 匹配到多处 → `quote_ambiguous`，要求模型给更多上下文，绝不替它挑一个；
  - block id 属于其他 parse → `block_missing`；
  - 长度/单位/否定等规则检查（数值型 claim 的 `96%` 必须在引文里）。
- 引文区分 `source_statement`（来源陈述）与 `inference`（推断）；
  来源陈述缺出处标注则降级告警。
- 任何校验失败 → 该 claim 记 `extraction_failed`，**不改写引文、
  不换块重搜**，留待复核或重抽。
- EVI-02 语义判别是注入缝：`semantic_judge` 返回
  `semantic_status_by_index`，由 `commit_extraction` 写入证据行的
  `semantic_support_status`；默认不接线（`pending`）。

`commit_extraction`（`services/knowledge.py`）把通过校验的 claim/
evidence 与产生它们的 `extraction_runs`/`model_runs` 关联提交，并从
`document_origins` 解析 `origin_ref` 建/归入 **source family**
（EVT-01 的独立来源计数依据：同一 canonical_url 的多次转载只算一个
来源家族）。

## 事件身份（domain/identity.py）

提案合并判定流：

```{mermaid}
flowchart TD
    P1["提案 A"] & P2["提案 B"] --> SAME{"事件类型相同<br/>且身份组件完全相等?"}
    SAME -- "否" --> AUTO["布尔合并门拒绝"]
    SAME -- "是" --> EV{"两提案证据是否<br/>来自独立 source family?"}
    EV -- "弱/同家族证据" --> DUP["不自动合并:<br/>新事件 + possible_duplicate 提案"]
    EV -- 独立 --> MERGE["自动并入同一事件<br/>(EVT-01: 十篇同公告=一事件)"]
```

七种强身份事件类型，各有必选身份组件（spec 05 §2 表）：

| 事件类型 | 身份组件示例 |
|---|---|
| paper_version | work_id + version + action |
| product_release | 发布记录 + 产品 + phase |
| facility_event | 主体 + 地点 + 事件类型 + 时点 |
| … | 共 7 类；`other` 永远不参与键控 |

- **布尔合并门**：两提案身份组件**完全相等**才允许自动合并进同一事件；
  不存在相似度阈值——"看起来像"不合并。
- 证据弱（单来源、转载家族）→ 不合并，建新事件并挂
  `possible_duplicate` 提案待复核。
- 同日两产品 ≠ 同一事件（EVT-02）；预告/内测/正式发布是三个 phase
  不同的事件（EVT-03）；十篇同公告 → 一事件一来源家族（EVT-01）。

## 事件构建（event_build）

```{mermaid}
sequenceDiagram
    autonumber
    participant H as event_build handler
    participant DB as 知识库（同事务）
    participant M as propose_events (L3)
    H->>DB: 读回 extraction_run 的全部 claim
    H->>M: 提案（身份组件/类型/时间/claim 引用）
    M-->>H: EventProposal
    Note over H: claim_ids ⊆ 已提交集合?<br/>越界引用丢弃计数
    Note over H: 时间表达 → parse_occurrence_time<br/>解析失败降级 unknown，绝不阻塞
    H->>DB: resolve_event：身份键命中→追加修订<br/>未命中→建事件+首修订（时间四元组）
```

输入是已提交的抽取批次（幂等键含 `extraction_commit`）：

1. 读回本次 `extraction_run` 的全部 claim；
2. 模型（`propose_events`，L3）产事件提案：身份组件、事件类型、
   **时间表达**、引用的 claim id；
3. Python 侧逐项验证：
   - `claim_ids ⊆ 本次已提交集合`，越界引用丢弃并计数；
   - 时间表达经 `domain/time.py: parse_occurrence_time` 确定性解析成
     `TimeValue`（ISO/中文日期，月/年覆盖整段区间；解析不了降级
     `unknown`，**绝不阻塞任务**）；
4. `resolve_event` 按身份键查既有事件：命中 → 追加修订/证据；
   未命中 → 建事件 + 首修订；时间四元组写进修订
   （occurred/projected `occurred_start/end`）；
5. 时间轴立即可见：正确的 日期未知/已知 分组、TIM-01 区间过滤、
   `late_discovery` 标记。

## 合并与撤销（EVT-04）

`apply_event_merge`（服务层事务）：

- `expected row_versions` 乐观并发；`membership_snapshot` 记录合并前
  两事件各自的成员（claims/证据）集合；
- 合并完成同事务内调 `mark_derived_stale`：沿
  事件 → claims → 证据 → `report_citations` 反向遍历，为引用过这些
  证据的报告写 `dependency_edges` + `derived_status`（首次
  `stale_since` 生效，即 StalePolicy 的防抖语义）+ 一条 audit_log；
- **undo**：冲突检测失败（成员已被后续操作改动）→ 返回 409 并生成
  补偿提案，而不是部分回滚。

## 时间轴与演进读取

- `services/timeline.py`（纯函数）：`(effective_sort_key, event_id)`
  keyset 游标；未知日期独立成组且永远排尾；`as_of` 过滤
  `recorded_at`（TIM-02）；修订选择保持旧文本不变（TIM-03）。
- `services/evolution.py`：演进构建六步——input_manifest 固定输入、
  边 basis=explicit/inferred、环检测拒绝（EVO-02）、`parallel` 边
  无向规范化、`insufficient_evidence` 状态、stale 传导
  （dependency_edges + derived_status，5 分钟防抖单任务）。
  EVO-01：无依据不建边。
