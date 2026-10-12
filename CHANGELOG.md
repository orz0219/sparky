# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 计划中

- **M1** 最小可用闭环：S0 立案 → S1 组阁 → S2 调研 → S5 定稿（终端输出）
- **M2** 讨论与收敛：S3 交锋 + S4 收敛判定 + 分歧台账
- **M3** 本地网页：`server.py` + SSE 实时气泡流
- **M4** 信息检索接入、`--resume` 断点续跑、单文件打包

详见 [docs/roadmap.md](docs/roadmap.md)。

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

[未发布]: https://github.com/orz0219/sparky/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/orz0219/sparky/releases/tag/v0.1.0
