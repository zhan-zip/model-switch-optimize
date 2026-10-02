"""协议适配层基础数据结构：统一请求、响应和适配器接口。

所有协议适配器必须实现 ProtocolAdapter 接口，将厂商特定的请求和响应转换为统一结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Usage:
    """统一 Token 用量结构。
    
    不同协议的 Token 字段不一致，统一归一化为：
    - input_tokens：输入 Token 数
    - output_tokens：输出 Token 数
    - total_tokens：总 Token 数
    - cached_input_tokens：缓存命中的输入 Token（可选）
    - reasoning_tokens：推理 Token（可选，如 o1）
    
    服务商未返回时保存 None，不能误写为 0。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass
class ModelRequest:
    """统一模型请求结构。
    
    协议适配器将此结构转换为厂商特定的 HTTP 请求。
    """

    model: str
    messages: list[dict[str, str]]
    timeout: int = 30
    max_tokens: int | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None


@dataclass
class ModelResponse:
    """统一模型响应结构。
    
    协议适配器将厂商特定响应转换为此结构。
    """

    ok: bool
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    error_class: str = ""
    detail: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
            "error_class": self.error_class,
            "detail": self.detail,
            "tool_calls": self.tool_calls,
            "finish_reason": self.finish_reason,
        }


class ProtocolAdapter:
    """协议适配器基类。
    
    每个协议适配器必须实现 call() 方法，将统一请求转换为厂商特定请求，
    并将厂商特定响应转换为统一响应。
    """

    protocol_name: str = ""

    def call(
        self,
        base_url: str,
        api_key: str,
        request: ModelRequest,
        **kwargs: Any,
    ) -> ModelResponse:
        """执行模型调用。
        
        Args:
            base_url: 服务商基础 URL
            api_key: API key
            request: 统一请求结构
            **kwargs: 适配器特定参数（如测试用的 opener）
            
        Returns:
            统一响应结构
        """
        raise NotImplementedError
