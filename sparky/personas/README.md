# 人设资源

> ⛔ 本目录**只有数据，没有代码**。人设生成逻辑在 [`../personas.py`](../personas.py)。

## 为什么人设是数据而不是代码

人设是**可替换的资源**，不是程序逻辑。把它们做成 YAML 带来三件事：

1. 用户可以不改代码就换掉整个评审团
2. 社区可以贡献维度库与人设模板
3. 生成逻辑与内容解耦 —— 改提示词不会碰到数据，改数据不会碰到逻辑

## 文件

| 文件 | 内容 |
|---|---|
| [`presets/dimensions.yaml`](presets/dimensions.yaml) | 常见设计维度库（10 个），作为 S1 的**参考种子** |
| [`presets/example-panel.yaml`](presets/example-panel.yaml) | 一个固定评审团样例（5 人），配 `--personas-file` 使用 |

## 两条硬性规则

1. **维度库只是种子，不是白名单。** S1 的真正任务是**从具体问题里推出维度**
   （见 [ADR 0002](../../docs/adr/0002-persona-generation-over-fixed-cast.md)）。
   把这份库当成"只能从里面挑"会直接毁掉工具的核心价值。
2. **`personas` 里必须有一个 `role_type: adversary`。**
   缺了的话，[`personas.py`](../personas.py) 会自动补一个并告警 ——
   这是设计规则（"没有反方就不开会"），不是兜底逻辑。

## 关于 YAML

本项目**零第三方依赖**（[ADR 0001](../../docs/adr/0001-python-stdlib-single-entry.md)），
标准库没有 YAML 解析器。实现时有两个选择：

- 自实现一个覆盖本项目所需子集的极简 YAML 解析器（只支持映射、列表、标量）
- 改为 JSON 格式

两条路都可以，**在 M1 实现时决定并补一篇 ADR**。
当前这批文件用 YAML 写，是为了带注释 —— 人读的体验更好。
