"""命令行参数解析与校验。

职责
    把 ``sys.argv`` 解析成 ``Config``（纯数据，只有"从哪个环境变量读密钥"
    的名字，不含密钥真值）。

输入
    ``sys.argv``

输出
    ``Config``

依赖
    仅标准库 ``argparse`` / ``pathlib``。

失败模式
    未知参数 / 取值越界 / 互斥组合冲突 → argparse 打印用法说明，退出码 2。
    **不提供 ``--api-key``**：密钥进 shell history 与 ``ps`` 输出是安全风险，
    用户只能通过环境变量提供。

    M1 也**不提供** ``--web`` / ``--port`` / ``--resume``：
    这些功能尚未实现（见 docs/roadmap.md）。暴露一个只会报错的开关
    比不暴露更糟 —— 用户会以为是自己用错了。

对应设计
    docs/configuration.md §4
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    Config,
)

EPILOG = """\
示例：
  python3 -m sparky "我想做一个给播客自动生成章节和文字稿的小工具，本地跑，不要云服务"
  python3 -m sparky "一个本地优先的记账工具" --panel-size 3 --max-rounds 2
  python3 -m sparky "给团队做一个内部知识库" --context-dir ./notes --concurrency 6
  python3 -m sparky "一个命令行番茄钟" --budget-tokens 200000

讨论是怎么进行的：
  第 1 轮全员各自陈述立场，并主动指出与他人的冲突。
  之后每一轮都只召集**未决分歧的当事双方**（定向加轮），而不是让全员再发一轮。
  收敛判定由规则完成，不问模型「你们达成一致了吗」——
  详见 docs/consensus.md。

密钥：
  从环境变量读取（默认 DEEPSEEK_API_KEY），真值建议集中放在
  ~/.config/llm/keys.env。命令行刻意不提供 --api-key。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m sparky",
        description="Sparky —— 多智能体头脑风暴定稿机：给一句话，还你一份能开工的设计文档。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("idea", metavar="想法", help="一两句话描述你想做的东西")

    group_llm = parser.add_argument_group("模型与供应商")
    group_llm.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"从哪个环境变量读密钥（不是密钥本身）。默认 {DEFAULT_API_KEY_ENV}",
    )
    group_llm.add_argument(
        "--base-url",
        default=None,
        help=f"OpenAI 兼容端点根地址。默认 {DEFAULT_BASE_URL}",
    )
    group_llm.add_argument("--model", default=None, help=f"默认模型。默认 {DEFAULT_MODEL}")
    group_llm.add_argument(
        "--model-strong",
        default=None,
        help="组阁与定稿用的模型（这两步最吃能力）。默认与 --model 相同",
    )
    group_llm.add_argument(
        "--model-fast",
        default=None,
        help="例行发言与调研用的模型。默认与 --model 相同",
    )
    group_llm.add_argument(
        "--max-tokens",
        type=int,
        default=16000,
        help=("单次调用的输出预算（含推理 token），默认 16000。"
              "推理模型会先把预算花在思考上，给小了会得到空正文"),
    )
    group_llm.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="采样温度，默认 0.7。定稿各节固定用 0.3",
    )
    group_llm.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="单次请求超时秒数，默认 180",
    )

    group_panel = parser.add_argument_group("专家组")
    group_panel.add_argument(
        "--panel-size",
        type=int,
        default=None,
        help=(
            "指定目标人数（覆盖自动推断的公式），合法范围 3–7。"
            "实际人数以维度覆盖为准；若模型给的与这里不一致会告警"
        ),
    )
    group_panel.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="同阶段并发度，默认 4",
    )

    group_debate = parser.add_argument_group("讨论与收敛")
    group_debate.add_argument(
        "--max-rounds",
        type=int,
        default=4,
        help=(
            "讨论轮数硬上限，默认 4。第 1 轮是全员立场陈述，"
            "之后每轮只召集未决分歧的当事双方（定向加轮）"
        ),
    )
    group_debate.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="全会话 token 硬上限；不设则不按预算停止",
    )

    group_run = parser.add_argument_group("运行")
    group_run.add_argument(
        "--context-dir",
        type=Path,
        default=None,
        help="本地资料目录；其中的文本材料会按设计维度分配给不同的专家",
    )
    group_run.add_argument(
        "--out",
        type=Path,
        default=Path(".sparky") / "sessions",
        help="会话根目录，默认 ./.sparky/sessions",
    )
    group_run.add_argument("--quiet", action="store_true", help="只输出最终结果与错误")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> Config:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.panel_size is not None and not (3 <= args.panel_size <= 7):
        parser.error(f"--panel-size 必须在 3–7 之间，实际是 {args.panel_size}")
    if args.concurrency < 1:
        parser.error(f"--concurrency 必须 ≥ 1，实际是 {args.concurrency}")
    if args.max_rounds < 1:
        parser.error(
            f"--max-rounds 必须 ≥ 1，实际是 {args.max_rounds}。"
            f"第 1 轮是全员立场陈述，设 0 等于不讨论"
        )
    if args.budget_tokens is not None and args.budget_tokens < 1000:
        parser.error(f"--budget-tokens 必须 ≥ 1000，实际是 {args.budget_tokens}")
    if not args.idea.strip():
        parser.error("想法不能为空")

    return Config(
        raw_input=args.idea.strip(),
        api_key_env=args.api_key_env,
        base_url=args.base_url,
        model=args.model,
        model_strong=args.model_strong,
        model_fast=args.model_fast,
        panel_size=args.panel_size,
        max_rounds=args.max_rounds,
        budget_tokens=args.budget_tokens,
        concurrency=args.concurrency,
        context_dir=args.context_dir,
        out_root=args.out,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        quiet=args.quiet,
    )
