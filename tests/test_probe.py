"""probe 模块测试：探测队列持久化、单轮探测、恢复出队、循环、run 联动。"""
import json
import time

from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.mocker import MockConsole, MockModelClient
from model_switch.model import ModelRegistry
from model_switch.pipeline import Pipeline
from model_switch.probe import ProbeQueue, probe_once, probe_watch
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


def _toolbox(tmp_path, stream=None, client=None):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    return Toolbox(
        config, ModelRegistry(config), stream,
        model_client=client or MockModelClient(),
        console=MockConsole(), mock=True,
    )


def test_probe_queue_enqueue_dedup_and_persist(tmp_path):
    probe_dir = tmp_path / "probe"
    queue = ProbeQueue(probe_dir)
    queue.enqueue("a/g1/m1", error_class="401", task="写爬虫")
    queue.enqueue("a/g1/m1", error_class="429", task="写爬虫")  # 去重（更新）
    queue.enqueue("a/g1/m2", error_class="5xx")

    # 跨实例持久化（另一进程视角）
    queue2 = ProbeQueue(probe_dir)
    refs = [e["model_ref"] for e in queue2.entries()]
    assert refs == ["a/g1/m1", "a/g1/m2"]  # 按入队顺序；重复入队去重更新
    assert queue2.entries()[0]["error_class"] == "429"  # 重复入队去重更新

    assert queue2.remove("a/g1/m1") is True
    assert [e["model_ref"] for e in queue2.entries()] == ["a/g1/m2"]
    assert queue2.remove("a/g1/m1") is False


def test_probe_once_recovers_and_dequeues(tmp_path):
    stream = EventStream()
    # m1 恢复（ok）、m2 仍故障（401）
    client = MockModelClient(rules={"m2": "401"})
    box = _toolbox(tmp_path, stream=stream, client=client)
    box.registry.update("a/g1/m1", available=False)
    box.registry.update("a/g1/m2", available=False)

    queue = ProbeQueue(tmp_path / "probe")
    queue.enqueue("a/g1/m1", error_class="401")
    queue.enqueue("a/g1/m2", error_class="429")

    result = probe_once(box, queue)

    assert result["probed"] == 2
    assert result["recovered"] == ["a/g1/m1"]
    assert result["results"] == {"a/g1/m1": True, "a/g1/m2": False}

    # 恢复者出队、registry 标可用；未恢复留队
    assert [e["model_ref"] for e in queue.entries()] == ["a/g1/m2"]
    assert box.registry.find("a/g1/m1").available is True
    assert box.registry.find("a/g1/m2").available is False

    types = [e.type for e in stream]
    assert types.count(EventType.PROBE_RESULT) == 2
    assert EventType.RECOVERED in types
    recovered = next(e for e in stream if e.type == EventType.RECOVERED)
    assert recovered.data["model_ref"] == "a/g1/m1"


def test_probe_once_empty_queue(tmp_path):
    box = _toolbox(tmp_path)
    queue = ProbeQueue(tmp_path / "probe")
    result = probe_once(box, queue)
    assert result == {"probed": 0, "recovered": [], "results": {}}


def test_probe_once_unknown_model_cleaned(tmp_path):
    box = _toolbox(tmp_path)
    queue = ProbeQueue(tmp_path / "probe")
    queue.enqueue("x/y/z", error_class="401")  # 配置已删的模型
    result = probe_once(box, queue)
    assert result["probed"] == 0
    assert queue.entries() == []  # 清出队列


def test_probe_watch_stops_by_condition(tmp_path):
    box = _toolbox(tmp_path)
    queue = ProbeQueue(tmp_path / "probe")
    rounds = {"n": 0}

    def stop():
        rounds["n"] += 1
        return rounds["n"] >= 2

    started = time.monotonic()
    summary = probe_watch(box, queue=queue, interval=0, stop=stop)
    assert summary["rounds"] == 2
    assert time.monotonic() - started < 5  # interval=0 快速循环


def test_probe_once_skips_paused_models(tmp_path):
    """暂停中（冷却）的模型跳过本轮探测：留队、不计入结果。"""
    stream = EventStream()
    client = MockModelClient()  # 一切 ok
    box = _toolbox(tmp_path, stream=stream, client=client)
    box.registry.update("a/g1/m1", available=False)
    box.registry.find("a/g1/m1").pause(60)  # m1 暂停中（冷却）

    queue = ProbeQueue(tmp_path / "probe")
    queue.enqueue("a/g1/m1", error_class="401")
    result = probe_once(box, queue)

    assert result["probed"] == 0  # 跳过，不探测
    assert [e["model_ref"] for e in queue.entries()] == ["a/g1/m1"]  # 留队下轮
    assert EventType.PROBE_RESULT not in [e.type for e in stream]


def test_pipeline_failure_enqueues_probe(tmp_path):
    stream = EventStream()
    from model_switch.mocker import MockDecisionClient

    client = MockDecisionClient(
        default_model="a/g1/m1", switch_to="b/g1/m3", fail_models={"m1": "401"}
    )
    box = _toolbox(tmp_path, stream=stream, client=client)
    probe_dir = tmp_path / "probe"
    # 测试隔离：prefs/pause/stats 走 tmp（防止污染真实运行态文件）
    pipeline = Pipeline(
        box,
        recorder=None,
        faults_dir=tmp_path / "faults",
        probe_dir=probe_dir,
        prefs_path=tmp_path / "prefs.md",
        pause_path=tmp_path / "pause.json",
        stats_path=tmp_path / "stats.json",
    )
    result = pipeline.run("写个爬虫")

    assert result.ok is True  # m1 失败切保底成功
    assert result.outcome == "switched"
    queue = ProbeQueue(probe_dir)
    refs = [e["model_ref"] for e in queue.entries()]
    assert refs == ["a/g1/m1"]  # 故障模型自动入探测队列
    assert queue.entries()[0]["error_class"] == "401"
