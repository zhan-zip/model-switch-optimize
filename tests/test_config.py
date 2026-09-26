"""config 模块测试：模板合法性、加载、结构错误、语义校验、机械兜底顺序。"""
import pytest

from model_switch.config import (
    CONFIG_TEMPLATE,
    ConfigError,
    load_config,
    resolve_api_key,
    validate_config,
)

VALID_YAML = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - { name: g1, key_env: KEY_A, models: [m1, m2] }
  - name: b
    base_url: https://b.example.com/v1
    groups:
      - { name: g1, key_env: KEY_B, models: [m3] }
fallback: { provider: b, group: g1, model: m3, key_env: KEY_B }
"""


def _write(tmp_path, content, name="models.yaml"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_template_is_valid(tmp_path):
    config = load_config(_write(tmp_path, CONFIG_TEMPLATE))
    assert validate_config(config) == []


def test_valid_config_loads(tmp_path):
    config = load_config(_write(tmp_path, VALID_YAML))
    assert validate_config(config) == []
    assert [str(r) for r in config.iter_model_refs()] == ["a/g1/m1", "a/g1/m2", "b/g1/m3"]
    assert str(config.fallback_ref()) == "b/g1/m3"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "none.yaml")


def test_missing_providers(tmp_path):
    content = "fallback: { provider: x, group: y, model: z, key_env: K }\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_missing_fallback_fields(tmp_path):
    content = VALID_YAML.replace(
        "fallback: { provider: b, group: g1, model: m3, key_env: KEY_B }",
        "fallback: { provider: b }",
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, content))


def test_fallback_model_not_in_group(tmp_path):
    content = VALID_YAML.replace("model: m3, key_env: KEY_B", "model: m99, key_env: KEY_B")
    errors = validate_config(load_config(_write(tmp_path, content)))
    assert any("fallback.model" in e for e in errors)


def test_fallback_key_env_mismatch(tmp_path):
    content = VALID_YAML.replace("model: m3, key_env: KEY_B", "model: m3, key_env: KEY_OTHER")
    errors = validate_config(load_config(_write(tmp_path, content)))
    assert any("key_env" in e for e in errors)


def test_duplicate_provider_name(tmp_path):
    content = VALID_YAML.replace("- name: b", "- name: a")
    errors = validate_config(load_config(_write(tmp_path, content)))
    assert any("重复" in e for e in errors)


def test_bad_base_url(tmp_path):
    content = VALID_YAML.replace("https://b.example.com/v1", "b.example.com")
    errors = validate_config(load_config(_write(tmp_path, content)))
    assert any("base_url" in e for e in errors)


def test_resolve_api_key(monkeypatch):
    monkeypatch.setenv("MSO_TEST_KEY", "  secret-value  ")
    assert resolve_api_key("MSO_TEST_KEY") == "secret-value"
    monkeypatch.delenv("MSO_TEST_KEY", raising=False)
    assert resolve_api_key("MSO_TEST_KEY") is None
