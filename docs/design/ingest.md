# 采集管线

覆盖 discover → fetch → parse → (index, route) → extract → event_build
的完整链条。核心不变量：**完整增量采集**——发现阶段不按主题/标题/top-k
丢弃文档，一切过滤都发生在后置的判别（route）阶段并留痕。

## 发现（discover / source_poll）

`sources/adapters/` 为每类来源提供增量发现适配器：

| 适配器 | 目标 | 增量机制 |
|---|---|---|
| `rss` / `atom` / `feed` | 订阅源 | 条目 id/guid 去重 |
| `api_list` | 列表型 JSON API | 游标/时间戳参数（`adapters/cursor.py`） |
| `html_list` / `sitemap` | 列表页 / sitemap | URL 归一后与已抓集合差 |
| `page_monitor` | 单页变更监测 | 块级 diff（`parsing/blocksdiff.py`），报告 new/changed |

- URL 归一（`domain/urlnorm.py`）：去 tracking 参数、统一 scheme/host
  大小写，保证"同一文档"不重复入池。
- `source_poll` 是 API 触发的轮询任务（请求级 `Idempotency-Key` 直接
  作幂等键的一部分）；`discover` 是调度器按 `next_poll_at` 入队的周期任务。
- 抓取前过 SSRF 防护（见 {doc}`security`）。

## 抓取（fetch）

`sources/fetcher.py` 直连抓取；需要 JS 的页面降级到 Playwright
浏览器渲染（`sources/browser.py`，环境未装浏览器内核时报告
`browser_unavailable` 而非失败）。每次抓取落三类东西：

1. 原始字节 → 对象存储（`sources/blobstore.py`，compose 卷 `objects`）；
2. HTTP 元数据 + 抓取结果（`capture` 表，含 outcome 枚举）；
3. `document_origins`（canonical_url 等来源信息，供 source family 使用）。

## 解析（parse）

`parsing/parser.py` 按 MIME 分派 html/pdf/text：

- HTML 正文抽取（`html.py`）+ 样板剔除；PDF 文本层抽取（`pdf.py`）。
- 质量判定（`quality.py`，阈值在 `Settings`）：文本占比越界、样板占比、
  可用字符下限（PAR-03）、链接密度。判定结果写进 parse 的
  `quality_flags`，不静默丢弃文档。
- 输出 `parsed_artifacts`：全文 + **块序列**（带稳定 `block_id` 与
  序号）。块是后续一切引用的坐标系统。
- `parse_status = ok|partial` 时，同一事务内同时派生 `index` 与
  `route` 两个子任务（index 消费分块，route 消费块序列——两者并行，
  不互相依赖）。

## 索引（index）

`retrieval/indexer.py` 调 chunker 分块（见 {doc}`retrieval`）写 `chunks`
表与 `recall_hits` 之前的索引代际。幂等键含
`(chunker_version, embedding_version)`：任何一版配置变更都会生成新的
索引代际而不是原地改写；任务开头检测三类版本漂移
（payload 与运行时代码、已存在行、embedding 版本）并显式失败，
防止"配置变了但旧索引还顶着新版本号"的静默错配。

## 判别（route）

对每个 parse，一条**通配行业任务**（幂等键中 industry = `*`）在
一个 job 里对全部 active industries 出判决：

- `direct / background / uncertain / unrelated` + 理由 + 块引用；
- `judge_industry`（L1）产候选；落库前 `_verified_block_references`
  校验每个 block_reference：block_id 必须真实存在，quote 必须是该块
  的**精确子串**——校验失败的引用被丢弃并记入
  `input_manifest.dropped_block_references`；
- direct 行业继续做主题关联 `judge_topics`（spec 04 §6 A 通道）：
  输出覆盖校验保证每个 active topic 都有判决或显式
  `fallback:no_verdict`；结果持久在 processing_decision 的
  `candidate_claims.topic_verdicts`；
- 行业数超过 `max_verdicts_per_doc`（默认 1024）时任务显式失败
  `too_many_industries`——**绝不静默截断**，进度里的
  `industries_judged` 永远是真实判决数；
- 每次判决写 `processing_decisions`（复合 scope 外键），scope 违规
  记 audit_log；
- 判决完成后派生 `extract`（每个 direct/uncertain 行业一个，
  幂等键含 `extraction_version`）。

## 状态可见性

每个任务的 payload/进度写 `job_events`（outbox，同事务）；
`GET /api/v1/jobs/{id}/events` 用 SSE 播放（见 {doc}`api`）。
RunCenter 页面（{doc}`frontend`）按 kind 聚合展示运行历史。
