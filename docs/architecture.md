# 架构说明

> 本文回答「代码怎么分、怎么串、数据长什么样」。
> 总体设计见 [design.md](design.md)；讨论协议见 [consensus.md](consensus.md)。

---

## 1. 模块清单

每个模块的职责、输入、输出、失败模式 —— **失败模式不许留空**。

标记 `M1` 的行是已经实现并有单测覆盖的；其余为设计定稿、待实现。

| 模块 | 职责 | 输入 | 输出 | 失败模式 |
|---|---|---|---|---|
| `errors.py` `M1` | 异常层次与退出码映射 | — | `SparkyError` 子类 | 无 IO，无失败模式 |
| `models.py` `M1` | 全部数据模型 + **严格**的 dict→对象解析 | LLM 返回的 dict | dataclass 实例 | 缺字段 / 类型不符 → 抛 `SchemaError`，带字段路径与原始值。**绝不填充默认值** |
| `__main__.py` `M1` | 进程入口，串起六阶段并翻译失败 | `sys.argv` | 退出码 | 参数非法 → 2；缺密钥 → 3；磁盘不可写 → 4；其余 → 1；未预期异常打印完整回溯 |
| `cli.py` `M1` | 解析命令行、校验取值、组装 `Config` | `sys.argv` | `Config` | 未知参数 / 取值越界 → 报错退出 2。**不提供 `--api-key`** |
| `config.py` `M1` | 装载配置与密钥；`base_url` 校验；提示词与上下文材料装载 | `Config` + 环境变量 + `keys.env` + `.env` | `RuntimeConfig` + 渲染好的提示词 | **缺密钥 → 立即报错退出 3**，不进入任何阶段；密钥文件格式错误 → 报错不跳过坏行；提示词缺标记 / 占位符不匹配 → 报错 |
| `llm.py` `M1`+`M3` | OpenAI 兼容调用：**非流式** `complete`/`complete_json` 与**流式** `stream_json`（含 SSE 分帧与 `SayExtractor` 增量抽取）；调用前落盘快照；重试；用量统计；截断与空正文检测 | messages + 模型参数 | `LlmReply`（正文 + usage） | 5xx/429/网络 → 重试 2 次后抛出；4xx → 立即抛出；响应非 JSON → 抛出；`finish_reason=length` 或正文为空 → 抛出（截断的 JSON 必然解析失败，不拿去解析） |
| `events.py` `M1`+`M3` | 21 类事件定义 + 同步发布/订阅总线 + **临时事件分级**（`turn.token` 不落盘） | `Event` | 回调 | 未登记的事件类型 → 抛 `ValueError`（不静默丢弃）；订阅者异常向上传递（表现层出错必须被看见） |
| `session.py` `M1` | 会话目录与 `meta.json`（**跑之前就建**）、事件溯源 JSONL、上下文快照、**并发锁** | `Event` + payload | 磁盘 JSONL/JSON + 内存状态 | 目录不可写 / JSONL 追加失败 → `PersistenceError` 退出 4；会话 id 撞名 → 报错不覆盖 |
| `render.py` `M1` | 终端渲染：订阅事件，打中文进度时间线 | `Event` | stdout | 事件类型没有渲染分支 → 抛 `KeyError`（实现遗漏必须暴露） |
| `brief.py` `M1` | **S0 立案**：口语 → 结构化 `Brief` | 用户输入 + 可选上下文 | `Brief` | LLM 输出非 JSON / 缺字段 → 报错；维度数不在 3–8 → 报错；维度 id 重复 → 报错 |
| `personas.py` `M1` | **S1 组阁**：定人数、生成人设、分配信息面 | `Brief` | `Panel` | 有维度没人负责 → 报错；两人追问清单重复 → 报错（信息面隔离被破坏）；无人是反方 → **自动补位**并告警（设计规则）；人数越界 → 裁剪并告警，**但绝不裁掉反方、也绝不制造维度空洞**，真裁不动就保留超员 |
| `recon.py` `M1` | **S2 调研**：N 人并发取证 | `Brief` + `Panel` | `ReconOutcome`（含失败者） | 某人失败 → 记入 `failures` + 发事件，会话继续，文档附录 A 标注缺席；**全部失败 → 报错**；上下文文件无人认领 → 告警并写进附录 B |
| `synthesis.py` `M1` | **S5 定稿**：装配骨架 + 6 节填充 + **9 项合规校验** + 写盘 | 全程状态 | `design.md` | 任一校验不过 → 报错且**不写盘**；台账条数比对不一致 → 报错（防篡改） |
| `debate.py` `M2` | **S3 交锋**：组织多轮讨论、登记分歧。含主持人（纯代码登记台账） | 台账 + 上轮 `Turn` | `DebateResult`（台账 + 逐轮发言） | 某人发言失败 → 记**显式弃权**（带原因），不伪造发言；连续弃权 2 轮移出并告警；引用了不存在的人设/分歧 → 告警并忽略该条，不让一条坏引用废掉整场；`closes` 与 `tradeoff_proposals` 撞同一项 → 告警并以 `closes` 为准 |
| `consensus.py` `M2` | **S4 收敛**：纯函数判定（6 个分支）+ `finalize` 收口 + 零分歧检出 | `(Ledger, round, Budget, max_rounds, stagnation)` | `Decision` | 无 —— 纯函数无 IO，无失败模式（这是选它的理由之一）。**禁止 import `llm`**，有单测守住 |
| `server.py` `M3` | 本地 HTTP 服务 + SSE 推送（**先补发历史、再进实时**，靠 `seq` 水位去重）+ 静态资源 + `POST /stop` + `POST /interject` | 事件总线 | HTTP 响应 | 端口占用 → 报错并提示换端口，**不静默换端口**；静态资源缺失 → 500 并写 stderr，**不返回空白页**；SSE 客户端断开 → 正常收尾路径，只捕获两个具体异常类型 |
| `web/*` `M3` | 前端（HTML/CSS/JS，零构建、零外链，约 1140 行）。**阅读体验见 [web-ui.md](web-ui.md) §2.5 的四个诊断** | SSE 事件 | DOM | JS 异常 → 顶部显示错误条；连接中断 → **不清屏**，已发生的事实必须留在屏幕上 |
| `tools/build_single_file.py` | 把包 + 前端资源打包成单个 `sparky.py` | 源码树 | `dist/sparky.py` | 资源缺失 → 报错，不产出半成品 |

