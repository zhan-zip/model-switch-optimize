"""stats 模块测试：记录聚合、滚动窗口、样本门槛、批量经验分。"""
from model_switch.stats import (
    DEFAULT_STATS_PATH,
    MIN_SAMPLES,
    WINDOW,
    all_summaries,
    load_stats,
    record_run,
    save_stats,
    summary,
)


def test_default_path_is_data_stats_json():
    assert DEFAULT_STATS_PATH.as_posix() == "data/stats.json"


def test_record_and_summary_roundtrip(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", True, 100, 0, path)
    record_run("a/g1/m1", True, 200, 0, path)
    record_run("a/g1/m1", False, 0, 1, path)
    exp = summary("a/g1/m1", path=path)
    assert exp == {"n": 3, "ok": 2, "rate": 67, "avg_latency_ms": 150}


def test_summary_below_min_samples_returns_none(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", True, 100, 0, path)
    record_run("a/g1/m1", True, 100, 0, path)
    assert summary("a/g1/m1", path=path) is None  # 2 < 3
    record_run("a/g1/m1", True, 100, 0, path)
    assert summary("a/g1/m1", path=path) is not None  # 3 >= 3


def test_summary_all_failed_uses_all_latencies(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", False, 300, 0, path)
    record_run("a/g1/m1", False, 300, 0, path)
    record_run("a/g1/m1", False, 300, 0, path)
    exp = summary("a/g1/m1", path=path)
    assert exp is not None
    assert exp["rate"] == 0
    assert exp["avg_latency_ms"] == 300  # 无成功样本时用全部耗时


def test_record_rolls_window(tmp_path):
    path = tmp_path / "stats.json"
    for i in range(WINDOW + 5):
        record_run("a/g1/m1", True, 100 + i, 0, path)
    entry = load_stats(path)["a/g1/m1"]["recent"]
    assert len(entry) == WINDOW  # 滚动窗口裁剪
    # 保留最近 WINDOW 次（100+5 .. 100+24）；v2 记录为对象，latency 走字段
    assert entry[0]["latency_ms"] == 105
    assert entry[-1]["latency_ms"] == 100 + WINDOW + 4


def test_load_stats_missing_or_corrupt(tmp_path):
    assert load_stats(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("不是 JSON", encoding="utf-8")
    assert load_stats(bad) == {}
    bad.write_text("[1]", encoding="utf-8")
    assert load_stats(bad) == {}  # 非 dict
    # 坏条目跳过、好条目保留
    good = tmp_path / "mixed.json"
    save_stats(
        {"a/g1/m1": {"recent": [[True, 10, 0]]}, "bad": "not-a-dict", "x": {"recent": "junk"}},
        good,
    )
    stats = load_stats(good)
    assert set(stats) == {"a/g1/m1"}


def test_summary_from_loaded_stats_dict(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(4):
        record_run("a/g1/m2", True, 50, 0, path)
    stats = load_stats(path)
    assert summary("a/g1/m2", stats=stats)["n"] == 4
    assert summary("a/g1/m1", stats=stats) is None  # 无记录


def test_all_summaries_batch(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(3):
        record_run("a/g1/m1", True, 10, 0, path)
    record_run("a/g1/m2", True, 10, 0, path)  # 样本不足
    rows = all_summaries(["a/g1/m1", "a/g1/m2", "a/g1/m3"], path=path)
    assert rows["a/g1/m1"] is not None
    assert rows["a/g1/m2"] is None
    assert rows["a/g1/m3"] is None


def test_min_samples_constant():
    assert MIN_SAMPLES == 3
    assert WINDOW == 20
