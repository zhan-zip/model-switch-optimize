"""协议适配器注册表。

根据配置中的 protocol 字段选择对应的适配器。
未指定 protocol 时默认为 openai_chat，保证旧配置兼容。
"""
from __future__ import annotations

from ..config import ConfigError
from .base import ProtocolAdapter
from .openai_chat import OpenAIChatAdapter

_REGISTRY: dict[str, type[ProtocolAdapter]] = {
    "openai_chat": OpenAIChatAdapter,
}


def get_adapter(protocol: str | None = None) -> ProtocolAdapter:
    """根据协议名称获取适配器实例。
    
    Args:
        protocol: 协议名称，None 时默认为 openai_chat
        
    Returns:
        协议适配器实例
        
    Raises:
        ConfigError: 未知协议
    """
    protocol = protocol or "openai_chat"
    adapter_class = _REGISTRY.get(protocol)
    if adapter_class is None:
        raise ConfigError(
            f"未知协议：{protocol}（当前支持：{', '.join(_REGISTRY.keys())}）"
        )
    return adapter_class()


def list_protocols() -> list[str]:
    """列出所有已注册的协议名称。"""
    return list(_REGISTRY.keys())
