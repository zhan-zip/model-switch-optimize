"""mcp 模块测试：MCP 薄封装（工具清单 / 调用 / 高危确认门槛）。"""
import asyncio
import json

from model_switch import ModelSwitcher
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

EXPECTED_TOOLS = {
    "call_model", "list_models", "test_connectivity", "console_check",
    "web_search", "fault_history",
    "choose_model", "plan_recovery", "conclude_diagnosis",
    "apply_fix", "run",
}


def _server(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        persist_events=False,
    )
    return build_mcp_server(switcher)


def _call(server, name, arguments=None):
    result = asyncio.run(server.call_tool(name, arguments or {}))
    # FastMCP 1.29：返回 (unstructured: [TextContent], structured: dict)
    if isinstance(result, tuple) and len(result) == 2:
        content, _structured = result
        raw = "".join(getattr(item, "text", str(item)) for item in content)
        return json.loads(raw)
    if isinstance(result, str):
        return json.loads(result)
    if isinstance(result, (list, tuple)):
        return json.loads("".join(getattr(item, "text", str(item)) for item in result))
    return json.loads(getattr(result, "text", str(result)))


def test_tool_list_complete(tmp_path):
    server = _server(tmp_path)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS


def test_list_models_returns_all(tmp_path):
    server = _server(tmp_path)
    payload = _call(server, "list_models")
    assert len(payload) == 3
    assert payload[0]["model_ref"] == "a/g1/m1"


def test_run_full_loop(tmp_path):
    server = _server(tmp_path)
    payload = _call(server, "run", {"task": "写个爬虫"})
    assert payload["ok"] is True
    assert payload["outcome"] == "ok"
    assert payload["model_ref"] == "a/g1/m1"


def test_choose_model_decision(tmp_path):
    server = _server(tmp_path)
    payload = _call(server, "choose_model", {"task": "任务"})
    assert payload["ok"] is True
    assert payload["model"] == "a/g1/m1"


def test_apply_fix_requires_user_confirmation(tmp_path):
    server = _server(tmp_path)
    payload = _call(server, "apply_fix", {"provider": "a", "action": "充值"})
    assert payload["ok"] is False
    assert payload["status"] == "not_confirmed"


def test_apply_fix_after_confirmation_recovers(tmp_path):
    server = _server(tmp_path)
    payload = _call(
        server, "apply_fix",
        {"provider": "a", "action": "充值", "model_ref": "a/g1/m1", "user_confirmed": True},
    )
    assert payload["ok"] is True
    assert payload["status"] == "recovered"
