"""prefs 模块测试：model_prefs.md 读写、任务偏好匹配、切换偏好沉淀。"""
from model_switch.prefs import (
    DEFAULT_PREFS_PATH,
    add_plan_pref,
    add_task_pref,
    load_prefs,
    match_plan_pref,
    match_task_pref,
    save_prefs,
)


def test_load_missing_file_returns_empty(tmp_path):
    prefs = load_prefs(tmp_path / "none.md")
    assert prefs == {"model_labels": {}, "task_prefs": {}, "plan_prefs": {}}


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


# -- 切换偏好（plan_prefs）--------------------------------------------


def test_plan_prefs_roundtrip(tmp_path):
    path = tmp_path / "model_prefs.md"
    save_prefs(
        {
            "model_labels": {"a/g1/m1": "推理、代码"},
            "task_prefs": {},
            "plan_prefs": {"401": ("a/g1/m2", 3), "429": ("b/g1/m3", 1)},
        },
        path,
    )
    prefs = load_prefs(path)
    assert prefs["plan_prefs"]["401"] == ("a/g1/m2", 3)
    assert prefs["plan_prefs"]["429"] == ("b/g1/m3", 1)
    # 旧两区块不受影响
    assert prefs["model_labels"]["a/g1/m1"] == "推理、代码"


def test_plan_prefs_bad_hits_column_defaults_to_one(tmp_path):
    path = tmp_path / "model_prefs.md"
    path.write_text(
        "## 切换偏好\n\n| 错误类别 | 切换目标 | 命中次数 |\n"
        "| --- | --- | --- |\n| 401 | a/g1/m2 | abc |\n| 429 | b/g1/m3 |\n",
        encoding="utf-8",
    )
    prefs = load_prefs(path)
    assert prefs["plan_prefs"]["401"] == ("a/g1/m2", 1)  # 非整数列回退 1
    assert prefs["plan_prefs"]["429"] == ("b/g1/m3", 1)  # 缺第三列回退 1


def test_add_plan_pref_accumulates_same_target(tmp_path):
    path = tmp_path / "model_prefs.md"
    add_plan_pref("401", "a/g1/m2", path)
    add_plan_pref("401", "a/g1/m2", path)
    prefs = load_prefs(path)
    assert prefs["plan_prefs"]["401"] == ("a/g1/m2", 2)


def test_add_plan_pref_resets_hits_on_target_change(tmp_path):
    path = tmp_path / "model_prefs.md"
    add_plan_pref("401", "a/g1/m2", path)
    add_plan_pref("401", "a/g1/m2", path)
    add_plan_pref("401", "b/g1/m3", path)  # 换目标视为修正，重置为 1
    prefs = load_prefs(path)
    assert prefs["plan_prefs"]["401"] == ("b/g1/m3", 1)


def test_add_plan_pref_preserves_other_sections(tmp_path):
    path = tmp_path / "model_prefs.md"
    save_prefs({"model_labels": {"a/g1/m1": "推理"}, "task_prefs": {"爬虫": "a/g1/m1"}, "plan_prefs": {}}, path)
    add_plan_pref("429", "b/g1/m3", path)
    prefs = load_prefs(path)
    assert prefs["model_labels"]["a/g1/m1"] == "推理"
    assert prefs["task_prefs"]["爬虫"] == "a/g1/m1"
    assert prefs["plan_prefs"]["429"] == ("b/g1/m3", 1)


def test_match_plan_pref_hit_and_miss(tmp_path):
    path = tmp_path / "model_prefs.md"
    save_prefs(
        {"model_labels": {}, "task_prefs": {}, "plan_prefs": {"429": ("a/g1/m2", 2)}}, path
    )
    assert match_plan_pref("429", path=path) == ("a/g1/m2", 2)
    assert match_plan_pref("401", path=path) is None


def test_match_plan_pref_from_loaded_prefs():
    prefs = {"model_labels": {}, "task_prefs": {}, "plan_prefs": {}}
    assert match_plan_pref("401", prefs=prefs) is None
