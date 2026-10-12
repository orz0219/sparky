# 架构说明

> 本文回答「代码怎么分、怎么串、数据长什么样」。
> 总体设计见 [design.md](design.md)；讨论协议见 [consensus.md](consensus.md)。

---

## 1. 模块清单

每个模块的职责、输入、输出、失败模式 —— **失败模式不许留空**。

| 模块 | 职责 | 输入 | 输出 | 失败模式 |
|---|---|---|---|---|
| `__main__.py` | 进程入口，`python3 -m sparky` 落点 | `sys.argv` | 退出码 | 参数非法 → 退出码 2 |
| `cli.py` | 解析命令行、校验互斥参数、组装 `Config` | `sys.argv` | `Config` | 未知参数 / 冲突组合 → 报错退出 2 |
| `config.py` | 装载配置与密钥；解析供应商 base_url 与模型 | `Config` + 环境变量 + `.env` | `RuntimeConfig` | **缺密钥 → 立即报错退出 3**，不进入任何阶段 |
| `llm.py` | OpenAI 兼容 `/chat/completions` 调用，SSE 流式；重试策略；用量统计 | messages + 模型参数 | token 流 + `usage` | 网络错误 / 4xx / 5xx / 响应非 JSON → 记录完整请求快照后抛出 |
| `events.py` | 事件模型定义与内存总线（发布/订阅） | `Event` | 回调 | 未知事件类型 → 抛错（不静默丢弃） |
| `session.py` | 会话状态机、事件溯源持久化、`replay()` | `Event` | 磁盘 JSONL + 内存状态 | 磁盘不可写 → 立即报错退出 4；日志截断 → 报错并提示 `--resume` 不可用 |
| `brief.py` | **S0 立案**：口语 → 结构化 `Brief` | 用户输入 + 可选上下文 | `Brief` | LLM 输出非 JSON / 缺必填字段 → 带原始输出报错 |
| `personas.py` | **S1 组阁**：定人数、生成人设、分配信息面 | `Brief` | `Panel` | 生成的人设缺 adversary → **自动补位**（设计规则，非兜底）；人数越界 → 裁剪并告警 |
| `recon.py` | **S2 调研**：N 人并发取证 | `Brief` + `Panel` | `list[StanceMemo]` | 某人调研失败 → 该人标记 `failed`，会话继续但文档中标注其缺席 |
| `debate.py` | **S3 交锋**：组织多轮讨论、登记分歧 | 台账 + 上轮 `Turn` | 本轮 `list[Turn]` + 更新台账 | 某轮发言解析失败 → 该人本轮弃权并记入台账，不伪造发言 |
| `consensus.py` | **S4 收敛**：纯函数判定 | `(Ledger, round, budget)` | `Decision` | 无 —— 纯函数无 IO，无失败模式（这是选它的理由之一） |
| `synthesis.py` | **S5 定稿**：装配模板 + 填充内容 | 全程状态 | `design.md` | 必需章节无法填充 → 报错，**不生成残缺文档** |
| `server.py` | 本地 HTTP 服务 + SSE 推送 + 静态资源 | 事件总线 | HTTP 响应 | 端口占用 → 报错并提示换端口，**不静默换端口** |
| `web/*` | 前端（HTML/CSS/JS，零构建） | SSE 事件 | DOM | JS 异常 → 在页面顶部显示错误条，不留白屏 |
| `tools/build_single_file.py` | 把包 + 前端资源打包成单个 `sparky.py` | 源码树 | `dist/sparky.py` | 资源缺失 → 报错，不产出半成品 |

## 2. 依赖方向（硬约束）

```
       ┌──────────────┐
       │ 表现层        │  server.py · web/*  ·  cli.py 的终端渲染
       └──────┬───────┘
              │  只允许依赖 ↓（事件总线）
       ┌──────▼───────┐
       │ 编排层        │  brief · personas · recon · debate · consensus · synthesis
       └──────┬───────┘
              │  只允许依赖 ↓
       ┌──────▼───────┐
       │ 基础层        │  llm.py · config.py · session.py · events.py
       └──────────────┘
```

三条禁令，实现时用 review 守住：

1. **编排层不得 import `server`**。它只往总线上发事件，谁在听不关它的事。
2. **表现层不得 import `llm`**。终端和网页都不能直接调模型。
3. **`consensus.py` 不得 import `llm`**。一旦它调了模型，终止条件就不可复现了。

