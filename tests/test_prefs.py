"""prefs 模块测试：model_prefs.md 读写与任务偏好匹配。"""
from model_switch.prefs import (
    DEFAULT_PREFS_PATH,
    add_task_pref,
    load_prefs,
    match_task_pref,
    save_prefs,
)


def test_load_missing_file_returns_empty(tmp_path):
    prefs = load_prefs(tmp_path / "none.md")
    assert prefs == {"model_labels": {}, "task_prefs": {}}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "model_prefs.md"
    save_prefs(
        {"model_labels": {"a/g1/m1": "推理、代码"}, "task_prefs": {"写爬虫": "b/g1/m3"}},
        path,
    )
    prefs = load_prefs(path)
    assert prefs["model_labels"]["a/g1/m1"] == "推理、代码"
    assert prefs["task_prefs"]["写爬虫"] == "b/g1/m3"


def test_load_ignores_headers_separators_and_junk(tmp_path):
    path = tmp_path / "model_prefs.md"
    path.write_text(
        "# 模型标签偏好\n"
        "\n"
        "## 模型标签\n"
        "\n"
        "| 模型 | 标签 |\n"
        "| --- | --- |\n"
        "| a/g1/m1 | 推理 |\n"
        "\n"
        "## 任务偏好\n"
        "\n"
        "| 任务关键词 | 模型 |\n"
        "| --- | --- |\n"
        "| 写爬虫 | b/g1/m3 |\n"
        "| 缺列 |\n"
        "|  | 空 |\n",
        encoding="utf-8",
    )
    prefs = load_prefs(path)
    assert prefs["model_labels"] == {"a/g1/m1": "推理"}
    assert prefs["task_prefs"] == {"写爬虫": "b/g1/m3"}


def test_add_task_pref_preserves_labels(tmp_path):
    path = tmp_path / "model_prefs.md"
    save_prefs({"model_labels": {"a/g1/m1": "推理"}, "task_prefs": {}}, path)
    add_task_pref("翻译文档", "a/g1/m1", path)
    prefs = load_prefs(path)
    assert prefs["model_labels"]["a/g1/m1"] == "推理"
    assert prefs["task_prefs"]["翻译文档"] == "a/g1/m1"


def test_match_task_pref_substring_and_longest_wins(tmp_path):
    path = tmp_path / "model_prefs.md"
    save_prefs({"model_labels": {}, "task_prefs": {"爬虫": "a/g1/m1", "写爬虫脚本": "b/g1/m3"}}, path)
    assert match_task_pref("帮我写爬虫脚本", prefs=None, path=path) == "b/g1/m3"
    assert match_task_pref("抓取网页爬虫", prefs=None, path=path) == "a/g1/m1"


def test_match_task_pref_no_match_returns_none(tmp_path):
    prefs = {"model_labels": {}, "task_prefs": {"爬虫": "a/g1/m1"}}
    assert match_task_pref("写个翻译", prefs=prefs) is None


def test_default_path_is_config_model_prefs():
    assert DEFAULT_PREFS_PATH.as_posix() == "config/model_prefs.md"
