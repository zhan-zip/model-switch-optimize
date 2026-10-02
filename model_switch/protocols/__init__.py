"""协议适配层：统一模型调用接口，支持多厂商协议。

本模块提供：
- ModelRequest / ModelResponse / Usage：统一请求和响应结构
- ProtocolAdapter：协议适配器基类
- get_adapter()：根据配置选择适配器
- OpenAIChatAdapter：OpenAI Chat Completions 适配器（当前默认）

阶段 1 只实现 openai_chat 适配器，后续阶段逐步增加 anthropic_messages、gemini_generate_content 等。
"""
from .base import ModelRequest, ModelResponse, ProtocolAdapter, Usage
from .registry import get_adapter

__all__ = [
    "ModelRequest",
    "ModelResponse",
    "Usage",
    "ProtocolAdapter",
    "get_adapter",
]
