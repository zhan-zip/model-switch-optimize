"""history 模块测试：故障入档（落盘 / 序号 / 事件 / 脱敏 / 读取集成）。"""
import json

from model_switch.events import EventStream, EventType
from model_switch.history import FaultRecorder
from model_switch.tools import Toolbox


def test_record_writes_json_and_emits_event(tmp_path):
    stream = EventStream()
    recorder = FaultRecorder(stream, faults_dir=tmp_path / "faults")
    payload = recorder.record("a/g1/m1", "401", detail="HTTP 401 Unauthorized", task="写爬虫")

    files = list((tmp_path / "faults").glob("fault-*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved == payload
    assert payload["model_ref"] == "a/g1/m1"
    assert payload["error_class"] == "401"
    assert payload["task"] == "写爬虫"

    types = [e.type for e in stream]
    assert EventType.FAULT_RECORDED in types
    event = next(e for e in stream if e.type == EventType.FAULT_RECORDED)
    assert event.data["model_ref"] == "a/g1/m1"
    assert event.data["snapshot"]["error_class"] == "401"


def test_record_seq_increases_and_survives_cleanup(tmp_path):
    faults = tmp_path / "faults"
    recorder = FaultRecorder(None, faults_dir=faults)
    recorder.record("a/g1/m1", "401")
    recorder.record("a/g1/m2", "429")
    assert recorder._next_seq() == 3
    # 删除首个文件后序号仍单调（不覆盖）
    (faults / "fault-0001.json").unlink()
    assert recorder._next_seq() == 3


def test_record_redacts_key_in_detail(tmp_path):
    stream = EventStream()
    recorder = FaultRecorder(stream, faults_dir=tmp_path / "faults")
    payload = recorder.record("a/g1/m1", "401", detail="Bearer sk-abcdef1234567890 被拒绝")
    text = json.dumps(payload, ensure_ascii=False)
    assert "sk-abcdef1234567890" not in text  # 完整 key 不落盘
    assert "sk-abcdef1234567890" not in stream.to_json()  # 完整 key 不入事件流
    assert payload["detail"] != "Bearer sk-abcdef1234567890 被拒绝"  # 已掩码


def test_record_extra_fields(tmp_path):
    recorder = FaultRecorder(None, faults_dir=tmp_path / "faults")
    payload = recorder.record("a/g1/m1", "429", extra={"latency_ms": 1200, "attempt": 2})
    assert payload["latency_ms"] == 1200
    assert payload["attempt"] == 2