### 实现中新增的三个模块

设计定稿时没有给这三者安排归属，M1 实现时补上并回填本文档：

- **`errors.py`**：把"失败就说失败"落成可执行的退出码映射。
  没有它，`__main__` 就得靠 `except Exception` 猜错误类型 —— 那正是本项目禁止的写法。
- **`models.py`**：全部数据模型集中一处，避免各阶段各造一套；
  更重要的是它承载了**严格解析**这条纪律（缺字段就报错，不填默认值）。
- **`render.py`**：终端渲染原本被含糊地归在"表现层"里，没有具体模块。
  它与 M3 的 SSE 服务是**并列的两个订阅者**，共用同一套事件协议。

## 2. 依赖方向（硬约束）

```
       ┌────────────────────────────────────────────────┐
       │ 表现层   render.py（终端）· server.py + web/*（M3）│
       └───────────────┬────────────────────────────────┘
                       │  只允许依赖 ↓（事件总线）
       ┌───────────────▼────────────────────────────────┐
       │ 编排层   __main__ · cli                        │
       │          brief · personas · recon · debate      │
       │          · consensus · synthesis               │
       └───────────────┬────────────────────────────────┘
                       │  只允许依赖 ↓
       ┌───────────────▼────────────────────────────────┐
       │ 基础层   config · llm · events · session        │
       │          errors · models（全体共用底座）         │
       └────────────────────────────────────────────────┘
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
两者不一致时以 transcript 为准（`replay()` 会重建并覆盖 ledger）。

> ⚠️ **M1 状态**：`ledger.json` 与 `replay()` 都还没实现 ——
> 它们要到 M2（分歧台账）与 M4（`--resume`）才落地。
> M1 的阶段目录里只有 `stances/`（S2 产物）与 `llm-calls/`。

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
| 事后二次提问 | 基于 transcript 的读取 + 单独 CLI 子命令 | "第 2 轮里成本官说了什么？" |
