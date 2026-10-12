"""S0 立案：把用户的口语描述变成结构化 ``Brief``。

职责
    1. 抽取四元组：目标 / 约束 / 非目标 / 未知项 —— **缺的部分标 unknown，不许编**
    2. 抽取设计维度（dimension）：做这个设计需要考虑哪些方面
    3. 标出"用户没说但显然重要"的维度，来源记为 ``inferred``

输入
    用户的原始一句话 + 可选的 ``--context-dir`` 摘要

输出
    ``Brief``：goal / constraints / non_goals / unknowns / dimensions / uncertainty

依赖
    ``llm``（单次调用）· ``events``

失败模式
    LLM 输出非 JSON → 带**原始输出**报错，不猜、不修补。
    缺必填字段 → 校验失败即报错，**不填充默认值**。
    输入过短（如"做一个 app"）→ **不追问**，把一切标 unknown，
    并把"确定产品形态"本身列为第一个未知项 —— 让专家组去收敛它。

对应设计
    docs/design.md §4.1 · docs/architecture.md §1
"""

# 本文件当前只有模块契约，没有实现。
# 见 AGENTS.md「当前阶段」：在收到明确指令之前不要开始实现。
