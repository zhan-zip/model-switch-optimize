"""协议适配层测试：统一请求/响应结构、OpenAI Chat 适配器、协议注册表。"""
import io
import json
import urllib.error

import pytest

from model_switch.client import ErrorClass


# ==================== 基础数据结构测试 ====================


def test_model_request_has_required_fields():
    """ModelRequest 必须包含 model、messages、timeout 等字段。"""
    from model_switch.protocols.base import ModelRequest

    req = ModelRequest(
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        timeout=30,
    )
    assert req.model == "test-model"
    assert req.messages == [{"role": "user", "content": "hello"}]
    assert req.timeout == 30
    assert req.max_tokens is None
    assert req.temperature is None


def test_model_request_with_optional_fields():
    """ModelRequest 可选字段应正确保存。"""
    from model_switch.protocols.base import ModelRequest

    req = ModelRequest(
        model="m1",
        messages=[],
        timeout=20,
        max_tokens=100,
        temperature=0.7,
    )
    assert req.max_tokens == 100
    assert req.temperature == 0.7


def test_usage_structure():
    """Usage 应包含 input/output/total/cached/reasoning tokens。"""
    from model_switch.protocols.base import Usage

    usage = Usage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cached_input_tokens=20,
        reasoning_tokens=None,
    )
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.total_tokens == 150
    assert usage.cached_input_tokens == 20
    assert usage.reasoning_tokens is None


def test_usage_defaults_to_none():
    """Usage 缺失字段应为 None，不能误写为 0。"""
    from model_switch.protocols.base import Usage

    usage = Usage()
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None


def test_model_response_success():
    """ModelResponse 成功响应应包含 ok、text、usage、latency_ms。"""
    from model_switch.protocols.base import ModelResponse, Usage

    resp = ModelResponse(
        ok=True,
        text="hello world",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        latency_ms=123,
    )
    assert resp.ok is True
    assert resp.text == "hello world"
    assert resp.usage.total_tokens == 15
    assert resp.latency_ms == 123
    assert resp.error_class == ""
    assert resp.detail == ""


def test_model_response_failure():
    """ModelResponse 失败响应应包含 error_class 和 detail。"""
    from model_switch.protocols.base import ModelResponse

    resp = ModelResponse(
        ok=False,
        error_class=ErrorClass.AUTH,
        detail="invalid key",
        latency_ms=50,
    )
    assert resp.ok is False
    assert resp.error_class == ErrorClass.AUTH
    assert resp.detail == "invalid key"
    assert resp.text == ""


# ==================== 协议注册表测试 ====================


def test_registry_default_protocol():
    """未指定协议时应默认返回 openai_chat。"""
    from model_switch.protocols.registry import get_adapter

    adapter = get_adapter(protocol=None)
    assert adapter.protocol_name == "openai_chat"


def test_registry_explicit_openai_chat():
    """显式指定 openai_chat 应返回对应适配器。"""
    from model_switch.protocols.registry import get_adapter

    adapter = get_adapter(protocol="openai_chat")
    assert adapter.protocol_name == "openai_chat"


def test_registry_unknown_protocol_raises():
    """未知协议应抛出 ConfigError。"""
    from model_switch.config import ConfigError
    from model_switch.protocols.registry import get_adapter

    with pytest.raises(ConfigError, match="未知协议"):
        get_adapter(protocol="unknown_protocol")


# ==================== OpenAI Chat 适配器测试 ====================


class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_opener(payload=None):
    body = payload or {
        "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    def open(req, timeout):
        return _FakeResponse(body)

    return open


def _http_error_opener(code, body=""):
    def open(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, code, "err", hdrs=None, fp=io.BytesIO(body.encode("utf-8"))
        )

    return open


def _raise_opener(exc):
    def open(req, timeout):
        raise exc

    return open


def test_openai_adapter_success():
    """OpenAI Chat 适配器成功请求应返回 text 和 usage。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(
        model="gpt-4o",
        messages=[{"role": "user", "content": "ping"}],
        timeout=30,
    )
    response = adapter.call(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        request=request,
        opener=_ok_opener(),
    )
    assert response.ok is True
    assert response.text == "pong"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.usage.total_tokens == 15


def test_openai_adapter_401():
    """OpenAI Chat 适配器 401 应分类为 AUTH。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-bad",
        request=request,
        opener=_http_error_opener(401),
    )
    assert response.ok is False
    assert response.error_class == ErrorClass.AUTH


def test_openai_adapter_429():
    """OpenAI Chat 适配器 429 应分类为 RATE_LIMIT。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-test",
        request=request,
        opener=_http_error_opener(429),
    )
    assert response.error_class == ErrorClass.RATE_LIMIT


def test_openai_adapter_5xx():
    """OpenAI Chat 适配器 5xx 应分类为 SERVER。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-test",
        request=request,
        opener=_http_error_opener(500),
    )
    assert response.error_class == ErrorClass.SERVER


