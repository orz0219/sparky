"""LLM 调用封装（OpenAI 兼容 ``/chat/completions``）。

职责
    对推理端点的唯一封装：组装请求、**调用前落盘上下文快照**、发送、
    校验响应、统计用量。所有阶段都通过它访问模型。

输入
    system / user 文本 + 目标模型 + 用途标签

输出
    ``LlmReply``（正文 + 用量 + finish_reason）

依赖
    仅标准库 ``urllib.request`` / ``urllib.error`` / ``json`` / ``time``，
    以及本包的 ``config`` / ``errors`` / ``models`` / ``session``。

失败模式
    网络错误 / 5xx / 429 → 重试至 ``MAX_RETRIES`` 次后抛 ``LlmError``。
    4xx（除 429）→ **立即**抛 ``LlmError``，不重试（密钥错、参数错重试无意义）。
    HTTP 200 但响应体非 JSON → 抛 ``LlmError``，带响应正文片段。
    ``finish_reason == "length"`` → 抛 ``LlmError``（输出被截断，JSON 必然解析失败）。
    正文为空 → 抛 ``LlmError``，并提示推理 token 吃光了预算。

    所有失败都**不返回空结果、不降级**：一次失败的调用必须让上层看见。

设计要点（三条来自实测，见 docs/configuration.md §6）
    1. 必须显式设 User-Agent —— 部分网关拦截 ``Python-urllib/x.y``（403/1010）。
    2. 推理模型的 reasoning token 会吃光输出预算，故 ``--max-tokens`` 有下限校验。
    3. ``GET /models`` 通不代表本端点通，所以连通性只由真实调用证明。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from .config import USER_AGENT, RuntimeConfig
from .errors import LlmError
from .models import Usage
from .session import Session

#: 可重试的 HTTP 状态码。429 是限流，其余是网关/上游抖动。
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

MAX_RETRIES = 2

#: 整段被 ``` 包裹时的剥离。这是**有界的格式归一化**，不是修补：
#: 提示词已明确要求不要围栏，但部分模型仍会加；剥离后会发 warning 事件，
#: 让这件事可见，而不是静默消化。
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)

#: JSON 解析失败时的重试次数。
#:
#: **这不是兜底逻辑**，理由有三：
#:   1. 它不掩盖任何缺失的前置条件 —— 重试用尽后照样硬报错，原始输出落盘；
#:   2. 它不"修补"内容 —— 绝不改写模型输出的任何字符（改写会静默篡改内容，
#:      比如把中文字符串里的全角逗号也一起换掉）；
#:   3. 它是可见的 —— 每次重试都发 ``json_parse_retry`` 告警事件。
#:
#: 触发原因是实测到的真实故障模式：中文模型会在 JSON **结构位置**写出全角
#: 逗号（``"甲"，"乙"``）。``response_format: json_object`` 并不做约束解码，
#: 拦不住这个。一次会话有 10+ 次调用，不重试的话单个标点就能废掉整场。
JSON_ATTEMPTS = 2

#: 重试时附加到 system 末尾的纠正提示。
JSON_CORRECTIVE_SUFFIX = """

## 上一次尝试失败的原因

你上一次的输出不是合法的 JSON —— 在 JSON 的**结构位置**使用了全角标点。

请严格区分：

- **结构符号必须是半角**：`,` 冒号 `:` 双引号 `"` 方括号 `[` `]` 花括号 `{` `}`
- **只有字符串内容里**才可以用中文标点，例如 `"why": "用户要求本地跑，不能用云 API。"`

