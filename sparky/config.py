"""配置、密钥与提示词资源装载。

职责
    1. 按优先级合并配置（内置默认 → ``~/.config/llm/keys.env`` → 项目 ``.env``
       → 进程环境变量 → 命令行），读出密钥真值，并校验连接参数。
    2. 从 ``sparky/prompts/*.md`` 的 PROMPT 标记中取出提示词模板并做**严格**替换。

输入
    ``Config``（来自 cli.py）+ 环境变量 + 密钥文件 + 提示词文件

输出
    ``RuntimeConfig``（含密钥真值 —— 只存在于内存，**永不落盘、永不进日志**）
    以及渲染好的提示词字符串

依赖
    仅标准库 ``os`` / ``pathlib`` / ``re`` / ``urllib.parse``。

失败模式
    **缺密钥 → 抛 ``ConfigError``，退出码 3，不进入任何阶段。**
    不提示"将以离线模式继续"，也不回退到别的供应商 ——
    缺模型就没有产出，硬跑只会浪费时间并产出一份残缺文档。

    ``base_url`` 非法 → 抛 ``ConfigError``。
    密钥文件存在但格式错误（非 ``KEY=VALUE`` 行且非注释）→ 抛 ``ConfigError``，
    不跳过坏行（跳过会让"我明明配了"变成玄学问题）。
    提示词文件缺标记 / 模板占位符与传入值不匹配 → 抛 ``ConfigError``。
    这两条**不设兜底**：一个缺了半段的提示词会让模型面对残缺指令并自行脑补，
    产出的东西无法排查。

设计要点
    ``RuntimeConfig.__repr__`` / ``__str__`` 屏蔽密钥，
    避免它被意外打进日志、transcript 或异常回溯。

    提示词占位符用 ``${名字}`` 而**不是** ``{名字}``：提示词正文里含大量
    JSON 花括号，用 ``str.format`` 会被它们撞死。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigError

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_MODEL = "deepseek-flash"

#: 本机约定：密钥真值集中在这里，权限 600，永不进 git
USER_KEYS_ENV = Path.home() / ".config" / "llm" / "keys.env"

#: 项目内 .env 只允许放非敏感项（base_url / model）
PROJECT_ENV = Path(".env")

#: LLM 请求必须带的 User-Agent。部分网关（Cloudflare 前置）会拦截
#: ``Python-urllib/x.y`` 这类"脚本味" UA，返回 403 + error code 1010。
#: 这是来自实测的硬性要求，见 docs/configuration.md §6 已知坑 1。
USER_AGENT = "Sparky/0.1 (+https://github.com/orz0219/sparky)"

_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

#: 提示词资源目录。提示词是**数据**，与代码分开存放，便于单独迭代。
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: 提示词占位符。刻意不用 ``{name}``：提示词正文里含大量 JSON 花括号。
_PLACEHOLDER = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")


def mask_secret(secret: str) -> str:
    """把密钥变成可安全打印的形式：只留头尾各 4 位。"""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 env 文件。

    支持 ``export`` 前缀、``#`` 注释、单双引号包裹的值。

    格式错误的行 → 抛 ``ConfigError``（不跳过）。跳过坏行会让
    "我明明配了密钥"变成无法排查的玄学问题。
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            raise ConfigError(
                f"{path}:{lineno} 不是合法的 KEY=VALUE 行：{raw!r}",
                hint="密钥文件里只允许 KEY=VALUE、# 注释和空行。",
            )
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def build_env_layer(cwd: Path) -> dict[str, str]:
    """按优先级叠出环境层：keys.env → 项目 .env → 进程环境变量。

    后者覆盖前者（``dict.update`` 顺序即优先级顺序）。
    """
    env: dict[str, str] = {}
    env.update(parse_env_file(USER_KEYS_ENV))
    env.update(parse_env_file(cwd / PROJECT_ENV))
    env.update(os.environ)
    return env


# ───────────────────── 上下文材料装载 ─────────────────────

#: 只有这些扩展名会被当作文本读入。其余文件**列出但不读**（不是静默跳过：
#: 跳过名单会写进附录 B，用户能看到哪些材料没被用上）。
TEXT_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".sql", ".java", ".go", ".rs",
})


@dataclass
class ContextFile:
    """一份被读入的上下文材料。"""

    name: str
    text: str
    truncated: bool

    def render(self) -> str:
        if self.truncated:
            return f"### {self.name}（已截断）\n{self.text}"
        return f"### {self.name}\n{self.text}"


def load_context_dir(
    directory: Path, *, budget: int = 6000
) -> tuple[list[ContextFile], list[str]]:
    """读入 ``--context-dir`` 下的文本材料。

    返回 ``(读到的文件, 被跳过的文件名)``。跳过名单不隐藏 —— 它会被写进
    最终文档的附录 B，用户能看到哪些材料没被用上。

    字符预算是**硬**的：超出即截断，并在正文里留下明确的截断标记。
    一份悄悄被截掉一半的材料会让模型基于残缺信息下结论，而用户毫不知情。
    """
    if not directory.exists():
        raise ConfigError(
            f"--context-dir 指向的目录不存在：{directory}",
            hint="路径是相对当前工作目录解析的，检查一下是否写错。",
        )
    if not directory.is_dir():
        raise ConfigError(f"--context-dir 不是目录：{directory}")

    all_files = sorted(p for p in directory.rglob("*") if p.is_file())
    text_files = [p for p in all_files if p.suffix.lower() in TEXT_SUFFIXES]
    skipped = [str(p.relative_to(directory)) for p in all_files if p not in text_files]

    if not text_files:
        return [], skipped

    per_file = max(500, budget // len(text_files))
    loaded: list[ContextFile] = []
    for path in text_files:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(
                f"无法按 UTF-8 解码 {path}：{exc}",
                hint=(
                    "扩展名说明它是文本，但内容不是 UTF-8。"
                    "请转换编码，或把它移出 --context-dir。"
                ),
            ) from exc
        except OSError as exc:
            raise ConfigError(f"读取 {path} 失败：{exc}") from exc

        name = str(path.relative_to(directory))
        if len(content) > per_file:
            loaded.append(
                ContextFile(
                    name=name,
                    text=content[:per_file] + f"\n…（已截断，原文共 {len(content)} 字）",
                    truncated=True,
                )
            )
        else:
            loaded.append(ContextFile(name=name, text=content, truncated=False))
    return loaded, skipped


# ───────────────────── 提示词资源 ─────────────────────


def load_prompt(prompt_name: str, block: str) -> str:
    """从 ``prompts/<prompt_name>.md`` 里取出 ``<!-- PROMPT:<block> -->`` 之间的正文。

    标记之外的文字（说明、设计要点）被忽略 —— 那些是给人读的文档。
    """
    path = PROMPTS_DIR / f"{prompt_name}.md"
    if not path.exists():
        raise ConfigError(
            f"提示词文件不存在：{path}",
            hint="sparky/prompts/ 是随包分发的资源，缺失说明安装不完整。",
        )
    text = path.read_text(encoding="utf-8")
    start_marker = f"<!-- PROMPT:{block} -->"
    end_marker = f"<!-- /PROMPT:{block} -->"
    start = text.find(start_marker)
    if start == -1:
        raise ConfigError(f"{path} 缺少起始标记 {start_marker}")
    end = text.find(end_marker, start)
    if end == -1:
        raise ConfigError(f"{path} 缺少结束标记 {end_marker}")
    body = text[start + len(start_marker):end].strip()
    if not body:
        raise ConfigError(f"{path} 的 {block} 块是空的")
    return body


def render_prompt(template: str, origin: str, /, **values: str) -> str:
    """把模板里的 ``${名字}`` 替换成实参。

    前两个参数是**位置限定**的（``/`` 之后才是关键字）。
    这不是风格偏好：调用方用 ``**values`` 传任意占位符，
    任何固定的关键字参数名都可能与某个占位符重名并把它静默吃掉。
    位置限定从根上消灭这一类冲突 —— 早先 ``build_prompt(name=...)``
    就被人设提示词里的 ``${name}`` 撞过一次。

    两个方向都严格：
      · 模板要了但没给 → 报错（缺内容会让模型自行脑补）
      · 给了但模板没要 → 报错（说明提示词与代码已经脱节，这种漂移必须当场暴露）
    """
    wanted = set(_PLACEHOLDER.findall(template))
    provided = set(values)
    missing = wanted - provided
    if missing:
        raise ConfigError(
            f"{origin}：模板需要这些占位符但调用方没提供：{sorted(missing)}"
        )
    unused = provided - wanted
    if unused:
        raise ConfigError(
            f"{origin}：调用方提供了模板里不存在的占位符：{sorted(unused)}。"
            f"模板与代码已脱节，请同步两者。"
        )
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def build_prompt(prompt_name: str, block: str, /, **values: str) -> str:
    """``load_prompt`` + ``render_prompt`` 的组合，覆盖绝大多数用法。"""
    return render_prompt(
        load_prompt(prompt_name, block), f"prompts/{prompt_name}.md#{block}", **values
    )


@dataclass
class Config:
    """命令行层的原始配置。字段为 ``None`` 表示"用户没显式指定"。

    这个区分很重要：``--model`` 没给时才回落到环境变量，
    否则用户显式传的值会被环境变量悄悄盖掉。
    """

    raw_input: str
    api_key_env: str = DEFAULT_API_KEY_ENV
    base_url: str | None = None
    model: str | None = None
    model_strong: str | None = None
    model_fast: str | None = None
    panel_size: int | None = None
    max_rounds: int = 4
    budget_tokens: int | None = None
    concurrency: int = 4
    context_dir: Path | None = None
    out_root: Path = field(default_factory=lambda: Path(".sparky") / "sessions")
    timeout: float = 180.0
    max_tokens: int = 16000
    temperature: float = 0.7
    quiet: bool = False


@dataclass
class RuntimeConfig:
    """解析完成、可直接使用的配置。含密钥真值。"""

    raw_input: str
    api_key: str
    base_url: str
    model_strong: str
    model_fast: str
    api_key_env: str
    panel_size: int | None
    max_rounds: int
    budget_tokens: int | None
    concurrency: int
    context_dir: Path | None
    out_root: Path
    timeout: float
    max_tokens: int
    temperature: float
    quiet: bool

    # 密钥绝不出现在 repr / str 里 —— 它们会进日志、进 traceback
    def __repr__(self) -> str:
        return (
            f"RuntimeConfig(base_url={self.base_url!r}, "
            f"api_key={mask_secret(self.api_key)!r}, "
            f"model_strong={self.model_strong!r}, model_fast={self.model_fast!r}, "
            f"panel_size={self.panel_size!r}, max_rounds={self.max_rounds!r})"
        )

    __str__ = __repr__


def _resolve(cli_value: str | None, env: dict[str, str], env_key: str, fallback: str) -> str:
    """按「命令行 → 环境变量 → 内置默认」的顺序取一个字符串配置。"""
    if cli_value:
        return cli_value
    env_value = env.get(env_key)
    if env_value:
        return env_value
    return fallback


def load_runtime_config(config: Config, *, cwd: Path | None = None) -> RuntimeConfig:
    """把 ``Config`` 解析成 ``RuntimeConfig``，缺密钥即报错退出。

    这是整个程序里**唯一**读取密钥真值的地方。
    """
    cwd = cwd or Path.cwd()
    env = build_env_layer(cwd)

    api_key = env.get(config.api_key_env)
    if not api_key:
        raise ConfigError(
            f"未找到环境变量 {config.api_key_env}",
            hint=(
                f"请把密钥写入 {USER_KEYS_ENV}（权限 600），或在当前 shell 里导出：\n"
                f"     echo '{config.api_key_env}=sk-...' >> {USER_KEYS_ENV}\n"
                f"     export {config.api_key_env}=sk-...\n"
                f"注意：sparky 不提供 --api-key 参数，密钥进 shell history 是安全风险。"
            ),
        )

    base_url = _resolve(config.base_url, env, "SPARKY_BASE_URL", DEFAULT_BASE_URL)
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"base_url 不是合法的 HTTP(S) 地址：{base_url!r}",
            hint="形如 https://api.deepseek.com/v1（注意要带上 /v1 这类路径前缀）。",
        )

    model = _resolve(config.model, env, "SPARKY_MODEL", DEFAULT_MODEL)
    model_strong = _resolve(config.model_strong, env, "SPARKY_MODEL_STRONG", model)
    model_fast = _resolve(config.model_fast, env, "SPARKY_MODEL_FAST", model)

    if config.concurrency < 1:
        raise ConfigError(f"--concurrency 必须 ≥ 1，实际是 {config.concurrency}")
    if config.max_tokens < 256:
        raise ConfigError(
            f"--max-tokens 必须 ≥ 256，实际是 {config.max_tokens}",
            hint=(
                "推理模型的响应含大段 reasoning token（实测一次简单调用就有 96–145 个 "
                "reasoning token），预算给小了会被截断成半句话，而截断的 JSON 必然解析失败。"
                "见 docs/configuration.md §6 已知坑 6。"
            ),
        )

    return RuntimeConfig(
        raw_input=config.raw_input,
        api_key=api_key,
        base_url=base_url,
        model_strong=model_strong,
        model_fast=model_fast,
        api_key_env=config.api_key_env,
        panel_size=config.panel_size,
        max_rounds=config.max_rounds,
        budget_tokens=config.budget_tokens,
        concurrency=config.concurrency,
        context_dir=config.context_dir,
        out_root=config.out_root,
        timeout=config.timeout,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        quiet=config.quiet,
    )
