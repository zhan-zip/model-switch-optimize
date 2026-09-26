"""HTTP 客户端：OpenAI 兼容协议调用与错误分类。

标准库实现（urllib），零额外依赖。
错误分类（供决策中枢判因）：401 / 429 / 5xx / 超时 / 断网。
调用签名统一为 (base_url, api_key, model, messages, *, timeout, max_tokens, opener)，
mock 客户端按同签名注入（mocker.MockModelClient）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .safety import redact_text

DEFAULT_TIMEOUT_SECONDS = 30
_DETAIL_LIMIT = 300
USER_AGENT = "model-switch-optimize/1.0 (OpenAI-compatible API client; Python urllib)"


class ErrorClass:
    """错误分类（决策中枢判因依据）。"""

    AUTH = "401"          # key 无效 / 未授权（401/403）
    RATE_LIMIT = "429"    # 限流
    SERVER = "5xx"        # 服务端错误
    TIMEOUT = "timeout"   # 超时
    NETWORK = "network"   # 断网 / DNS / 连接失败
    CONFIG = "config"     # 配置问题（如环境变量未设置）
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CallResult:
    """一次模型调用的结果（成功或分类后的失败）。"""

    ok: bool
    model: str = ""
    text: str = ""
    error_class: str = ""
    detail: str = ""
    latency_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "model": self.model,
            "text": self.text,
            "error_class": self.error_class,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
        }


ModelClient = Callable[..., CallResult]


def call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int | None = None,
    opener: Callable[..., Any] | None = None,
) -> CallResult:
    """POST {base_url}/chat/completions，返回分类后的 CallResult。"""

    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,  # 部分网关（Cloudflare）拦截默认 urllib UA
        "Accept": "application/json",
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    open_ = opener or urllib.request.urlopen

    start = time.perf_counter()
    try:
        with open_(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return CallResult(
            ok=False, model=model,
            error_class=_classify_http(exc.code),
            detail=_http_detail(exc),
            latency_ms=_elapsed_ms(start),
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            error_class = ErrorClass.TIMEOUT
        else:
            error_class = ErrorClass.NETWORK
        return CallResult(
            ok=False, model=model,
            error_class=error_class,
            detail=redact_text(str(exc))[:_DETAIL_LIMIT],
            latency_ms=_elapsed_ms(start),
        )
    except TimeoutError:
        return CallResult(
            ok=False, model=model,
            error_class=ErrorClass.TIMEOUT,
            detail="request timed out",
            latency_ms=_elapsed_ms(start),
        )
    except Exception as exc:  # 其他未预期错误（如响应体非 JSON）
        return CallResult(
            ok=False, model=model,
            error_class=ErrorClass.UNKNOWN,
            detail=redact_text(f"{type(exc).__name__}: {exc}")[:_DETAIL_LIMIT],
            latency_ms=_elapsed_ms(start),
        )

    latency_ms = _elapsed_ms(start)
    try:
        choices = raw.get("choices") or []
        text = str((choices[0].get("message") or {}).get("content", ""))
        usage = raw.get("usage") or {}
    except (AttributeError, IndexError, TypeError):
        return CallResult(
            ok=False, model=model,
            error_class=ErrorClass.UNKNOWN,
            detail=redact_text(f"unexpected response shape: {str(raw)[:_DETAIL_LIMIT]}"),
            latency_ms=latency_ms,
        )
    return CallResult(ok=True, model=model, text=text, latency_ms=latency_ms, usage=usage)


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _classify_http(code: int) -> str:
    if code in (401, 403):
        return ErrorClass.AUTH
    if code == 429:
        return ErrorClass.RATE_LIMIT
    if code >= 500:
        return ErrorClass.SERVER
    return ErrorClass.UNKNOWN


def _http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = exc.read().decode("utf-8", errors="replace")
    except Exception:
        payload = ""
    head = f"HTTP {exc.code} {exc.reason}"
    return redact_text(f"{head} {payload}".strip())[:_DETAIL_LIMIT]
