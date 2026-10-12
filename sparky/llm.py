"""LLM 调用封装（OpenAI 兼容 + SSE 流式）。

职责
    对 ``POST {base_url}/chat/completions`` 的唯一封装：组装请求、流式解析 SSE、
    统计用量、按 ``base_url`` 附加服务商专属参数、在调用**之前**落盘上下文快照。

输入
    messages + 模型参数 + 目标会话的调用编号

输出
    token 流（生成器）+ 最终 ``usage``

依赖
    仅标准库 ``urllib.request`` / ``json`` / ``ssl``。
    不 import 任何编排层模块（基础层不得反向依赖）。

失败模式
    网络错误 / 4xx / 5xx / 响应体非 JSON / SSE 分帧异常 → 记录完整请求快照后
    **向上抛出**，不重试超过配置次数，不返回空结果。

    硬性要求（来自实测踩坑，实现时不能省）：
      · 必须显式设置一个正常 User-Agent。部分网关会拦截 ``Python-urllib/x.y``
        这类"脚本味" UA，返回 403 + error code 1010。
      · 推理模型的响应含大段 reasoning token，``max_tokens`` 给小了会被截断，
        而截断的 JSON 解析必然失败 → 默认给足输出预算。
      · ``GET /models`` 能通不代表 ``/chat/completions`` 能通，连通性检查
        必须打真正的推理端点。
      · 落盘的请求快照必须**擦除 Authorization 头**。

对应设计
    docs/design.md §6.1 · docs/configuration.md §6 · docs/architecture.md §4
"""

# 本文件当前只有模块契约，没有实现。
# 见 AGENTS.md「当前阶段」：在收到明确指令之前不要开始实现。
