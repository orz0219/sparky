"""会话状态机与事件溯源持久化。

职责
    管理一次会话的完整生命周期：
      · 跑流水线**之前**就建好会话目录并写 ``meta.json``
      · 把每个事件以 JSONL 追加写入 ``transcript.jsonl``（**先落盘，再广播**）
      · 维护内存态（Brief / Panel / StanceMemo / 各轮 Turn / Ledger）
      · ``replay()``：从 JSONL 重建完整状态，支撑续跑、审计与前端刷新补发

输入
    事件 + 会话根目录

输出
    磁盘：``meta.json`` · ``transcript.jsonl`` · ``ledger.json`` 等
    内存：``Session`` 状态对象

依赖
    仅标准库 ``json`` / ``pathlib`` / ``datetime`` / ``os``。

失败模式
    目录不可写 → 立即报错退出，退出码 4。
    ``transcript.jsonl`` 末行截断（进程被杀）→ 报错并提示 ``--resume`` 不可用，
    **不静默跳过坏行**（那会让状态与日志不一致）。
    ``replay()`` 发现事件序不合法（如 turn.end 先于 turn.start）→ 报错。

设计要点
    ``transcript.jsonl`` 是**权威**，``ledger.json`` 只是给人看的快照；
    两者不一致时以 transcript 为准，replay 会重建并覆盖 ledger。

对应设计
    docs/architecture.md §4 · docs/design.md §6.3
"""

# 本文件当前只有模块契约，没有实现。
# 见 AGENTS.md「当前阶段」：在收到明确指令之前不要开始实现。
