"""两阶段审批测试：prepare / execute 状态机、确认/拒绝/过期/重复/并发/覆盖、事件序列、三端接口。"""
import json
import re

from model_switch.client import CallResult
from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.history import FaultRecorder
from model_switch.mocker import MockConsole, MockDecisionClient, MockModelClient
from model_switch.model import ModelRegistry
from model_switch.pipeline import Pipeline
from model_switch.prefs import load_prefs, save_prefs
from model_switch.task_store import (
    AWAITING_CONFIRMATION,
    COMPLETED,
    DENIED,
    EXPIRED,
    FAILED,
    READY,
    RUNNING,
    load_task,
    save_task,
    update_task,
)
from model_switch.tools import Toolbox

from conftest import SequenceClient

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

CHOOSE = json.dumps(
    {"model": "a/g1/m1", "reason": "合适", "task_type": "code"}, ensure_ascii=False
)


def _toolbox(tmp_path, stream=None, client=None):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    return Toolbox(
        config, ModelRegistry(config), stream,
        model_client=client or MockModelClient(), console=MockConsole(), mock=True,
    )


def _pipeline(box, tmp_path, **kwargs):
    kwargs.setdefault("prefs_path", tmp_path / "prefs.md")
    kwargs.setdefault("pause_path", tmp_path / "pause.json")
    kwargs.setdefault("stats_path", tmp_path / "stats.json")
    kwargs.setdefault("tasks_dir", tmp_path / "tasks")
    return Pipeline(
        box,
        recorder=FaultRecorder(None, faults_dir=tmp_path / "faults"),
        probe_dir=tmp_path / "probe",
        **kwargs,
    )


# -- prepare 状态判定 -------------------------------------------------


def test_prepare_confirm_never_returns_ready(tmp_path):
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})  # 决策者（保底 m3）返回带 task_type
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="never")

    assert info["ok"] is True
    assert info["status"] == READY
    assert info["selected_model"] == "a/g1/m1"
    assert info["task_type"] == "code"  # 决策 task_type 透传
    assert re.match(r"^task-[0-9a-f]{12}$", info["task_id"])
    assert info["expires_at"]
    # 落盘 + 事件
    record = load_task(info["task_id"], tmp_path / "tasks")
    assert record is not None and record["status"] == READY
    types = [e.type for e in stream]
    assert EventType.TASK_PREPARED in types
    prepared = next(e for e in stream if e.type == EventType.TASK_PREPARED)
    assert prepared.data["task_id"] == info["task_id"]
    assert prepared.data["task_type"] == "code"


def test_prepare_confirm_always_awaits(tmp_path):
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="always")
    assert info["status"] == AWAITING_CONFIRMATION


def test_prepare_once_no_pref_awaits(tmp_path):
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="once")
    assert info["status"] == AWAITING_CONFIRMATION


