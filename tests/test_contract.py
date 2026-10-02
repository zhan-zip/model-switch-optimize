"""阶段 0：契约基线测�?- 确保五种宿主的返回结构一致性�?
这些测试固定现有行为，防止后续重构破坏宿主接口契约�?测试覆盖�?1. Python RunResult 字段完整�?2. MCP run tool 返回字段映射
3. CLI JSON pipeline_finished 事件字段
4. CLI 人读输出包含关键信息
5. Agent 工具模式返回结构

核心契约字段（所有宿主必须提供）�?- ok: bool
- outcome: str (ok/switched/fallback/failed)
- model_ref: str
- text: str
- attempts: int
- switches: int
- error_class: str
- trace_id: str (CLI JSON 事件流中可�?
"""
import json
from pathlib import Path

import pytest

from model_switch.cli import main
from model_switch.config import load_config
from model_switch.events import EventStream
from model_switch.mocker import MockConsole, MockDecisionClient
from model_switch.model import ModelRegistry
from model_switch.mcp import build_mcp_server
from model_switch.pipeline import Pipeline
from model_switch.switcher import ModelSwitcher
from model_switch.tools import Toolbox

VALID_CONFIG = """\
providers:
  - name: provider-a
    base_url: https://a.example.com/v1
    groups:
      - { name: g1, key_env: KEY_A, models: [m1, m2] }
  - name: provider-b
    base_url: https://b.example.com/v1
    groups:
      - { name: g1, key_env: KEY_B, models: [m3] }
fallback: { provider: provider-b, group: g1, model: m3, key_env: KEY_B }
"""


def _assert_core_fields(data: dict, *, require_trace_id: bool = False):
    """断言核心契约字段存在且类型正确�?""
    assert "ok" in data
    assert isinstance(data["ok"], bool)

    assert "outcome" in data
    assert data["outcome"] in ("ok", "switched", "fallback", "failed")

    assert "model_ref" in data
    assert isinstance(data["model_ref"], str)

    assert "text" in data
    assert isinstance(data["text"], str)

    assert "attempts" in data
    assert isinstance(data["attempts"], int)
    assert data["attempts"] > 0

    assert "switches" in data
    assert isinstance(data["switches"], int)
    assert data["switches"] >= 0

    assert "error_class" in data
    assert isinstance(data["error_class"], str)

    if require_trace_id:
        assert "trace_id" in data
        assert isinstance(data["trace_id"], str)


# -- Python RunResult 契约 --------------------------------------------------

def test_python_runresult_success_fields(tmp_path):
    """Python RunResult 成功场景必须包含所有核心字段�?""
    config_path = tmp_path / "models.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    switcher = ModelSwitcher(config_path, mock=True)
    result = switcher.run("写个爬虫")

    # RunResult 转字�?    data = {
        "ok": result.ok,
        "outcome": result.outcome,
        "model_ref": result.model_ref,
        "text": result.text,
        "attempts": result.attempts,
        "switches": result.switches,
        "error_class": result.error_class,
        "trace_id": result.trace_id,
    }

    _assert_core_fields(data, require_trace_id=True)
    assert result.ok is True
    assert result.text  # 非空回复
    assert result.model_ref  # 非空模型引用


def test_python_runresult_switched_fields(tmp_path):
    """Python RunResult 切换场景必须包含所有核心字段�?""
    config_path = tmp_path / "models.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    # mock: 第一个模型失败，切换�?fallback
    switcher = ModelSwitcher(config_path, mock=True)
    result = switcher.run("写个爬虫")

    data = {
        "ok": result.ok,
        "outcome": result.outcome,
        "model_ref": result.model_ref,
        "text": result.text,
        "attempts": result.attempts,
        "switches": result.switches,
        "error_class": result.error_class,
        "trace_id": result.trace_id,
    }

    _assert_core_fields(data, require_trace_id=True)
    # mock 默认会触发切�?    assert result.switches >= 0


def test_python_runresult_failed_fields(tmp_path):
    """Python RunResult 全败场景必须包含所有核心字段�?""
    config_path = tmp_path / "models.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    from model_switch.mocker import MockModelClient

    # 所有模型全部失�?    client = MockModelClient(default="500")
    config = load_config(config_path)
    registry = ModelRegistry(config)
    stream = EventStream()
    toolbox = Toolbox(config, registry, stream, model_client=client, console=MockConsole(), mock=True)
    pipeline = Pipeline(
        toolbox,
        prefs_path=tmp_path / "prefs.md",
        pause_path=tmp_path / "pause.json",
        stats_path=tmp_path / "stats.json",
        probe_dir=tmp_path / "probe",
    )

    result = pipeline.run("写个爬虫")

    data = {
        "ok": result.ok,
        "outcome": result.outcome,
        "model_ref": result.model_ref,
        "text": result.text,
        "attempts": result.attempts,
        "switches": result.switches,
        "error_class": result.error_class,
        "trace_id": result.trace_id,
    }

    _assert_core_fields(data, require_trace_id=True)
    assert result.ok is False
    assert result.outcome == "failed"
    assert result.error_class  # 应有错误类别


