"""任务闭环编排：一次 run = 一条 trace。

选型 -> 调用 -> 失败入档 -> 切换（plan_recovery 决策）-> 机械兜底 -> 完成。

- 正常：选型 -> 调用成功 -> pipeline_finished(outcome=ok)
- 故障：失败 -> fault_recorded -> plan_recovery 决策切换 -> switched_to -> 调新模型（循环）
- 机械兜底（决策全灭 / 无可用 / 超切换上限）：按配置顺序逐个试调（跳过已失败者）
  -> mechanical_fallback -> 首个成功 pipeline_finished(outcome=fallback) / 全败 (outcome=failed)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .events import EventType
from .history import DEFAULT_FAULTS_DIR, FaultRecorder
from .manager import DecisionError, DecisionManager
from .safety import redact

MAX_SWITCHES = 5

OUTCOME_OK = "ok"
OUTCOME_SWITCHED = "switched"
OUTCOME_FALLBACK = "fallback"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class RunResult:
    """一次 run 的结果。"""

    ok: bool
    outcome: str  # ok / switched / fallback / failed
    model_ref: str
    text: str
    attempts: int
    error_class: str = ""
    switches: int = 0
    trace_id: str = ""


class Pipeline:
    """任务闭环：持有工具箱，编排选型 / 调用 / 切换 / 机械兜底 / 故障入档。"""

    def __init__(
        self,
        toolbox: Any,
        *,
        confirm_mode: str = "never",
        manager: DecisionManager | None = None,
        recorder: FaultRecorder | None = None,
        faults_dir: Any = DEFAULT_FAULTS_DIR,
        max_switches: int = MAX_SWITCHES,
    ):
        self.toolbox = toolbox
        self.confirm_mode = confirm_mode
        self.max_switches = max_switches
        self.manager = manager
        self.recorder = recorder or FaultRecorder(toolbox.stream, faults_dir=faults_dir)

    def run(self, task: str) -> RunResult:
        """执行一次任务闭环。"""

        trace_id = self.toolbox.stream.trace_id if self.toolbox.stream is not None else ""
        self._emit(EventType.PIPELINE_STARTED, {"task": task})
        mgr = self.manager if self.manager is not None else DecisionManager(self.toolbox)

        failed: set[str] = set()
        switches = 0
        attempts = 0
        last_error = ""

        # 1. 选型（决策全灭 -> 直接机械兜底）
        try:
            decision = mgr.choose_model(task, confirm_mode=self.confirm_mode)
            current = str(decision.decision["model"])
        except DecisionError:
            return self._mechanical(task, failed, attempts, switches, trace_id)

        # 2. 调用 -> 失败 -> 切换循环
        while True:
            attempts += 1
            result = self.toolbox.call_model(current, [{"role": "user", "content": task}])
            if result.ok:
                outcome = OUTCOME_OK if switches == 0 else OUTCOME_SWITCHED
                return self._finish(
                    RunResult(True, outcome, current, result.text, attempts, "", switches, trace_id)
                )
            failed.add(current)
            last_error = result.error_class
            self.recorder.record(
                current, result.error_class, detail=result.detail, task=task
            )
            if switches >= self.max_switches:
                return self._mechanical(task, failed, attempts, switches, trace_id, last_error)
            try:
                plan = mgr.plan_recovery(current, result.error_class, detail=result.detail)
                target = str(plan.decision["switch_to"])
            except DecisionError:
                return self._mechanical(task, failed, attempts, switches, trace_id, last_error)
            self._emit(
                EventType.SWITCH_TRIGGERED,
                {"from": current, "to": target, "error": result.error_class},
            )
            current = target
            switches += 1
            self._emit(EventType.SWITCHED_TO, {"to": target})

    # -- 机械兜底 -----------------------------------------------------

    def _mechanical(
        self,
        task: str,
        failed: set[str],
        attempts: int,
        switches: int,
        trace_id: str,
        last_error: str = "",
    ) -> RunResult:
        """按配置顺序逐个试调（跳过已失败者）；首个成功即用，全败报错。"""

        candidates = [
            str(status.ref) for status in self.toolbox.registry.all() if str(status.ref) not in failed
        ]
        results: dict[str, dict[str, Any]] = {}
        for ref in candidates:
            attempts += 1
            call = self.toolbox.call_model(ref, [{"role": "user", "content": task}])
            results[ref] = {"ok": call.ok, "error_class": "" if call.ok else call.error_class}
            if call.ok:
                self._emit(
                    EventType.MECHANICAL_FALLBACK,
                    {"tried_models": list(results), "results": results},
                )
                return self._finish(
                    RunResult(
                        True, OUTCOME_FALLBACK, ref, call.text, attempts, "", switches, trace_id
                    )
                )
            failed.add(ref)
            last_error = call.error_class
            self.recorder.record(ref, call.error_class, detail=call.detail, task=task)
        self._emit(
            EventType.MECHANICAL_FALLBACK,
            {"tried_models": list(results), "results": results},
        )
        return self._finish(
            RunResult(False, OUTCOME_FAILED, "", "", attempts, last_error, switches, trace_id)
        )

    # -- 内部 ---------------------------------------------------------

    def _finish(self, result: RunResult) -> RunResult:
        self._emit(
            EventType.PIPELINE_FINISHED,
            {
                "outcome": result.outcome,
                "summary": {
                    "ok": result.ok,
                    "model_ref": result.model_ref,
                    "attempts": result.attempts,
                    "switches": result.switches,
                    "error_class": result.error_class,
                },
            },
        )
        return result

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        if self.toolbox.stream is not None:
            self.toolbox.stream.emit(type_, redact(data))
