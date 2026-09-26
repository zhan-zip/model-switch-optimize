"""model 模块测试：注册表构建、查找、状态更新。"""
from model_switch.config import load_config
from model_switch.model import ModelRegistry

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


def _config(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    return load_config(path)


def test_registry_builds_all_models(tmp_path):
    registry = ModelRegistry(_config(tmp_path))
    refs = [str(s.ref) for s in registry.all()]
    assert refs == ["a/g1/m1", "a/g1/m2", "b/g1/m3"]


def test_registry_find_and_missing(tmp_path):
    registry = ModelRegistry(_config(tmp_path))
    assert registry.find("a/g1/m1") is not None
    assert registry.find("a/g1/m99") is None


def test_registry_update(tmp_path):
    registry = ModelRegistry(_config(tmp_path))
    updated = registry.update("a/g1/m1", available=False, last_error_class="401")
    assert updated.available is False
    assert registry.find("a/g1/m1").last_error_class == "401"
    assert registry.update("nope/nope/nope", available=True) is None


def test_status_mark(tmp_path):
    registry = ModelRegistry(_config(tmp_path))
    status = registry.find("b/g1/m3")
    status.mark(False, error_class="429", error_detail="rate limited")
    assert status.available is False
    assert status.last_error_class == "429"
    assert status.last_checked != ""


def test_status_to_dict_has_no_plain_key(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_A", "sk-realsecretkey1234567890abcdef")
    registry = ModelRegistry(_config(tmp_path))
    dumped = registry.find("a/g1/m1").to_dict()
    assert "sk-realsecretkey1234567890abcdef" not in str(dumped)
    assert dumped["key_status"].endswith("****")
