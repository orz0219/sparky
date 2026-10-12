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

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
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
    """推理端点的客户端。一个会话共用一个实例。"""

    def __init__(self, runtime: RuntimeConfig, session: Session) -> None:
        self._runtime = runtime
        self._session = session
        self._endpoint = runtime.base_url.rstrip("/") + "/chat/completions"
        self._host = urlparse(self._endpoint).netloc.lower()

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
        """发起一次非流式调用，返回完整正文。

        ``thinking=False`` 会请求关闭推理（仅 DeepSeek 系生效）。
        适用于**机械性任务**：把已经推理好的材料转写、装配、制表。
        2026-10-12 实测：一次简单问答的 completion token 从 188 降到 22，
        而且不再有"8000 token 全烧在 reasoning 上、正文为空"的风险。

        M1 全部阶段都是结构化输出，非流式足够；
        M3 的 ``turn.token`` 打字机效果会再加一个 ``stream()`` 方法，
        两者互不影响。
        """
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
        """调用并解析 JSON。

        解析失败时**有界重试**（见 ``JSON_ATTEMPTS`` 的说明）：重试时在 system
        末尾附加一段纠正提示，并把上一次的原始输出留在 ``llm-calls/`` 里。
        第二次仍失败 → 抛 ``LlmError``。

        绝不做的是**改写模型输出**：把全角逗号全局替换成半角会连字符串内容
        一起改掉，那是静默篡改。
        """
        current_system = system
        last: tuple[json.JSONDecodeError, LlmReply] | None = None

        for attempt in range(1, JSON_ATTEMPTS + 1):
            reply = self.complete(
                system=current_system,
                user=user,
                purpose=purpose if attempt == 1 else f"{purpose}（JSON 重试 {attempt}）",
                model=model,
                json_mode=True,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking=thinking,
            )
            text = reply.content
            fence = _FENCE.match(text)
            if fence:
                self._session.warn(
                    "json_fence",
                    f"模型把 JSON 包在代码围栏里（{purpose}），已剥离。"
                    f"这通常意味着提示词的约束被忽略了，值得复查。",
                )
                text = fence.group(1)

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

    # ───────────────────── 内部实现 ─────────────────────

    def _post_with_retries(self, payload: dict[str, Any], *, purpose: str) -> tuple[str, int]:
        """带重试的 POST，返回 ``(响应正文, 调用编号)``。

        每次尝试之前都重新保存请求快照；编号一路带回给调用方，
        因为并发下回头读 ``session.call_count`` 会拿到别的线程的编号。
        """
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            call_index = self._session.begin_llm_call(
                payload, purpose=f"{purpose}（第 {attempt + 1} 次尝试）" if attempt else purpose
            )
            try:
                raw = self._post_once(payload, call_index=call_index)
                return raw, call_index
            except _TransientFailure as exc:
                last_error = exc
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
        # 循环必定在内部 return 或 raise，这里只是让类型检查满意
        raise LlmError(f"{purpose}：不可达分支（{last_error}）")

    def _post_once(self, payload: dict[str, Any], *, call_index: int) -> str:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                # 必须显式设置：默认的 Python-urllib UA 会被部分网关拒绝
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self._runtime.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._runtime.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
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
        except urllib.error.URLError as exc:
            raise _TransientFailure(f"网络错误：{exc.reason}") from exc
        except TimeoutError as exc:
            raise _TransientFailure(
                f"请求超时（{self._runtime.timeout:g}s）"
            ) from exc

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
        """校验响应结构、检查截断、保存响应、累计用量。"""
        try:
            body = json.loads(raw)
            choice = body["choices"][0]
            message = choice["message"]
            content = message["content"]
            finish_reason = choice.get("finish_reason")
            usage_raw = body.get("usage") or {}
            model = body.get("model", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(
                f"{purpose}：响应结构不符合 OpenAI 兼容格式（缺 {exc}）",
                hint=f"完整响应：{raw[:600]}",
            ) from exc

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
                    f"完整响应：{self._session.root}/llm-calls/"
                    f"{call_index:04d}-response.txt"
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


class _TransientFailure(Exception):
    """内部信号：这一次失败值得重试。不外泄。"""
