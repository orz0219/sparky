# 人设资源

> ⛔ 本目录**只有数据，没有代码**。人设生成逻辑在 [`../personas.py`](../personas.py)。
>
> **且这些数据当前没有被任何代码读取** —— 原因见下面「关于 YAML」。

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

## 关于 YAML：M1 决定先不读这些文件

本项目**零第三方依赖**（[ADR 0001](../../docs/adr/0001-python-stdlib-single-entry.md)），
标准库没有 YAML 解析器。三条路：

| 方案 | 代价 |
|---|---|
| 自实现覆盖本子集的极简 YAML 解析器 | 约 100 行 + 它的边角 bug |
| 改成 JSON | 失去注释，而这两个文件的注释承载了"为什么是这个维度" |
| **暂不读取（M1 的选择）** | `--dimensions-file` / `--personas-file` 两个开关不提供 |

M1 选了第三条。理由：**这两个文件的去留不影响主流程** ——
S1 的维度是从 `Brief` 现场推出来的（[ADR 0002](../../docs/adr/0002-persona-generation-over-fixed-cast.md)），
不必依赖这份种子库。与其为了"让文件被读到"引入一个解析器，
不如先把六个阶段跑通。

这三个方案的取舍会在 M4 落地时定下来，并按仓库约定补一篇 ADR。

**因此 `--dimensions-file` 与 `--personas-file` 目前不在命令行里** ——
不暴露一个只会报错的开关，见 [../../docs/configuration.md](../../docs/configuration.md) §4.0。

## 两条硬性规则
