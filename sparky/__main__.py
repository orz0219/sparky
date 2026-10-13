"""进程入口：``python3 -m sparky [想法]`` 的落点。

职责
    组装最外层调用链：解析参数 → 装载配置 → 起工作台 → （可选）立刻跑一次 →
    收口，并把失败翻译成明确的中文信息与退出码。

    **两种模式**：
      · 给了想法 —— 起网页后立刻发起一份提案并等它跑完（与从前一致）
      · 没给想法 —— 只起**提案工作台**：网页上能看历史提案、看进行中的提案、
        新建提案。进程一直服务到 Ctrl-C。

输入
    ``sys.argv``

输出
    进程退出码：
        0  正常结束（**包含未收敛的 force_stop**，那不是失败；
           也包含工作台模式下用户按 Ctrl-C 退出 —— 那是它正常的关闭方式）
        2  命令行参数错误
        3  配置错误（缺密钥等）
        4  会话持久化错误
        1  运行期错误（LLM 报错、校验不过等）

依赖
    ``cli`` · ``config`` · ``workspace`` · ``server`` · ``render``

失败模式
    任何 ``SparkyError`` 都不许被吞掉 —— 必须打印明确的中文原因后
    以对应退出码退出。
    **单次运行失败不会拖垮工作台**：错误写进 ``meta.json`` + transcript、
    打到终端、记进运行句柄，进程继续服务。
    键盘中断（Ctrl-C）：把仍在跑的提案标记为 interrupted，保留已落盘内容。
    未被 ``SparkyError`` 覆盖的异常视为 bug：打印完整回溯，退出码 1
    （不假装正常结束）。

设计要点
    网页服务**先于**会话启动。这与 M1 的顺序相反，是个改进：
    从前是先建会话再起服务，端口冲突时会留下一个空会话目录再把它标成失败；
    现在端口冲突根本不会建会话，用户拿到一条干净的配置错误。

对应设计
    docs/design.md §3.2 · docs/architecture.md §1 · §6 · docs/web-ui.md §8
"""

from __future__ import annotations

import sys
import time
import traceback
from collections.abc import Sequence

from . import cli, server as server_stage
from .config import load_runtime_config
from .errors import EXIT_OK, EXIT_RUNTIME, SparkyError
from .render import TerminalRenderer
from .workspace import Workspace

#: 工作台模式下的空转周期（秒）。
#:
#: 刻意用 ``time.sleep`` 循环而不是 ``threading.Event.wait()``：
#: 后者在部分平台上会一直阻塞到超时才响应 Ctrl-C，用户会觉得"按了没反应"。
IDLE_TICK_SECONDS = 0.5


def main(argv: Sequence[str] | None = None) -> int:
    """跑完一次头脑风暴会议，或开着提案工作台。返回进程退出码。"""
    renderer = TerminalRenderer()
    workspace: Workspace | None = None
    http_server = None
    #: 工作台模式下 Ctrl-C 是正常的关闭方式，不该算失败。
    interactive = False

    try:
        config = cli.parse_args(argv)
        runtime = load_runtime_config(config)
        renderer = TerminalRenderer(quiet=runtime.quiet)

        workspace = Workspace(runtime=runtime, renderer=renderer)

        url = f"http://127.0.0.1:{runtime.port}"
        if runtime.web:
            http_server, _thread = server_stage.start(
                workspace, host="127.0.0.1", port=runtime.port
            )
            renderer.note(f"本地网页：{url}")
        else:
            renderer.note("（--no-web：只在终端输出）")

        if runtime.raw_input:
            handle = workspace.start(runtime)
            if runtime.web and runtime.open_browser:
                _open_browser(renderer, f"{url}/#/p/{handle.session_id}")
            handle.thread.join()  # type: ignore[union-attr]
            return handle.exit_code

        # ── 提案工作台：没有想法，只开着一个网页等你在上面发起提案 ──
        interactive = True
        if runtime.web and runtime.open_browser:
            _open_browser(renderer, f"{url}/#/")
        renderer.note(
            f"提案工作台已就绪。提案清单来自 {workspace.out_root}"
            f"（可用 --out 换）。在网页上新建提案，或按 Ctrl-C 退出。"
        )
        while True:
            time.sleep(IDLE_TICK_SECONDS)

    except KeyboardInterrupt:
        _handle_interrupt(workspace, renderer)
        return EXIT_OK if interactive else EXIT_RUNTIME
    except SparkyError as exc:
        renderer.fail("")
        renderer.fail(exc.render())
        return exc.exit_code
    except Exception:  # noqa: BLE001 —— 未预期的异常是 bug，必须带回溯暴露
        renderer.fail("")
        renderer.fail("错误：遇到未预期的内部异常（这是 bug，不是你的用法问题）")
        traceback.print_exc()
        return EXIT_RUNTIME
    finally:
        # 无论成功、失败还是中断，都要把网页服务关掉，
        # 否则进程会因为非 daemon 的连接而挂着不退。
        server_stage.stop(http_server)


def _open_browser(renderer: TerminalRenderer, url: str) -> None:
    """打开浏览器。打不开不是致命错误，但**必须说出来**（不静默）。"""
    if not server_stage.open_browser(url):
        renderer.fail(f"⚠ 没能自动打开浏览器，请手动访问 {url}")


def _handle_interrupt(workspace: Workspace | None, renderer: TerminalRenderer) -> None:
    """Ctrl-C：保住已落盘的内容，并告诉用户怎么找回它。"""
    renderer.blank()
    if workspace is None:
        renderer.fail("已中断（工作台还没建起来，没有内容落盘）。")
        return
    try:
        interrupted = workspace.interrupt_all()
    except Exception as secondary:  # noqa: BLE001
        renderer.fail(
            f"已中断；但状态写入失败（{type(secondary).__name__}: {secondary}）。"
            f"{workspace.out_root} 下可能有状态不完整的会话。"
        )
        return
    if not interrupted:
        renderer.fail("已中断（当时没有正在跑的提案）。")
        return
    for session_id in interrupted:
        renderer.fail(f"已中断：{session_id}（已落盘内容保留在 {workspace.out_root}）")


if __name__ == "__main__":
    sys.exit(main())
