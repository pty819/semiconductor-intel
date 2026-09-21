# 安全模型

纵深防御四层：

```{mermaid}
flowchart TD
    REQ["外部输入: 用户请求 / 网页内容 / 模型输出"] --> L1["① 数据库层<br/>RLS FORCE + intel_app NOLOGIN<br/>GUC scope 策略"]
    REQ --> L2["② 进程边界<br/>SSRF IP 段拒绝 + 浏览器拦截<br/>CodeAct 沙箱 network=False"]
    REQ --> L3["③ 模型边界<br/>MW-01 秘密扫描 + 预算<br/>工具网关 HMAC + 存活复查"]
    REQ --> L4["④ 仓库/会话卫生<br/>Argon2 + pepper + CSRF<br/>env-only 秘密 + 推送前扫描"]
    L1 & L2 & L3 & L4 --> OK["任何一层失守<br/>其余层仍约束"]
```

分四层：数据库（RLS）、进程边界（SSRF/沙箱）、模型边界（网关/中间件）、
仓库卫生（秘密管理）。

## 1. RLS 租户隔离

详见 {doc}`data-model`。要点复述：

- 业务表 `ENABLE + FORCE RLS`；应用角色 `intel_app` NOLOGIN、非 owner、
  非 BYPASSRLS，只有 DML 授权（迁移 0004）；
- 每事务 `SET LOCAL ROLE intel_app` + scope GUC；策略谓词读 GUC，
  不绑 scope 看到零行；
- API 与所有 worker opener 同一纪律；跨 owner 的调度扫描由 superuser
  显式承担，不混入业务连接。

## 2. 出站抓取（SSRF）

`sources/ssrf.py` + `sources/browser.py`（Task 7/8 硬化轮）：

- 目标解析后按 IP 拒绝私网/链路本地/环回段；重定向逐跳复检；
- 直连与浏览器两条路径都强制：浏览器用 CDP 路由拦截替代放行请求，
  Service Worker 场景在组合根 `new_context(service_workers="block")` 关闭；
- 抓取体量上限：XML/页面尺寸双上限；guarded client 显式报错而不是
  静默绕过（"loud guarded-client"原则）；
- 已知残余：DNS TOCTOU 需 `--host-resolver-rules` 级别的固定解析，
  登记为联调期硬化项。

## 3. 模型边界

- **秘密扫描**（MW-01）：每条进入 LLM 的消息先过 secret 扫描，
  命中即 `MiddlewareBlocked`——任务失败并审计，调用不发出；
- **预算**：每任务 LLM 调用上限 400（`llm_max_calls_per_job`）；
- **工具网关**：HMAC 令牌（owner/industry/job/actions/expiry）+
  逐调用 job 存活复查 + fail-closed 秘钥（详见 {doc}`nooa`）；
- **沙箱**：investigation CodeActV2 `execution_backend="sandbox"`、
  `network=False`；cell 输出上限（MW-03）双保险；
- **untrusted data 不进指令通道**：文档正文以参数/工具结果形式进
  模型，docstring 模板只含 `{self.attr}` 类实例状态——框架约定
  禁止 `{param}` 文本注入。

## 4. 认证与会话

- 密码 Argon2id + `session_pepper`（生产必须注入真值，dev 默认值
  明确标注 change-me）；
- 会话 cookie 签名 + CSRF token（写端点校验，guard 先于业务读取）；
- 登录轮换语义 = 每次登录签发全新随机 token（结构性防固定），吊销
  是显式事件（disabled_at 检查在认证路径上）。

## 5. 仓库卫生

- 全部密钥走环境变量（`.env.example` 只有占位）；compose 默认不含
  生产秘密；
- 推送前秘密扫描：正则扫 AKIA/gho_/ghp_/sk-/PEM 块/Slack token 等；
- 仓库当前含内网地址（82 数据库/LLM 端点默认值），**保持私有仓库**
  的原因即在此；转公开前需先参数化这些默认值；
- NOOA 以固定上游 commit 引入，供应链上可审计、可再生（`uv.lock`
  锁全树）。

## 威胁模型速记

| 威胁 | 缓解 |
|---|---|
| 跨租户读取 | RLS FORCE + GUC 策略 + scope 绑定依赖注入 |
| 模型幻觉污染知识库 | 引文码点级校验/claim id 成员校验/时间确定性解析，不合法即丢 |
| 提示注入（网页内容） | 内容走数据通道；工具面收敛到网关五工具；动作白名单 |
| 失控 agent（费用/循环） | 每 job 调用上限 + 取消在调用边界 + 租约 fencing |
| SSRF（抓取内网） | IP 段拒绝 + 双路径强制 + 体量上限 |
| 秘密泄漏 | env-only + 中间件扫描 + 仓库推送前正则扫描 |