def test_openai_adapter_timeout():
    """OpenAI Chat 适配器超时应分类为 TIMEOUT。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-test",
        request=request,
        opener=_raise_opener(TimeoutError()),
    )
    assert response.error_class == ErrorClass.TIMEOUT


def test_openai_adapter_network_error():
    """OpenAI Chat 适配器网络错误应分类为 NETWORK。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    exc = urllib.error.URLError(OSError("connection failed"))
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-test",
        request=request,
        opener=_raise_opener(exc),
    )
    assert response.error_class == ErrorClass.NETWORK


def test_openai_adapter_bad_response_shape():
    """OpenAI Chat 适配器异常响应结构应分类为 UNKNOWN。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-test",
        request=request,
        opener=_ok_opener({"unexpected": True}),
    )
    assert response.ok is False
    assert response.error_class == ErrorClass.UNKNOWN


def test_openai_adapter_request_url():
    """OpenAI Chat 适配器应构造正确的 /chat/completions 路径。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[{"role": "user", "content": "test"}], timeout=10)

    called_url = None

    def capture_opener(req, timeout):
        nonlocal called_url
        called_url = req.full_url
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    adapter.call("https://api.example.com/v1", "sk-test", request, opener=capture_opener)
    assert called_url == "https://api.example.com/v1/chat/completions"


def test_openai_adapter_request_headers():
    """OpenAI Chat 适配器应设置正确的 Authorization 和 User-Agent。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)

    captured_req = None

    def capture_opener(req, timeout):
        nonlocal captured_req
        captured_req = req
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    adapter.call("https://x.com/v1", "sk-abc123", request, opener=capture_opener)
    assert captured_req.get_header("Authorization") == "Bearer sk-abc123"
    assert "model-switch-optimize" in captured_req.get_header("User-agent")


def test_openai_adapter_request_body():
    """OpenAI Chat 适配器应构造正确的请求体。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        timeout=30,
        max_tokens=100,
    )

    captured_body = None

    def capture_opener(req, timeout):
        nonlocal captured_body
        captured_body = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    adapter.call("https://x.com/v1", "sk-test", request, opener=capture_opener)
    assert captured_body["model"] == "test-model"
    assert captured_body["messages"] == [{"role": "user", "content": "hello"}]
    assert captured_body["max_tokens"] == 100


def test_openai_adapter_usage_missing():
    """OpenAI Chat 适配器响应缺失 usage 时应保存 None。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-test",
        request=request,
        opener=_ok_opener({"choices": [{"message": {"content": "ok"}}]}),
    )
    assert response.ok is True
    assert response.usage.total_tokens is None


def test_openai_adapter_detail_redacted():
    """OpenAI Chat 适配器错误详情应自动脱敏。"""
    from model_switch.protocols.base import ModelRequest
    from model_switch.protocols.openai_chat import OpenAIChatAdapter

    adapter = OpenAIChatAdapter()
    request = ModelRequest(model="m1", messages=[], timeout=10)
    response = adapter.call(
        base_url="https://x.com/v1",
        api_key="sk-test",
        request=request,
        opener=_http_error_opener(401, '{"error": "invalid key sk-abcd1234567890"}'),
    )
    assert "sk-abcd1234567890" not in response.detail
    assert "sk-abcd****" in response.detail


# ==================== 旧配置兼容测试 ====================


def test_old_config_without_protocol_field(tmp_path):
    """旧配置没有 protocol 字段应能正常加载并默认为 openai_chat。"""
    from model_switch.config import load_config

    old_yaml = """\
providers:
  - name: provider-a
    base_url: https://a.example.com/v1
    groups:
      - name: default
        key_env: KEY_A
        models: [gpt-4o]
fallback: { provider: provider-a, group: default, model: gpt-4o, key_env: KEY_A }
"""
    path = tmp_path / "old.yaml"
    path.write_text(old_yaml, encoding="utf-8")
    config = load_config(path)
    assert config.providers[0].protocol == "openai_chat"
    assert config.providers[0].options == {}


def test_new_config_with_protocol_field(tmp_path):
    """新配置显式指定 protocol 应正确加载。"""
    from model_switch.config import load_config

    new_yaml = """\
providers:
  - name: provider-a
    protocol: openai_chat
    base_url: https://a.example.com/v1
    options:
      retry_count: 3
    groups:
      - name: default
        key_env: KEY_A
        models: [gpt-4o]
fallback: { provider: provider-a, group: default, model: gpt-4o, key_env: KEY_A }
"""
    path = tmp_path / "new.yaml"
    path.write_text(new_yaml, encoding="utf-8")
    config = load_config(path)
    assert config.providers[0].protocol == "openai_chat"
    assert config.providers[0].options["retry_count"] == 3


def test_config_unknown_protocol_validation(tmp_path):
    """配置中的未知协议应在 validate_config 阶段失败。"""
    from model_switch.config import load_config, validate_config

    unknown_yaml = """\
providers:
  - name: provider-a
    protocol: unknown_protocol
    base_url: https://a.example.com/v1
    groups:
      - name: default
        key_env: KEY_A
        models: [m1]
fallback: { provider: provider-a, group: default, model: m1, key_env: KEY_A }
"""
    path = tmp_path / "unknown.yaml"
    path.write_text(unknown_yaml, encoding="utf-8")
    config = load_config(path)
    errors = validate_config(config)
    assert any("未知协议" in e or "unknown_protocol" in e for e in errors)