def test_prepare_once_pref_hit_ready(tmp_path):
    """once 且任务偏好命中 -> ready（自动批准，execute 免确认）。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    save_prefs(
        {"model_labels": {}, "task_prefs": {"爬虫": "a/g1/m2"}, "plan_prefs": {}}, prefs_path
    )
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path, prefs_path=prefs_path)

    info = pipeline.prepare("写个爬虫抓数据", confirm_mode="once")
    assert info["status"] == READY


def test_prepare_decision_all_failed_ready_empty_model(tmp_path):
    """决策全灭：task 备为 ready + selected_model=""，execute 时机械兜底。"""
    stream = EventStream()
    client = MockModelClient(rules={"m3": "401"})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="never")
    assert info["ok"] is True
    assert info["status"] == READY
    assert info["selected_model"] == ""

    result = pipeline.execute(info["task_id"])
    assert result.ok is True
    assert result.outcome == "fallback"
    assert result.model_ref == "a/g1/m1"  # 机械兜底清单序首个


# -- execute 状态转移 -------------------------------------------------


def test_execute_ready_runs_without_confirm(tmp_path):
    """ready（自动批准）任务：execute 无需 user_confirmed 直接执行。"""
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})  # 决策返回选型 JSON；任务调用成功
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="never")
    result = pipeline.execute(info["task_id"])

    assert result.ok is True
    assert result.outcome == "ok"
    assert result.model_ref == "a/g1/m1"
    assert result.task_type == "code"
    # 终态 + 事件序列（task_prepared -> （无 approved，ready 直接跑）-> finished）
    record = load_task(info["task_id"], tmp_path / "tasks")
    assert record is not None and record["status"] == COMPLETED
    types = [e.type for e in stream]
    assert EventType.TASK_PREPARED in types
    assert EventType.TASK_APPROVED not in types  # ready 自动批准不发 approved
    assert EventType.PIPELINE_FINISHED in types


def test_execute_awaiting_denied_without_confirm(tmp_path):
    """awaiting_confirmation 未确认 -> denied：任务不执行、终态、事件留痕。"""
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="always")
    result = pipeline.execute(info["task_id"], user_confirmed=False)

    assert result.ok is False
    assert result.error_class == "denied"
    assert result.outcome == "denied"
    assert result.text  # 拒绝说明
    record = load_task(info["task_id"], tmp_path / "tasks")
    assert record["status"] == DENIED
    types = [e.type for e in stream]
    assert EventType.TASK_DENIED in types
    assert EventType.TASK_APPROVED not in types
    # 拒绝后不可重复执行
    again = pipeline.execute(info["task_id"], user_confirmed=True)
    assert again.ok is False and again.error_class == "not_reexecutable"


def test_execute_awaiting_confirm_runs_and_settles_pref(tmp_path):
    """awaiting + user_confirmed=true -> approved -> 执行；once 批准后沉淀任务偏好。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    save_prefs({"model_labels": {}, "task_prefs": {"爬虫": "a/g1/m2"}, "plan_prefs": {}}, prefs_path)
    client = MockModelClient(rules={"m3": CHOOSE})  # 决策返回选型 JSON；任务成功
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path, prefs_path=prefs_path)

    # once：新任务无偏好命中 -> awaiting；确认后执行并沉淀偏好
    info = pipeline.prepare("写个代码生成器", confirm_mode="once")
    assert info["status"] == AWAITING_CONFIRMATION
    result = pipeline.execute(info["task_id"], user_confirmed=True)
    assert result.ok is True

    types = [e.type for e in stream]
    assert EventType.TASK_APPROVED in types
    approved = next(e for e in stream if e.type == EventType.TASK_APPROVED)
    assert approved.data["task_id"] == info["task_id"]
    # once 确认成功后沉淀任务偏好
    prefs = load_prefs(prefs_path)
    assert prefs["task_prefs"]["写个代码生成器"] == "a/g1/m1"


def test_execute_expired_denied(tmp_path):
    """过期任务：task_expired 事件 + 终态 + 拒绝执行。"""
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)
    tasks_dir = tmp_path / "tasks"

    info = pipeline.prepare("写个爬虫", confirm_mode="always")
    # 手工把到期时间改到过去
    from datetime import datetime, timedelta, timezone

    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    update_task(info["task_id"], {"expires_at": expired_at}, tasks_dir)

    result = pipeline.execute(info["task_id"], user_confirmed=True)
    assert result.ok is False
    assert result.error_class == "task_expired"
    record = load_task(info["task_id"], tasks_dir)
    assert record["status"] == EXPIRED
    types = [e.type for e in stream]
    assert EventType.TASK_EXPIRED in types


def test_execute_not_found(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, client=MockModelClient())
    pipeline = _pipeline(box, tmp_path)
    result = pipeline.execute("task-" + "f" * 12)
    assert result.ok is False
    assert result.error_class == "not_found"


def test_execute_reexecute_rejected(tmp_path):
    """已完成的同一 task_id 不能重复执行（终态保护）。"""
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, client=MockModelClient())
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="never")
    first = pipeline.execute(info["task_id"])
    assert first.ok is True
    second = pipeline.execute(info["task_id"], user_confirmed=True)
    assert second.ok is False
    assert second.error_class == "not_reexecutable"


def test_execute_running_concurrency_protection(tmp_path):
    """running 状态：并发/重复执行保护（同一 task_id 拒绝二次进入）。"""
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, client=MockModelClient())
    pipeline = _pipeline(box, tmp_path)
    tasks_dir = tmp_path / "tasks"

    info = pipeline.prepare("写个爬虫", confirm_mode="never")
    update_task(info["task_id"], {"status": RUNNING}, tasks_dir)  # 模拟另一进程已进入执行

    result = pipeline.execute(info["task_id"])
    assert result.ok is False
    assert result.error_class == "already_running"


def test_execute_model_override_invalid(tmp_path):
    """覆盖模型不在清单 / 不可用 / 暂停中 -> 校验失败，任务不执行。"""
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, client=MockModelClient())
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="never")
    r1 = pipeline.execute(info["task_id"], model_override="不存在/模型/x")
    assert r1.ok is False and r1.error_class == "invalid_override"
    # 覆盖一个暂停中模型
    box.registry.find("a/g1/m2").pause(60)
    r2 = pipeline.execute(info["task_id"], model_override="a/g1/m2")
    assert r2.ok is False and r2.error_class == "invalid_override"