## 3. 数据流

```
                     用户输入
                        │
        ┌───────────────▼────────────────┐
        │ cli.py → Config                │
        │ config.py → RuntimeConfig      │  ← 环境变量 / ~/.config/llm/keys.env
        └───────────────┬────────────────┘
                        │
        ┌───────────────▼────────────────┐
        │ session.py  开一个 Session     │──────────► .sparky/sessions/<id>/
        └───────────────┬────────────────┘             (先建目录，后跑)
                        │
   ┌────────────────────▼─────────────────────────────────┐
   │                    编排流水线                          │
   │                                                       │
   │  brief.run()      ──► Brief ──────────┐               │
   │  personas.run()   ──► Panel ──────────┤               │
   │  recon.run()      ──► [StanceMemo]    │  每一步都      │
   │  debate.run()     ──► [Turn] + Ledger │  往总线发事件  │
   │  consensus.decide() ──► Decision ─────┤  并落盘        │
   │  synthesis.run()  ──► design.md ──────┘               │
   └────────────────────┬─────────────────────────────────┘
                        │  Event
        ┌───────────────▼────────────────┐
        │ events.py  内存总线 (pub/sub)   │
        └───────┬────────────────┬───────┘
                │                │
     ┌──────────▼─────┐   ┌──────▼──────────┐
     │ 终端渲染        │   │ server.py → SSE │
     │ (stdout)       │   │ → web/index.html│
     └────────────────┘   └─────────────────┘
```

**关键顺序**：`session.py` 在流水线开跑**之前**就建好目录并写入 `meta.json`。
这样即使进程在第 3 秒崩掉，磁盘上也留着"这次想干什么"。

## 4. 磁盘布局

```
<工作目录>/.sparky/sessions/20261012-103000-podcast/
├── meta.json            会话元信息：原始输入、开始时间、参数、最终状态
├── brief.json           S0 产物
├── personas.json        S1 产物
├── stances/             S2 产物，一人一文件
│   ├── p1.json
│   └── p2.json
├── rounds/              S3 产物，一轮一文件
│   ├── r1.json
│   └── r2.json
├── ledger.json          分歧台账的**当前快照**（权威副本在 transcript 里）
├── transcript.jsonl     ★ 事件溯源日志，一切状态的来源
├── llm-calls/           ★ 每次 LLM 调用的上下文快照
│   ├── 0001-request.json
│   ├── 0001-response.txt
│   └── ...
├── design.md            ★ 最终产物
└── report.html          可选：单文件静态报告（把整场讨论打包成一个 HTML）
```

### 为什么 `ledger.json` 和 `transcript.jsonl` 同时存在

`transcript.jsonl` 是**权威**，`ledger.json` 只是**为了人看方便**的快照。
两者不一致时以 transcript 为准（`session.py` 的 `replay()` 会重建并覆盖 ledger）。

### 为什么调用快照要单独存

这是来自一条真实踩坑的硬性要求：**上下文快照必须在调用 LLM 之前落盘**。

如果写在调用之后，那么一旦这次调用失败，你手上恰恰**没有**"这次喂了什么"的记录，
而失败时最需要的就是这个。代价是失败也会留下一条 `-request.json` —— 这个代价是值得付的。

## 5. 事件协议（内部总线 & SSE 共用）

事件是编排层与表现层之间**唯一**的通信方式。SSE 直接复用同一套结构，
所以下面这张表同时就是 [web-ui.md](web-ui.md) §2 的对外协议。

