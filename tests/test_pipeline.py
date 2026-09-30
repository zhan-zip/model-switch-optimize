"""pipeline 模块测试：run 闭环（成功 / 切换 / 机械兜底 / 全败 / 入档 / 事件序列）。"""
import json

from model_switch.client import CallResult
from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.history import FaultRecorder
from model_switch.mocker import MockConsole, MockDecisionClient, MockModelClient
from model_switch.model import ModelRegistry
from model_switch.pipeline import Pipeline
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
    # 测试隔离：prefs/pause/stats 默认全走 tmp（防止污染真实 config/model_prefs.md、
    # data/pause.json、data/stats.json——切换成功沉淀偏好 / 每次调用记统计都会写盘）
    kwargs.setdefault("prefs_path", tmp_path / "prefs.md")
    kwargs.setdefault("pause_path", tmp_path / "pause.json")
    kwargs.setdefault("stats_path", tmp_path / "stats.json")
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
    assert finished.data["text"] == result.text
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
    pipeline = _pipeline(box, tmp_path, confirm_mode="always")
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


def test_run_restores_from_probe_queue(tmp_path):
    """测试跨进程故障记忆恢复：启动时从 probe 队列恢复故障模型状态。"""
    from model_switch.probe import ProbeQueue

    # 预置 probe 队列：两个模型已知故障
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    queue = ProbeQueue(probe_dir)
    queue.enqueue("a/g1/m1", error_class="401", task="任务A")
    queue.enqueue("a/g1/m2", error_class="429", task="任务B")

    # 创建 pipeline，注入 probe_queue
    stream = EventStream()
    client = MockModelClient()
    toolbox = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = Pipeline(
        toolbox,
        recorder=FaultRecorder(None, faults_dir=tmp_path / "faults"),
        probe_dir=probe_dir,
        prefs_path=tmp_path / "prefs.md",
        pause_path=tmp_path / "pause.json",
        stats_path=tmp_path / "stats.json",
    )

    # 验证：启动时自动恢复故障状态
    pipeline.restore_from_probe_queue()
    assert pipeline.toolbox.registry.find("a/g1/m1").available is False
    assert pipeline.toolbox.registry.find("a/g1/m2").available is False
    assert pipeline.toolbox.registry.find("b/g1/m3").available is None


# -- 切换偏好（plan_prefs）闭环 -----------------------------------------


def test_run_llm_switch_success_saves_plan_pref(tmp_path):
    """首次故障：LLM 决策切换成功后沉淀切换偏好。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="a/g1/m2", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    result = pipeline.run("写个爬虫")

    assert result.ok is True
    assert result.model_ref == "a/g1/m2"
    assert result.switches == 1
    # 沉淀：401 -> a/g1/m2（LLM 决策切换成功）
    assert load_prefs(prefs_path)["plan_prefs"]["401"] == ("a/g1/m2", 1)


def test_run_pref_hit_second_time_program_switch(tmp_path):
    """同错类第二次故障：切换偏好命中，程序直接切换（零 LLM 切换决策调用）。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    save_prefs(
        {"model_labels": {}, "task_prefs": {}, "plan_prefs": {"401": ("a/g1/m2", 1)}}, prefs_path
    )
    client = MockDecisionClient(default_model="a/g1/m1", fail_models={"m1": "401"})
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    result = pipeline.run("写个爬虫")

    assert result.ok is True
    assert result.model_ref == "a/g1/m2"  # 偏好目标
    assert result.switches == 1
    # 切换决策零 LLM 调用（只发生了选型决策一次）
    plan_calls = [
        c for c in client.calls if "故障切换决策者" in c["messages"][-1]["content"]
    ]
    assert len(plan_calls) == 0
    types = [e.type for e in stream]
    assert EventType.PLAN_PREF_HIT in types
    # 命中后成功：命中次数累计
    assert load_prefs(prefs_path)["plan_prefs"]["401"] == ("a/g1/m2", 2)


def test_run_pref_hit_target_fails_then_llm_corrects(tmp_path):
    """偏好目标也故障：不命中（目标 available=False），走 LLM 修正并更新偏好。"""
    stream = EventStream()
    prefs_path = tmp_path / "prefs.md"
    save_prefs(
        {"model_labels": {}, "task_prefs": {}, "plan_prefs": {"401": ("a/g1/m2", 1)}}, prefs_path
    )
    client = MockDecisionClient(
        default_model="a/g1/m1",
        switch_to="b/g1/m3",
        fail_models={"m1": "401", "m2": "401"},  # 偏好目标 m2 同错类再故障
    )
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)

    result = pipeline.run("写个爬虫")

    assert result.ok is True
    assert result.model_ref == "b/g1/m3"  # LLM 修正切到保底
    assert result.switches == 2
    plan_calls = [
        c for c in client.calls if "故障切换决策者" in c["messages"][-1]["content"]
    ]
    assert len(plan_calls) == 1  # 仅 m2 失败后走了一次 LLM 切换决策
    types = [e.type for e in stream]
    assert EventType.PLAN_PREF_HIT in types  # 第一次命中（m1 401）
    assert EventType.PLAN_PREF_MISS in types  # 第二次未命中（m2 已不可用）
    # 偏好更新：401 -> b/g1/m3（换目标重置为 1）
    assert load_prefs(prefs_path)["plan_prefs"]["401"] == ("b/g1/m3", 1)