def test_execute_model_override_valid(tmp_path):
    """覆盖模型通过校验 -> 用覆盖模型执行。"""
    stream = EventStream()
    client = MockModelClient()
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="never")
    result = pipeline.execute(info["task_id"], model_override="a/g1/m2")
    assert result.ok is True
    assert result.model_ref == "a/g1/m2"


def test_execute_fault_switch_keeps_working(tmp_path):
    """已确认模型故障：自动切换保障任务完成（不再次确认）。"""
    stream = EventStream()
    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="a/g1/m2", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="always")
    result = pipeline.execute(info["task_id"], user_confirmed=True)
    assert result.ok is True
    assert result.outcome == "switched"
    assert result.model_ref == "a/g1/m2"
    assert EventType.TASK_APPROVED in [e.type for e in stream]


# -- task_type 透传与统计 -------------------------------------------------


def test_execute_task_type_persisted_and_recorded(tmp_path):
    """prepare 决策 task_type=code：execute 透传到 RunResult 与 stats 记录。"""
    stream = EventStream()
    stats_path = tmp_path / "stats.json"
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path, stats_path=stats_path)

    info = pipeline.prepare("写个爬虫", confirm_mode="never")
    assert info["task_type"] == "code"
    result = pipeline.execute(info["task_id"])
    assert result.task_type == "code"
    from model_switch.stats import load_stats

    stats = load_stats(stats_path)
    assert stats["a/g1/m1"]["recent"][0]["task_type"] == "code"


def test_run_result_default_task_type_general(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, client=MockModelClient())
    result = _pipeline(box, tmp_path).run("写个爬虫")
    assert result.task_type == "general"


# -- switcher / MCP / CLI 三端 -------------------------------------------------


def test_switcher_prepare_execute_flow(tmp_path):
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", persist_events=False,
    )
    info = switcher.prepare("写个爬虫")
    assert info["status"] == READY
    result = switcher.execute(info["task_id"])
    assert result.ok is True
    assert result.task_type == "general"


def test_mcp_prepare_run_and_execute_run(tmp_path):
    from model_switch.mcp import build_mcp_server
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", persist_events=False,
    )
    server = build_mcp_server(switcher)

    import asyncio

    def _call(name, arguments=None):
        raw = asyncio.run(server.call_tool(name, arguments or {}))
        if isinstance(raw, tuple) and len(raw) == 2:
            content = raw[0]
            text = "".join(getattr(item, "text", str(item)) for item in content)
            return json.loads(text)
        if isinstance(raw, str):
            return json.loads(raw)
        return json.loads("".join(getattr(item, "text", str(item)) for item in raw))

    info = _call("prepare_run", {"task": "写个爬虫"})
    assert info["ok"] is True
    assert info["status"] == "ready"
    assert info["task_id"]

    result = _call("execute_run", {"task_id": info["task_id"]})
    assert result["ok"] is True
    assert result["task_type"] == "general"

    # awaiting 任务未确认 -> denied
    info2 = _call("prepare_run", {"task": "写个报告", "confirm_mode": "always"})
    assert info2["status"] == "awaiting_confirmation"
    denied = _call("execute_run", {"task_id": info2["task_id"], "user_confirmed": False})
    assert denied["ok"] is False
    assert denied["error_class"] == "denied"


def test_cli_cross_process_prepare_execute(tmp_path, monkeypatch, capsys):
    """跨进程 CLI：prepare 落盘 -> 新进程 execute 读取执行。"""
    from model_switch.cli import main

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")

    # 进程一：prepare
    assert main(["prepare", "写个爬虫", "--mock", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    task_id = output["task_id"]
    assert (tmp_path / "data/tasks" / f"{task_id}.json").exists()

    # 进程二：execute（跨进程继续执行）
    assert main(["execute", task_id, "--confirm", "--mock", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    finished = next(e for e in events if e["type"] == "pipeline_finished")
    assert finished["data"]["task_type"]  # execute 结果带 task_type


def test_cli_prepare_human_output_guidance(tmp_path, monkeypatch, capsys):
    from model_switch.cli import main

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")

    assert main(["prepare", "写个爬虫", "--mock"]) == 0
    output = capsys.readouterr().out
    assert "task_id" in output
    assert "mso execute" in output

    assert main(["execute", "task-missing-id", "--mock"]) == 1
    output = capsys.readouterr().out
    assert "任务不存在" in output


def test_cli_denied_human_output(tmp_path, monkeypatch, capsys):
    from model_switch.cli import main

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")

    assert main(["prepare", "写个爬虫", "--mock", "--confirm", "always"]) == 0
    output = capsys.readouterr().out
    import re as _re

    task_id = _re.search(r"task-[0-9a-f]{12}", output).group(0)
    assert main(["execute", task_id, "--mock"]) == 1
    output = capsys.readouterr().out
    assert "已拒绝" in output