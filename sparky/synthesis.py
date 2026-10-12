"""S5 定稿：装配模板并填充内容，产出 design.md。

职责
    1. **先用代码装配骨架**（带占位符的固定模板）—— 结构不由 LLM 决定
    2. 分节填充内容（每节的 prompt 只带该节所需的状态切片）
    3. 跑合规校验（9 项，见 docs/output-spec.md §3）
    4. 校验全过才写盘

输入
    全程状态：Brief · Panel（含缺席者）· StanceMemo 集合 · 各轮 Turn · Ledger

输出
    ``design.md`` + ``doc.ready`` 事件

依赖
    ``llm`` · ``events`` · 会话目录

失败模式
    **任何一项合规校验不过 → 抛错，不写盘。** 不生成残缺文档。
    特别地这几项是**防篡改**检查，必须严格相等：
      · 取舍台账行数 == Ledger 中 tradeoff 条目数
      · 未决问题条数 == Ledger 中 stalemate 条目数
      · 附录 A 人数 == Panel 人数
    若不等，说明 LLM 漏抄或篡改了台账 → 报错。
    全文出现 ``TODO`` / ``待补充`` / ``...`` 占位 → 报错。

    零分歧的会话**不报错**，但在文档顶部插入「本次讨论未产生实质分歧」
    告警块（见 docs/consensus.md §6）。

对应设计
    docs/design.md §4.6 · docs/output-spec.md
"""

# 本文件当前只有模块契约，没有实现。
# 见 AGENTS.md「当前阶段」：在收到明确指令之前不要开始实现。