尤其注意数组元素之间与对象键值之间：写成 `"甲"，"乙"` 是错的，
必须写成 `"甲", "乙"`。
"""


@dataclass
class LlmReply:
    """一次成功调用的结果。"""

    content: str
    usage: Usage
    finish_reason: str
    model: str
    call_index: int


class LlmClient:
    """推理端点的客户端。一个会话共用一个实例。

    **线程安全**：S2/S3 阶段会并发调用同一个实例。共享状态只有
    ``self._runtime`` 与 ``self._session``，而 ``Session`` 内部有锁。
    """

    def __init__(self, runtime: RuntimeConfig, session: Session) -> None:
        self._runtime = runtime
        self._session = session
        self._endpoint = runtime.base_url.rstrip("/") + "/chat/completions"
        self._host = urlparse(self._endpoint).netloc.lower()
        #: OpenCode 系要求每个请求带一个会话 id，否则直接 400 MissingSessionID。
        #:
        #: **必须是 ASCII。** 这里曾经直接用 Sparky 的 session_id，
        #: 而它含中文 slug —— HTTP 头按 latin-1 编码，一发出就
        #: ``UnicodeEncodeError: 'latin-1' codec can't encode characters``。
        #: 改成 session_id 的哈希：同样是"这次会话"的稳定标识，但保证是 ASCII，
        #: 而且出问题时还能反查回原始 session_id。
        self._session_header_value = "sparky-" + hashlib.sha256(
            session.session_id.encode("utf-8")
        ).hexdigest()[:16]

    def _provider_headers(self) -> dict[str, str]:
        """服务商专属的**额外请求头**。必须按 ``base_url`` 判定。

        与 ``thinking`` 同一条规则（见 docs/configuration.md §6 已知坑 5）：
        发错服务商不是被忽略，是会报错。

        OpenCode Zen / Go 缺 ``x-opencode-session`` 会直接
        ``400 MissingSessionID``（实测 2026-10-12）。这个头不是可选的"优化"，
        是**能不能连通**的前提。
        """
        if "opencode.ai" in self._host:
            return {"x-opencode-session": self._session_header_value}
        return {}

    def _supports_thinking_switch(self) -> bool:
        """``thinking`` 是 DeepSeek 系专属参数，发给别家会 400。

        服务商专属参数必须按 ``base_url`` 判定 —— 见 docs/configuration.md §6 已知坑 5。
        不发送它是**预期行为**，不是降级，所以不发告警。
        """
        return "deepseek" in self._host

    # ───────────────────── 对外接口 ─────────────────────

    def complete(
        self,
        *,
        system: str,
        user: str,
        purpose: str,
        model: str | None = None,
        json_mode: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: bool = True,
    ) -> LlmReply:
        """发起一次**非流式**调用，返回完整正文。

        用于不需要实时呈现的调用（立案、组阁、定稿各节）。

        ``thinking=False`` 会请求关闭推理（仅 DeepSeek 系生效）。
        2026-10-12 实测：一次简单问答的 completion token 从 188 降到 22，
        而且不再有"预算全烧在 reasoning 上、正文为空"的风险。
        """
        payload = self._build_payload(
            system=system,
            user=user,
            model=model,
            json_mode=json_mode,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
        )
        body, call_index = self._post_with_retries(payload, purpose=purpose)
        return self._parse(body, call_index=call_index, purpose=purpose)

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        purpose: str,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: bool = True,
    ) -> tuple[dict[str, Any], LlmReply]:
        """调用并解析 JSON（非流式）。重试与解析规则见 ``_complete_and_parse``。"""

        def caller(current_system: str, attempt: int) -> LlmReply:
            return self.complete(
                system=current_system,
                user=user,
                purpose=purpose if attempt == 1 else f"{purpose}（JSON 重试 {attempt}）",
                model=model,
                json_mode=True,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking=thinking,
            )

        return self._complete_and_parse(caller=caller, system=system, purpose=purpose)

    def stream_json(
        self,
        *,
        system: str,
        user: str,
        purpose: str,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: bool = True,
        on_delta: Callable[[str, bool], None] | None = None,
    ) -> tuple[dict[str, Any], LlmReply]:
        """流式调用并解析 JSON，用于需要**实时呈现**的发言。

        ``on_delta(文本, 是否重置)`` 会在收到可展示的新增文本时被调用。
        文本是从 ``say`` 字段里增量抽出来的**人话**，不是裸 JSON ——
        见 ``SayExtractor`` 的说明。

        重试时（网络抖动或 JSON 解析失败）会先以 ``reset=True`` 回调一次，
        让界面清空重来 —— 否则用户会看到两段文字接在一起。
        """

        def caller(current_system: str, attempt: int) -> LlmReply:
            if attempt > 1 and on_delta is not None:
                on_delta("", True)
            payload = self._build_payload(
                system=current_system,
                user=user,
                model=model,
                json_mode=True,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking=thinking,
            )
            payload["stream"] = True
            return self._stream_reply(
                payload,
                purpose=purpose if attempt == 1 else f"{purpose}（JSON 重试 {attempt}）",
                on_delta=on_delta,
            )

        return self._complete_and_parse(caller=caller, system=system, purpose=purpose)

    # ───────────────────── 共用：重试 + 解析 ─────────────────────

    def _complete_and_parse(
        self,
        *,
        caller: Callable[[str, int], LlmReply],
        system: str,
        purpose: str,
    ) -> tuple[dict[str, Any], LlmReply]:
        """调用 + 剥离围栏 + 解析 JSON，解析失败时有界重试。

        重试时在 system 末尾附加纠正提示（明说"结构符号必须半角"），
        并把上一次的原始输出留在 ``llm-calls/`` 里。第二次仍失败 → 抛 ``LlmError``。

        **绝不做的是改写模型输出**：把全角逗号全局替换成半角会连字符串内容
        一起改掉，那是静默篡改。
        """
        current_system = system
        last: tuple[json.JSONDecodeError, LlmReply] | None = None

        for attempt in range(1, JSON_ATTEMPTS + 1):
            reply = caller(current_system, attempt)
            text = self._strip_fence(reply, purpose)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                last = (exc, reply)
                if attempt < JSON_ATTEMPTS:
                    self._session.warn(
                        "json_parse_retry",
                        f"{purpose}：第 {attempt} 次输出不是合法 JSON"
                        f"（{exc.msg}，位置 {exc.pos}），已附加纠正提示后重试。"
                        f"上一次的原始输出见 llm-calls/{reply.call_index:04d}-response.txt",
                    )
                    current_system = system + JSON_CORRECTIVE_SUFFIX
                    continue
                break

            if not isinstance(parsed, dict):
                raise LlmError(
                    f"{purpose}：期望顶层是 JSON 对象，实际是 {type(parsed).__name__}",
                    hint=f"原始输出见 llm-calls/{reply.call_index:04d}-response.txt",
                )
            return parsed, reply

        assert last is not None  # 循环只在解析失败时走到这里
        exc, reply = last
        raise LlmError(
            f"{purpose}：连续 {JSON_ATTEMPTS} 次输出都不是合法 JSON"
            f"（{exc.msg}，位置 {exc.pos}）",
            hint=(
                f"两次的原始输出都在 {self._session.root}/llm-calls/ 下"
                f"（最后一次是 {reply.call_index:04d}-response.txt），请先看那里。\n"
                f"不猜、不修补 —— 本工具不会去改写模型输出的字符。"
            ),
        ) from exc

    def _strip_fence(self, reply: LlmReply, purpose: str) -> str:
        """剥掉整段包裹的 markdown 围栏（有界的格式归一化，不是修补）。"""
        fence = _FENCE.match(reply.content)
        if not fence:
            return reply.content
        self._session.warn(
            "json_fence",
            f"模型把 JSON 包在代码围栏里（{purpose}），已剥离。"
            f"这通常意味着提示词的约束被忽略了，值得复查。",
        )
        return fence.group(1)

    # ───────────────────── 请求构造 ─────────────────────

    def _build_payload(
        self,
        *,
        system: str,
        user: str,
        model: str | None,
        json_mode: bool,
        max_tokens: int | None,
        temperature: float | None,
        thinking: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self._runtime.model_strong,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens or self._runtime.max_tokens,
            "temperature": self._runtime.temperature if temperature is None else temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if not thinking and self._supports_thinking_switch():
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        headers = {
            "Content-Type": "application/json",
            # 必须显式设置：默认的 Python-urllib UA 会被部分网关拒绝
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self._runtime.api_key}",
            "Accept": "application/json",
            **self._provider_headers(),
        }
        # 前置检查而不是兜底：HTTP 头按 latin-1 编码，非 ASCII 会在发出去的
        # 那一刻抛 UnicodeEncodeError，而那个回溯完全看不出是哪来的头。
        # 实测踩过一次（session_id 里带中文 slug），所以在这里挡住。
        for name, value in headers.items():
            try:
                value.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise LlmError(
                    f"请求头 {name} 含非 ASCII 字符，HTTP 头无法承载：{value!r}",
                    hint="头值必须是 ASCII；需要唯一标识就用哈希或 UUID。",
                ) from exc
        return urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _translate_http_error(self, exc: urllib.error.HTTPError, call_index: int) -> None:
        """把 HTTP 错误翻译成"值得重试"或"直接报错"。"""
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in _TRANSIENT_STATUS:
            raise _TransientFailure(f"HTTP {exc.code}：{detail}") from exc
        raise LlmError(
            f"推理端点返回 HTTP {exc.code}",
            hint=(
                f"{detail}\n"
                f"4xx 不重试：这通常是密钥、模型名或参数的问题。\n"
                f"请求快照：{self._session.root}/llm-calls/{call_index:04d}-request.json"
            ),
        ) from exc

    # ───────────────────── 非流式 ─────────────────────

    def _post_with_retries(self, payload: dict[str, Any], *, purpose: str) -> tuple[str, int]:
        """带重试的 POST，返回 ``(响应正文, 调用编号)``。

        每次尝试之前都重新保存请求快照；编号一路带回给调用方，
        因为并发下回头读 ``session.call_count`` 会拿到别的线程的编号。
        """
        for attempt in range(MAX_RETRIES + 1):
            call_index = self._session.begin_llm_call(
                payload, purpose=f"{purpose}（第 {attempt + 1} 次尝试）" if attempt else purpose
            )
            try:
                return self._post_once(payload, call_index=call_index), call_index
            except _TransientFailure as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise LlmError(
                    f"{purpose}：连续 {MAX_RETRIES + 1} 次调用均失败 —— {exc}",
                    hint=(
                        f"请求快照在 {self._session.root}/llm-calls/。\n"
                        f"检查网络连通性与端点 {self._endpoint} 是否可达。"
                    ),
                ) from exc
        raise LlmError(f"{purpose}：不可达分支")

    def _post_once(self, payload: dict[str, Any], *, call_index: int) -> str:
        try:
            with urllib.request.urlopen(
                self._request(payload), timeout=self._runtime.timeout
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self._translate_http_error(exc, call_index)
        except urllib.error.URLError as exc:
            raise _TransientFailure(f"网络错误：{exc.reason}") from exc
        except TimeoutError as exc:
            raise _TransientFailure(f"请求超时（{self._runtime.timeout:g}s）") from exc

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LlmError(
                f"推理端点返回的不是 JSON（{exc.msg}）",
                hint=f"响应正文前 400 字：{raw[:400]}",
            ) from exc
        if isinstance(body, dict) and "error" in body:
            raise LlmError(
                f"推理端点返回错误：{json.dumps(body['error'], ensure_ascii=False)[:400]}"
            )
        return raw

    def _parse(self, raw: str, *, call_index: int, purpose: str) -> LlmReply:
        """从非流式响应里取出正文与用量，然后交给共用的收尾逻辑。"""
        try:
            body = json.loads(raw)
            choice = body["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            usage_raw = body.get("usage") or {}
            model = body.get("model", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(
                f"{purpose}：响应结构不符合 OpenAI 兼容格式（缺 {exc}）",
                hint=f"完整响应：{raw[:600]}",
            ) from exc
        return self._finalize(
            content=content,
            finish_reason=finish_reason,
            usage_raw=usage_raw,
            model=model,
            call_index=call_index,
            purpose=purpose,
        )

    # ───────────────────── 流式 ─────────────────────

    def _stream_reply(
        self,
        payload: dict[str, Any],
        *,
        purpose: str,
        on_delta: Callable[[str, bool], None] | None,
    ) -> LlmReply:
        """流式 POST + 收尾。重试语义与 ``_post_with_retries`` 一致。"""
        for attempt in range(MAX_RETRIES + 1):
            call_index = self._session.begin_llm_call(
                payload, purpose=f"{purpose}（第 {attempt + 1} 次尝试）" if attempt else purpose
            )
            try:
                return self._stream_once(
                    payload, call_index=call_index, on_delta=on_delta, purpose=purpose
                )
            except _TransientFailure as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise LlmError(
                    f"{purpose}：连续 {MAX_RETRIES + 1} 次流式调用均失败 —— {exc}",
                    hint=f"请求快照在 {self._session.root}/llm-calls/。",
                ) from exc
        raise LlmError(f"{purpose}：不可达分支")

    def _stream_once(
        self,
        payload: dict[str, Any],
        *,
        call_index: int,
        on_delta: Callable[[str, bool], None] | None,
        purpose: str,
    ) -> LlmReply:
        """读一次 SSE 流，边收边抽 ``say``，最后交给共用的收尾逻辑。"""
        extractor = SayExtractor()
        parts: list[str] = []
        finish_reason: str | None = None
        usage_raw: dict[str, Any] = {}

        try:
            with urllib.request.urlopen(
                self._request(payload), timeout=self._runtime.timeout
            ) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue  # 空行与 SSE 注释（keepalive）
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise LlmError(
                            f"流式分片不是合法 JSON（{exc.msg}）",
                            hint=f"分片内容前 300 字：{body[:300]}",
                        ) from exc
                    if chunk.get("usage"):
                        # 实测 DeepSeek 在流式下也会回 usage，不需要额外参数
                        usage_raw = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        piece = (choice.get("delta") or {}).get("content")
                        if not piece:
                            continue
                        parts.append(piece)
                        shown = extractor.feed(piece)
                        if shown and on_delta is not None:
                            on_delta(shown, False)
        except urllib.error.HTTPError as exc:
            self._translate_http_error(exc, call_index)
        except urllib.error.URLError as exc:
            raise _TransientFailure(f"网络错误：{exc.reason}") from exc
        except TimeoutError as exc:
            raise _TransientFailure(f"流式请求超时（{self._runtime.timeout:g}s）") from exc

        return self._finalize(
            content="".join(parts),
            finish_reason=finish_reason,
            usage_raw=usage_raw,
            model=str(payload.get("model", "")),
            call_index=call_index,
            purpose=purpose,
        )

    # ───────────────────── 共用收尾 ─────────────────────

    def _finalize(
        self,
        *,
        content: str | None,
        finish_reason: str | None,
        usage_raw: dict[str, Any],
        model: str,
        call_index: int,
        purpose: str,
    ) -> LlmReply:
        """截断检测、空正文检测、落盘响应、累计用量。流式与非流式共用。"""
        usage = Usage(
            calls=1,
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            reasoning_tokens=int(
                (usage_raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
            ),
        )

        if finish_reason == "length":
            raise LlmError(
                f"{purpose}：输出被 max_tokens 截断（"
                f"completion_tokens={usage.completion_tokens}，"
                f"其中 reasoning_tokens={usage.reasoning_tokens}）",
                hint=(
                    "截断的 JSON 必然解析失败，所以直接报错而不是拿去解析。\n"
                    "处理办法一：调大 --max-tokens。推理模型的 reasoning token 会先消耗预算。\n"
                    "处理办法二：这一节若本来就只需转写/装配而不需新判断，"
                    "给它关掉推理（见 synthesis.SECTION_THINKING）。"
                ),
            )

        if content is None or not content.strip():
            raise LlmError(
                f"{purpose}：模型返回了空正文（finish_reason={finish_reason!r}，"
                f"completion_tokens={usage.completion_tokens}，"
                f"reasoning_tokens={usage.reasoning_tokens}）",
                hint=(
                    "常见原因是输出预算被 reasoning 吃光了。请调大 --max-tokens。\n"
                    f"完整响应：{self._session.root}/llm-calls/{call_index:04d}-response.txt"
                ),
            )

        self._session.finish_llm_call(
            call_index,
            content=content,
            usage={**usage.to_dict(), "finish_reason": finish_reason, "model": model},
        )
        self._session.add_usage(usage)
        return LlmReply(
            content=content,
            usage=usage,
            finish_reason=finish_reason,
            model=model,
            call_index=call_index,
        )


#: 在流式 JSON 前缀里定位 say 字段的开头。
_SAY_KEY = re.compile(r'"say"\s*:\s*"')


class SayExtractor:
    """从**流式 JSON 增量**里抽出 ``say`` 字段的值。

    为什么需要它：发言的输出是结构化 JSON，如果直接把流式原文显示出来，
    用户看到的是一段裸 JSON 被逐字敲出来（``{"say": "我反对……``）。
    而我们把 ``say`` 放在 schema 的**第一位**，JSON 又是顺序生成的，
    所以流式前缀里最先出现的就是这段人话 —— 边收边抽即可。

    它做两件事：

    1. **只吐 ``say`` 的值**，不吐键名、不吐后面的结构化字段；
    2. **把 JSON 转义还原成可显示文本**（``\\n`` → 换行、``\\"`` → 引号、
       ``\\uXXXX`` → 字符）。

    转义还原必须**只吐完整的转义单元**：分片边界可能正好切在 ``\\`` 与
    ``n`` 之间、或 ``\\u12`` 与 ``34`` 之间。遇到不完整就原地等下一片，
    绝不用半个转义序列去猜 —— 猜错会显示出一个不存在的字符。

    找不到 ``say`` 键（模型没按 schema 写）时不吐任何东西。
    **这不是兜底失败**：界面会停在"正在生成…"，``turn.end`` 一到就显示完整内容。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._cursor = -1
        self._closed = False

    @property
    def started(self) -> bool:
        """是否已经找到 say 字段的开头。"""
        return self._cursor >= 0

    @property
    def closed(self) -> bool:
        """say 的字符串是否已经收尾（遇到未转义的引号）。"""
        return self._closed

    def feed(self, chunk: str) -> str:
        """喂入一段增量，返回**本次新增的、可安全显示**的文本。"""
        if self._closed:
            return ""
        self._buf += chunk

        if self._cursor < 0:
            match = _SAY_KEY.search(self._buf)
            if match is None:
                # 键还没出现。只留尾巴，避免 buffer 随整个 JSON 无界增长。
                if len(self._buf) > _SAY_BUFFER_LIMIT:
                    self._buf = self._buf[-_SAY_BUFFER_TAIL:]
                return ""
            self._cursor = match.end()

        produced: list[str] = []
        while self._cursor < len(self._buf):
            char = self._buf[self._cursor]
            if char == "\\":
                decoded = self._read_escape()
                if decoded is None:
                    break  # 转义序列还没收完，等下一片
                produced.append(decoded)
                continue
            if char == '"':
                self._closed = True
                break
            produced.append(char)
            self._cursor += 1
        return "".join(produced)

    def _read_escape(self) -> str | None:
        """读一个完整的转义序列。不完整时返回 ``None`` 并原地等待。"""
        if self._cursor + 1 >= len(self._buf):
            return None
        marker = self._buf[self._cursor + 1]
        if marker == "u":
            if self._cursor + 6 > len(self._buf):
                return None
            digits = self._buf[self._cursor + 2: self._cursor + 6]
            try:
                decoded = chr(int(digits, 16))
            except ValueError as exc:
                raise LlmError(
                    f"流式输出里的 \\u 转义不是合法十六进制：{digits!r}"
                ) from exc
            self._cursor += 6
            return decoded
        self._cursor += 2
        return _ESCAPES.get(marker, marker)


_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
}

#: say 键迟迟不出现时，buffer 的保留上限与尾部长度。
_SAY_BUFFER_LIMIT = 8192
_SAY_BUFFER_TAIL = 4096


class _TransientFailure(Exception):
    """内部信号：这一次失败值得重试。不外泄。"""
