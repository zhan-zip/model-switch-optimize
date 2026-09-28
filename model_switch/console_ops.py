"""控制台操作：流程与数据契约定义（外部 Playwright MCP 占位）。

真实实现：宿主环境运行外部 Playwright MCP server，按 CONSOLE_SPECS 实现 adapter，
经 Toolbox(console=adapter) 注入；开发/演示期用 mocker.MockConsole。
本模块只定义操作流程与数据契约，不内嵌浏览器实现——接入真实 MCP 仅换实现、流程不变。
"""
from __future__ import annotations

from typing import Any, Callable

from .events import EventType
from .safety import redact

ConfirmHandler = Callable[[str, str, str], bool]  # (operation, reason, evidence) -> granted

CONSOLE_SPECS: list[dict[str, Any]] = [
    {
        "action": "login",
        "params": {"provider": "服务商名"},
        "returns": {"ok": bool, "detail": "登录结果"},
        "high_risk": True,
        "steps": ["打开控制台登录页", "填入账号信息（data/auth/ 提供）", "完成登录并校验登录态"],
    },
    {
        "action": "check_balance",
        "params": {"provider": "服务商名"},
        "returns": {"ok": bool, "balance": "余额信息"},
        "high_risk": False,
        "steps": ["进入控制台余额/用量页", "读取余额并截图留证"],
    },
    {
        "action": "check_group",
        "params": {"provider": "服务商名"},
        "returns": {"ok": bool, "groups": "分组与 key 归属信息"},
        "high_risk": False,
        "steps": ["进入控制台分组/令牌页", "读取 key 所属分组"],
    },
    {
        "action": "check_connectivity",
        "params": {"provider": "服务商名"},
        "returns": {"ok": bool, "detail": "控制台侧连通验证结果"},
        "high_risk": False,
        "steps": ["在控制台发起一次测试调用（如可用）"],
    },
    {
        "action": "repair",
        "params": {"provider": "服务商名", "action": "建议动作（充值/换分组/换 key 等）"},
        "returns": {"ok": bool, "detail": "修复执行结果"},
        "high_risk": True,
        "steps": [
            "按建议动作在控制台执行（充值 / 换分组 / 换 key 等）",
            "每一步先请求人工确认（confirm_requested，附理由+证据）",
            "执行后返回结果并留证",
        ],
    },
]


class ConsoleOps:
    """控制台修复操作编排：高危动作先人工确认，granted 才执行。"""

    def __init__(self, toolbox: Any, *, confirm_handler: ConfirmHandler | None = None):
        self.toolbox = toolbox
        self.confirm_handler = confirm_handler

    def check(self, provider: str) -> dict[str, Any]:
        """四项检查（网站可达 / 余额 / 分组 / 连通）——转发工具层。"""

        return self.toolbox.console_check(provider)

    def repair(
        self,
        provider: str,
        action: str,
        *,
        reason: str = "",
        evidence: str = "",
    ) -> dict[str, Any]:
        """控制台修复动作：confirm_requested -> granted -> console.repair 执行。"""

        operation = f"控制台执行修复动作：{provider} - {action}"
        self._emit(
            EventType.CONFIRM_REQUESTED,
            {"operation": operation, "reason": reason, "evidence": evidence},
        )
        if self.confirm_handler is None:
            granted = True  # 未注入确认通道：CLI 人读模式由调用方处理；高危默认见 fix.apply_fix
        else:
            granted = bool(self.confirm_handler(operation, reason, evidence))
        event = EventType.CONFIRM_GRANTED if granted else EventType.CONFIRM_DENIED
        self._emit(event, {"operation": operation})
        if not granted:
            return {"ok": False, "status": "denied", "operation": operation}

        console = getattr(self.toolbox, "_console", None)
        if console is None or not hasattr(console, "repair"):
            return {
                "ok": False,
                "status": "console_not_configured",
                "operation": operation,
                "detail": "未配置控制台实现（真实场景接入外部 Playwright MCP，演示用 mock）",
            }
        return console.repair(provider, action)

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        if self.toolbox.stream is not None:
            self.toolbox.stream.emit(type_, redact(data))
