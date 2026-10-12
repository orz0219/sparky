"""S1 组阁：决定开几个人、都是谁、各自知道什么。

职责
    1. 按 ``panel_size = clamp(3, 7, ceil(维度数 / 1.5))`` 定人数，再按
       不确定性 / 安全约束修正
    2. 为每个维度生成一个有名字、有立场、有偏见、有盲点的``Persona``
    3. 为每个 ``Persona`` 分配 ``InfoScope``（追问清单 / 检索词 / 文件 / 预算）
    4. **校验并强制反方**：无人 ``role_type == "adversary"`` 时自动补位

输入
    ``Brief`` + 可选 ``--dimensions-file`` / ``--personas-file``

输出
    ``Panel``（3–7 个 ``Persona``）

依赖
    ``llm`` · ``events`` · 仅标准库 ``json``（若读 YAML 模板则需自实现极简解析
    或改为 JSON —— 零第三方依赖是硬约束，见 ADR 0001）

失败模式
    LLM 输出人数越界 → 裁剪到边界并发出 ``warning``，不静默接受。
    维度覆盖有空洞（某维度无 owner）→ 报错。
    ``blind_spot`` 或 ``bias`` 缺失 → 校验失败即报错（它们是张力的来源，
    缺了讨论会退化成轮流念稿）。

设计要点
    ``blind_spot`` **不进本人的 prompt**，只进**其他人**的 prompt
    作为"你可以从这里攻击他"的提示 —— 这是让讨论真的打起来的关键。

对应设计
    docs/design.md §4.2 · docs/personas.md
"""

# 本文件当前只有模块契约，没有实现。
# 见 AGENTS.md「当前阶段」：在收到明确指令之前不要开始实现。
