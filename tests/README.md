# 测试

> M1 + M2 + M3 已有 **244 个单测**，全部通过。用标准库 `unittest`，不引入 pytest
> —— 零第三方依赖是硬约束（[ADR 0001](../docs/adr/0001-python-stdlib-single-entry.md)）。

```bash
python3 -m unittest discover -s tests -t .
```

## 结构

```
tests/
├── __init__.py
├── support.py           共用夹具：临时会话、Brief/Panel/Ledger/Turn 构造器、
│                        「全过」的文档样板、假调研结果
├── test_consensus.py  ★ S4 收敛：6 个判定分支 + 判断顺序 + 纯函数性
├── test_debate.py     ★ S3 主持人：台账状态机的每条规则
├── test_turn_prompts.py 发言提示词组装的**集成级**防漂移
├── test_server.py     ★ 网页服务：事件扇出、SSE 分帧与历史补发（**真实 HTTP**）、
│                        前端零外链与行数预算、端口冲突与重用
├── test_streaming.py     SayExtractor：分片边界切在转义序列中间
├── test_synthesis.py    9 项合规校验 + 节契约同步 + 摘要两级阈值
├── test_personas.py     人数裁剪、反方强制、维度覆盖、信息面隔离
├── test_config.py       密钥/提示词/材料装载的严格性
└── test_models.py       严格解析、弃权语义、会话持久化、并发安全
```

## 四条测试纪律

1. **测试里不得真实调用 LLM。** 全部是纯函数与纯 IO，或喂假客户端。
   真实调用会让测试变慢、变贵、且结果不稳定 —— 那不是测试，是抽样。

2. **每个"应该通过"的用例旁边都要有一个"应该失败"的用例。**
   本项目的检查大多是"拦截型"的（合规校验、防篡改比对、严格解析），
   只测通过侧等于没测 —— 一个永远返回"通过"的校验函数也能让测试全绿。

3. **每修一个 bug，补一个能复现它的测试。** 本项目已经踩过的坑（见下）
   全都隐蔽，没有回归测试就等于没修。

4. **判断顺序要单独测。** `decide()` 的六个分支各自正确还不够 ——
   顺序错了会在边界情况下误判（例如"最后一轮刚好谈拢"被判成未收敛）。
   `TestBranchOrder` 专门测这个。

## 已经踩过、已有回归测试的坑

| 坑 | 测试 |
|---|---|
| 分歧生成失控 → "真收敛"从设计上不可达 | `TestDisagreementCap` |
| 超上限的议题被悄悄丢掉 | `test_opens_beyond_cap_are_recorded_not_dropped` |
| 为 1 个字的摘要溢出否掉整场会话 | `test_slightly_over_target_is_accepted_not_retried` |
| "未决"的口径在 `describe()` 与终端渲染之间不一致 | `test_describe_counts_every_status` |
| 第 1 轮正常陈述立场的人被判成"弃权" | `TestAbstentionSemantics` |
| `redact()` 把 `prompt_tokens` 也擦成 `<已擦除>`，附录 D 变废 | `test_usage_counters_are_not_redacted` |
| 提示词占位符 `${name}` 被 `build_prompt(name=...)` 的参数名吃掉 | `test_placeholder_may_be_called_name` |
| 改模板不改 `SECTION_INPUTS`（或反之）造成漂移 | `TestSectionContract` |
| 发言提示词的块与代码调用对不上 | `TestPromptContract` |
| `_subsection` 把标题行算进正文，让检查永远为真 | `test_rejected_subsection_without_reason` |
| 分隔行 `\|---\|---\|` 被当成表格数据行 | `test_separator_rows_are_not_counted_as_data` |
| 并发 `emit` 撕裂 JSONL 行或跳号 | `test_concurrent_emit_keeps_transcript_intact` |
| 并发下 LLM 调用编号重复，快照互相覆盖 | `test_concurrent_llm_call_indices_are_unique` |
| `allow_reuse_address=False` 导致服务停掉后端口无法重用 | `test_same_port_can_be_reused_after_stop` |
| 流式分片切在转义序列中间（`\` + `n`、`\u12` + `34`） | `TestEscapeBoundaries` |
| 刷新页面重放上千条打字机增量 | `test_ephemeral_events_are_not_replayed` |
| 前端漏处理某个事件类型 → 界面静默停在某个状态 | `test_every_known_event_type_has_a_handler` |
| 前端偷偷引入外链 → 断网就废 | `test_no_external_resources` |
| 开发夹具与事件协议脱节 | `TestDemoFixture` |

## 还没测到的（M4 的功课）

- `--resume` 的续跑幂等性
- `transcript.jsonl` 末行截断时的 `replay()` 行为
- 真实多轮讨论下的"成本随轮数增长"是否符合预算模型
- 前端的**交互**（点击台账跳到对应发言、折叠轮次）—— 那需要真浏览器，
  目前只靠 `tools/demo_server.py` 目视确认
