# Sparky 文档索引

**从这里开始：[design.md](design.md) —— 总体设计说明书。**

---

## 按阅读目的找

| 你的目的 | 读这篇 | 大概要多久 |
|---|---|---|
| 搞懂这个工具想干什么、怎么设计的 | [design.md](design.md) | 15 分钟 |
| 准备动手写代码 | [architecture.md](architecture.md) | 10 分钟 |
| 想改人设分配逻辑 | [personas.md](personas.md) | 8 分钟 |
| 想改讨论 / 收敛逻辑 | [consensus.md](consensus.md) | 10 分钟 |
| 想改产出文档结构 | [output-spec.md](output-spec.md) | 8 分钟 |
| 想改前端 | [web-ui.md](web-ui.md) | 6 分钟 |
| 要接新的模型供应商 | [configuration.md](configuration.md) | 5 分钟 |
| 想知道什么时候能用 | [roadmap.md](roadmap.md) | 3 分钟 |
| 想知道"为什么这么设计" | [adr/](adr/) | 按需 |
| 有具体疑问 | [faq.md](faq.md) | 按需 |

---

## 文档地图

```
docs/
├── README.md          ← 你在这里
├── design.md          ★ 主文档：问题定义、目标、架构、关键设计、数据模型、取舍台账、未决问题
├── architecture.md      模块清单（含失败模式）、依赖禁令、数据流、事件协议、时序、并发模型
├── personas.md          两段式人设生成、人数公式、反方强制、信息面分配
├── consensus.md       ★ 分歧台账、收敛纯函数、定向加轮、和谐告警、死锁处理
├── output-spec.md       最终文档的章节模板、合规校验表、标记约定、反例
├── web-ui.md            SSE 协议、页面布局、交互、零构建约束
├── configuration.md     配置优先级、密钥约定、供应商地址表、8 条已知坑
├── roadmap.md           M0–M4 里程碑与可检验的验收标准
├── faq.md               常见问题
└── adr/                 架构决策记录
    ├── 0001-python-stdlib-single-entry.md
    ├── 0002-persona-generation-over-fixed-cast.md
    ├── 0003-consensus-as-explicit-ledger.md
    └── 0004-information-scoping.md
```

---

## 三篇最该先读的

如果只读三篇：

1. **[design.md](design.md)** —— 五分钟内知道这东西是不是你要的
2. **[consensus.md](consensus.md)** —— 本项目的灵魂：「什么时候算讨论完了」
3. **[output-spec.md](output-spec.md)** —— 最终交付物长什么样

---

## 文档约定

- 全部中文。代码标识符、命令、API 名保持原文。
- **失败模式不许留空**：每个模块、每个设计点都要写清"它怎么坏"。
- 疑问不回避：想不清楚的写进 [design.md](design.md) §11 未决问题，
  而不是用模糊措辞糊过去。
- 涉及取舍就写成台账：选了什么、放弃了什么、**什么条件下该反过来选**。