# -- 暂停（冷却）闭环 ---------------------------------------------------


def test_run_applies_pause_seconds_and_persists(tmp_path):
    """决策返回 pause_seconds：失败模型被暂停（事件 + 持久 pause.json）。"""
    stream = EventStream()
    pause_path = tmp_path / "pause.json"
    choose_m1 = json.dumps({"model": "a/g1/m1", "reason": "合适"})
    switch_m2 = json.dumps(
        {"switch_to": "a/g1/m2", "diagnose": False, "pause_seconds": 60, "reason": "顶上"}
    )
    switch_m3 = json.dumps(
        {"switch_to": "b/g1/m3", "diagnose": False, "pause_seconds": 60, "reason": "再顶"}
    )
    fail_429 = CallResult(ok=False, model="x", error_class="429", detail="rate limited")
    # 调用序列：选型 -> m1 任务(429) -> 切换决策(m2, 冷却 m1) -> m2 任务(429)
    #          -> 切换决策(m3, 冷却 m2) -> m3 任务成功
    client = SequenceClient([choose_m1, fail_429, switch_m2, fail_429, switch_m3])
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)
    result = pipeline.run("任务")

    assert result.ok is True
    assert result.model_ref == "b/g1/m3"
    assert result.switches == 2
    types = [e.type for e in stream]
    assert types.count(EventType.MODEL_PAUSED) == 2  # m1 与 m2 均被冷却
    from model_switch.model import load_pause_state

    assert set(load_pause_state(pause_path)) == {"a/g1/m1", "a/g1/m2"}


def test_mechanical_skips_paused_models(tmp_path):
    """机械兜底候选排除暂停中模型（暂停者不试）。"""
    stream = EventStream()
    client = MockModelClient(rules={"m3": "401"})  # 选型决策全灭 -> 机械兜底
    box = _toolbox(tmp_path, stream=stream, client=client)
    box.registry.find("a/g1/m1").pause(60)  # 清单序首个暂停

    result = _pipeline(box, tmp_path).run("任务")

    assert result.ok is True
    assert result.outcome == "fallback"
    assert result.model_ref == "a/g1/m2"  # m1 暂停被跳过，m2 兜底成功
    mech = next(e for e in stream if e.type == EventType.MECHANICAL_FALLBACK)
    assert "a/g1/m1" not in mech.data["tried_models"]


def test_run_restores_pause_state(tmp_path):
    """启动时从持久暂停表恢复（过期条目顺手清扫）。"""
    from datetime import datetime, timedelta, timezone

    from model_switch.model import load_pause_state, save_pause_state

    pause_path = tmp_path / "pause.json"
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    save_pause_state({"a/g1/m1": future, "a/g1/m2": past}, pause_path)

    stream = EventStream()
    client = MockModelClient(rules={"m3": "401"})  # 选型决策全灭 -> 机械兜底
    box = _toolbox(tmp_path, stream=stream, client=client)
    pipeline = _pipeline(box, tmp_path)
    result = pipeline.run("任务")

    # m1 恢复暂停态被兜底跳过；m2 过期暂停被清扫、可兜底
    assert result.model_ref == "a/g1/m2"
    assert load_pause_state(pause_path) == {"a/g1/m1": future}  # 过期条目已清扫


# -- run 统计（阶段6 增强项 ③ stats）-------------------------------------


def test_run_records_stats_per_model(tmp_path):
    """run 闭环每次模型调用记录统计（成功与失败都记，滚动窗口）。"""
    from model_switch.stats import load_stats, summary

    stats_path = tmp_path / "stats.json"
    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="a/g1/m2", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=EventStream(), client=client)
    pipeline = _pipeline(box, tmp_path, stats_path=stats_path)
    pipeline.run("写个爬虫")  # m1 失败 401 -> 切 m2 成功

    stats = load_stats(stats_path)
    assert stats["a/g1/m1"]["recent"] == [[False, 42, 0]]  # 失败调用也记录
    assert stats["a/g1/m2"]["recent"] == [[True, 42, 1]]  # 成功 + 切换数 1
    # 单次样本：summary 门槛内返回 None
    assert summary("a/g1/m1", stats=stats) is None
    assert summary("a/g1/m2", stats=stats) is None


def test_run_mechanical_records_stats(tmp_path):
    """机械兜底路径的调用同样记录统计（决策调用不记——stats 语义=任务执行经验）。"""
    from model_switch.stats import load_stats

    stats_path = tmp_path / "stats.json"
    client = MockModelClient(rules={"m3": "401"})  # 决策全灭 -> 机械兜底
    box = _toolbox(tmp_path, stream=EventStream(), client=client)
    pipeline = _pipeline(box, tmp_path, stats_path=stats_path)
    pipeline.run("任务")  # 兜底：m3 决策调用失败（不记）-> m1 兜底成功（记）

    stats = load_stats(stats_path)
    assert stats["a/g1/m1"]["recent"][0][0] is True  # 兜底成功记 m1
    assert "b/g1/m3" not in stats  # 决策调用不记（非任务执行）
