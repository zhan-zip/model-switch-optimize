"""修复执行（apply_fix）：人工门禁 -> 执行 -> 重测 -> 恢复。

流程：console_ops.repair（内含 confirm_requested / granted / denied）
-> fix_applied -> 连通重测 recheck_result -> 恢复 recovered。
denied：只留建议不执行；重测失败：留 recheck_result，不发 recovered。
阶段4：修复动作为 mock / 数据契约（console_ops.CONSOLE_SPECS）；
真实浏览器修复 = 宿主接入外部 Playwright MCP，仅换实现、流程不变。
"""
from __future__ import annotations

from typing import Any, Callable

from .console_ops import ConfirmHandler, ConsoleOps
from .events import EventType
from .safety import redact


def apply_fix(
    toolbox: Any,
    provider: str,
    action: str,
    *,
    model_ref: str | None = None,
    reason: str = "",
    evidence: str = "",
    confirm_handler: ConfirmHandler | None = None,
) -> dict[str, Any]:
    """执行一次修复：人工门禁（console_ops.repair）-> 重测 -> 恢复。"""

    ops = ConsoleOps(toolbox, confirm_handler=confirm_handler)
    repair_result = ops.repair(provider, action, reason=reason, evidence=evidence)

    if not repair_result.get("ok"):
        # denied / console_not_configured：只留建议，不执行
        return {
            "ok": False,
            "status": str(repair_result.get("status", "repair_failed")),
            "provider": provider,
            "action": action,
            "repair": repair_result,
        }

    operation = f"{provider} - {action}"
    _emit(toolbox, EventType.FIX_APPLIED, {"operation": operation, "result": repair_result})
    result: dict[str, Any] = {
        "ok": True,
        "status": "fixed",
        "provider": provider,
        "action": action,
        "repair": repair_result,
    }

    if model_ref:
        checks = toolbox.test_connectivity(model_ref)
        detail = checks.get(model_ref, {})
        passed = bool(detail.get("ok"))
        _emit(
            toolbox,
            EventType.RECHECK_RESULT,
            {"model_ref": model_ref, "passed": passed, "details": detail},
        )
        result["recheck"] = {"model_ref": model_ref, "passed": passed}
        if passed:
            _emit(toolbox, EventType.RECOVERED, {"model_ref": model_ref})
            result["status"] = "recovered"
    return result


def _emit(toolbox: Any, type_: str, data: dict[str, Any]) -> None:
    if toolbox.stream is not None:
        toolbox.stream.emit(type_, redact(data))
