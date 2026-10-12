# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 计划中

- **M2** 讨论与收敛：S3 交锋 + S4 收敛判定 + 分歧台账
- **M3** 本地网页：`server.py` + SSE 实时气泡流
- **M4** 信息检索接入、`--resume` 断点续跑、单文件打包

详见 [docs/roadmap.md](docs/roadmap.md)。

---

## [0.2.0] —— 2026-10-12

**M1 最小可用闭环。`python3 -m sparky "..."` 现在能真的产出一份设计文档。**

### 新增

- `errors.py` 异常层次与退出码映射（0 成功 / 1 运行期 / 2 参数 / 3 配置 / 4 持久化）
- `models.py` 全部数据模型 + **严格**的 dict→对象解析
- `render.py` 终端渲染，订阅事件打中文进度时间线
- `events.py` 21 类事件定义 + 同步发布/订阅总线
- `config.py` 配置合并、密钥装载、`base_url` 校验、
  **提示词资源装载**（`<!-- PROMPT:x -->` 标记块 + `${占位符}` 严格替换）、
  上下文材料装载
- `session.py` 会话目录与 `meta.json`（**跑之前就建**）、事件溯源 JSONL、
  LLM 上下文快照、并发锁
- `llm.py` OpenAI 兼容调用：调用前落盘快照、重试、用量统计、
  **截断与空正文检测**、JSON 解析失败的有界重试
- `cli.py` + `__main__.py` 命令行与流水线编排
- 四个阶段模块：`brief.py`（S0 立案）、`personas.py`（S1 组阁）、
  `recon.py`（S2 调研，N 并发）、`synthesis.py`（S5 定稿 + 9 项合规校验）
- 提示词改为**机器可读的标记块格式**，`config.build_prompt` 直接读取
- 87 个单测（`python3 -m unittest discover -s tests -t .`）

### 实现中确认的六个真实问题

1. **推理模型的输出预算会被 reasoning 吃光。**
   `deepseek-flash` 在 `max_tokens=40` 时 `completion_tokens` 全是
   reasoning token，正文**完全为空**，而 `finish_reason` 未必是 `length`；
   长输入的定稿节甚至出现 `reasoning_tokens=8000`（整个预算）。
   三层处理：`--max-tokens` 默认提到 16000 并设 256 下限；
   `llm.py` 显式检测截断与空正文；**机械性任务关掉推理**
   （`thinking: {"type": "disabled"}`，实测 completion token 188 → 22）。

2. **提示词占位符不能用 `{x}`。**
   提示词正文里全是 JSON 花括号，`str.format` 会被撞死。
   改用 `${x}`；并且 `render_prompt` 的前两个参数改为**位置限定** ——
   早先 `build_prompt(name=...)` 被人设提示词里的 `${name}` 静默吃掉过一次。

3. **中文模型会在 JSON 结构位置写全角逗号。**
   实测到 `"甲"，"乙"`，`response_format: json_object` 拦不住
   （它不做约束解码）。处理方式是**有界重试 + 纠正提示**，
   而不是改写模型输出 —— 全局替换全角标点会连字符串内容一起改掉。

4. **`redact()` 会过度擦除。**
   原先的模式匹配裸的 `token`，把 `prompt_tokens` / `completion_tokens`
   等用量统计也擦成了 `<已擦除>`，附录 D 会因此变成一串无用占位符。
   已收窄到真正的凭证字段名。

5. **人数裁剪会把唱反调者切掉。**
   `_clamp_size` 原先是"从尾部一刀切"，而模型把反方排在最后一位时
   就被切掉了，随后校验报「专家组里没有唱反调者」。
   现在裁剪有三条优先级：不裁反方 → 优先裁维度最少的 → 不制造维度空洞；
   真裁不动就保留超员并告警。

6. **`_subsection` 把标题行算进正文，让检查形同虚设。**
   「§3 是否含被否决方案」的检查原本永远为真 ——
   因为 `### 3.2 被否决的方案` 这行标题里就有"否决"二字。
   已在取子节时丢掉标题行。

### 与设计定稿的偏差

- 新增了 `errors.py` / `models.py` / `render.py` 三个模块 ——
  设计定稿时没有给它们安排归属，已回填 [docs/architecture.md](docs/architecture.md)
- `llm.py` 在 M1 是**非流式**的（`turn.token` 打字轮效果属于 M3）
- `--web` / `--port` / `--resume` **刻意不在 M1 暴露** ——
  暴露一个只会报错的开关比不暴露更糟

### 已知不满足

- `--panel-size` 是「目标人数」而非硬性上限：模型偶尔会多给一个人。
  不一致时发出 `panel_size_mismatch` 告警并以实际生成为准 ——
  强行裁剪会破坏维度覆盖，那更糟。

---

## [0.1.0] —— 2026-10-12

**设计定稿版本。本版本不含任何实现代码。**

### 新增

- 总体设计说明书 [docs/design.md](docs/design.md)：
  问题定义（四个卡点 P1–P4）、成功判据 S1–S5、六阶段流水线、
  核心数据模型、成本控制、本项目自己的取舍台账与未决问题
- 架构说明 [docs/architecture.md](docs/architecture.md)：
  14 个模块的职责/输入/输出/**失败模式**、依赖方向禁令、
  事件协议（21 类事件）、会话磁盘布局、并发模型
- 人设分配机制 [docs/personas.md](docs/personas.md)：
  两段式生成（先维度后角色）、人数公式、反方强制规则、信息面分配
- 讨论与共识协议 [docs/consensus.md](docs/consensus.md)：
  分歧台账、收敛纯函数（6 个判定分支）、定向加轮、
  和谐告警（零分歧是失败信号）、死锁处理、8 条反模式
- 定稿文档规范 [docs/output-spec.md](docs/output-spec.md)：
  固定章节模板、**9 项合规校验表**、三种标记约定、一个反例对照
- 前端设计 [docs/web-ui.md](docs/web-ui.md)：SSE 协议、页面布局、零构建约束
- 配置说明 [docs/configuration.md](docs/configuration.md)：
  密钥集中约定、供应商地址表、**8 条来自实测的已知坑**
- 路线图 [docs/roadmap.md](docs/roadmap.md)：M0–M4 及可检验的验收标准
- 常见问题 [docs/faq.md](docs/faq.md)
- 四篇架构决策记录：
  - [ADR 0001](docs/adr/0001-python-stdlib-single-entry.md)
    Python 标准库 + 单入口，零第三方依赖、零前端构建
  - [ADR 0002](docs/adr/0002-persona-generation-over-fixed-cast.md)
    人设按维度现场生成，而非从固定角色池挑选
  - [ADR 0003](docs/adr/0003-consensus-as-explicit-ledger.md)
    共识 = 显式分歧台账，而非 LLM 自述一致
  - [ADR 0004](docs/adr/0004-information-scoping.md)
    信息面隔离：每个 Agent 看到不同的上下文
- 目录骨架与 14 个模块的**契约占位**（仅有 docstring，无逻辑）
- 提示词设计草案、人设维度库样例

### 说明

本版本刻意不含实现。设计阶段的产物是**决策与边界**，
把它们写清楚之后，实现才不至于边写边改架构。

[未发布]: https://github.com/orz0219/sparky/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/orz0219/sparky/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/orz0219/sparky/releases/tag/v0.1.0
