"""事件流：一次任务处理 = 一条 trace_id。

事件格式：{trace_id, seq, ts, type, cause, data}
- seq    trace 内单调递增
- cause  触发本事件的上一事件 seq，形成追溯链（根事件为 None）
- 事件追加式、不可变；库可订阅；可落盘 data/events/<trace_id>.jsonl 供回放审计
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .safety import redact


class EventType:
    """内置事件类型（详见设计文档事件类型表）。"""

    PIPELINE_STARTED = "pipeline_started"
    DECISION_MODEL_PICKED = "decision_model_picked"
    DECISION_REQUESTED = "decision_requested"
    DECISION_MADE = "decision_made"
    MODEL_CALLED = "model_called"
    MODEL_RESPONDED = "model_responded"
    MODEL_FAILED = "model_failed"
    SWITCH_TRIGGERED = "switch_triggered"
    SWITCHED_TO = "switched_to"
    MECHANICAL_FALLBACK = "mechanical_fallback"
    FAULT_RECORDED = "fault_recorded"
    PLAN_PREF_HIT = "plan_pref_hit"
    PLAN_PREF_MISS = "plan_pref_miss"
    MODEL_PAUSED = "model_paused"
    MODEL_RESUMED = "model_resumed"
    DIAGNOSIS_STARTED = "diagnosis_started"
    DIAGNOSIS_FACTS = "diagnosis_facts"
    DIAGNOSIS_CONCLUSION = "diagnosis_conclusion"
    CONFIRM_REQUESTED = "confirm_requested"
    CONFIRM_GRANTED = "confirm_granted"
    CONFIRM_DENIED = "confirm_denied"
    FIX_APPLIED = "fix_applied"
    RECHECK_RESULT = "recheck_result"
    RECOVERED = "recovered"
    PROBE_RESULT = "probe_result"
    ONBOARDING_CONNECTIVITY = "onboarding_connectivity"
    ONBOARDING_PROFILE = "onboarding_profile"
    ONBOARDING_PROFILE_CONFIRMED = "onboarding_profile_confirmed"
    ONBOARDING_LABELS_SAVED = "onboarding_labels_saved"
    PIPELINE_FINISHED = "pipeline_finished"


EventType.ALL = frozenset(
    value for name, value in vars(EventType).items()
    if not name.startswith("_") and isinstance(value, str)
)


@dataclass(frozen=True)
class Event:
    """单个事件：追加式、不可变。"""

    trace_id: str
    seq: int
    ts: str
    type: str
    cause: int | None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "cause": self.cause,
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    def human_line(self) -> str:
        """CLI 人读格式：[#seq ←cause] type | k=v ..."""

        cause = f" ←{self.cause}" if self.cause is not None else ""
        detail = " ".join(f"{k}={v}" for k, v in self.data.items()) or "-"
        return f"[#{self.seq}{cause}] {self.type} | {detail}"


_AUTO = object()  # cause 缺省哨兵：自动指向上一个事件


class EventStream:
    """一个 trace 的事件流：追加式、不可变、可订阅、可落盘。"""

    def __init__(self, trace_id: str | None = None, persist_dir: Path | str | None = None):
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self._events: list[Event] = []
        self._subscribers: list[Callable[[Event], None]] = []
        self._persist_path: Path | None = None
        if persist_dir is not None:
            persist_dir = Path(persist_dir)
            persist_dir.mkdir(parents=True, exist_ok=True)
            self._persist_path = persist_dir / f"{self.trace_id}.jsonl"
            self._persist_path.write_text("", encoding="utf-8")

    # -- 查询 ---------------------------------------------------------

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def last(self) -> Event | None:
        return self._events[-1] if self._events else None

    # -- 订阅 ---------------------------------------------------------

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        """库可订阅：每个事件追加后回调一次。"""

        self._subscribers.append(callback)

    # -- 发事件 -------------------------------------------------------

    def emit(self, type_: str, data: dict[str, Any] | None = None, cause: Any = _AUTO) -> Event:
        """追加一个事件；data 自动脱敏；cause 缺省指向上一个事件 seq。"""

        if cause is _AUTO:
            last = self.last()
            cause = last.seq if last is not None else None
        payload = redact(dict(data or {}))
        event = Event(
            trace_id=self.trace_id,
            seq=len(self._events) + 1,
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            type=type_,
            cause=cause,
            data=payload,
        )
        self._events.append(event)
        for callback in self._subscribers:
            callback(event)
        if self._persist_path is not None:
            with self._persist_path.open("a", encoding="utf-8") as fh:
                fh.write(event.to_json() + "\n")
        return event

    # -- 输出 ---------------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]

    def to_json(self, pretty: bool = False) -> str:
        if pretty:
            return json.dumps(self.to_list(), ensure_ascii=False, indent=2, default=str)
        return json.dumps(self.to_list(), ensure_ascii=False, default=str)

    def human_lines(self) -> str:
        return "\n".join(event.human_line() for event in self._events)
