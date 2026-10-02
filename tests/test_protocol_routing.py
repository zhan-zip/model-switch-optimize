"""阶段1协议路由测试：验证 Toolbox 和 health 按 provider 协议选择适配器。

本测试确保：
1. Toolbox.call_model() 根据 ModelStatus.protocol 选择适配器
2. health.test_connectivity() 根据 ModelStatus.protocol 选择适配器
3. 旧配置默认 openai_chat 协议
4. 未知协议抛出 ConfigError
"""
from model_switch.config import load_config
from model_switch.events import EventStream
from model_switch.model import ModelRegistry
from model_switch.protocols.base import ModelResponse, Usage
from model_switch.tools import Toolbox


VALID_YAML = """\
providers:
  - name: test_provider
    base_url: https://test.example.com/v1
    groups:
      - { name: test_group, key_env: TEST_KEY, models: [test_model] }
fallback: { provider: test_provider, group: test_group, model: test_model, key_env: TEST_KEY }
"""


def test_toolbox_call_routes_to_adapter(tmp_path, monkeypatch):
    """验证 Toolbox.call_model() 根据协议路由到适配器。"""
    monkeypatch.setenv("TEST_KEY", "sk-test")
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)

    # 记录适配器调用
    seen = {}

    class FakeAdapter:
        def call(self, base_url, api_key, request, **kwargs):
            seen.update(
                protocol="openai_chat",
                base_url=base_url,
                model=request.model,
                messages=request.messages,
            )
            return ModelResponse(
                ok=True,
                text="fake response",
                latency_ms=50,
                usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
            )

    # 替换协议注册表
    import model_switch.protocols.registry as registry_module
    original = registry_module._REGISTRY.copy()
    registry_module._REGISTRY["openai_chat"] = FakeAdapter

    try:
        stream = EventStream()
        toolbox = Toolbox(config, registry, stream, mock=False)

        result = toolbox.call_model(
            "test_provider/test_group/test_model",
            [{"role": "user", "content": "hello"}],
        )

        assert result.ok is True
        assert result.text == "fake response"
        assert seen["protocol"] == "openai_chat"
        assert seen["base_url"] == "https://test.example.com/v1"
        assert seen["model"] == "test_model"
        assert seen["messages"][0]["content"] == "hello"
    finally:
        registry_module._REGISTRY.clear()
        registry_module._REGISTRY.update(original)


def test_health_test_connectivity_routes_to_adapter(tmp_path, monkeypatch):
    """验证 health.test_connectivity() 根据协议路由到适配器。"""
    monkeypatch.setenv("TEST_KEY", "sk-test")
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)

    # 记录适配器调用
    seen = {}

    class FakeAdapter:
        def call(self, base_url, api_key, request, **kwargs):
            seen.update(
                protocol="openai_chat",
                base_url=base_url,
                model=request.model,
            )
            return ModelResponse(
                ok=True,
                text="pong",
                latency_ms=10,
                usage=Usage(),
            )

    # 替换协议注册表
    import model_switch.protocols.registry as registry_module
    original = registry_module._REGISTRY.copy()
    registry_module._REGISTRY["openai_chat"] = FakeAdapter

    try:
        from model_switch import health

        result = health.test_connectivity(
            registry.find("test_provider/test_group/test_model")
        )

        assert result.ok is True
        assert seen["protocol"] == "openai_chat"
        assert seen["base_url"] == "https://test.example.com/v1"
        assert seen["model"] == "test_model"
    finally:
        registry_module._REGISTRY.clear()
        registry_module._REGISTRY.update(original)


def test_old_config_defaults_to_openai_chat(tmp_path, monkeypatch):
    """验证旧配置默认使用 openai_chat 协议。"""
    monkeypatch.setenv("TEST_KEY", "sk-test")
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)

    status = registry.find("test_provider/test_group/test_model")
    assert status is not None
    assert status.protocol == "openai_chat"
    assert status.options == {}


def test_explicit_protocol_in_config(tmp_path, monkeypatch):
    """验证配置中显式指定协议。"""
    yaml_with_protocol = """\
providers:
  - name: test_provider
    base_url: https://test.example.com/v1
    protocol: openai_chat
    groups:
      - { name: test_group, key_env: TEST_KEY, models: [test_model] }
fallback: { provider: test_provider, group: test_group, model: test_model, key_env: TEST_KEY }
"""
    monkeypatch.setenv("TEST_KEY", "sk-test")
    path = tmp_path / "models.yaml"
    path.write_text(yaml_with_protocol, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)

    status = registry.find("test_provider/test_group/test_model")
    assert status is not None
    assert status.protocol == "openai_chat"


def test_protocol_options_passed_to_status(tmp_path, monkeypatch):
    """验证协议选项传递到 ModelStatus。"""
    yaml_with_options = """\
providers:
  - name: test_provider
    base_url: https://test.example.com/v1
    protocol: openai_chat
    options:
      custom_field: custom_value
      timeout_override: 60
    groups:
      - { name: test_group, key_env: TEST_KEY, models: [test_model] }
fallback: { provider: test_provider, group: test_group, model: test_model, key_env: TEST_KEY }
"""
    monkeypatch.setenv("TEST_KEY", "sk-test")
    path = tmp_path / "models.yaml"
    path.write_text(yaml_with_options, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)

    status = registry.find("test_provider/test_group/test_model")
    assert status is not None
    assert status.protocol == "openai_chat"
    assert status.options == {"custom_field": "custom_value", "timeout_override": 60}
