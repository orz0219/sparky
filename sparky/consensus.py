"""S4 收敛：判定讨论是否可以结束。

职责
    ``decide(ledger, round, budget, cfg) -> Decision``

输入
    分歧台账 · 已进行轮数 · 已消耗预算 · 配置

输出
    ``Decision``：``done`` / ``another_round(focus=[...])`` /
    ``force_stop(reason)``（reason ∈ budget_exhausted / max_rounds /
    stagnation / deadlock）

依赖
    **无。** 不许 import ``llm``，不许做任何 IO，不许用随机数。

失败模式
    无 —— 纯函数无 IO，因此没有失败模式。**这正是选它的理由之一**：
    终止条件必须是控制流，不能是创作。

    ★ 三条禁令（实现时不得违反）：
      1. 不得 import ``llm``。一旦调了模型，终止条件就不可复现了。
      2. 不得做任何 IO。
      3. 判断顺序必须是：done → budget → max_rounds → stagnation → deadlock
         → another_round。「done」必须排在预算/轮数之前 ——
         若刚好在最后一轮达成一致，不应被误判为未收敛。

    停滞检测的粒度（``(立场方向, closes, opens)`` 三元组）是**拍脑袋定的**，
    见 docs/design.md §11 未决问题 2，需要真实案例校准。

对应设计
    docs/design.md §4.5 · docs/consensus.md §4
"""

# 本文件当前只有模块契约，没有实现。
# 见 AGENTS.md「当前阶段」：在收到明确指令之前不要开始实现。
