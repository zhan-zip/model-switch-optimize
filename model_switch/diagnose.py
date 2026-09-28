"""诊断编排：四项事实（console_check）-> 内置规则库（rules）-> 结论（conclude_diagnosis）。

供 `mso diagnose <服务商>` 与 run --diagnose 复用；事件全程留痕：
diagnosis_started -> diagnosis_facts -> (decision_*) -> diagnosis_conclusion。
决策模型全部不可用时 DecisionError 向上传播（调用方处理）。
"""
from __future__ import annotations

from typing import Any

from .events import EventType
from .manager import DecisionManager
from .rules import load_rules
from .safety import redact


def run_diagnose(
    toolbox: Any,
    provider: str,
    *,
    manager: DecisionManager | None = None,
    rules: str | None = None,
) -> dict[str, Any]:
    """对一个服务商做诊断：四项事实 -> 规则库 -> 结论与建议动作。"""

    _emit(toolbox, EventType.DIAGNOSIS_STARTED, {"provider": provider})

    facts = toolbox.console_check(provider)
    if facts.get("status") == "console_not_configured":
        # 无控制台实现（真实场景接入外部 Playwright MCP，演示用 mock）则无法收集四项事实
        _emit(
            toolbox,
            EventType.DIAGNOSIS_FACTS,
            {"provider": provider, "facts": facts, "note": "console_not_configured"},
        )
        return {
            "ok": False,
            "status": "console_not_configured",
            "provider": provider,
            "detail": "四项检查依赖控制台实现（真实接入外部 Playwright MCP，演示用 --mock）",
        }
    _emit(toolbox, EventType.DIAGNOSIS_FACTS, {"provider": provider, "facts": facts})

    mgr = manager if manager is not None else DecisionManager(toolbox)
    rules_text = rules if rules is not None else load_rules()
    decision = mgr.conclude_diagnosis(facts, rules=rules_text)
    conclusion = str(decision.decision.get("conclusion", ""))
    actions = list(decision.decision.get("actions", []))
    _emit(
        toolbox,
        EventType.DIAGNOSIS_CONCLUSION,
        {
            "provider": provider,
            "conclusion": conclusion,
            "actions": actions,
            "decision_model": decision.decision_model,
        },
    )
    return {
        "ok": True,
        "status": "concluded",
        "provider": provider,
        "facts": facts,
        "conclusion": conclusion,
        "actions": actions,
        "decision_model": decision.decision_model,
    }


def _emit(toolbox: Any, type_: str, data: dict[str, Any]) -> None:
    if toolbox.stream is not None:
        toolbox.stream.emit(type_, redact(data))
