# 检索

选型在设计阶段锁定（D13/D14）：

- **BM25**：PostgreSQL 扩展 `pg_textsearch` + 中文分词 `pg_jieba`
  （parser 名 `jieba`）。查询构造用 `<@>` 操作符——pg_textsearch 返回
  **负分**（越小越相关），smoke 测试显式断言负分以防语义回归。
- **向量**：`pgvector` + `pgvectorscale`（diskann 索引）。默认基线是
  **精确排序** `ORDER BY embedding <=> $1`；diskann 索引建成非默认
  代际，切换为默认必须先过 REC-08 评测（98% recall@10 门）。

## 分块（chunker）

`retrieval/chunker.py`，版本号 `chunker@1`：

- 标题层级 + 段落切分，目标 ~800 token，上限 1200，重叠 100；
- 表格按行分组并携带表头（表头单独成行时后续行都带 header 前缀）；
- 每块记录来源 `block_ids`（可回溯到 parse 的块坐标系统）；
- `chunker_version` 写入 chunks 与索引代际——版本变更是新代际，
  不是原地覆盖。

## 索引代际（index_generation）

`0003` 迁移建立 INSERT-only 的代际注册表（spec 03 §8）：

1. 词典/分词配置/embedding 模型任一变更 → 声明新 generation；
2. 双写回填（新旧两代 chunks 并存）；
3. 评测通过后切换默认代际；旧代际按需下线。

v1 只落了注册表与"版本漂移显式失败"（见 {doc}`ingest`），完整
双写/回填/切换编排是显式登记的延后项。

## 四通道召回（recall）

`retrieval/recall.py`，spec 04 §6：

```{mermaid}
flowchart LR
    Q["查询"] --> C1["语义<br/>向量 &lt;=&gt;"]
    Q --> C2["FTS<br/>BM25 + jieba"]
    Q --> C3["精确别名<br/>pg_trgm"]
    Q --> C4["正例邻近<br/>已采纳证据邻块"]
    C1 & C2 & C3 & C4 --> U["union + 按文档归并"]
    U --> RRF["RRF k=60<br/>只定顺序, 不做阈值删除"]
    RRF --> P["top-k 排序展示<br/>(文档不删, REC-01)"]
    C1 & C2 & C3 & C4 -.逐通道留痕.-> H["recall_hits"]
```

| 通道 | 手段 | 捕捉 |
|---|---|---|
| 语义 | 向量 `<=>`（embedding 经 L1 route，缺失不阻塞归档，REC-04） | 改写、近义 |
| FTS | pg_textsearch BM25 + jieba 分词术语 | 术语直击 |
| 精确别名 | pg_trgm 相似 | 型号名、别名拼写 |
| 正例邻近 | 已采纳证据的邻近块 | 同文档上下文（无 chunk 的邻近命中落 `chunk_id NULL` 的 recall_hits） |

- **RRF k=60 只定顺序**：四通道 union、按文档归并，分数仅用于排序，
  不做阈值删除——`top-k` 不删除文档，只决定展示顺序（REC-01 断言
  整篇仅一段相关也能命中）。
- `recall_hits` 逐通道落库留痕（每条命中标注来源通道），支撑前端
  的召回解释与后续 REC-08 评测。
- 批量 100 + 游标持久化（REC-03：超一页候选续批，offset 寻址完整
  RRF 排序，`seen` 仅作去重守卫——不允许跳行）。

## 与 RLS 的关系

所有检索 SQL 在 RLS 之上**仍显式携带** owner/industry 过滤条件
（ISO-03 变体）：数据库策略是安全底线，查询谓词是性能与正确性的
双保险。BM25 k1/b（`Settings.bm25_k1=1.5, bm25_b=0.75`）与 diskann
查询参数（`search_list_size=100, rescore=50`）集中在 Settings，
并有测试跨 bm25.py / ORM / 迁移 / Settings 四处交叉钉住一致性。
