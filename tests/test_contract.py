"""阶段 0 契约基线：固定 Python、CLI、Agent 和 MCP 的核心结果字段。"""

import asyncio
import json

from model_switch import ModelSwitcher
from model_switch.cli import main
from model_switch.mcp import build_mcp_server


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

CORE_FIELDS = (
    "ok", "outcome", "model_ref", "text", "attempts", "switches",
    "error_class", "trace_id",
)


def _switcher(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    return ModelSwitcher(
        path,
        mock=True,
        faults_dir=tmp_path / "faults",
        probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks",
        persist_events=False,
    )


def _assert_result_fields(result):
    for field in CORE_FIELDS:
        assert hasattr(result, field), f"RunResult 缺少字段: {field}"
    assert isinstance(result.ok, bool)
    assert result.outcome in {"ok", "switched", "fallback", "failed"}
    assert isinstance(result.model_ref, str)
    assert isinstance(result.text, str)
    assert isinstance(result.attempts, int)
    assert isinstance(result.switches, int)
    assert isinstance(result.error_class, str)
    assert isinstance(result.trace_id, str)


def _call_mcp(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if isinstance(result, tuple) and len(result) == 2:
        content, _structured = result
        raw = "".join(getattr(item, "text", str(item)) for item in content)
        return json.loads(raw)
    if isinstance(result, str):
        return json.loads(result)
    return json.loads("".join(getattr(item, "text", str(item)) for item in result))


def test_python_run_result_contract(tmp_path):
    result = _switcher(tmp_path).run("写个爬虫")
    _assert_result_fields(result)
    assert result.ok is True
    assert result.text


def test_agent_dispatch_result_contract(tmp_path):
    result = _switcher(tmp_path).dispatch(
        "call_model",
        {"model_ref": "a/g1/m1", "messages": [{"role": "user", "content": "测试"}]},
    )
    assert result["ok"] is True
    assert result["tool"] == "call_model"
    assert result["result"]["ok"] is True
    assert isinstance(result["result"]["text"], str)


def test_mcp_run_contract(tmp_path):
    switcher = _switcher(tmp_path)
    payload = _call_mcp(build_mcp_server(switcher), "run", {"task": "写个爬虫"})
    assert set(CORE_FIELDS).issubset(payload)
    assert payload["ok"] is True
    assert isinstance(payload["text"], str)


def test_cli_json_pipeline_finished_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")

    assert main(["run", "写个爬虫", "--mock", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    finished = next(event for event in events if event["type"] == "pipeline_finished")
    data = finished["data"]
    assert {"outcome", "text", "summary"}.issubset(data)
    assert {"ok", "model_ref", "attempts", "switches", "error_class"}.issubset(data["summary"])
    assert isinstance(data["text"], str)


def test_cli_human_output_contains_result(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "models.yaml").write_text(VALID_YAML, encoding="utf-8")

    assert main(["run", "写个爬虫", "--mock"]) == 0
    output = capsys.readouterr().out
    assert "任务结果" in output
    assert "最终模型" in output


def test_contract_helper_rejects_missing_text():
    data = {"ok": True, "outcome": "ok", "model_ref": "a/g1/m1"}
    missing = set(CORE_FIELDS) - set(data)
    assert "text" in missing
