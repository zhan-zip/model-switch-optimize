"""events 模块测试：seq/cause 追溯链、不可变、订阅、落盘、自动脱敏。"""
import dataclasses
import json

import pytest

from model_switch.events import EventStream, EventType


def test_seq_increments_and_cause_chain():
    stream = EventStream()
    e1 = stream.emit(EventType.PIPELINE_STARTED, {"task": "demo"})
    e2 = stream.emit(EventType.MODEL_CALLED, {"model_ref": "a/g1/m1", "attempt": 1})
    e3 = stream.emit(EventType.MODEL_RESPONDED, {"latency_ms": 812})
    assert [e.seq for e in stream] == [1, 2, 3]
    assert e1.cause is None
    assert e2.cause == 1
    assert e3.cause == 2


def test_explicit_root_cause():
    stream = EventStream()
    stream.emit(EventType.PIPELINE_STARTED)
    e = stream.emit(EventType.MODEL_FAILED, {"error_class": "401"}, cause=None)
    assert e.cause is None


def test_event_is_immutable():
    stream = EventStream()
    event = stream.emit(EventType.PIPELINE_STARTED, {"task": "demo"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.type = "hacked"


def test_subscribe_receives_all_events():
    stream = EventStream()
    seen = []
    stream.subscribe(seen.append)
    stream.emit(EventType.PIPELINE_STARTED)
    stream.emit(EventType.PIPELINE_FINISHED, {"outcome": "ok"})
    assert len(seen) == 2
    assert seen[1].type == "pipeline_finished"


def test_persist_jsonl(tmp_path):
    stream = EventStream(trace_id="trace-0001", persist_dir=tmp_path)
    stream.emit(EventType.PIPELINE_STARTED, {"task": "demo"})
    stream.emit(EventType.MODEL_FAILED, {"error_class": "429"})
    path = tmp_path / "trace-0001.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    assert lines[1]["cause"] == 1
    assert all(line["trace_id"] == "trace-0001" for line in lines)


def test_emit_redacts_sensitive_data():
    stream = EventStream()
    key = "sk-abcdefgh1234567890abcdef"
    event = stream.emit(EventType.MODEL_CALLED, {"api_key": key, "model_ref": "a/g1/m1"})
    dumped = json.dumps(event.to_dict(), ensure_ascii=False)
    assert key not in dumped
    assert event.data["api_key"].endswith("****")
    assert event.data["model_ref"] == "a/g1/m1"


def test_to_json_roundtrip():
    stream = EventStream()
    stream.emit(EventType.DECISION_MADE, {"point": "choose_model", "choice": "a/g1/m1"})
    restored = json.loads(stream.to_json())
    assert restored[0]["type"] == "decision_made"
    assert restored[0]["data"]["choice"] == "a/g1/m1"


def test_event_type_table_complete():
    assert len(EventType.ALL) == 26
    assert "mechanical_fallback" in EventType.ALL
    assert "onboarding_labels_saved" in EventType.ALL
