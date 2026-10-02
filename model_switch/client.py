"""HTTP 客户端：OpenAI 兼容协议调用与错误分类。

标准库实现（urllib），零额外依赖。
错误分类（供决策中枢判因）：401 / 429 / 5xx / 超时 / 断网。

阶段1：本模块保留 CallResult 和 call_openai_compatible 作为兼容层，
内部委托给 protocols.openai_chat.OpenAIChatAdapter。
新代码应使用 protocols 层的 ModelRequest/ModelResponse。
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
    """一次模型调用的结果（成功或分类后的失败）。
    
    阶段1 保留此结构作为兼容层，新代码应使用 protocols.ModelResponse。
    """

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
    """POST {base_url}/chat/completions，返回分类后的 CallResult。
    
    阶段1：委托给 OpenAIChatAdapter，保持旧接口兼容。
    """
    from .protocols.base import ModelRequest
    from .protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(
        model=model,
        messages=messages,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    response = adapter.call(base_url, api_key, request, opener=opener)

    # 转换为旧的 CallResult 格式
    usage_dict = {}
    if response.usage.input_tokens is not None:
        usage_dict["prompt_tokens"] = response.usage.input_tokens
    if response.usage.output_tokens is not None:
        usage_dict["completion_tokens"] = response.usage.output_tokens
    if response.usage.total_tokens is not None:
        usage_dict["total_tokens"] = response.usage.total_tokens

    return CallResult(
        ok=response.ok,
        model=model,
        text=response.text,
        error_class=response.error_class,
        detail=response.detail,
        latency_ms=response.latency_ms,
        usage=usage_dict,
    )
