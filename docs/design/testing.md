# 测试策略

门禁：`uv run ruff check src tests` 干净 + `uv run pytest`
**676 passed / 1 skipped / 15 deselected**（integration/live 默认排除）。

## 分层

| 层 | 目录 | 依赖 | 数量级 |
|---|---|---|---|
| 单元（纯函数/服务/工作流，fake store） | `tests/unit/` | 无 | 主力 |
| 契约（OpenAPI path diff、DTO schema 对设计包） | `tests/contract/` | 无 | 每设计面一组 |
| golden 评测（FakeLLM 脚本化跑 extract/route 流水） | `tests/eval/` + `tools/run_golden.py` | 无 | Etching 三主题 ≥5 样本/主题 |
| 集成（真 Postgres：jieba/BM25 `<@>`/diskann） | `tests/integration/` | marker `integration` | 默认跳过 |
| 真实模型 | `tests/` marker `live` | marker `live` | 默认跳过 |

fake store 的纪律：**两侧接缝同构**——生产 SQL 适配器与测试 double
实现同一 Protocol，行为性断言（如事件时间落库 → 时间轴分组正确）
在 double 上跑端到端；SQL 形状（语句文本、SET ROLE、GUC 绑定）用
statement-recording 连接单独钉住。已知边界：FakeConn 抓不到真实
CHECK/FK 违例——联调期补一条真库写入冒烟。

## 契约测试（tests/contract/test_openapi.py）

- **path diff**：FastAPI 实际路由集合 vs 设计包 `openapi.json`，
  非空 diff 必须列出缺失路由；8 个显式豁免（如
  `/health/ready` 未接线）各带书面理由；
- **DTO schema 子集**：从 `intel.contracts.models` 生成 JSON Schema，
  与设计包 `schemas.json` 比对 CorrectionCommand 判别联合、
  TimeValue 不变量、GenerationViewerLink；
- 设计包快照 **vendored** 在 `tests/contract/design_pack/`
  （v1.3），仓库自包含；`INTEL_DESIGN_PACK` 环境变量可指向活的
  设计包 checkout 对比新版本——上游漂移会被测试显式暴露而不是
  静默通过。

## golden 评测（Task 15）

`fixtures/golden/{etching-rf,etching-materials,etching-oes}/` 合成样本；
`tools/run_golden.py` 用 FakeLLM 脚本化跑 extract/route 流水，输出
nooa-bench 风格 trajectory JSON。行为报告（引文命中率、误并率等）
是真实 LLM 接入后的离线回归基线。曾有 golden 样本数字（OES `96%`）
与 EVI-01 数值校验不一致的教训——样本即规格，必须自洽。

## 工程纪律（从本仓库事故中沉淀）

1. **提交前序列不可跳**：`ruff check --fix` → `ruff format`（仅本次
   改动文件；全仓 format 会重写 ~60 个历史文件）→ **全量** pytest
   → commit。两次"格式化后没重跑全量、带红提交"的事故后固化为
   规则。
2. **脚本化补丁后先 grep 确认落地**再跑测试（sed 锚点静默空转
   事故的教训）。
3. **PIE790 全局忽略**：省略号是 NOOA 生成标记（见 {doc}`nooa`）。
4. **每条模型输出落库前必有校验测试**：block 引用精确子串、
   claim id 成员、时间确定性解析——终审发现的 I-2/I-3 一类缺口
   以此为门。

## 终审覆盖

两轮全分支终审（T10–13、T14–17）的 Critical/Important 全部修复并
复审通过；搁置项（SSE 握手连接持有、generation_runs 写侧、更正类
review 提交、mock 深度等）逐条登记在
`.superpowers/sdd/2026-09-20-semiconductor-intel-v1/progress.md`
台账并注明代价。
