"""连通测试：向模型发一条极小消息，验证全链路（网络 / key / 分组 / 模型）。

阶段1：test_connectivity 改用协议适配器路由，根据 ModelStatus.protocol 选择适配器。
"""

from __future__ import annotations

from .client import CallResult, ErrorClass, ModelClient, response_to_result
from .config import resolve_api_key
from .model import ModelStatus
from .protocols.base import ModelRequest
from .protocols.registry import get_adapter

PROBE_MESSAGES: list[dict[str, str]] = [{"role": "user", "content": "ping"}]
PROBE_MAX_TOKENS = 8
PROBE_TIMEOUT_SECONDS = 20


def test_connectivity(
    status: ModelStatus,
    *,
    client: ModelClient | None = None,
    timeout: int = PROBE_TIMEOUT_SECONDS,
) -> CallResult:
    """对单个模型做连通测试；key 未设置返回 CONFIG 错误（不发起网络请求）。

    阶段1：使用协议适配器路由，根据 status.protocol 选择适配器。
    """

    api_key = resolve_api_key(status.key_env)
    if api_key is None:
        return CallResult(
            ok=False,
            model=str(status.ref),
            error_class=ErrorClass.CONFIG,
            detail=f"环境变量 {status.key_env} 未设置（key 只走环境变量）",
        )

    # 保留显式 client 注入作为测试和宿主兼容入口；默认路径使用协议适配器。
    if client is not None:
        return client(
            status.base_url,
            api_key,
            status.ref.model,
            PROBE_MESSAGES,
            timeout=timeout,
            max_tokens=PROBE_MAX_TOKENS,
        )

    adapter = get_adapter(status.protocol)
    request = ModelRequest(
        model=status.ref.model,
        messages=PROBE_MESSAGES,
        timeout=timeout,
        max_tokens=PROBE_MAX_TOKENS,
    )
    response = adapter.call(
        status.base_url,
        api_key,
        request,
        **status.options,
    )

    return response_to_result(response, str(status.ref))
