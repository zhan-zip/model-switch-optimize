"""health 模块测试：连通测试（mock client 注入）。

注意：通过模块属性访问 health.test_connectivity，
避免 from-import 把 test_ 开头的函数带进测试命名空间被 pytest 误收集。
"""
from model_switch import health
from model_switch.client import CallResult, ErrorClass
from model_switch.config import load_config
from model_switch.model import ModelRegistry

VALID_YAML = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - { name: g1, key_env: KEY_A, models: [m1] }
fallback: { provider: a, group: g1, model: m1, key_env: KEY_A }
"""


def _registry(tmp_path, monkeypatch=None):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("KEY_A", "sk-x")
    return ModelRegistry(load_config(path))


def test_missing_key_returns_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("KEY_A", raising=False)
    registry = _registry(tmp_path)
    result = health.test_connectivity(registry.find("a/g1/m1"))
    assert result.ok is False
    assert result.error_class == ErrorClass.CONFIG
    assert "KEY_A" in result.detail


def test_probe_calls_client_with_tiny_message(tmp_path, monkeypatch):
    """阶段1：test_connectivity 不再接受 client 参数，改用协议适配器。

    本测试通过 monkeypatch 协议注册表来验证路由逻辑。
    """
    monkeypatch.setenv("KEY_A", "sk-x")
    registry = _registry(tmp_path)

    # 记录适配器调用
    seen = {}

    from model_switch.protocols.base import ModelResponse, Usage

    class FakeAdapter:
        def call(self, base_url, api_key, request, **kwargs):
            seen.update(
                base_url=base_url,
                model=request.model,
                messages=request.messages,
                max_tokens=request.max_tokens,
            )
            return ModelResponse(
                ok=True,
                text="pong",
                latency_ms=10,
                usage=Usage(),
            )

    # 替换协议注册表
    import model_switch.protocols.registry as registry_module
    original_adapters = registry_module._REGISTRY.copy()
    registry_module._REGISTRY["openai_chat"] = FakeAdapter

    try:
        result = health.test_connectivity(registry.find("a/g1/m1"))
        assert result.ok is True
        assert seen["base_url"] == "https://a.example.com/v1"
        assert seen["model"] == "m1"
        assert seen["max_tokens"] <= 8  # 极小消息，省 token
        assert seen["messages"][0]["content"] == "ping"
    finally:
        registry_module._REGISTRY.clear()
        registry_module._REGISTRY.update(original_adapters)
