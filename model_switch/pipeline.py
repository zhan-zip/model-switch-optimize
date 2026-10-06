"""任务闭环编排：一次 run = 一条 trace。

选型 -> 调用 -> 失败入档 -> 切换（plan_recovery 决策）-> 机械兜底 -> 完成。

- 正常：选型 -> 调用成功 -> pipeline_finished(outcome=ok)
- 故障：失败 -> fault_recorded -> plan_recovery 决策切换 -> switched_to -> 调新模型（循环）
- 机械兜底（决策全灭 / 无可用 / 超切换上限）：按配置顺序逐个试调（跳过已失败者）
  -> mechanical_fallback -> 首个成功 pipeline_finished(outcome=fallback) / 全败 (outcome=failed)

阶段3：两阶段审批。prepare 只选型不执行（写 data/tasks/），execute 执行已批准且未过期的
任务；run() 保持一调用完成（prepare+自动批准+execute 的兼容快捷方式）。
prepare 进程 ── task_id ──> execute 进程（跨进程继续执行）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .events import EventType
from .history import DEFAULT_FAULTS_DIR, FaultRecorder
from .manager import DecisionError, DecisionManager
from .model import DEFAULT_PAUSE_PATH, load_pause_state, prune_pause_state, restore_pause_state, save_pause_state
from .prefs import (
    DEFAULT_PREFS_PATH,
    add_plan_pref,
    add_task_pref,
    load_prefs,
    match_task_pref,
)
from .probe import DEFAULT_PROBE_DIR, ProbeQueue
from .safety import redact
from .stats import DEFAULT_STATS_PATH, record_run
from .task_store import (
    DEFAULT_TASKS_DIR,
    AWAITING_CONFIRMATION,
    COMPLETED,
    DENIED,
    EXPIRED,
    FAILED,
    READY,
    RUNNING,
    expires_at_from,
    is_finished,
    load_task,
    minimal_audit,
    new_task_id,
    prune_expired,
    save_task,
    update_task,
)

MAX_SWITCHES = 5

OUTCOME_OK = "ok"
OUTCOME_SWITCHED = "switched"
OUTCOME_FALLBACK = "fallback"
OUTCOME_FAILED = "failed"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    task_type: str = "general"


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
        tasks_dir: Any = DEFAULT_TASKS_DIR,
    ):
        self.toolbox = toolbox
        self.confirm_mode = confirm_mode
        self.max_switches = max_switches
        self.manager = manager
        self.prefs_path = prefs_path
        self.pause_path = pause_path
        self.stats_path = stats_path
        self.tasks_dir = tasks_dir
        self.recorder = recorder or FaultRecorder(toolbox.stream, faults_dir=faults_dir)
        self.probe_queue = probe_queue if probe_queue is not None else ProbeQueue(probe_dir)

    def run(self, task: str, prompt: str | None = None) -> RunResult:
        """执行一次任务闭环（阶段3 前行为不变：一调用完成）。
        
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
        mgr = self._manager()

        # 1. 选型（决策全灭 -> 直接机械兜底）
        try:
            decision = mgr.choose_model(actual_task, confirm_mode=self.confirm_mode)
            current = str(decision.decision["model"])
            task_type = str(decision.decision.get("task_type", "general"))
        except DecisionError:
            return self._mechanical(actual_task, set(), 0, 0, trace_id)

        # 2. 调用 -> 失败 -> 切换循环（与 execute 共用 _execute_flow）
        return self._execute_flow(actual_task, current, task_type, trace_id)

    # -- 两阶段审批（阶段3）---------------------------------------------

    def prepare(
        self,
        task: str,
        prompt: str | None = None,
        *,
        confirm_mode: str | None = None,
    ) -> dict[str, Any]:
        """准备任务：选型但不执行，任务状态落盘 data/tasks/ 供跨进程继续。

        返回 info dict：{task_id, status, task_type, selected_model, reason,
        expires_at, ok}。任务文本脱敏后写盘，不保存任何凭据。
        """
        actual_task = f"{prompt}\n\n上下文：{task}" if prompt else task
        mode = confirm_mode or self.confirm_mode

        # 选型需最新状态（故障恢复 / 冷却恢复）
        self.restore_from_probe_queue()
        self.restore_pauses()

        trace_id = self.toolbox.stream.trace_id if self.toolbox.stream is not None else ""
        self._emit(EventType.PIPELINE_STARTED, {"task": actual_task, "stage": "prepare"})
        mgr = self._manager()

        # 决策全灭：备一个 ready + selected_model="" 的任务，execute 时走机械兜底
        selected_model = ""
        reason = "决策模型全灭，执行时机械兜底"
        task_type = "general"
        try:
            decision = mgr.choose_model(actual_task, confirm_mode="never")
            selected_model = str(decision.decision["model"])
            reason = str(decision.decision.get("reason", ""))
            task_type = str(decision.decision.get("task_type", "general"))
        except DecisionError:
            pass

        # 状态判定：never 直接 ready；once 且任务偏好命中 -> ready；否则等确认
        status = READY if mode == "never" else AWAITING_CONFIRMATION
        if mode == "once":
            prefs = load_prefs(self.prefs_path)
            if match_task_pref(actual_task, prefs) is not None:
                status = READY

        task_id = new_task_id()
        created_at = _now_iso()
        record = {
            "version": 1,
            "task_id": task_id,
            "status": status,
            "task": redact(actual_task),
            "task_type": task_type,
            "selected_model": selected_model,
            "reason": reason,
            "confirm_mode": mode,
            "created_at": created_at,
            "expires_at": expires_at_from(created_at),
            "trace_id": trace_id,
        }
        save_task(record, self.tasks_dir)
        self._emit(
            EventType.TASK_PREPARED,
            {
                "task_id": task_id,
                "status": status,
                "task_type": task_type,
                "selected_model": selected_model,
                "reason": reason,
                "expires_at": record["expires_at"],
                "task": actual_task,
            },
        )
        return {
            "ok": True,
            "task_id": task_id,
            "status": status,
            "task_type": task_type,
            "selected_model": selected_model,
            "reason": reason,
            "expires_at": record["expires_at"],
        }

    def execute(
        self,
        task_id: str,
        *,
        user_confirmed: bool = False,
        model_override: str | None = None,
    ) -> RunResult:
        """执行一个已准备的任务：校验状态 -> 批准/拒绝/过期 -> 执行。

        - awaiting_confirmation 且 user_confirmed=false -> denied（任务不执行）
        - ready（自动批准）或 user_confirmed=true -> approved -> 执行
        - 过期 / 已终态 / running（并发保护）-> 拒绝执行
        - model_override 非空：覆盖推荐模型，覆盖值必须重新校验
        """
        record = load_task(task_id, self.tasks_dir)
        if record is None:
            return self._task_error(
                task_id, FAILED, "not_found", "not_found", "任务不存在"
            )
        if is_finished(record):
            return self._task_error(
                task_id, record.get("status", FAILED), "not_reexecutable",
                "not_reexecutable", f"任务已终态（{record.get('status')}），不能重复执行",
            )
        if record.get("status") == RUNNING:
            return self._task_error(
                task_id, RUNNING, "already_running",
                "already_running", "任务正在执行中，禁止并发执行",
            )

        now = _now_iso()
        if now > str(record.get("expires_at", "")):
            record = self._finalize_task(record, EXPIRED, outcome="expired")
            self._emit(
                EventType.TASK_EXPIRED,
                {
                    "task_id": task_id,
                    "task_type": record.get("task_type", "general"),
                    "expires_at": record.get("expires_at", ""),
                },
            )
            return self._task_error(
                task_id, EXPIRED, "task_expired",
                "task_expired", "任务已过期（30 分钟有效期），不能执行",
                task_type=record.get("task_type", "general"),
            )

        status = record.get("status")
        selected_model = str(record.get("selected_model", ""))
        task_type = str(record.get("task_type", "general"))

        # 批准 / 拒绝
        if status == AWAITING_CONFIRMATION:
            if not user_confirmed:
                record = self._finalize_task(record, DENIED, outcome="denied")
                self._emit(
                    EventType.TASK_DENIED,
                    {"task_id": task_id, "task_type": task_type, "model_ref": selected_model},
                )
                return self._task_error(
                    task_id, DENIED, "denied", "denied",
                    "任务已拒绝：未确认批准，不执行", task_type=task_type,
                )
            record = update_task(task_id, {"status": "approved"}, self.tasks_dir) or record
            self._emit(
                EventType.TASK_APPROVED,
                {"task_id": task_id, "task_type": task_type, "model_ref": selected_model},
            )

        # 覆盖推荐模型：重新校验（存在 / 未暂停 / 可用）
        if model_override:
            status_obj = self.toolbox.registry.find(model_override)
            if status_obj is None:
                return self._task_error(
                    task_id, status, "invalid_override",
                    "invalid_override", f"覆盖模型不在清单内：{model_override}",
                    task_type=task_type,
                )
            if status_obj.available is False or status_obj.is_paused():
                return self._task_error(
                    task_id, status, "invalid_override",
                    "invalid_override", f"覆盖模型不可用或暂停中：{model_override}",
                    task_type=task_type,
                )
            selected_model = model_override

        # 决策全灭（selected_model 为空）：直接机械兜底
        if not selected_model:
            result = self._mechanical(
                str(record.get("task", "")), set(), 0, 0,
                self.toolbox.stream.trace_id if self.toolbox.stream is not None else "",
                task_type=task_type,
            )
            self._finalize_task(record, COMPLETED if result.ok else FAILED, result=result)
            return result

        # 执行开始：标 running（并发/重复执行保护落盘）
        update_task(task_id, {"status": RUNNING}, self.tasks_dir)
        try:
            result = self._execute_flow(
                str(record.get("task", "")), selected_model, task_type,
                self.toolbox.stream.trace_id if self.toolbox.stream is not None else "",
            )
            # once 模式确认成功：沉淀任务偏好（对齐 run() 的 once 语义，两路径并存）
            if (
                result.ok
                and record.get("confirm_mode") == "once"
                and status == AWAITING_CONFIRMATION
                and selected_model
            ):
                try:
                    add_task_pref(
                        str(record.get("task", "")), selected_model, self.prefs_path
                    )
                except OSError:
                    pass
        finally:
            # 终态写盘（最小审计：任务文本截断摘要）
            self._finalize_task(record, COMPLETED if result.ok else FAILED, result=result)
        return result

    def _task_error(
        self,
        task_id: str,
        status: str,
        error_class: str,
        outcome: str,
        detail: str,
        *,
        task_type: str = "general",
    ) -> RunResult:
        return RunResult(
            ok=False, outcome=outcome, model_ref="", text=detail,
            attempts=0, error_class=error_class, switches=0,
            trace_id=task_id, task_type=task_type,
        )

    def _finalize_task(
        self, record: dict[str, Any], status: str, *, outcome: str = "", result: RunResult | None = None
    ) -> dict[str, Any]:
        """任务终态写盘（最小审计记录：任务文本截断摘要，不存全文与凭据）。"""
        if result is not None:
            audit = minimal_audit(
                record, outcome=result.outcome if result.outcome != OUTCOME_OK else "ok"
            )
        else:
            audit = minimal_audit(record, outcome=outcome)
        audit["status"] = status
        update_task(str(record.get("task_id", "")), audit, self.tasks_dir)
        return audit

    # -- 共用执行流 -----------------------------------------------------

    def _execute_flow(
        self,
        task: str,
        current: str,
        task_type: str,
        trace_id: str,
    ) -> RunResult:
        """调用 -> 失败切换循环 -> 机械兜底（run 与 execute 共用）。

        阶段3：原 run() 第 2 步整体提取为该方法，行为逐字段不变。
        task_type 透传给统计记录。
        """
        mgr = self._manager()
        failed: set[str] = set()
        switches = 0
        attempts = 0
        last_error = ""
        last_switch_error = ""  # 触发最后一次切换的错误类别（切换成功后沉淀偏好）

        while True:
            attempts += 1
            result = self.toolbox.call_model(current, [{"role": "user", "content": task}])
            self._record_stats(
                current, result, switches, call_kind="task", task_type=task_type
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
                    RunResult(
                        True, outcome, current, result.text, attempts, "", switches,
                        trace_id, task_type,
                    )
                )
            failed.add(current)
            last_error = result.error_class
            self._on_fault(current, result.error_class, result.detail, task)
            if switches >= self.max_switches:
                return self._mechanical(task, failed, attempts, switches, trace_id, last_error, task_type)
            try:
                plan = mgr.plan_recovery(current, result.error_class, detail=result.detail)
                target = str(plan.decision["switch_to"])
            except DecisionError:
                return self._mechanical(task, failed, attempts, switches, trace_id, last_error, task_type)
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
        task_type: str = "general",
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
                ref, call, switches, call_kind="task", task_type=task_type
            )
            results[ref] = {"ok": call.ok, "error_class": "" if call.ok else call.error_class}
            if call.ok:
                self._emit(
                    EventType.MECHANICAL_FALLBACK,
                    {"tried_models": list(results), "results": results},
                )
                return self._finish(
                    RunResult(
                        True, OUTCOME_FALLBACK, ref, call.text, attempts, "", switches,
                        trace_id, task_type,
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
            RunResult(False, OUTCOME_FAILED, "", "", attempts, last_error, switches,
                      trace_id, task_type)
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

    def _manager(self) -> DecisionManager:
        """惰性构造决策中枢（run/execute 共用）。"""

        if self.manager is not None:
            return self.manager
        return DecisionManager(
            self.toolbox, prefs_path=self.prefs_path, stats_path=self.stats_path
        )

    def _finish(self, result: RunResult) -> RunResult:
        self._emit(
            EventType.PIPELINE_FINISHED,
            {
                "outcome": result.outcome,
                "text": result.text,
                "task_type": result.task_type,
                "summary": {
                    "ok": result.ok,
                    "model_ref": result.model_ref,
                    "attempts": result.attempts,
                    "switches": result.switches,
                    "error_class": result.error_class,
                    "task_type": result.task_type,
                },
            },
        )
        return result

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        if self.toolbox.stream is not None:
            self.toolbox.stream.emit(type_, redact(data))
