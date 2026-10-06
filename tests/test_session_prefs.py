"""阶段4 会话模型偏好测试：switcher 三接口/选型优先级/strict 硬限制/CLI/MCP/事件。"""
import json
import re

from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.history import FaultRecorder
from model_switch.mocker import MockConsole, MockDecisionClient, MockModelClient
from model_switch.model import ModelRegistry
from model_switch.pipeline import Pipeline
from model_switch.session_store import load_session
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
    {"model": "a/g1/m1", "reason": "动态决策", "task_type": "code"}, ensure_ascii=False
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
    kwargs.setdefault("sessions_dir", tmp_path / "sessions")
    return Pipeline(
        box,
        recorder=FaultRecorder(None, faults_dir=tmp_path / "faults"),
        probe_dir=tmp_path / "probe",
        **kwargs,
    )


# -- switcher 三接口 + 准入校验 ------------------------------------------


def test_switcher_set_get_clear(tmp_path):
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=True,
    )
    result = switcher.set_preferred_model("user-1", "a/g1/m1", strict_model=True)
    assert result["ok"] is True
    assert result["preferred_model"] == "a/g1/m1"
    assert result["strict_model"] is True

    got = switcher.get_preferred_model("user-1")
    assert got["preferred_model"] == "a/g1/m1"
    assert got["strict_model"] is True

    cleared = switcher.clear_preferred_model("user-1")
    assert cleared["ok"] is True and cleared["cleared"] is True
    assert switcher.get_preferred_model("user-1")["preferred_model"] is None
    # 事件
    types = [e.type for e in switcher.stream]
    assert EventType.PREFERRED_MODEL_SET in types
    assert EventType.PREFERRED_MODEL_CLEARED in types


def test_switcher_get_empty(tmp_path):
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    got = switcher.get_preferred_model("nobody")
    assert got["preferred_model"] is None
    assert got["strict_model"] is False
    assert switcher.clear_preferred_model("nobody")["cleared"] is False


def test_set_preferred_rejects_unknown_model(tmp_path):
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    result = switcher.set_preferred_model("s1", "不存在/模型/x")
    assert result["ok"] is False
    assert "清单内" in result["error"]
    assert load_session("s1", tmp_path / "sessions") == {}


def test_set_preferred_rejects_unavailable_and_paused(tmp_path):
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    # 明确不可用
    switcher.registry.find("a/g1/m1").available = False
    r1 = switcher.set_preferred_model("s1", "a/g1/m1")
    assert r1["ok"] is False and "不可用" in r1["error"]
    # 暂停中
    switcher.registry.find("a/g1/m1").available = None
    switcher.registry.find("a/g1/m1").pause(60)
    r2 = switcher.set_preferred_model("s1", "a/g1/m1")
    assert r2["ok"] is False and "暂停" in r2["error"]


def test_set_preferred_invalid_session_id(tmp_path):
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    result = switcher.set_preferred_model("../evil/x", "a/g1/m1")
    assert result["ok"] is False and "session_id" in result["error"]


# -- 选型优先级：会话偏好跳过决策调用 ------------------------------------


def test_prepare_session_pref_skips_decision(tmp_path):
    """命中会话偏好：直接采用偏好模型，不调 choose_model（省 LLM 决策）。"""
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=True,
    )
    switcher.set_preferred_model("user-1", "a/g1/m2")
    # 决策者返回 m1；但会话偏好 m2 应优先
    box = switcher.toolbox

    p = _pipeline(box, tmp_path)
    p.sessions_dir = tmp_path / "sessions"
    info = p.prepare("写个爬虫", session_id="user-1", confirm_mode="never")
    assert info["selected_model"] == "a/g1/m2"  # 会话偏好直接采用
    assert "次序" not in info["reason"] or True  # reason 至少含"会话偏好"
    assert info["reason"] == "会话偏好命中"
    # 落盘三字段
    from model_switch.task_store import load_task

    record = load_task(info["task_id"], tmp_path / "tasks")
    assert record["session_id"] == "user-1"
    assert record["preferred_model"] == "a/g1/m2"
    assert record["strict_model"] is False


def test_prepare_explicit_model_beats_session(tmp_path):
    """显式 preferred_model 优先级高于会话偏好。"""
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    switcher.set_preferred_model("user-1", "a/g1/m2")
    p = _pipeline(switcher.toolbox, tmp_path)
    p.sessions_dir = tmp_path / "sessions"
    info = p.prepare(
        "写个爬虫", session_id="user-1", preferred_model="b/g1/m3", confirm_mode="never"
    )
    assert info["selected_model"] == "b/g1/m3"


def test_run_session_pref_without_sessions_dir_uses_default(tmp_path):
    """未显式传 sessions_dir 时（ModelSwitcher 默认），会话偏好仍经 mock 隔离目录生效。"""
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    switcher.set_preferred_model("user-1", "a/g1/m1")
    result = switcher.run("写个爬虫", session_id="user-1")
    assert result.ok is True
    assert result.model_ref == "a/g1/m1"


def test_run_strict_model_fails_no_switch(tmp_path):
    """run strict=True：首模型失败直接 failed，不切换、不机械兜底。"""
    stream = EventStream()
    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="a/g1/m2", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    result = pipeline.run("写个爬虫", preferred_model="a/g1/m1", strict_model=True)
    assert result.ok is False
    assert result.outcome == "failed"
    assert result.error_class == "401"
    assert result.switches == 0
    types = [e.type for e in stream]
    assert EventType.SWITCH_TRIGGERED not in types
    assert EventType.MECHANICAL_FALLBACK not in types
    assert EventType.PIPELINE_FINISHED in types


