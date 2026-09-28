"""console_ops 模块测试：契约完备性、修复门禁（granted/denied/未配置）。"""
from model_switch.config import load_config
from model_switch.console_ops import CONSOLE_SPECS, ConsoleOps
from model_switch.events import EventStream, EventType
from model_switch.mocker import MockConsole
from model_switch.model import ModelRegistry
from model_switch.tools import Toolbox

from conftest import SequenceClient

VALID_YAML = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - { name: g1, key_env: KEY_A, models: [m1] }
  - name: b
    base_url: https://b.example.com/v1
    groups:
      - { name: g1, key_env: KEY_B, models: [m2] }
fallback: { provider: b, group: g1, model: m2, key_env: KEY_B }
"""


def _toolbox(tmp_path, stream=None, console=MockConsole()):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    return Toolbox(config, ModelRegistry(config), stream, model_client=SequenceClient(), console=console, mock=True)


def test_console_specs_contract_complete():
    actions = {spec["action"] for spec in CONSOLE_SPECS}
    assert actions == {"login", "check_balance", "check_group", "check_connectivity", "repair"}
    by_action = {spec["action"]: spec for spec in CONSOLE_SPECS}
    assert by_action["login"]["high_risk"] is True
    assert by_action["repair"]["high_risk"] is True
    assert all(by_action[a]["high_risk"] is False for a in ("check_balance", "check_group", "check_connectivity"))
    assert all(spec.get("steps") for spec in CONSOLE_SPECS)


def test_repair_granted_executes_and_emits(tmp_path):
    stream = EventStream()
    console = MockConsole()
    box = _toolbox(tmp_path, stream=stream, console=console)
    ops = ConsoleOps(box, confirm_handler=lambda *a: True)

    result = ops.repair("a", "充值", reason="余额不足", evidence="余额 0 元")

    assert result["ok"] is True
    assert console.repairs == [{"provider": "a", "action": "充值"}]
    types = [e.type for e in stream]
    assert types == [EventType.CONFIRM_REQUESTED, EventType.CONFIRM_GRANTED]


def test_repair_denied_does_not_execute(tmp_path):
    stream = EventStream()
    console = MockConsole()
    box = _toolbox(tmp_path, stream=stream, console=console)
    ops = ConsoleOps(box, confirm_handler=lambda *a: False)

    result = ops.repair("a", "充值")

    assert result["ok"] is False
    assert result["status"] == "denied"
    assert console.repairs == []
    types = [e.type for e in stream]
    assert types == [EventType.CONFIRM_REQUESTED, EventType.CONFIRM_DENIED]


def test_repair_without_console_returns_not_configured(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, console=None)
    ops = ConsoleOps(box, confirm_handler=lambda *a: True)
    result = ops.repair("a", "充值")
    assert result["ok"] is False
    assert result["status"] == "console_not_configured"


def test_check_forwards_to_console_check(tmp_path):
    box = _toolbox(tmp_path)
    ops = ConsoleOps(box)
    facts = ops.check("a")
    assert facts["ok"] is True
    assert set(facts["facts"]) == {"reachable", "balance", "group", "connectivity"}
