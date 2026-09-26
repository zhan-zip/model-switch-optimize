"""连通测试：向模型发一条极小消息，验证全链路（网络 / key / 分组 / 模型）。"""

from __future__ import annotations

from .client import CallResult, ErrorClass, ModelClient, call_openai_compatible
from .config import resolve_api_key
from .model import ModelStatus

PROBE_MESSAGES: list[dict[str, str]] = [{"role": "user", "content": "ping"}]
PROBE_MAX_TOKENS = 8
PROBE_TIMEOUT_SECONDS = 20


def test_connectivity(
    status: ModelStatus,
    *,
    client: ModelClient = call_openai_compatible,
    timeout: int = PROBE_TIMEOUT_SECONDS,
) -> CallResult:
    """对单个模型做连通测试；key 未设置返回 CONFIG 错误（不发起网络请求）。"""

    api_key = resolve_api_key(status.key_env)
    if api_key is None:
        return CallResult(
            ok=False,
            model=str(status.ref),
            error_class=ErrorClass.CONFIG,
            detail=f"环境变量 {status.key_env} 未设置（key 只走环境变量）",
        )
    return client(
        status.base_url,
        api_key,
        status.ref.model,
        PROBE_MESSAGES,
        timeout=timeout,
        max_tokens=PROBE_MAX_TOKENS,
    )
