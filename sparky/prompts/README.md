# 提示词设计草案

> 这些提示词是**真正在跑的**（`config.build_prompt` 从本目录读取），
> 不是纸面草案。但它们仍未经过多轮实测调优。

## 文件格式：标记块

每份 `.md` 里用 HTML 注释标出**机器读取的提示词块**，标记之外的文字
（用途说明、设计要点）是给人读的文档，加载器会忽略：

```markdown
<!-- PROMPT:system -->
你是一个需求分析师……
<!-- /PROMPT:system -->

<!-- PROMPT:user -->
用户的描述：${raw_input}
<!-- /PROMPT:user -->
```

一份文件可以有多个块（如 `persona.md` 有 `system` / `size_user` / `expand_user`）。

**缺标记即报错**，不设兜底 —— 一个缺了半段的提示词会让模型面对残缺指令
并自行脑补，产出的东西无法排查。

## 占位符语法是 `${名字}`，不是 `{名字}`

**这一点是踩过坑才定的**：提示词正文里含大量 JSON 花括号
（`{"goal": "...", "dimensions": [...]}`），用 Python 的 `str.format`
会被那些花括号撞死。所以改用 `${名字}` 加正则替换。

配套地，`render_prompt` 的前两个参数是**位置限定**的 ——
调用方用 `**values` 传任意占位符，任何固定的关键字参数名都可能与某个
占位符重名。早先 `build_prompt(name=...)` 就被人设提示词里的 `${name}`
静默吃掉过一次。

替换是**双向严格**的：模板要了没给会报错，给了模板没要也会报错
（后者说明提示词与代码已经脱节，这种漂移必须当场暴露）。
`tests/test_synthesis.py::TestSectionContract` 会在测试阶段核对
`synthesis.SECTION_INPUTS` 与模板里的实际占位符。

## 索引

| 文件 | 用于 | 对应设计 |
|---|---|---|
| [brief.md](brief.md) | S0 立案 | [../docs/design.md](../docs/design.md) §4.1 |
| [persona.md](persona.md) | S1 组阁 | [../docs/personas.md](../docs/personas.md) |
| [recon.md](recon.md) | S2 调研 | [../docs/design.md](../docs/design.md) §4.3 |
| [turn.md](turn.md) | S3 发言 | [../docs/consensus.md](../docs/consensus.md) §2 |
| [synthesis.md](synthesis.md) | S5 定稿 | [../docs/output-spec.md](../docs/output-spec.md) |

## 共同的写作纪律

所有提示词都遵守以下几条，实现时不要为了"提高成功率"而放松：

1. **要求结构化输出（JSON）**，并且**解析失败就报错**，不做模糊匹配修补
   （唯一的例外：整段被 ``` 围栏包住时剥离围栏，并发出 `json_fence` 告警
   让它可见）。
2. **禁止不可验证的措辞**：明确列出「很好 / 先进 / 健壮 / 尽力而为 /
   合理的 / 适当的」这类词为禁用词。
3. **禁止编造依据**。没有依据就写"未验证"，而不是编一个听起来可信的数字。
4. **要求明确表态**。禁止"两种方案各有优劣，建议根据实际情况选择"这类
   不表态的回答 —— 那是把决策推回给用户，正是本工具要消除的东西。
5. **反方（adversary）有专属提示词变体**，禁止其提出建设性替代方案。
6. **用户输入框定为「数据」而非「指令」**，并在模板里明写这一点，
   防止提示词注入。

## 各块由哪个阶段加载

| 提示词块 | 加载方 | 次数 |
|---|---|---|
| `brief#system` + `brief#user` | `brief.py` | 1 |
| `persona#system` + `persona#size_user` | `personas.py` | 1 |
| `persona#system` + `persona#expand_user` | `personas.py` | 1 |
| `recon#system` + `recon#user` | `recon.py` | N（每人一次，并发） |
| `recon#nosearch_note` | `recon.py` | 填入 `${search_context}` |
| `synthesis#system` + 六节块 | `synthesis.py` | 6 |
| `turn.md` 各块 | `debate.py` | M2 才接上 |
