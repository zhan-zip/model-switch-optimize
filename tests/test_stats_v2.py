"""阶段2 统计与健康 v2 测试：v2 格式写盘、旧格式迁移、健康聚合/分级、宿主接口。"""
import json as _json

import pytest

from model_switch.config import load_config
from model_switch.model import ModelRegistry
from model_switch.stats import (
    STATS_VERSION,
    health_status,
    health_summary,
    load_stats,
    record_run,
    save_stats,
    summary,
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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# -- v2 磁盘格式写盘 ------------------------------------------------------


def test_record_writes_v2_object_and_version(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", True, 820, 0, path, protocol="openai_chat",
               error_class=None, input_tokens=210, output_tokens=407, total_tokens=617,
               call_kind="task", task_type="general")
    raw = _json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == STATS_VERSION == 2
    record = raw["models"]["a/g1/m1"]["recent"][0]
    assert record["ok"] is True
    assert record["latency_ms"] == 820
    assert record["switches"] == 0
    assert record["error_class"] is None
    assert record["input_tokens"] == 210
    assert record["output_tokens"] == 407
    assert record["total_tokens"] == 617
    assert record["call_kind"] == "task"
    assert record["protocol"] == "openai_chat"
    assert record["task_type"] == "general"
    assert record["ts"]


def test_record_token_missing_stored_as_none(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", True, 100, 0, path)
    stats = load_stats(path)
    rec = stats["a/g1/m1"]["recent"][0]
    assert rec["input_tokens"] is None
    assert rec["output_tokens"] is None
    assert rec["total_tokens"] is None
    assert rec["error_class"] is None


def test_record_failure_stores_error_class(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", False, 100, 1, path, call_kind="task", error_class="401")
    rec = load_stats(path)["a/g1/m1"]["recent"][0]
    assert rec["ok"] is False
    assert rec["error_class"] == "401"
    assert rec["switches"] == 1


def test_save_stats_upgrades_legacy_arrays_to_v2(tmp_path):
    path = tmp_path / "legacy.json"
    # 旧数组格式（v1），保存应升级为 v2 顶层
    path.write_text('{"a/g1/m1": {"recent": [[true, 100, 0]]}}', encoding="utf-8")
    stats = load_stats(path)
    save_stats(stats, tmp_path / "out.json")
    raw = _json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert raw["models"]["a/g1/m1"]["recent"][0]["ok"] is True
    assert raw["models"]["a/g1/m1"]["recent"][0]["latency_ms"] == 100


def test_load_v2_roundtrip(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(3):
        record_run("a/g1/m1", True, 50, 0, path, protocol="openai_chat")
    stats = load_stats(path)
    assert len(stats["a/g1/m1"]["recent"]) == 3
    assert stats["a/g1/m1"]["recent"][-1]["protocol"] == "openai_chat"


def test_summary_compatible_with_v1_arrays(tmp_path):
    # summary 兼容旧数组记录
    path = tmp_path / "s.json"
    save_stats({"a/g1/m1": {"recent": [[True, 100, 0], [True, 200, 0], [False, 300, 0]]}}, path)
    exp = summary("a/g1/m1", path=path)
    assert exp == {"n": 3, "ok": 2, "rate": 67, "avg_latency_ms": 150}


# -- 调用用途隔离 --------------------------------------------------------


def test_call_kind_task_probe_isolated(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(3):
        record_run("a/g1/m1", True, 100, 0, path, call_kind="task")
    record_run("a/g1/m1", False, 200, 0, path, call_kind="probe", error_class="429")
    # 任务摘要只看 task（成功 3 次）
    task_summary = health_summary("a/g1/m1", path=path, call_kind="task")
    assert task_summary["n"] == 3
    assert task_summary["ok_n"] == 3
    assert task_summary["rate"] == 100
    # probe 记录单独存在（放宽样本门槛 1）
    probe_summary = health_summary("a/g1/m1", path=path, call_kind="probe", min_samples=1)
    assert probe_summary is not None
    assert probe_summary["n"] == 1
    assert probe_summary["ok_n"] == 0
    assert probe_summary["error_distribution"] == {"429": 1}
    # 最近探测结果
    assert task_summary["last_probe"] == {"ok": False, "ts": probe_summary["last_call_ts"]}


# -- 健康聚合 ------------------------------------------------------------


def test_health_summary_fields(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", True, 100, 0, path, call_kind="task", error_class=None,
               input_tokens=10, output_tokens=20, total_tokens=30)
    record_run("a/g1/m1", True, 200, 0, path, call_kind="task", error_class=None,
               input_tokens=20, output_tokens=30, total_tokens=50)
    record_run("a/g1/m1", False, 300, 0, path, call_kind="task", error_class="429")
    h = health_summary("a/g1/m1", path=path)
    assert h["n"] == 3
    assert h["ok_n"] == 2
    assert h["fail_n"] == 1
    assert h["rate"] == 67
    assert h["consecutive_fail"] == 1
    assert h["error_distribution"] == {"429": 1}
    assert h["avg_latency_ms"] == 200  # 3 次均值
    assert h["p50_latency_ms"] == 100  # 成功样本 [100,200]，lower median 取 100
    assert h["p95_latency_ms"] == 200
    assert h["tokens"]["total_total"] == 80
    assert h["tokens"]["missing_count"] == 1  # 失败调用无 Token -> 缺失计数 1
    assert h["data_completeness"] == 67  # (3-1)/3
    assert h["last_call_ts"]
    assert h["last_ok_ts"]


def test_health_summary_rolling_window_and_call_kind_only(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", True, 100, 0, path, call_kind="probe")  # probe 不计入 task
    h = health_summary("a/g1/m1", path=path, call_kind="task")
    assert h is None  # task 样本 <3
    h2 = health_summary("a/g1/m1", path=path, call_kind="probe", min_samples=1)
    assert h2["n"] == 1


# -- 健康分级（10.5 节阈值） ---------------------------------------------


def test_health_status_unknown_below_min_samples(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", True, 100, 0, path)
    assert health_status("a/g1/m1", path=path) == "unknown"


def test_health_status_healthy(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(6):
        record_run("a/g1/m1", True, 100, 0, path)
    assert health_status("a/g1/m1", path=path) == "healthy"


def test_health_status_unhealthy_consecutive_fail_3(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(3):
        record_run("a/g1/m1", False, 300, 0, path, error_class="429")
    assert health_status("a/g1/m1", path=path) == "unhealthy"


def test_health_status_unhealthy_last_error_401(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(3):
        record_run("a/g1/m1", True, 100, 0, path)
    record_run("a/g1/m1", False, 100, 0, path, error_class="401")  # 最近错误 401 未恢复
    assert health_status("a/g1/m1", path=path) == "unhealthy"


def test_health_status_unhealthy_low_rate(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(4):
        record_run("a/g1/m1", False, 100, 0, path, error_class="5xx")
    record_run("a/g1/m1", True, 100, 0, path)
    # 5/6 -> rate 17% < 50%
    assert health_status("a/g1/m1", path=path) == "unhealthy"


def test_health_status_degraded_low_rate_429(tmp_path):
    path = tmp_path / "stats.json"
    record_run("a/g1/m1", False, 100, 0, path, error_class="429")
    record_run("a/g1/m1", False, 100, 0, path, error_class="429")
    for _ in range(4):
        record_run("a/g1/m1", True, 100, 0, path)
    # 4/6 -> rate 67% < 80% -> degraded
    assert health_status("a/g1/m1", path=path) == "degraded"


def test_health_status_healthy_ignores_probe(tmp_path):
    path = tmp_path / "stats.json"
    for _ in range(3):
        record_run("a/g1/m1", True, 100, 0, path, call_kind="task")
    for _ in range(3):
        record_run("a/g1/m1", False, 100, 0, path, call_kind="probe", error_class="429")
    assert health_status("a/g1/m1", path=path) == "healthy"  # 只看 task


# -- 宿主接口：ModelSwitcher.health ----------------------------------------


def test_switcher_health_all_models(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_A", "sk-a")
    monkeypatch.setenv("KEY_B", "sk-b")
    from model_switch import ModelSwitcher
    from model_switch.stats import record_run

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        persist_events=False,
    )
    stats_path = switcher.pipeline.stats_path
    for _ in range(3):
        record_run("a/g1/m1", True, 100, 0, stats_path, call_kind="task")
    result = switcher.health()
    assert set(result) == {"a/g1/m1", "a/g1/m2", "b/g1/m3"}
    assert result["a/g1/m1"]["health"] == "healthy"
    assert result["a/g1/m1"]["summary"]["rate"] == 100
    assert result["a/g1/m2"]["summary"] is None
    assert result["a/g1/m2"]["health"] == "unknown"

    single = switcher.health("a/g1/m1")
    assert set(single) == {"a/g1/m1"}


def test_switcher_health_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_A", "sk-a")
    monkeypatch.setenv("KEY_B", "sk-b")
    from model_switch import ModelSwitcher
    from model_switch.stats import record_run

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        persist_events=False,
    )
    stats_path = switcher.pipeline.stats_path
    for _ in range(3):
        record_run("a/g1/m1", True, 100, 0, stats_path, call_kind="task")
    switcher.registry.find("a/g1/m1").pause(60)
    result = switcher.health("a/g1/m1")
    assert result["a/g1/m1"]["health"] == "paused"
    assert result["a/g1/m1"]["paused"] is True


# -- 宿主接口：MCP model_health 与 CLI stats --json -------------------------


def test_mcp_model_health_tool(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("KEY_A", "sk-a")
    monkeypatch.setenv("KEY_B", "sk-b")
    from model_switch import ModelSwitcher
    from model_switch.mcp import build_mcp_server
    from model_switch.stats import record_run

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        persist_events=False,
    )
    stats_path = switcher.pipeline.stats_path
    for _ in range(3):
        record_run("a/g1/m1", True, 100, 0, stats_path, call_kind="task")
    server = build_mcp_server(switcher)
    tools = asyncio.run(server.list_tools())
    assert "model_health" in {t.name for t in tools}
    payload = asyncio.run(server.call_tool("model_health", {}))
    raw = "".join(getattr(item, "text", str(item)) for item in payload[0])
    data = _json.loads(raw)
    assert data["a/g1/m1"]["health"] == "healthy"


def test_cli_stats_json(workspace, capsys):
    from model_switch.cli import main

    main(["init"])
    capsys.readouterr()
    for _ in range(3):
        record_run("provider-a/default/gpt-4o", True, 123, 0, call_kind="task")
    assert main(["stats", "--json"]) == 0

    out = _json.loads(capsys.readouterr().out)
    assert out["version"] == 2
    assert out["models"]["provider-a/default/gpt-4o"]["health"] == "healthy"
    assert out["models"]["provider-a/default/gpt-4o"]["summary"]["rate"] == 100


def test_stats_json_empty(workspace, capsys):
    from model_switch.cli import main

    main(["init"])
    capsys.readouterr()
    assert main(["stats", "--json"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["version"] == 2
    # 全部模型 summary=None health=unknown
    for ref, data in out["models"].items():
        assert data["summary"] is None
        assert data["health"] == "unknown"


# -- T3：Toolbox 探测统计链路（真实模式写 probe；mock 零污染） --------------


def test_toolbox_probe_writes_stats_real_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_A", "sk-a")
    monkeypatch.setenv("KEY_B", "sk-b")
    import model_switch.protocols.registry as reg

    from model_switch.protocols.base import ModelResponse, Usage

    class FakeAdapter:
        def call(self, base_url, api_key, request, **kwargs):
            return ModelResponse(ok=True, text="pong", latency_ms=10, usage=Usage())

    orig = reg._REGISTRY.copy()
    reg._REGISTRY["openai_chat"] = FakeAdapter
    try:
        path = tmp_path / "models.yaml"
        path.write_text(VALID_YAML, encoding="utf-8")
        config = load_config(path)
        registry = ModelRegistry(config)
        stats_path = tmp_path / "stats.json"
        from model_switch.events import EventStream
        from model_switch.tools import Toolbox

        toolbox = Toolbox(
            config, registry, EventStream(), mock=False, stats_path=stats_path,
        )
        toolbox.test_connectivity("a/g1/m1")
        stats = load_stats(stats_path)
        rec = stats["a/g1/m1"]["recent"][-1]
        assert rec["call_kind"] == "probe"
        assert rec["ok"] is True
        assert rec["protocol"] == "openai_chat"
        assert rec["error_class"] is None
    finally:
        reg._REGISTRY.clear()
        reg._REGISTRY.update(orig)


def test_toolbox_probe_skips_stats_in_mock_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_A", "sk-a")
    monkeypatch.setenv("KEY_B", "sk-b")
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)
    stats_path = tmp_path / "stats.json"
    from model_switch.events import EventStream
    from model_switch.mocker import MockModelClient
    from model_switch.tools import Toolbox

    toolbox = Toolbox(
        config, registry, EventStream(), mock=True,
        model_client=MockModelClient(), stats_path=stats_path,
    )
    toolbox.test_connectivity("a/g1/m1")
    assert not stats_path.exists()  # mock 探测不写统计（零污染）


# -- T6：manager 选型注入健康列 -------------------------------------------


def test_manager_choose_model_injects_health_column(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY_A", "sk-a")
    monkeypatch.setenv("KEY_B", "sk-b")
    from model_switch.manager import DecisionManager
    from model_switch.tools import Toolbox

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)
    stats_path = tmp_path / "stats.json"
    for _ in range(3):
        record_run("a/g1/m1", True, 100, 0, stats_path, call_kind="task")
    toolbox = Toolbox(
        config, registry, None, mock=True, stats_path=stats_path,
    )
    manager = DecisionManager(toolbox, prefs_path=tmp_path / "prefs.md", stats_path=stats_path)
    from model_switch.prefs import load_prefs

    prefs = {"model_labels": {}}
    table = manager._model_table(prefs)
    assert "健康: status=healthy" in table  # m1 有健康列
    assert "健康" not in "\n".join(
        line for line in table.splitlines() if "a/g1/m2" in line
    )  # m2 无样本不显示健康列