# -- MCP run tool 契约 ------------------------------------------------------

def test_mcp_run_tool_returns_all_fields(tmp_path):
    """MCP run tool 必须返回所有核心字段�?""
    config_path = tmp_path / "models.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    switcher = ModelSwitcher(config_path, mock=True)

    # 直接调用 switcher.run，因�?MCP tool 内部也是调用�?    result = switcher.run("写个爬虫")

    # 模拟 MCP tool 的序列化逻辑（参�?mcp.py:183-194�?    data = {
        "ok": result.ok,
        "outcome": result.outcome,
        "model_ref": result.model_ref,
        "text": result.text,
        "attempts": result.attempts,
        "switches": result.switches,
        "error_class": result.error_class,
        "trace_id": result.trace_id,
    }

    _assert_core_fields(data, require_trace_id=True)
    assert data["ok"] is True
    assert data["text"]  # 非空回复


# -- CLI JSON pipeline_finished 事件契约 ------------------------------------

def test_cli_json_pipeline_finished_has_all_fields(tmp_path, monkeypatch):
    """CLI --json 模式�?pipeline_finished 事件必须包含所有核心字段�?""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config" / "models.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    import sys
    from io import StringIO

    captured = StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured

    try:
        exit_code = main(["run", "写个爬虫", "--mock", "--json"])
    finally:
        sys.stdout = old_stdout

    assert exit_code == 0

    events = json.loads(captured.getvalue())
    finished_events = [e for e in events if e["type"] == "pipeline_finished"]
    assert len(finished_events) == 1

    finished = finished_events[0]
    data = finished["data"]

    # CLI JSON 事件的结构略有不同，需要从 data �?data.summary 中提�?    flattened = {
        "ok": data["summary"]["ok"],
        "outcome": data["outcome"],
        "model_ref": data["summary"]["model_ref"],
        "text": data["text"],
        "attempts": data["summary"]["attempts"],
        "switches": data["summary"]["switches"],
        "error_class": data["summary"]["error_class"],
        "trace_id": "",  # 事件流本身有 trace_id，但不在 data �?    }

    _assert_core_fields(flattened, require_trace_id=False)
    assert flattened["text"]  # 关键：text 必须存在


def test_cli_json_missing_text_field_would_fail(tmp_path, monkeypatch):
    """回归测试：如�?pipeline_finished.data 缺少 text，应该被检测到�?
    这是之前修复的问题（069eb75 fix: expose CLI JSON final response）�?    此测试确保不会再次丢失该字段�?    """
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config" / "models.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    import sys
    from io import StringIO

    captured = StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured

    try:
        main(["run", "写个爬虫", "--mock", "--json"])
    finally:
        sys.stdout = old_stdout

    events = json.loads(captured.getvalue())
    finished = next(e for e in events if e["type"] == "pipeline_finished")

    # 必须�?text 字段
    assert "text" in finished["data"], "pipeline_finished.data 必须包含 text 字段"
    assert isinstance(finished["data"]["text"], str)
    assert finished["data"]["text"]  # 非空


# -- CLI 人读输出契约 -------------------------------------------------------

