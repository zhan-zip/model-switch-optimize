"""manager 模块测试：三决策点、容错解析、决策者降级、确认策略。"""
import json

import pytest

from model_switch.client import CallResult
from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.manager import DecisionError, DecisionManager, extract_json
from model_switch.model import ModelRegistry
from model_switch.prefs import load_prefs, save_prefs
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


def _toolbox(tmp_path, client, stream=None):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)
    return Toolbox(config, registry, stream, model_client=client, mock=True)


def _choose_json(model="a/g1/m1"):
    return json.dumps({"model": model, "reason": "更合适"}, ensure_ascii=False)


# -- extract_json ---------------------------------------------------


def test_extract_json_plain_and_noisy():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('前置说明 {"a": 1} 后置说明') == {"a": 1}
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_extract_json_invalid_returns_none():
    assert extract_json("不是 JSON") is None
    assert extract_json("[1, 2]") is None  # 非 dict
    assert extract_json("{坏掉的") is None


# -- 决策者选定 -------------------------------------------------------


def test_pick_decision_model_prefers_fallback(tmp_path):
    box = _toolbox(tmp_path, SequenceClient())
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md")
    assert mgr.pick_decision_model() == "b/g1/m3"


def test_pick_decision_model_skips_unavailable(tmp_path):
    box = _toolbox(tmp_path, SequenceClient())
    box.registry.update("b/g1/m3", available=False)
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md")
    assert mgr.pick_decision_model() == "a/g1/m1"


def test_pick_decision_model_all_unavailable_raises(tmp_path):
    box = _toolbox(tmp_path, SequenceClient())
    for ref in ("a/g1/m1", "a/g1/m2", "b/g1/m3"):
        box.registry.update(ref, available=False)
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md")
    with pytest.raises(DecisionError):
        mgr.pick_decision_model()


# -- choose_model ----------------------------------------------------


def test_choose_model_decision_and_events(tmp_path):
    stream = EventStream()
    client = SequenceClient([_choose_json()])
    box = _toolbox(tmp_path, client, stream)
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md")
    decision = mgr.choose_model("写个爬虫")
    assert decision.point == "choose_model"
    assert decision.decision["model"] == "a/g1/m1"
    assert decision.decision_model == "b/g1/m3"  # 保底担任决策者
    types = [e.type for e in stream]
    assert EventType.DECISION_REQUESTED in types
    assert EventType.DECISION_MODEL_PICKED in types
    assert EventType.DECISION_MADE in types


def test_choose_model_rejects_hallucination_then_retries(tmp_path):
    client = SequenceClient(['{"model": "x/y/z", "reason": "幻觉"}', _choose_json()])
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md")
    decision = mgr.choose_model("任务")
    assert decision.decision["model"] == "a/g1/m1"
    assert len(client.calls) == 2  # 第一次被拒，反馈后重试


def test_choose_model_switches_decision_model_on_failure(tmp_path):
    client = SequenceClient(
        [
            CallResult(ok=False, model="m3", error_class="401", detail="挂了"),
            _choose_json(),
        ]
    )
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md")
    decision = mgr.choose_model("任务")
    assert decision.decision_model == "a/g1/m1"  # 保底失败换清单序


def test_choose_model_prompt_contains_matched_task_pref(tmp_path):
    prefs_path = tmp_path / "prefs.md"
    save_prefs({"model_labels": {}, "task_prefs": {"爬虫": "b/g1/m3"}}, prefs_path)
    client = SequenceClient([_choose_json()])
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=prefs_path)
    mgr.choose_model("帮我写爬虫")
    content = client.calls[0]["messages"][0]["content"]
    assert "任务偏好命中：b/g1/m3" in content


# -- 确认策略 ---------------------------------------------------------


def test_confirm_always_granted_and_denied(tmp_path):
    stream = EventStream()
    client = SequenceClient([_choose_json()])
    box = _toolbox(tmp_path, client, stream)
    granted = DecisionManager(box, confirm_handler=lambda *a: True, prefs_path=tmp_path / "p.md")
    decision = granted.choose_model("任务", confirm_mode="always")
    assert decision.confirmed is True
    types = [e.type for e in stream]
    assert EventType.CONFIRM_REQUESTED in types
    assert EventType.CONFIRM_GRANTED in types
    assert types.index(EventType.CONFIRM_REQUESTED) < types.index(EventType.CONFIRM_GRANTED)

    stream2 = EventStream()
    client2 = SequenceClient([_choose_json()])
    box2 = _toolbox(tmp_path, client2, stream2)
    denied = DecisionManager(box2, confirm_handler=lambda *a: False, prefs_path=tmp_path / "p.md")
    decision2 = denied.choose_model("任务", confirm_mode="always")
    assert decision2.confirmed is False
    assert EventType.CONFIRM_DENIED in [e.type for e in stream2]


def test_confirm_once_saves_pref_then_skips(tmp_path):
    prefs_path = tmp_path / "prefs.md"
    client = SequenceClient([_choose_json()])
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, confirm_handler=lambda *a: True, prefs_path=prefs_path)
    first = mgr.choose_model("写个爬虫", confirm_mode="once")
    assert first.confirmed is True
    prefs = load_prefs(prefs_path)
    assert prefs["task_prefs"]["写个爬虫"] == "a/g1/m1"

    # 第二次：命中偏好，不再确认（confirmed 保持 None）
    client2 = SequenceClient([_choose_json()])
    box2 = _toolbox(tmp_path, client2)
    mgr2 = DecisionManager(box2, confirm_handler=lambda *a: False, prefs_path=prefs_path)
    second = mgr2.choose_model("写个爬虫", confirm_mode="once")
    assert second.confirmed is None


