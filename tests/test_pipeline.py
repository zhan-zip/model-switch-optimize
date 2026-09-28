"""pipeline 模块测试：run 闭环（成功 / 切换 / 机械兜底 / 全败 / 入档 / 事件序列）。"""
import json

from model_switch.client import CallResult
from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.history import FaultRecorder
from model_switch.mocker import MockConsole, MockDecisionClient, MockModelClient
from model_switch.model import ModelRegistry
from model_switch.pipeline import Pipeline
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

CHOOSE = json.dumps({"model": "a/g1/m1", "reason": "合适"}, ensure_ascii=False)
SWITCH = json.dumps(
    {"switch_to": "a/g1/m2", "diagnose": False, "reason": "顶上"}, ensure_ascii=False
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
    return Pipeline(
        box,
        recorder=FaultRecorder(None, faults_dir=tmp_path / "faults"),
        probe_dir=tmp_path / "probe",
        **kwargs,
    )


def test_run_success_first_try(tmp_path):
    stream = EventStream()
    # 决策者（保底 m3）返回选型 JSON；任务调用 default ok
    client = MockModelClient(rules={"m3": CHOOSE})
    box = _toolbox(tmp_path, stream=stream, client=client)

    result = _pipeline(box, tmp_path).run("写个爬虫")

    assert result.ok is True
    assert result.outcome == "ok"
    assert result.model_ref == "a/g1/m1"
    assert result.attempts == 1
    assert result.switches == 0

    types = [e.type for e in stream]
    assert types[0] == EventType.PIPELINE_STARTED
    assert types[-1] == EventType.PIPELINE_FINISHED
    assert EventType.SWITCH_TRIGGERED not in types
    assert EventType.MECHANICAL_FALLBACK not in types
    finished = stream.events[-1]
    assert finished.data["outcome"] == "ok"
    assert finished.data["summary"]["attempts"] == 1


def test_run_failure_switches_and_succeeds(tmp_path):
    stream = EventStream()
    # 决策模型（保底 m3）按 prompt 分流：选型选 m1、切换切 m2；m1 任务调用 401
    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="a/g1/m2", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=stream, client=client)

    result = _pipeline(box, tmp_path).run("写个爬虫")

    assert result.ok is True
    assert result.outcome == "switched"
    assert result.model_ref == "a/g1/m2"
    assert result.attempts == 2
    assert result.switches == 1

    types = [e.type for e in stream]
    assert EventType.SWITCH_TRIGGERED in types
    assert EventType.SWITCHED_TO in types
    assert types.index(EventType.SWITCH_TRIGGERED) < types.index(EventType.SWITCHED_TO)
    assert EventType.MECHANICAL_FALLBACK not in types

    # 故障入档
    recorder = FaultRecorder(None, faults_dir=tmp_path / "faults")
    files = sorted((tmp_path / "faults").glob("fault-*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["model_ref"] == "a/g1/m1"
    assert payload["error_class"] == "401"
    assert payload["task"] == "写个爬虫"

    switched = next(e for e in stream if e.type == EventType.SWITCH_TRIGGERED)
    assert switched.data["from"] == "a/g1/m1"
    assert switched.data["to"] == "a/g1/m2"
    assert switched.data["error"] == "401"


def test_run_decision_failure_mechanical_fallback(tmp_path):
    stream = EventStream()
    # 决策全灭：决策者（保底 m3）调用 401；任务调用全 ok
    client = MockModelClient(rules={"m3": "401"})
    box = _toolbox(tmp_path, stream=stream, client=client)

    result = _pipeline(box, tmp_path).run("写个爬虫")

    # choose 决策全灭 -> 机械兜底按清单序首个（a/g1/m1）成功
    assert result.ok is True
    assert result.outcome == "fallback"
    assert result.model_ref == "a/g1/m1"

    types = [e.type for e in stream]
    assert EventType.MECHANICAL_FALLBACK in types
    mech = next(e for e in stream if e.type == EventType.MECHANICAL_FALLBACK)
    assert "a/g1/m1" in mech.data["tried_models"]
    assert mech.data["results"]["a/g1/m1"]["ok"] is True


def test_run_all_failed(tmp_path):
    stream = EventStream()
    client = MockModelClient(default="429")  # 一切调用（决策+任务）全失败
    box = _toolbox(tmp_path, stream=stream, client=client)

    result = _pipeline(box, tmp_path).run("写个爬虫")

    assert result.ok is False
    assert result.outcome == "failed"
    assert result.attempts >= 3  # 3 模型全部试过
    assert result.error_class == "429"

    types = [e.type for e in stream]
    assert types[-1] == EventType.PIPELINE_FINISHED
    assert types[-2] == EventType.MECHANICAL_FALLBACK
    mech = next(e for e in stream if e.type == EventType.MECHANICAL_FALLBACK)
    assert len(mech.data["results"]) == 3  # 全部模型都试了


def test_run_max_switches_then_mechanical(tmp_path):
    stream = EventStream()
    # 决策永远切到 m2，m1/m2/m3 任务调用全部失败 -> m2 再败后决策被拒（不能切回刚失败模型）
    # -> DecisionError -> 机械兜底 -> 候选 m3 也败 -> 全败
    client = MockDecisionClient(
        default_model="a/g1/m1",
        switch_to="a/g1/m2",
        fail_models={"m1": "401", "m2": "429", "m3": "5xx"},
    )
    box = _toolbox(tmp_path, stream=stream, client=client)

    result = _pipeline(box, tmp_path, max_switches=2).run("写个爬虫")

    assert result.ok is False
    assert result.outcome == "failed"
    assert result.switches == 1  # m1->m2 一次切换；m2 再败后决策被 validator 拒（不能切回刚失败模型）
    assert result.attempts == 3  # m1 + m2 + m3（兜底）
    assert result.error_class == "5xx"
    mech = next(e for e in stream if e.type == EventType.MECHANICAL_FALLBACK)
    assert mech.data["tried_models"] == ["b/g1/m3"]  # 仅剩未失败的 m3 作为兜底候选


def test_run_choose_fails_directly_mechanical(tmp_path):
    stream = EventStream()
    # 决策者返回不可解析文本（"[mock] ok"）+ 保底 401：choose 全灭 -> 机械兜底
    replies = [CallResult(ok=False, model="m3", error_class="401", detail="挂")]
    client = SequenceClient(replies)
    box = _toolbox(tmp_path, stream=stream, client=client)

    result = _pipeline(box, tmp_path).run("写个爬虫")

    assert result.ok is True
    assert result.outcome == "fallback"
    assert result.model_ref == "a/g1/m1"  # 清单序首个


def test_run_confirm_mode_passthrough(tmp_path):
    stream = EventStream()
    client = MockModelClient(rules={"m3": CHOOSE})  # 决策者返回选型 JSON
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = Pipeline(
        box,
        recorder=FaultRecorder(None, faults_dir=tmp_path / "faults"),
        confirm_mode="always",
    )
    result = pipeline.run("任务")  # confirm_handler 未注入 -> 默认 granted
    assert result.ok is True
    types = [e.type for e in stream]
    assert EventType.CONFIRM_REQUESTED in types
    assert EventType.CONFIRM_GRANTED in types


def test_run_result_carries_trace_id(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, client=MockModelClient())
    result = _pipeline(box, tmp_path).run("任务")
    assert result.trace_id == stream.trace_id
