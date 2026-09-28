"""周期探测：故障模型入队持久化，定期重测连通，恢复出队。

- 队列：data/probe/queue.json（跨进程持久——run 故障时登记，mso probe 消费）
- 单轮：队列逐个 test_connectivity -> 恢复者发 probe_result(ok) + recovered、出队、registry 标可用
- 循环：按 config.probe_interval 周期重测（--watch；嵌入宿主可派后台线程/子进程调同一接口）
- "自动切回"：恢复即回归可用池，下次选型自然可用（不硬切正在执行的任务）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .events import EventType
from .safety import redact

DEFAULT_PROBE_DIR = Path("data/probe")


class ProbeQueue:
    """故障模型探测队列：JSON 持久化（跨进程）。"""

    def __init__(self, probe_dir: Path | str = DEFAULT_PROBE_DIR):
        self.probe_dir = Path(probe_dir)

    @property
    def _path(self) -> Path:
        return self.probe_dir / "queue.json"

    def entries(self) -> list[dict[str, Any]]:
        """获取所有队列条目（兼容 load_all 语义）。"""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def load_all(self) -> list[dict[str, Any]]:
        """加载所有队列条目（别名方法，语义更明确）。"""
        return self.entries()

    def enqueue(self, model_ref: str, *, error_class: str = "", task: str = "") -> dict[str, Any]:
        """故障模型入队（已在队则更新错误与时间）；返回该条目。"""

        from datetime import datetime, timezone

        entries = self.entries()
        entry = {
            "model_ref": model_ref,
            "enqueued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error_class": error_class,
            "task": task,
        }
        entries = [e for e in entries if e.get("model_ref") != model_ref]
        entries.append(entry)
        self._write(entries)
        return entry

    def remove(self, model_ref: str) -> bool:
        entries = self.entries()
        remaining = [e for e in entries if e.get("model_ref") != model_ref]
        removed = len(remaining) != len(entries)
        if removed:
            self._write(remaining)
        return removed

    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.probe_dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def probe_once(
    toolbox: Any,
    queue: ProbeQueue | None = None,
    *,
    probe_dir: Path | str = DEFAULT_PROBE_DIR,
) -> dict[str, Any]:
    """单轮探测全部队列；恢复者出队 + probe_result/recovered 事件 + registry 标可用。"""

    queue = queue if queue is not None else ProbeQueue(probe_dir)
    results: dict[str, bool] = {}
    recovered: list[str] = []
    for entry in queue.entries():
        ref = str(entry.get("model_ref", ""))
        if not ref or toolbox.registry.find(ref) is None:
            queue.remove(ref)  # 未知模型（配置已删）：清出队列
            continue
        checks = toolbox.test_connectivity(ref)
        ok = bool(checks.get(ref, {}).get("ok"))
        _emit(toolbox, EventType.PROBE_RESULT, {"model_ref": ref, "ok": ok})
        results[ref] = ok
        if ok:
            queue.remove(ref)
            toolbox.registry.update(ref, available=True, last_error_class="", last_error_detail="")
            _emit(toolbox, EventType.RECOVERED, {"model_ref": ref})
            recovered.append(ref)
    return {"probed": len(results), "recovered": recovered, "results": results}


def probe_watch(
    toolbox: Any,
    *,
    interval: int | None = None,
    queue: ProbeQueue | None = None,
    probe_dir: Path | str = DEFAULT_PROBE_DIR,
    stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """循环探测（--watch / 宿主后台任务）；stop 为可注入停止条件（测试用）。"""

    seconds = interval if interval is not None else int(toolbox.config.probe_interval)
    summary: dict[str, Any] = {"rounds": 0, "recovered": []}
    while True:
        result = probe_once(toolbox, queue, probe_dir=probe_dir)
        summary["rounds"] += 1
        summary["recovered"].extend(result["recovered"])
        if stop is not None and stop():
            break
        time.sleep(seconds)
    return summary


def _emit(toolbox: Any, type_: str, data: dict[str, Any]) -> None:
    if toolbox.stream is not None:
        toolbox.stream.emit(type_, redact(data))
