# 前端（Vue 3）

`web/`：Vue 3.5 + Vite 8 + TypeScript 6 + Pinia 4 + Vue Router 5。
**用户明确指定 Vue，不是 React**（计划文本里的 React 已被该指示覆盖）。
中文界面。

```{mermaid}
flowchart LR
    subgraph views["pages/ (09 §2 矩阵)"]
        L[Login] --- W[Workspace] --- D[Documents] --- T[Timeline] --- C[Conversations]
        EV[EventDetail] --- RV[Reviews] --- RC[RunCenter]
    end
    subgraph state["stores/ (Pinia)"]
        MS["mock.ts<br/>演示数据 + 写模拟"]
        SS["session.ts<br/>真实 API 接线(联调)"]
        UI[ui.ts]
    end
    subgraph infra["api/ + schemas/"]
        LOC["locate.ts<br/>locateQuoteInParse"]
        SCH["schemas/ DTO 类型"]
    end
    ED["EvidenceDrawer<br/>四层下钻"]
    views --> state
    state --> infra
    ED -.按 parse_id 定位.-> LOC
```

## 页面矩阵

按规格 09 §2：Login、Industries、Workspace（话题列表/向导）、
Documents、Topic、Timeline、Evolution、EventDetail、Conversations、
Reports、Reviews、RunCenter、Settings。当前为 **mock 数据驱动**
（`stores/mock.ts`）：`pnpm build` 通过 + 核心交互不变量成立即为完成
门禁；真实 API 接线（`stores/session.ts` 已备）在联调阶段完成。

## 不变量（设计 09 的硬性约束）

1. **证据抽屉四层下钻**：引用 → claim → 原文高亮 → 快照版本。
   `web/src/api/locate.ts: locateQuoteInParse` 与后端同规：
   - `parse.parse_id !== evidence.parse_id` → 直接抛错，
     **绝不**到另一份（更新的）正文里搜旧引用；
   - `status=invalid`（引用失效/待修正）时展示失效态而不是邻近
     模糊匹配；
   - verified 才渲染高亮 + 前后 120 字上下文。
2. **时间范围 ≠ as_of**：TimelineControls 上是两个独立控件——
   "事情发生的时间窗"（occurred）与"以哪个时间点的知识看"
   （as_of）永不被一个滑杆合并；`sort=occurred|discovered` 第三轴。
3. **写路径无乐观确认**：提交后等待服务器结果；409 版本冲突保留
   输入并双栏对比差异。
4. 引用覆盖率徽章（CoverageBadge）：报告未满 100% 覆盖不可能出现
   （后端已拦截），徽章展示的是证据健康度而非放行门槛。

## 运行中心（RunCenter）与 SSE

任务事件流消费 `GET /jobs/{id}/events`（SSE）：定时器管理的重连
在流完成后必须清理（曾修复：迟到的 `es?.close()` 会误关下一轮
mock 流）；`Last-Event-ID` 断点续播由后端保证，前端只需带
`Last-Event-ID` 头重连。

## 已知边界（联调清单）

mock 深度与 09 §2 全矩阵尚有差距（部分页面交互不完整、SSE 演示是
桩而非真 EventSource URL、logout 不清 mock 状态等）——均登记在
SDD 台账，属联调阶段工作，非门禁缺陷。
