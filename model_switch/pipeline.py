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
from .model import DEFAULT_PAUSE_PATH, load_pause_state, prune_pause_state, restore_pause_state, save_pause_state
from .prefs import DEFAULT_PREFS_PATH, add_plan_pref
from .probe import DEFAULT_PROBE_DIR, ProbeQueue
from .safety import redact
from .stats import DEFAULT_STATS_PATH, record_run

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
        probe_queue: ProbeQueue | None = None,
        probe_dir: Any = DEFAULT_PROBE_DIR,
        prefs_path: Any = DEFAULT_PREFS_PATH,
        pause_path: Any = DEFAULT_PAUSE_PATH,
        stats_path: Any = DEFAULT_STATS_PATH,
    ):
        self.toolbox = toolbox
        self.confirm_mode = confirm_mode
        self.max_switches = max_switches
        self.manager = manager
        self.prefs_path = prefs_path
        self.pause_path = pause_path
        self.stats_path = stats_path
        self.recorder = recorder or FaultRecorder(toolbox.stream, faults_dir=faults_dir)
        self.probe_queue = probe_queue if probe_queue is not None else ProbeQueue(probe_dir)

    def run(self, task: str, prompt: str | None = None) -> RunResult:
        """执行一次任务闭环。
        
        Args:
            task: 任务文本（若提供 prompt，task 作为上下文拼接）
            prompt: 自定义 prompt（可选）
        """
        # 若提供 prompt，将 task 作为上下文拼接
        actual_task = f"{prompt}\n\n上下文：{task}" if prompt else task

        # 启动时从探测队列恢复故障模型状态 + 从暂停表恢复冷却状态（过期顺手清扫）
        self.restore_from_probe_queue()
        self.restore_pauses()

        trace_id = self.toolbox.stream.trace_id if self.toolbox.stream is not None else ""
        self._emit(EventType.PIPELINE_STARTED, {"task": actual_task})
        mgr = (
            self.manager
            if self.manager is not None
            else DecisionManager(
                self.toolbox, prefs_path=self.prefs_path, stats_path=self.stats_path
            )
        )

        failed: set[str] = set()
        switches = 0
        attempts = 0
        last_error = ""
        last_switch_error = ""  # 触发最后一次切换的错误类别（切换成功后沉淀偏好）

        # 1. 选型（决策全灭 -> 直接机械兜底）
        try:
            decision = mgr.choose_model(actual_task, confirm_mode=self.confirm_mode)
            current = str(decision.decision["model"])
        except DecisionError:
            return self._mechanical(actual_task, failed, attempts, switches, trace_id)

        # 2. 调用 -> 失败 -> 切换循环
        while True:
            attempts += 1
            result = self.toolbox.call_model(current, [{"role": "user", "content": actual_task}])
            self._record_stats(
                current, result, switches, call_kind="task", task_type="general"
            )
            if result.ok:
                # 切换后成功：沉淀切换偏好（同错类下次程序直切；失败不阻断结果返回）
                if switches > 0 and last_switch_error:
                    try:
                        add_plan_pref(last_switch_error, current, self.prefs_path)
                    except OSError:
                        pass
                outcome = OUTCOME_OK if switches == 0 else OUTCOME_SWITCHED
                return self._finish(
                    RunResult(True, outcome, current, result.text, attempts, "", switches, trace_id)
                )
            failed.add(current)
            last_error = result.error_class
            self._on_fault(current, result.error_class, result.detail, actual_task)
            if switches >= self.max_switches:
                return self._mechanical(actual_task, failed, attempts, switches, trace_id, last_error)
            try:
                plan = mgr.plan_recovery(current, result.error_class, detail=result.detail)
                target = str(plan.decision["switch_to"])
            except DecisionError:
                return self._mechanical(actual_task, failed, attempts, switches, trace_id, last_error)
            # 失败模型冷却（决策建议的可选 pause_seconds）
            pause_seconds = plan.decision.get("pause_seconds")
            if (
                isinstance(pause_seconds, int)
                and not isinstance(pause_seconds, bool)
                and 1 <= pause_seconds <= 86400
            ):
                self._apply_pause(current, pause_seconds, reason="故障切换决策建议冷却")
            self._emit(
                EventType.SWITCH_TRIGGERED,
                {"from": current, "to": target, "error": result.error_class},
            )
            current = target
            switches += 1
            last_switch_error = result.error_class
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
            str(status.ref)
            for status in self.toolbox.registry.all()
            if str(status.ref) not in failed and not status.is_paused()
        ]
        results: dict[str, dict[str, Any]] = {}
        for ref in candidates:
            attempts += 1
            call = self.toolbox.call_model(ref, [{"role": "user", "content": task}])
            self._record_stats(
                ref, call, switches, call_kind="task", task_type="general"
            )
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
            self._on_fault(ref, call.error_class, call.detail, task)
        self._emit(
            EventType.MECHANICAL_FALLBACK,
            {"tried_models": list(results), "results": results},
        )
        return self._finish(
            RunResult(False, OUTCOME_FAILED, "", "", attempts, last_error, switches, trace_id)
        )

    def restore_from_probe_queue(self) -> None:
        """启动时从探测队列恢复故障模型状态。

        读取持久化的探测队列，将其中记录的故障模型在 ModelRegistry 中标记为不可用，
        以便后续选型/切换逻辑跳过这些已知故障的模型。
        """
        for entry in self.probe_queue.entries():
            ref = entry.get("model_ref")
            if not ref:
                continue
            status = self.toolbox.registry.find(ref)
            if status is not None:
                status.available = False
                status.last_error_class = entry.get("error_class", "")
                status.last_error_detail = entry.get("task", "")
                status.last_checked = entry.get("enqueued_at", "")

    def restore_pauses(self) -> None:
        """启动时从持久暂停表恢复冷却状态（过期条目顺手清扫=到期自动解除）。"""

        pauses = restore_pause_state(self.toolbox.registry, self.pause_path)
        try:
            save_pause_state(pauses, self.pause_path)
        except OSError:
            pass  # 暂停表清扫写回失败不阻断主流程（内存态已生效）

    def _apply_pause(self, model_ref: str, seconds: int, *, reason: str) -> None:
        """暂停（冷却）一个模型：内存态 + 持久表 + 事件；到期自动解除。"""

        status = self.toolbox.registry.find(model_ref)
        if status is None:
            return
        status.pause(seconds)
        try:
            pauses = prune_pause_state(load_pause_state(self.pause_path))
            if status.paused_until:
                pauses[model_ref] = status.paused_until
            save_pause_state(pauses, self.pause_path)
        except OSError:
            pass  # 暂停持久化失败不阻断主流程（内存态仍生效，本进程内有效）
        self._emit(
            EventType.MODEL_PAUSED,
            {"model_ref": model_ref, "seconds": seconds, "reason": reason},
        )

    def _record_stats(
        self,
        model_ref: str,
        result: Any,
        switches: int,
        *,
        call_kind: str,
        task_type: str,
    ) -> None:
        """记录一次模型调用结果（滚动窗口经验分）；写盘失败不阻断主流程。

        阶段2：写入 v2 对象记录，包含错误类别、协议、Token（缺失为 None）。
        """

        try:
            status = self.toolbox.registry.find(model_ref)
            protocol = status.protocol if status is not None else "openai_chat"
            usage = result.usage or {}
            record_run(
                model_ref,
                result.ok,
                result.latency_ms,
                switches,
                self.stats_path,
                call_kind=call_kind,
                task_type=task_type,
                protocol=protocol,
                error_class=None if result.ok else (result.error_class or None),
                input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
                output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
        except OSError:
            pass

    def _on_fault(self, model_ref: str, error_class: str, detail: str, task: str) -> None:
        """故障入档 + 登记探测队列（恢复探测走 mso probe / --watch）。"""

        self.recorder.record(model_ref, error_class, detail=detail, task=task)
        try:
            self.probe_queue.enqueue(model_ref, error_class=error_class, task=task)
        except OSError:
            pass  # 探测队列写入失败不阻断主流程

    # -- 内部 ---------------------------------------------------------

    def _finish(self, result: RunResult) -> RunResult:
        self._emit(
            EventType.PIPELINE_FINISHED,
            {
                "outcome": result.outcome,
                "text": result.text,
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