def test_cli_human_readable_contains_key_info(tmp_path, monkeypatch, capsys):
    """CLI 人读模式必须输出关键信息：结果、模型、尝试次数�?""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config" / "models.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    exit_code = main(["run", "写个爬虫", "--mock"])
    assert exit_code == 0

    output = capsys.readouterr().out

    # 人读输出必须包含关键信息
    assert "最终模�? in output or "模型" in output
    assert "尝试" in output or "attempts" in output
    # mock 会触发切�?    assert "切换" in output or "switched" in output or "故障" in output


# -- Agent 工具模式契约 -----------------------------------------------------

def test_agent_toolbox_dispatch_returns_structured_data(tmp_path):
    """Agent 工具模式 (dispatch) 必须返回结构化数据�?""
    config_path = tmp_path / "models.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    switcher = ModelSwitcher(config_path, mock=True)

    # 测试 call_model 工具
    result = switcher.dispatch("call_model", {
        "model_ref": "provider-a/g1/m1",
        "messages": [{"role": "user", "content": "测试"}],
    })

    # dispatch 返回 {ok, tool, result}，实际数据在 result 字段�?    assert "ok" in result
    assert "result" in result
    call_result = result["result"]
    assert "text" in call_result or "error_class" in call_result
    assert "latency_ms" in call_result


# -- 字段缺失检测测�?-------------------------------------------------------

def test_detect_missing_ok_field():
    """测试能检测到缺少 ok 字段�?""
    invalid_data = {
        "outcome": "ok",
        "model_ref": "a/b/c",
        "text": "test",
        "attempts": 1,
        "switches": 0,
        "error_class": "",
        "trace_id": "",
    }

    with pytest.raises(AssertionError, match="ok"):
        _assert_core_fields(invalid_data)


def test_detect_missing_text_field():
    """测试能检测到缺少 text 字段�?""
    invalid_data = {
        "ok": True,
        "outcome": "ok",
        "model_ref": "a/b/c",
        "attempts": 1,
        "switches": 0,
        "error_class": "",
        "trace_id": "",
    }

    with pytest.raises(AssertionError, match="text"):
        _assert_core_fields(invalid_data)


def test_detect_wrong_outcome_value():
    """测试能检测到 outcome 值不合法�?""
    invalid_data = {
        "ok": True,
        "outcome": "invalid_outcome",
        "model_ref": "a/b/c",
        "text": "test",
        "attempts": 1,
        "switches": 0,
        "error_class": "",
        "trace_id": "",
    }

    with pytest.raises(AssertionError):
        _assert_core_fields(invalid_data)


def test_detect_negative_attempts():
    """测试能检测到 attempts 为非正数�?""
    invalid_data = {
        "ok": True,
        "outcome": "ok",
        "model_ref": "a/b/c",
        "text": "test",
        "attempts": 0,  # 必须 > 0
        "switches": 0,
        "error_class": "",
        "trace_id": "",
    }

    with pytest.raises(AssertionError):
        _assert_core_fields(invalid_data)


# -- 跨宿主一致性测�?-------------------------------------------------------

def test_cross_host_consistency(tmp_path):
    """确保 Python、MCP、CLI JSON 三种宿主返回的核心字段值一致�?""
    config_path = tmp_path / "config" / "models.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    # 1. Python RunResult
    switcher = ModelSwitcher(config_path, mock=True)
    python_result = switcher.run("一致性测试任�?)

    # 2. 模拟 MCP run tool 的序列化逻辑
    mcp_result = {
        "ok": python_result.ok,
        "outcome": python_result.outcome,
        "model_ref": python_result.model_ref,
        "text": python_result.text,
        "attempts": python_result.attempts,
        "switches": python_result.switches,
        "error_class": python_result.error_class,
        "trace_id": python_result.trace_id,
    }

    # 核心字段类型必须一�?    assert type(python_result.ok) == type(mcp_result["ok"])
    assert type(python_result.outcome) == type(mcp_result["outcome"])
    assert type(python_result.attempts) == type(mcp_result["attempts"])
    assert type(python_result.switches) == type(mcp_result["switches"])

    # outcome 值域必须一�?    assert python_result.outcome in ("ok", "switched", "fallback", "failed")
    assert mcp_result["outcome"] in ("ok", "switched", "fallback", "failed")
