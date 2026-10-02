"""OpenAI Chat Completions 协议适配器。

将现有 client.py 中的 OpenAI 兼容调用迁移到协议适配器。
保持原有请求路径、Header、错误分类和响应解析逻辑。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..client import ErrorClass
from ..safety import redact_text
from .base import ModelRequest, ModelResponse, ProtocolAdapter, Usage

USER_AGENT = "model-switch-optimize/1.0 (OpenAI-compatible API client; Python urllib)"
_DETAIL_LIMIT = 300


class OpenAIChatAdapter(ProtocolAdapter):
    """OpenAI Chat Completions 协议适配器。"""

    protocol_name = "openai_chat"

    def call(
        self,
        base_url: str,
        api_key: str,
        request: ModelRequest,
        **kwargs: Any,
    ) -> ModelResponse:
        """执行 OpenAI Chat Completions 请求。
        
        Args:
            base_url: 服务商基础 URL
            api_key: API key
            request: 统一请求结构
            opener: 可选，测试用的 urllib opener
        """
        url = base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.response_format is not None:
            payload["response_format"] = request.response_format

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        http_request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = kwargs.get("opener") or urllib.request.urlopen

        start = time.perf_counter()
        try:
            with opener(http_request, timeout=request.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return ModelResponse(
                ok=False,
                error_class=self._classify_http(exc.code),
                detail=self._http_detail(exc),
                latency_ms=self._elapsed_ms(start),
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                error_class = ErrorClass.TIMEOUT
            else:
                error_class = ErrorClass.NETWORK
            return ModelResponse(
                ok=False,
                error_class=error_class,
                detail=redact_text(str(exc))[:_DETAIL_LIMIT],
                latency_ms=self._elapsed_ms(start),
            )
        except TimeoutError:
            return ModelResponse(
                ok=False,
                error_class=ErrorClass.TIMEOUT,
                detail="request timed out",
                latency_ms=self._elapsed_ms(start),
            )
        except Exception as exc:
            return ModelResponse(
                ok=False,
                error_class=ErrorClass.UNKNOWN,
                detail=redact_text(f"{type(exc).__name__}: {exc}")[:_DETAIL_LIMIT],
                latency_ms=self._elapsed_ms(start),
            )

        latency_ms = self._elapsed_ms(start)
        try:
            choices = raw.get("choices") or []
            text = str((choices[0].get("message") or {}).get("content", ""))
            usage_raw = raw.get("usage") or {}
        except (AttributeError, IndexError, TypeError):
            return ModelResponse(
                ok=False,
                error_class=ErrorClass.UNKNOWN,
                detail=redact_text(f"unexpected response shape: {str(raw)[:_DETAIL_LIMIT]}"),
                latency_ms=latency_ms,
            )

        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens"),
            output_tokens=usage_raw.get("completion_tokens"),
            total_tokens=usage_raw.get("total_tokens"),
            cached_input_tokens=usage_raw.get("prompt_tokens_details", {}).get("cached_tokens"),
        )

        return ModelResponse(
            ok=True,
            text=text,
            usage=usage,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    @staticmethod
    def _classify_http(code: int) -> str:
        if code in (401, 403):
            return ErrorClass.AUTH
        if code == 429:
            return ErrorClass.RATE_LIMIT
        if code >= 500:
            return ErrorClass.SERVER
        return ErrorClass.UNKNOWN

    @staticmethod
    def _http_detail(exc: urllib.error.HTTPError) -> str:
        try:
            payload = exc.read().decode("utf-8", errors="replace")
        except Exception:
            payload = ""
        head = f"HTTP {exc.code} {exc.reason}"
        return redact_text(f"{head} {payload}".strip())[:_DETAIL_LIMIT]
