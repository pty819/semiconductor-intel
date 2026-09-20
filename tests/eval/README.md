# Golden set (offline skeleton)

Etching 三主题合成样本（RF/射频、材料选择、OES 监控算法），每主题至少 5 份，位于 `fixtures/golden/`. 标签是建设骨架，**不能当人工真值**（spec 11 §3）。

## Run

FakeLLM only — no live provider, no network.

```bash
uv run python tools/run_golden.py --fixtures fixtures/golden --out traces/golden-eval
```

Writes:

- `trajectory.json` — nooa-bench-style event list (`event_id`, `event_type`, `prefill`, `synthetic`, plus route/extract stage fields)
- `behavior.json` — nooa-bench `schema_version=2` placeholder (`content_policy=aggregate-counts-only`) with empty interface signals and 11 §3 leak-stage buckets (`source_not_fetched`, `fetched_not_indexed`, `recall_miss`, `route_error`, `extract_error`)

The runner scripts `FakeLLMClient` JSON for `describe_document` / `judge_industry` / `extract_claims`, then executes the real `route` and `extract` job handlers against in-memory stores.

## Not yet scored

Full eval (60 docs / 40 events, holdout phrasing, historical replay, citation coverage) is later. This task only lands the harness and the first 15 synthetic samples.
