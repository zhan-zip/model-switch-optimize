"""fix 模块测试：apply_fix 人工门禁修复（granted/denied/重测/恢复）。"""
from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.fix import apply_fix
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


def _toolbox(tmp_path, stream=None, client=None, console=MockConsole()):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    return Toolbox(
        config, ModelRegistry(config), stream,
        model_client=client or SequenceClient(), console=console, mock=True,
    )


def test_apply_fix_granted_recovers(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)

    result = apply_fix(
        box, "a", "充值",
        model_ref="a/g1/m1", reason="余额不足", evidence="余额 0.00 元",
        confirm_handler=lambda *args: True,
    )

    assert result["ok"] is True
    assert result["status"] == "recovered"
    types = [e.type for e in stream]
    assert types == [
        EventType.CONFIRM_REQUESTED,
        EventType.CONFIRM_GRANTED,
        EventType.FIX_APPLIED,
        EventType.MODEL_CALLED,      # 重测探测
        EventType.MODEL_RESPONDED,
        EventType.RECHECK_RESULT,
        EventType.RECOVERED,
    ]
    confirm = stream.events[0]
    assert confirm.data["reason"] == "余额不足"
    assert "0.00" in confirm.data["evidence"]
    recheck = next(e for e in stream if e.type == EventType.RECHECK_RESULT)
    assert recheck.data["passed"] is True


def test_apply_fix_denied_keeps_advice_only(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)

    result = apply_fix(
        box, "a", "充值",
        model_ref="a/g1/m1",
        confirm_handler=lambda *args: False,
    )

    assert result["ok"] is False
    assert result["status"] == "denied"
    types = [e.type for e in stream]
    assert types == [EventType.CONFIRM_REQUESTED, EventType.CONFIRM_DENIED]
    assert EventType.FIX_APPLIED not in types
    assert EventType.RECHECK_RESULT not in types


def test_apply_fix_recheck_failed_no_recovery(tmp_path):
    stream = EventStream()
    from model_switch.client import CallResult

    client = SequenceClient(
        [CallResult(ok=False, model="m1", error_class="429", detail="still limited")]
    )
    box = _toolbox(tmp_path, stream=stream, client=client)

    result = apply_fix(
        box, "a", "充值",
        model_ref="a/g1/m1",
        confirm_handler=lambda *args: True,
    )

    assert result["ok"] is True
    assert result["status"] == "fixed"  # 修复执行了，但重测未通过
    assert result["recheck"]["passed"] is False
    types = [e.type for e in stream]
    assert EventType.RECHECK_RESULT in types
    assert EventType.RECOVERED not in types


def test_apply_fix_without_model_ref_skips_recheck(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)

    result = apply_fix(
        box, "a", "换分组",
        confirm_handler=lambda *args: True,
    )

    assert result["ok"] is True
    assert result["status"] == "fixed"
    types = [e.type for e in stream]
    assert types == [
        EventType.CONFIRM_REQUESTED,
        EventType.CONFIRM_GRANTED,
        EventType.FIX_APPLIED,
    ]


def test_apply_fix_without_console_not_configured(tmp_path):
    box = _toolbox(tmp_path, console=None)
    result = apply_fix(box, "a", "充值", confirm_handler=lambda *args: True)
    assert result["ok"] is False
    assert result["status"] == "console_not_configured"
