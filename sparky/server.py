"""本地 HTTP 服务 + SSE 推送 + 静态资源。

职责
    用标准库 ``http.server.ThreadingHTTPServer`` 提供：
      · ``GET /``                → ``web/index.html``
      · ``GET /app.js|/style.css`` → 静态资源
      · ``GET /events``          → SSE 事件流（**先补发历史，再进实时**）
      · ``POST /stop``           → 优雅停止（当前轮结束后进入定稿）

输入
    事件总线（订阅）+ 会话的 ``transcript.jsonl``（用于补发历史）

输出
    HTTP 响应。只监听 ``127.0.0.1``。

依赖
    仅标准库 ``http.server`` / ``socketserver`` / ``json`` / ``pathlib``。
    **不得 import 任何编排层模块之外的业务逻辑**（表现层不得 import ``llm``）。

失败模式
    端口占用 → **报错退出并提示换端口**，不静默换端口
    （静默换端口会让用户对着旧地址刷新，问题更难查）。
    SSE 客户端断开 → 注销该订阅者，不影响正在进行的讨论。
    静态资源缺失 → 500 并记录，不返回空页面。

    每 15 秒发送一次 ``: keepalive`` 注释行防连接超时；
    会话结束时发 ``session.end`` 并主动关闭连接。

对应设计
    docs/web-ui.md · docs/architecture.md §5
"""

# 本文件当前只有模块契约，没有实现。
# 见 AGENTS.md「当前阶段」：在收到明确指令之前不要开始实现。
