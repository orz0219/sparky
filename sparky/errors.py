"""异常层次与退出码映射。

职责
    定义全项目的异常基类与四类具体错误，并把每一类绑定到一个明确的进程退出码。
    这是「失败就说失败」原则的落地点 —— 每类失败都有确定的、可被脚本判断的出口。

输入
    ——

输出
    ``SparkyError`` 及其子类。每个实例带 ``exit_code``。

依赖
    无。这是最底层的模块，不许 import 任何其他 sparky 模块。

失败模式
    本模块自身无失败模式（只定义类型，不做任何 IO）。

设计要点
    ``__main__.py`` 捕获 ``SparkyError`` 后**打印中文原因并以 exit_code 退出**。
    未被 ``SparkyError`` 覆盖的异常一律视为 bug，打印完整 traceback 并用退出码 1。
    禁止 ``except Exception: pass`` —— 那会让失败变成一份看起来正常的文档。
"""

from __future__ import annotations

# 退出码（与 docs/architecture.md §1 的 __main__.py 一行保持一致）
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_PERSISTENCE = 4


class SparkyError(Exception):
    """所有可预期的失败。带进程退出码。"""

    exit_code: int = EXIT_RUNTIME

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # hint 用于给出"下一步怎么办"，例如提示该配哪个环境变量
        self.hint = hint

    def render(self) -> str:
        """渲染成给终端用户看的中文错误块。"""
        lines = [f"错误：{self.message}"]
        if self.hint:
            lines.append("")
            lines.extend(f"     {line}" for line in self.hint.splitlines())
        return "\n".join(lines)


class UsageError(SparkyError):
    """命令行参数错误。退出码 2。"""

    exit_code = EXIT_USAGE


class ConfigError(SparkyError):
    """配置或密钥缺失、非法。退出码 3。"""

    exit_code = EXIT_CONFIG


class PersistenceError(SparkyError):
    """会话目录不可写、日志损坏等。退出码 4。"""

    exit_code = EXIT_PERSISTENCE


class LlmError(SparkyError):
    """LLM 调用失败：网络、HTTP 错误码、响应体非法、输出被截断。退出码 1。"""

    exit_code = EXIT_RUNTIME


class SchemaError(SparkyError):
    """结构化输出缺字段、类型不符。

    独立成类是因为它的排查方式与 ``LlmError`` 不同：
    看到 ``SchemaError`` 要去看 ``llm-calls/`` 里的原始响应，
    而 ``LlmError`` 多数是网络或配额问题。
    """

    exit_code = EXIT_RUNTIME