| 事件 | 携带数据 | 前端反应 |
|---|---|---|
| `session.start` | `session_id, raw_input, config` | 建立会话头 |
| `brief.ready` | `Brief` | 渲染立案卡 |
| `panel.ready` | `Panel` | 渲染人设卡区 |
| `persona.assigned` | `Persona` | 该人设卡片高亮 |
| `recon.start` | `persona_id, scope` | 该卡片转「调研中」 |
| `recon.memo` | `StanceMemo` | 挂载立场备忘 |
| `round.start` | `round, focus` | 时间线新增一轮 |
| `turn.start` | `round, persona_id` | 气泡占位 |
| `turn.token` | `persona_id, delta` | **逐字追加**（打字机效果） |
| `turn.end` | `Turn` | 气泡定稿 |
| `disagreement.open` | `Disagreement` | 台账面板新增一条 |
| `disagreement.update` | `id, patch` | 台账条目刷新 |
| `disagreement.close` | `id, status, resolution\|tradeoff` | 台账条目划线/降级 |
| `round.end` | `round, ledger_digest` | 时间线收口 |
| `convergence.decision` | `Decision` | 顶部状态条更新 |
| `synthesis.start` | `—` | 显示"正在定稿" |
| `doc.ready` | `path, stats` | 展示产物与下载链接 |
| `warning` | `code, message` | 黄色告警条（如"本次零分歧"） |
| `error` | `stage, message, detail` | 红色错误条 + 终止 |
| `session.end` | `status, usage` | 收口，显示总成本 |

**约定**：
- `turn.token` 是**唯一**高频事件，必须允许前端批量按帧渲染，不要每 token 触发一次 layout。
- 任何事件都不携带密钥。
- `error` 事件之后必定跟 `session.end`。

## 6. 一次完整会话的时序

```
User          cli/config     session      brief/personas/recon/debate/consensus/synthesis   server/终端
 │                │            │                          │                                  │
 │──"想法"───────►│            │                          │                                  │
 │                │─校验配置──►│                          │                                  │
 │                │            │─建目录/写meta.json──────►│                                  │
 │                │            │──────────────────────────┼──session.start──────────────────►│
 │                │            │                          │                                  │
 │                │            │                    ┌─────▼─────┐                            │
 │                │            │                    │ S0 立案   │──brief.ready──────────────►│
 │                │            │                    │ S1 组阁   │──panel.ready──────────────►│
 │                │            │                    └─────┬─────┘                            │
 │                │            │                          │                                  │
 │                │            │                    ┌─────▼─────────────────┐                │
 │                │            │                    │ S2 调研（N 人并发）   │──recon.memo───►│
 │                │            │                    └─────┬─────────────────┘                │
 │                │            │                          │                                  │
 │                │            │              ┌───────────▼────────────┐                     │
 │                │            │              │ S3 交锋 第1轮（N 并发）│──turn.token───────►│
 │                │            │              └───────────┬────────────┘                     │
 │                │            │              ┌───────────▼────────────┐                     │
 │                │            │              │ S4 收敛（纯规则）      │──decision─────────►│
 │                │            │              └───────────┬────────────┘                     │
 │                │            │                          │                                  │
 │                │            │              ┌───────────▼────────────┐                     │
 │                │            │              │ S5 定稿                │──doc.ready────────►│
 │                │            │              └───────────┬────────────┘                     │
 │                │            │◄───session.end───────────┘                                  │
 │◄──退出码 0─────┴────────────┘                                                             │
```

## 7. 并发模型

| 阶段 | 并发 | 说明 |
|---|---|---|
| S0 | 串行 | 单次调用 |
| S1 | 串行 | 单次调用 + 规则校验 |
| S2 | **N 并发** | 各人信息面独立，无依赖 |
| S3 每轮 | **N 并发** | **两段式**：先全体生成，再统一登记分歧 |
| S4 | 串行 | 纯计算，微秒级 |
| S5 | 串行 | 单次调用（可能分章节多次调用） |

**两段式的必要性**：如果边生成边登记分歧，同一轮内先完成的人可能读到后完成者
刚写进台账的条目，导致**同轮不同人看到的世界不一致**。这件事在并发下不可复现，
是最难查的一类 bug。所以硬性规定：**轮内只读上轮快照，写台账必须等本轮全员结束**。

## 8. 扩展点

留出的口子（架构上支持，v0.1 不实现）：

| 扩展 | 位置 | 说明 |
|---|---|---|
| 新增信息源 | `recon.py` 的 `InfoSource` 接口 | 如接入本地文档库、Jira、代码仓库 |
| 每 Agent 独立供应商 | `config.py` 的 `persona_model_map` | 进一步降低同质化 |
| 自定义人设模板 | `personas/presets/*.yaml` | 用户固定评审团 |
| 自定义输出模板 | `synthesis.py` 的模板路径参数 | 如输出 RFC / PRD 格式 |
| 事后二次提问 | `session.py` 的 `replay()` + 单独 CLI 子命令 | "第 2 轮里成本官说了什么？" |
