# NOOA 适配层

[NOOA](https://github.com/NVIDIA-NeMo/labs-OO-Agents) 是代码生成型
智能体框架：方法含 `...` 即为 LLM 生成方法（docstring 即提示词），
返回类型注解即输出契约（Pydantic 强制结构化）。本仓库以 git 依赖
固定在 `d4d46f78`；`nooa_adapter/` 是系统与 NOOA 的唯一接触面。

> 坑位提醒：`ruff` 规则 PIE790（多余 `...`）在 pyproject 里**全局
> 关闭**——自动修复会删掉 docstring 后的 `...`，把生成方法静默变成
> 返回 None 的普通方法。任何"这个方法返回 None 了"的怪象先查省略号。

## 智能体与模型分层

`nooa_adapter/factory.py` 按规格 06/14 建 7 个 Agent 类
（Routing / Extraction / Relation / Evolution / Answer / Investigation /
QueryPlanner）。模型路由三档（spec 14 §3.1），经 unifiedllm registry
（`registry/routes.yaml`）指向任意 OpenAI-v1 兼容端点：

| 档 | 用途 | 方法 |
|---|---|---|
| L1 | 量大浅判 | describe_document, judge_industry, judge_topics, extract 的辅助判别 |
| L2 | 结构化抽取与撰写 | extract_claims, assess_duplicate, assess_relation, compose_report, compose_answer 等 |
| L3 | 判断与综合 | propose_events, investigate, summarize/compose_evolution, resolve_followup |

全部 12 个策略方法的档位分配有 pin 测试逐一钉住（曾修复过
compose_report 误挂 L3 的档位漂移）。`routes.yaml` 当前是显式占位
模型名 `openai/model-l1|2|3` + `api_key_env: LLM_API_KEY`——不硬编码
任何厂商模型名，真实模型 id 在联调时对端点的 `/v1/models` 实测后填入，
填错会在调用时快速 4xx 而非静默路由错配。

## 三层中间件（D16：防失控）

一次 LLM 调用穿过中间件链的完整时序：

```{mermaid}
sequenceDiagram
    autonumber
    participant A as 生成方法
    participant MW1 as llm_guard (MW-01)
    participant MW2 as agent_guard (MW-02)
    participant MW3 as cell_guard (MW-03)
    participant LLM as L1/L2/L3 端点
    participant SINK as SqlUsageSink → model_runs
    A->>MW2: agent 调用前
    MW2->>MW2: job 取消/丢租约? → MiddlewareBlocked
    A->>MW1: llm 调用
    MW1->>MW1: scan_for_secrets(messages) 命中即阻断
    MW1->>MW1: budget.check_llm_call(job) ≤400 次
    MW1->>LLM: 放行调用
    LLM-->>MW1: 响应 + usage
    MW1->>SINK: record(usage)（失败仅记日志）
    MW1-->>A: 响应
    Note over MW3: execute_python 之后:<br/>enforce_cell_limits ≤100k 字符
```

`install_intel_middleware(agent, …)` 挂三个拦截器：

```python
async def llm_guard(ctx, nxt):            # MW-01
    scan_for_secrets(ctx.messages)        # 命中即 MiddlewareBlocked，fake.call_count==0
    budget.check_llm_call(job_id)         # 每 job 上限 Settings.llm_max_calls_per_job=400
    resp = await nxt(ctx)
    usage_sink.record(scope, resp.usage)  # → model_runs 行（SqlUsageSink, RLS 合规）
    return resp

async def agent_guard(ctx, nxt):          # MW-02
    if scope.cancelled_or_lease_lost(): raise MiddlewareBlocked("job inactive")
    return await nxt(ctx)

async def cell_guard(ctx, nxt):           # MW-03
    out = await nxt(ctx)
    enforce_cell_limits(out)              # CodeAct 输出上限 Settings.cell_output_max_chars=100k
    return out
```

取消语义因此是**调用边界生效**：任务取消/丢租约后，下一次模型调用前
必然中断，不会跑完整个生成再丢弃。

## 工具网关（gateway）

```{mermaid}
flowchart TD
    SB["CodeActV2 沙箱<br/>execution_backend=sandbox, network=False"] -->|"五工具调用 + HMAC 令牌"| GW["ToolGateway"]
    GW --> V1{"签名/过期/动作白名单?"}
    V1 -- 失败 --> R1["拒绝"]
    V1 -- 通过 --> V2{"job 仍持租约?"}
    V2 -- 否 --> R2["拒绝<br/>(逐调用存活复查)"]
    V2 -- 是 --> X["执行: search_archive / read_evidence /<br/>read_document_blocks / fetch_public / search_web"]
```

Investigation 的 CodeActV2 沙箱（`execution_backend="sandbox"`、
`network=False`）里，模型能做的外部动作只剩五个工具：
`search_archive / read_evidence / read_document_blocks / fetch_public /
search_web`。每个调用携带 HMAC-SHA256 令牌（`compare_digest` 比较），
声明 `owner / industry / job / actions / expiry`：

- 令牌校验失败、过期、动作不在授权集 → 拒绝；
- **job 存活复查**：每次工具调用都重新确认 job 仍持有租约；
- `gateway_secret` 为空时构造即抛错（fail-closed），research runner
  拒绝启动；
- online-only 动作在铸币与接缝两层都被 archive 模式排除（QA-01 的
  "归档模式零 search_web"由此在机制上成立）。

## 上下文与摘要

- `TokenBudgetSummarizer` 接在 investigate 的 L3 客户端上
  （threshold=context_budget×0.65，preserve_recent=16，target 6000
  字符），任务 finally 里 `aclose()` 排空——长调查不爆上下文，
  且无回退窗口规则由 factory 保证。
- `Context` 块（NOOA 概念）使用约定：稳定前缀用 `prefix=True`
  进可缓存前缀；每轮变动的运行时状态用 `Context(expr=…)`。

## 追踪与用量

- 每个后台任务被 `_wrap_per_job` 包进 `job_trace_session`：
  JSONL exporter 写 `Settings.job_trace_dir/job-{id}.jsonl`，
  finally flush；NOOA 的 trace viewer 默认 5001 端口。
- `SqlUsageSink` 把每次 LLM 调用的 usage 写 `model_runs`（自建连接、
  `SET LOCAL ROLE intel_app` + scope GUC，失败仅记日志——可观测性
  不得拖垮业务调用）。
- `record_generation_run` / `link_generation_run_models` 已就绪；
  生产路径尚无 generation run 产生者，是登记的延后项。
