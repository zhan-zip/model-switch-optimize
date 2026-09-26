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
    monkeypatch.setenv("KEY_A", "sk-x")
    registry = _registry(tmp_path)
    seen = {}

    def fake_client(base_url, api_key, model, messages, *, timeout, max_tokens):
        seen.update(base_url=base_url, model=model, messages=messages, max_tokens=max_tokens)
        return CallResult(ok=True, model=model, text="pong", latency_ms=10)

    result = health.test_connectivity(registry.find("a/g1/m1"), client=fake_client)
    assert result.ok is True
    assert seen["base_url"] == "https://a.example.com/v1"
    assert seen["model"] == "m1"
    assert seen["max_tokens"] <= 8  # 极小消息，省 token
    assert seen["messages"][0]["content"] == "ping"
