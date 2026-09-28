"""diagnose 模块测试：诊断编排（四项事实 -> 规则库 -> 结论 + 事件序列）。"""
import json

import pytest

from model_switch.config import load_config
from model_switch.diagnose import run_diagnose
from model_switch.events import EventStream, EventType
from model_switch.manager import DecisionError
from model_switch.mocker import MockConsole, MockModelClient
from model_switch.model import ModelRegistry
from model_switch.tools import Toolbox

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

CONCLUSION_JSON = json.dumps(
    {"conclusion": "余额不足", "actions": ["充值", "换分组"]}, ensure_ascii=False
)


def _toolbox(tmp_path, stream=None, console=MockConsole(), rules=None):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    client = MockModelClient(rules={"m2": rules if rules is not None else CONCLUSION_JSON})
    return Toolbox(
        config, ModelRegistry(config), stream,
        model_client=client, console=console, mock=True,
    )


def test_diagnose_full_flow_with_events(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)

    result = run_diagnose(box, "a")

    assert result["ok"] is True
    assert result["conclusion"] == "余额不足"
    assert result["actions"] == ["充值", "换分组"]
    assert result["decision_model"] == "b/g1/m2"  # 保底模型担任决策者
    assert set(result["facts"]["facts"]) == {"reachable", "balance", "group", "connectivity"}

    types = [e.type for e in stream]
    assert types == [
        EventType.DIAGNOSIS_STARTED,
        EventType.DIAGNOSIS_FACTS,
        EventType.DECISION_REQUESTED,
        EventType.DECISION_MODEL_PICKED,
        EventType.MODEL_CALLED,
        EventType.MODEL_RESPONDED,
        EventType.DECISION_MADE,
        EventType.DIAGNOSIS_CONCLUSION,
    ]


def test_diagnose_without_console_short_circuits(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, console=None)

    result = run_diagnose(box, "a")

    assert result["ok"] is False
    assert result["status"] == "console_not_configured"
    types = [e.type for e in stream]
    assert EventType.DIAGNOSIS_STARTED in types
    assert EventType.DIAGNOSIS_FACTS in types
    assert EventType.DIAGNOSIS_CONCLUSION not in types  # 未做结论


def test_diagnose_feeds_rules_into_decision(tmp_path):
    box = _toolbox(tmp_path)
    run_diagnose(box, "a", rules="自定义规则：余额不足优先充值")
    prompt = box._client.calls[-1]["messages"][0]["content"]
    assert "自定义规则：余额不足优先充值" in prompt


def test_diagnose_decision_failure_propagates(tmp_path):
    box = _toolbox(tmp_path, rules="401")  # 决策模型调用失败 -> 决策者降级 -> 全灭
    box.registry.update("a/g1/m1", available=False)
    box.registry.update("b/g1/m2", available=False)
    with pytest.raises(DecisionError):
        run_diagnose(box, "a")


def test_diagnose_uses_preset_facts(tmp_path):
    console = MockConsole(
        facts={"a": {"balance": {"ok": False, "evidence": "余额 0.00 元"}}}
    )
    box = _toolbox(tmp_path, console=console)
    result = run_diagnose(box, "a")
    assert result["facts"]["facts"]["balance"]["ok"] is False
