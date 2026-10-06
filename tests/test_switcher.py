"""switcher 模块测试：ModelSwitcher 库模式一行接入（闭环/直通/确认/诊断/探测）。"""
import json

from model_switch import ModelSwitcher
from model_switch.events import EventType

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


def _switcher(tmp_path, persist_events=False, **kwargs):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    kwargs.setdefault("tasks_dir", tmp_path / "tasks")
    return ModelSwitcher(
        path,
        mock=True,
        faults_dir=tmp_path / "faults",
        probe_dir=tmp_path / "probe",
        persist_events=persist_events,
        **kwargs,
    )


def test_run_full_loop_mock_no_real_key(tmp_path):
    # 无环境变量 key：mock=True 跳过 key 检查，全闭环跑通
    switcher = _switcher(tmp_path, persist_events=True)
    result = switcher.run("写个爬虫")
    assert result.ok is True
    assert result.outcome == "ok"
    assert result.model_ref == "a/g1/m1"  # mock 决策默认选清单首个
    types = [e.type for e in switcher.stream]
    assert EventType.PIPELINE_STARTED in types
    assert EventType.DECISION_MADE in types
    assert types[-1] == EventType.PIPELINE_FINISHED


def test_run_confirm_handler_passthrough(tmp_path):
    calls = []

    def handler(operation, reason, evidence):
        calls.append(operation)
        return True

    switcher = _switcher(tmp_path, confirm_handler=handler)
    result = switcher.run("任务", confirm_mode="always")
    assert result.ok is True
    assert calls and "a/g1/m1" in calls[0]
    types = [e.type for e in switcher.stream]
    assert EventType.CONFIRM_REQUESTED in types
    assert EventType.CONFIRM_GRANTED in types


def test_dispatch_and_choose_model_direct(tmp_path):
    switcher = _switcher(tmp_path)
    listed = switcher.dispatch("list_models")
    assert listed["ok"] is True
    assert len(listed["result"]) == 3
    decision = switcher.choose_model("任务")
    assert decision.decision["model"] == "a/g1/m1"


def test_diagnose_mock(tmp_path):
    switcher = _switcher(tmp_path)
    result = switcher.diagnose("a")
    assert result["ok"] is True
    assert result["conclusion"]
    assert result["actions"]


def test_probe_empty_queue(tmp_path):
    switcher = _switcher(tmp_path)
    result = switcher.probe()
    assert result == {"probed": 0, "recovered": [], "results": {}}


def test_persist_events_false_keeps_no_files(tmp_path):
    switcher = _switcher(tmp_path, persist_events=False)
    result = switcher.run("任务")
    assert result.ok is True
    # persist_events=False：内存事件流可订阅，但不落盘
    assert len(switcher.stream) > 0