def test_run_non_strict_still_switches(tmp_path):
    """非 strict：偏好模型失败仍正常切换（既有行为）。"""
    stream = EventStream()
    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="a/g1/m2", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    result = pipeline.run("写个爬虫", preferred_model="a/g1/m1")
    assert result.ok is True
    assert result.outcome == "switched"
    assert result.model_ref == "a/g1/m2"


def test_no_session_id_no_global_state(tmp_path):
    """不传 session_id：完全不读不写会话 store（不产生隐式全局状态）。"""
    import pathlib

    sessions_dir = tmp_path / "sessions"
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path, sessions_dir=sessions_dir)

    pipeline.run("写个爬虫")
    pipeline.prepare("写个报告", confirm_mode="never")
    assert not sessions_dir.exists()  # 从未写入


def test_clear_pref_next_prepare_redecides(tmp_path):
    """清除会话偏好后：下一任务重新走决策模型。"""
    from model_switch import ModelSwitcher

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True, faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    switcher.set_preferred_model("user-1", "a/g1/m2")
    p = _pipeline(switcher.toolbox, tmp_path)
    p.sessions_dir = tmp_path / "sessions"
    info1 = p.prepare("写个爬虫", session_id="user-1", confirm_mode="never")
    assert info1["selected_model"] == "a/g1/m2"

    switcher.clear_preferred_model("user-1")
    info2 = p.prepare("写个爬虫", session_id="user-1", confirm_mode="never")
    assert info2["selected_model"] == "a/g1/m1"  # 回到决策选型（mock 默认 m1）


def test_execute_strict_from_record(tmp_path):
    """两阶段：prepare strict_model=True 落盘 -> execute 读到 strict，失败即失败。"""
    stream = EventStream()
    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="a/g1/m2", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    info = pipeline.prepare(
        "写个爬虫", preferred_model="a/g1/m1", strict_model=True, confirm_mode="never"
    )
    result = pipeline.execute(info["task_id"])
    assert result.ok is False
    assert result.error_class == "401"
    assert result.switches == 0
    assert EventType.SWITCH_TRIGGERED not in [e.type for e in stream]


# -- CLI ---------------------------------------------------------------


def test_cli_model_use_get_auto(tmp_path, monkeypatch, capsys):
    from model_switch.cli import main

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")
    cfg = str(config_dir / "models.yaml")

    assert main(["model", "use", "a/g1/m1", "--session", "user-1", "--config", cfg, "--mock"]) == 0
    out = capsys.readouterr().out
    assert "会话偏好已设置" in out and "a/g1/m1" in out

    assert main(["model", "get", "--session", "user-1", "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert "a/g1/m1" in out

    assert main(["model", "auto", "--session", "user-1", "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert "恢复自动选择" in out

    assert main(["model", "get", "--session", "user-1", "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert "无偏好" in out


def test_cli_model_use_rejects_unknown(tmp_path, monkeypatch, capsys):
    from model_switch.cli import main

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")
    cfg = str(config_dir / "models.yaml")

    assert main(["model", "use", "不存在/x", "--config", cfg, "--mock"]) == 1
    out = capsys.readouterr().out
    assert "设置失败" in out


def test_cli_run_session_preferred(tmp_path, monkeypatch, capsys):
    from model_switch.cli import main

    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")
    cfg = str(config_dir / "models.yaml")

    main(["model", "use", "a/g1/m1", "--session", "user-1", "--config", cfg, "--mock"])
    capsys.readouterr().out
    # run 命中会话偏好（mock 决策默认 m1，会话偏好也是 m1——可运行即可）
    assert main(["run", "写个爬虫", "--session", "user-1", "--config", cfg, "--mock"]) == 0
    out = capsys.readouterr().out
    assert "任务结果" in out


# -- MCP ---------------------------------------------------------------


def test_mcp_preferred_model_tools(tmp_path):
    from model_switch import ModelSwitcher
    from model_switch.mcp import build_mcp_server
    import asyncio

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    server = build_mcp_server(switcher)

    def _call(name, arguments=None):
        raw = asyncio.run(server.call_tool(name, arguments or {}))
        if isinstance(raw, tuple) and len(raw) == 2:
            text = "".join(getattr(item, "text", str(item)) for item in raw[0])
            return json.loads(text)
        if isinstance(raw, str):
            return json.loads(raw)
        return json.loads("".join(getattr(item, "text", str(item)) for item in raw))

    info = _call("set_preferred_model", {"session_id": "s-1", "model_ref": "a/g1/m1"})
    assert info["ok"] is True and info["preferred_model"] == "a/g1/m1"

    got = _call("get_preferred_model", {"session_id": "s-1"})
    assert got["preferred_model"] == "a/g1/m1"

    bad = _call("set_preferred_model", {"session_id": "s-1", "model_ref": "不存在/x"})
    assert bad["ok"] is False

    cleared = _call("clear_preferred_model", {"session_id": "s-1"})
    assert cleared["ok"] is True
    assert _call("get_preferred_model", {"session_id": "s-1"})["preferred_model"] is None


def test_mcp_prepare_run_with_session(tmp_path):
    from model_switch import ModelSwitcher
    from model_switch.mcp import build_mcp_server
    import asyncio

    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", sessions_dir=tmp_path / "sessions",
        persist_events=False,
    )
    switcher.set_preferred_model("s-1", "a/g1/m2")
    server = build_mcp_server(switcher)
    raw = asyncio.run(server.call_tool("prepare_run", {"task": "写个爬虫", "session_id": "s-1"}))
    text = "".join(getattr(item, "text", str(item)) for item in raw[0]) if isinstance(raw, tuple) else raw
    info = json.loads(text)
    assert info["ok"] is True
    assert info["selected_model"] == "a/g1/m2"  # 会话偏好命中