"""rules 模块测试：规则库加载（默认 / 自定义 / 缺失降级）。"""
from pathlib import Path

from model_switch.rules import DEFAULT_RULES_PATH, load_rules


def test_default_rules_exist_and_nonempty():
    assert DEFAULT_RULES_PATH.exists()
    text = load_rules()
    assert text
    assert "401" in text
    assert "codex_only" in text


def test_load_custom_path(tmp_path):
    path = tmp_path / "my-rules.md"
    path.write_text("自定义规则", encoding="utf-8")
    assert load_rules(path) == "自定义规则"


def test_missing_rules_returns_empty(tmp_path):
    assert load_rules(tmp_path / "none.md") == ""
    assert load_rules() != ""  # 默认规则库随程序发布
