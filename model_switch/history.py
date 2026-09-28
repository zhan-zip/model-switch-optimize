"""故障历史写入侧：data/faults/ 落盘 + fault_recorded 事件。

记录文件 fault-<序号>.json 递增；内容自动脱敏；读取侧见 tools.fault_history（阶段2）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EventType
from .safety import redact

DEFAULT_FAULTS_DIR = Path("data/faults")


class FaultRecorder:
    """故障入档：JSON 落盘（序号递增）+ 事件留痕（fault_recorded），自动脱敏。"""

    def __init__(self, stream: Any | None = None, faults_dir: Path | str = DEFAULT_FAULTS_DIR):
        self.stream = stream
        self.faults_dir = Path(faults_dir)

    def record(
        self,
        model_ref: str,
        error_class: str,
        *,
        detail: str = "",
        task: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """一次故障入档；返回落盘内容（已脱敏）。"""

        self.faults_dir.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq()
        payload: dict[str, Any] = {
            "seq": seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model_ref": model_ref,
            "error_class": error_class,
            "detail": detail,
            "task": task,
        }
        if extra:
            payload.update(extra)
        payload = redact(payload)
        path = self.faults_dir / f"fault-{seq:04d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.stream is not None:
            self.stream.emit(
                EventType.FAULT_RECORDED,
                {"model_ref": model_ref, "snapshot": payload},
            )
        return payload

    def _next_seq(self) -> int:
        """现有最大序号 + 1（文件被清理也保持单调不覆盖）。"""

        highest = 0
        for path in self.faults_dir.glob("fault-*.json"):
            try:
                highest = max(highest, int(path.stem.split("-")[-1]))
            except ValueError:
                continue
        return highest + 1
