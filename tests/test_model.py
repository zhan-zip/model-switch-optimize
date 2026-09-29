"""model 模块测试：注册表构建、查找、状态更新、暂停（冷却）与持久化。"""
from datetime import datetime, timedelta, timezone

from model_switch.config import load_config
from model_switch.model import (
    ModelRegistry,
    load_pause_state,
    prune_pause_state,
    restore_pause_state,
    save_pause_state,
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


# -- 暂停（冷却）状态 ---------------------------------------------------


def test_pause_and_is_paused_and_resume(tmp_path):
    registry = ModelRegistry(_config(tmp_path))
    status = registry.find("a/g1/m1")
    assert status.is_paused() is False  # 未暂停
    status.pause(60)
    assert status.is_paused() is True  # 暂停中
    assert status.paused_until is not None
    status.resume()
    assert status.is_paused() is False


def test_pause_expires_automatically(tmp_path):
    registry = ModelRegistry(_config(tmp_path))
    status = registry.find("a/g1/m1")
    # 已过期的暂停：视为未暂停（到期自动解除）
    status.paused_until = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat(timespec="seconds")
    assert status.is_paused() is False
    # 坏格式视为未暂停
    status.paused_until = "not-a-time"
    assert status.is_paused() is False


def test_to_dict_contains_pause_fields(tmp_path):
    registry = ModelRegistry(_config(tmp_path))
    status = registry.find("a/g1/m1")
    dumped = status.to_dict()
    assert dumped["paused"] is False
    assert dumped["paused_until"] is None
    status.pause(60)
    dumped = status.to_dict()
    assert dumped["paused"] is True
    assert dumped["paused_until"]


# -- 暂停持久化 ---------------------------------------------------------


def test_pause_state_save_load_roundtrip(tmp_path):
    path = tmp_path / "pause.json"
    until = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
    save_pause_state({"a/g1/m1": until}, path)
    assert load_pause_state(path) == {"a/g1/m1": until}


def test_load_pause_state_missing_or_corrupt(tmp_path):
    assert load_pause_state(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("不是 JSON", encoding="utf-8")
    assert load_pause_state(bad) == {}
    bad.write_text("[1, 2]", encoding="utf-8")
    assert load_pause_state(bad) == {}  # 非 dict


def test_prune_pause_state_drops_expired_and_bad(tmp_path):
    now = datetime.now(timezone.utc)
    past = (now - timedelta(seconds=1)).isoformat(timespec="seconds")
    future = (now + timedelta(seconds=60)).isoformat(timespec="seconds")
    kept = prune_pause_state(
        {"a/g1/m1": future, "a/g1/m2": past, "a/g1/m3": "bad-time"},
        now=now,
    )
    assert kept == {"a/g1/m1": future}


def test_restore_pause_state_into_registry(tmp_path):
    path = tmp_path / "pause.json"
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    save_pause_state({"a/g1/m1": future, "a/g1/m2": past, "x/y/z": future}, path)
    registry = ModelRegistry(_config(tmp_path))
    kept = restore_pause_state(registry, path)
    # 过期清扫（=到期自动解除）、未知模型忽略
    assert kept == {"a/g1/m1": future}
    assert registry.find("a/g1/m1").is_paused() is True
    assert registry.find("a/g1/m2").is_paused() is False
