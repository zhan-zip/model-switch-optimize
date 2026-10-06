"""mcp 模块测试：MCP 薄封装（工具清单 / 调用 / 高危确认门槛 / 版本防御）。"""
import asyncio
import json
import sys
import types

import pytest

from model_switch import ModelSwitcher
from model_switch.mcp import _resolve_fastmcp, build_mcp_server

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
    "model_health",
    "apply_fix", "run",
    "prepare_run", "execute_run",
}


def _server(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks",
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


def test_resolve_fastmcp_ok_on_mcp1(monkeypatch):
    # 正常路径：mcp 1.x 环境直接返回 FastMCP 类
    stub = types.ModuleType("mcp.server.fastmcp")
    real = sys.modules["mcp.server.fastmcp"]
    try:
        stub.FastMCP = real.FastMCP
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", stub)
        assert _resolve_fastmcp() is real.FastMCP
    finally:
        monkeypatch.undo()


def test_resolve_fastmcp_raises_clear_error_when_mcp2(monkeypatch):
    # 模拟 mcp 2.x：fastmcp 墓碑 shim 无 FastMCP 属性 -> ImportError -> 清晰 RuntimeError
    stub = types.ModuleType("mcp.server.fastmcp")
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", stub)
    with pytest.raises(RuntimeError) as exc:
        _resolve_fastmcp()
    msg = str(exc.value)
    assert "mcp>=1.29,<2.0.0" in msg
    assert "pip install" in msg