# -- plan_recovery ---------------------------------------------------


def test_plan_recovery_ok(tmp_path):
    client = SequenceClient([json.dumps({"switch_to": "b/g1/m3", "diagnose": True, "reason": "顶上"})])
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=tmp_path / "p.md")
    decision = mgr.plan_recovery("a/g1/m1", "429")
    assert decision.point == "plan_recovery"
    assert decision.decision["switch_to"] == "b/g1/m3"
    assert decision.decision["diagnose"] is True


def test_plan_recovery_rejects_bad_output_then_raises(tmp_path):
    client = SequenceClient(
        [
            '{"switch_to": "a/g1/m1", "diagnose": true, "reason": "回到刚失败的模型"}',
            '{"switch_to": "x/y/z", "diagnose": true, "reason": "幻觉"}',
        ]
    )
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=tmp_path / "p.md")
    with pytest.raises(DecisionError):
        mgr.plan_recovery("a/g1/m1", "429")


def test_plan_recovery_retry_succeeds(tmp_path):
    client = SequenceClient(
        [
            '{"switch_to": "x/y/z", "diagnose": true, "reason": "幻觉"}',
            '{"switch_to": "a/g1/m2", "diagnose": false, "reason": "换一个"}',
        ]
    )
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=tmp_path / "p.md")
    decision = mgr.plan_recovery("a/g1/m1", "timeout")
    assert decision.decision["switch_to"] == "a/g1/m2"
    assert decision.decision["diagnose"] is False


# -- conclude_diagnosis ----------------------------------------------


def test_conclude_diagnosis_ok(tmp_path):
    client = SequenceClient([json.dumps({"conclusion": "余额不足", "actions": ["充值", "换分组"]})])
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=tmp_path / "p.md")
    facts = {"reachable": {"ok": True}, "balance": {"ok": False}}
    decision = mgr.conclude_diagnosis(facts, rules="规则：余额不足时建议充值")
    assert decision.point == "conclude_diagnosis"
    assert decision.decision["conclusion"] == "余额不足"
    assert decision.decision["actions"] == ["充值", "换分组"]


def test_conclude_diagnosis_rejects_empty_actions(tmp_path):
    client = SequenceClient(
        [
            '{"conclusion": "有结论", "actions": []}',
            '{"conclusion": "有结论", "actions": ["充值"]}',
        ]
    )
    box = _toolbox(tmp_path, client)
    mgr = DecisionManager(box, prefs_path=tmp_path / "p.md")
    decision = mgr.conclude_diagnosis({"facts": {}})
    assert decision.decision["actions"] == ["充值"]


# -- plan_recovery 切换偏好（plan_prefs）------------------------------


def test_plan_recovery_pref_hit_program_decision(tmp_path):
    """切换偏好命中且目标可用：程序直接决策，不调 LLM。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    save_prefs(
        {"model_labels": {}, "task_prefs": {}, "plan_prefs": {"429": ("a/g1/m2", 2)}}, prefs_path
    )
    client = SequenceClient()  # program 命中不应发起任何 LLM 调用
    box = _toolbox(tmp_path, client, stream)
    mgr = DecisionManager(box, prefs_path=prefs_path)
    decision = mgr.plan_recovery("a/g1/m1", "429")
    assert decision.point == "plan_recovery"
    assert decision.decision_model == "program"
    assert decision.decision["switch_to"] == "a/g1/m2"
    assert len(client.calls) == 0
    types = [e.type for e in stream]
    assert EventType.PLAN_PREF_HIT in types
    assert EventType.PLAN_PREF_MISS not in types
    assert EventType.DECISION_REQUESTED not in types  # 未走 LLM 决策通道


def test_plan_recovery_pref_hit_target_unavailable_falls_to_llm(tmp_path):
    """命中但目标已不可用（available=False）：按未命中走 LLM。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    save_prefs(
        {"model_labels": {}, "task_prefs": {}, "plan_prefs": {"429": ("a/g1/m2", 2)}}, prefs_path
    )
    client = SequenceClient(
        [json.dumps({"switch_to": "b/g1/m3", "diagnose": False, "reason": "换一个"})]
    )
    box = _toolbox(tmp_path, client, stream)
    box.registry.update("a/g1/m2", available=False)  # 偏好目标已故障
    mgr = DecisionManager(box, prefs_path=prefs_path)
    decision = mgr.plan_recovery("a/g1/m1", "429")
    assert decision.decision_model == "b/g1/m3"  # 保底担任决策者
    assert decision.decision["switch_to"] == "b/g1/m3"
    types = [e.type for e in stream]
    assert EventType.PLAN_PREF_MISS in types
    assert EventType.PLAN_PREF_HIT not in types


def test_plan_recovery_pref_disabled_by_flag(tmp_path):
    """use_plan_pref=False：旁路偏好机制，直接走 LLM。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    save_prefs(
        {"model_labels": {}, "task_prefs": {}, "plan_prefs": {"429": ("a/g1/m2", 2)}}, prefs_path
    )
    client = SequenceClient(
        [json.dumps({"switch_to": "b/g1/m3", "diagnose": False, "reason": "正常决策"})]
    )
    box = _toolbox(tmp_path, client, stream)
    mgr = DecisionManager(box, prefs_path=prefs_path)
    decision = mgr.plan_recovery("a/g1/m1", "429", use_plan_pref=False)
    assert decision.decision_model == "b/g1/m3"
    assert decision.decision["switch_to"] == "b/g1/m3"
    assert len(client.calls) == 1
    # 旁路时不发命中/未命中事件
    types = [e.type for e in stream]
    assert EventType.PLAN_PREF_HIT not in types
    assert EventType.PLAN_PREF_MISS not in types
