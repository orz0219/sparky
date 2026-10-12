# 测试

> M1 已有 **87 个单测**，全部通过。用标准库 `unittest`，不引入 pytest
> —— 零第三方依赖是硬约束（[ADR 0001](../docs/adr/0001-python-stdlib-single-entry.md)）。

```bash
python3 -m unittest discover -s tests -t .
```

## 结构

```
tests/
├── __init__.py
├── support.py          共用夹具：临时会话、Brief/Panel 构造器、一份「全过」的文档样板
├── test_synthesis.py   ★ 9 项合规校验 + 节契约同步
├── test_personas.py      人数裁剪、反方强制、维度覆盖、信息面隔离
├── test_config.py        密钥/价格/提示词装载的严格性
└── test_models.py        严格解析、会话持久化、并发安全
```

## 三条测试纪律

1. **测试里不得真实调用 LLM。** 目前的测试全部是纯函数与纯 IO，
   没有一处需要网络。要引入带 LLM 的测试时必须用假客户端。
   真实调用会让测试变慢、变贵、且结果不稳定 —— 那不是测试，是抽样。

2. **每个"应该通过"的用例旁边都要有一个"应该失败"的用例。**
   本项目的检查大多是"拦截型"的（合规校验、防篡改比对、严格解析），
   只测通过侧等于没测 —— 一个永远返回"通过"的校验函数也能让测试全绿。
   写 `test_synthesis.py` 时这条尤其重要。

3. **每修一个 bug，补一个能复现它的测试。** 本项目已经踩过的坑
   （见下）全都隐蔽，没有回归测试就等于没修。

## 已经踩过、已有回归测试的坑

| 坑 | 测试 |
|---|---|
| `redact()` 把 `prompt_tokens` 也擦成 `<已擦除>`，附录 D 变废 | `test_usage_counters_are_not_redacted` |
| 提示词占位符 `${name}` 被 `build_prompt(name=...)` 的参数名吃掉 | `test_placeholder_may_be_called_name` |
| 改模板不改 `SECTION_INPUTS`（或反之）造成漂移 | `TestSectionContract` |
| `_subsection` 把标题行算进正文，让"是否含否决理由"的检查永远为真 | `test_rejected_subsection_without_reason` |
| 分隔行 `\|---\|---\|` 被当成表格数据行 | `test_separator_rows_are_not_counted_as_data` |
| 并发 `emit` 撕裂 JSONL 行或跳号 | `test_concurrent_emit_keeps_transcript_intact` |
| 并发下 LLM 调用编号重复，快照互相覆盖 | `test_concurrent_llm_call_indices_are_unique` |

## 还没测到的（M2 的功课）

- `consensus.decide()` 的 6 个判定分支 —— 它还没实现，但它是最该测的纯函数
- 轮内并发后台账的一致性
- S2 的信息面隔离断言（现在靠 `_build_recon_user` 里的检查，缺单测）
- `--resume` 的续跑幂等性
