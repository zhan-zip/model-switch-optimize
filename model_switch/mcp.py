"""MCP 薄封装：模块能力暴露为 MCP tools，MCP 宿主零代码接入。

宿主配置（如 Claude Code 的 mcpServers）：
  {"mcpServers": {"mso": {"command": "mso", "args": ["mcp"]}}}

暴露 12 个 tools：
  六工具     call_model / list_models / test_connectivity / console_check /
             web_search / fault_history（dispatch 同款能力）
  三决策点   choose_model / plan_recovery / conclude_diagnosis
  健康        model_health（阶段2：健康摘要查询）
  修复       apply_fix（高危：宿主先向用户确认，经 user_confirmed=true 传入）
  闭环       run（托管宿主直接跑完整任务闭环）

人工确认协议（MCP 单向调用无反向通道）：高危操作由宿主先行向用户确认，
确认后以 user_confirmed=true 调用；confirm_requested/granted 事件全程留痕。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

def _resolve_fastmcp():
    """解析 FastMCP 类：mcp 1.x 用 mcp.server.fastmcp.FastMCP；识别到 2.x 时给清晰降级指引。"""

    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
        return FastMCP
    except ImportError:
        # mcp 2.x：fastmcp 模块为墓碑 shim（import 即抛 ModuleNotFoundError），顶层亦无 FastMCP
        import importlib.metadata as _md

        try:
            _ver = _md.version("mcp")
        except Exception:
            _ver = "未知"
        raise RuntimeError(
            f"检测到 mcp {_ver}，本模块基于 mcp 1.x（需 mcp>=1.29,<2.0.0）。"
            '请执行：pip install "mcp>=1.29,<2.0.0"'
        ) from None


FastMCP = _resolve_fastmcp()

from .client import ErrorClass  # noqa: F401  文档引用
from .config import DEFAULT_CONFIG_PATH
from .fix import apply_fix as _apply_fix_impl
from .switcher import ModelSwitcher


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def build_mcp_server(switcher: ModelSwitcher) -> FastMCP:
    """把 ModelSwitcher 的能力包装为 FastMCP server。"""

    mcp: FastMCP = FastMCP("model-switch-optimize")
    toolbox = switcher.toolbox
    manager = switcher.manager

    # -- 六工具 ---------------------------------------------------------

    @mcp.tool()
    def call_model(model_ref: str, messages: list[dict[str, str]], timeout: int = 30) -> str:
        """调用指定模型进行对话（model_ref = 服务商/分组/模型名）"""
        return _dumps(toolbox.call_model(model_ref, messages, timeout).to_dict())

    @mcp.tool()
    def list_models() -> str:
        """列出全部模型清单与运行时状态"""
        return _dumps(toolbox.list_models())

    @mcp.tool()
    def test_connectivity(model_ref: str = "") -> str:
        """对指定模型（省略则全部）做连通测试"""
        return _dumps(toolbox.test_connectivity(model_ref or None))

    @mcp.tool()
    def console_check(provider: str) -> str:
        """控制台四项检查（网站可达 / 余额 / key 分组 / 模型连通）"""
        return _dumps(toolbox.console_check(provider))

    @mcp.tool()
    def web_search(query: str) -> str:
        """联网搜索（跑分数据等），保底实现"""
        return _dumps(toolbox.web_search(query))

    @mcp.tool()
    def fault_history() -> str:
        """读取故障历史（data/faults/）"""
        return _dumps(toolbox.fault_history())

    # -- 三决策点 ---------------------------------------------------------

    @mcp.tool()
    def choose_model(task: str, confirm_mode: str = "never") -> str:
        """决策点：选型（任务 + 清单/状态/偏好 -> 选哪个模型）"""
        try:
            decision = manager.choose_model(task, confirm_mode=confirm_mode)
        except Exception as exc:  # DecisionError
            return _dumps({"ok": False, "error": str(exc)})
        return _dumps(
            {
                "ok": True,
                "model": decision.decision.get("model"),
                "reason": decision.decision.get("reason"),
                "decision_model": decision.decision_model,
                "confirmed": decision.confirmed,
            }
        )

    @mcp.tool()
    def plan_recovery(failed_ref: str, error_class: str) -> str:
        """决策点：故障切换（错误类型 + 可用模型 -> 切换目标 + 是否诊断）"""
        try:
            decision = manager.plan_recovery(failed_ref, error_class)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})
        return _dumps(
            {
                "ok": True,
                "switch_to": decision.decision.get("switch_to"),
                "diagnose": decision.decision.get("diagnose"),
                "pause_seconds": decision.decision.get("pause_seconds"),  # 可选：失败模型冷却建议
                "reason": decision.decision.get("reason"),
                "decision_model": decision.decision_model,  # "program" = 切换偏好命中（未调 LLM）
            }
        )

    @mcp.tool()
    def conclude_diagnosis(facts: dict) -> str:
        """决策点：诊断结论（四项事实 + 内置规则库 -> 结论与建议）"""
        try:
            decision = manager.conclude_diagnosis(facts)
        except Exception as exc:
            return _dumps({"ok": False, "error": str(exc)})
        return _dumps(
            {
                "ok": True,
                "conclusion": decision.decision.get("conclusion"),
                "actions": decision.decision.get("actions"),
            }
        )

    # -- 修复（高危人工门禁） ----------------------------------------------

    @mcp.tool()
    def apply_fix(
        provider: str,
        action: str,
        model_ref: str = "",
        user_confirmed: bool = False,
        reason: str = "",
        evidence: str = "",
    ) -> str:
        """执行修复（高危）。宿主必须先向用户确认，确认后以 user_confirmed=true 调用；
        执行后自动重测连通（model_ref 可选），恢复发 recovered 事件。"""
        if not user_confirmed:
            return _dumps(
                {
                    "ok": False,
                    "status": "not_confirmed",
                    "detail": "高危操作：请先向用户确认（附理由+证据），确认后以 user_confirmed=true 重新调用",
                }
            )
        result = _apply_fix_impl(
            toolbox,
            provider,
            action,
            model_ref=model_ref or None,
            reason=reason,
            evidence=evidence,
            confirm_handler=lambda *args: True,  # 宿主已确认；confirm_requested/granted 事件仍留痕
        )
        return _dumps(result)

    # -- 完整闭环（托管宿主） ------------------------------------------------

    @mcp.tool()
    def model_health(model_ref: str = "") -> str:
        """健康摘要查询（阶段2）：省略 model_ref 返回全部模型。"""
        return _dumps(switcher.health(model_ref or None))

    @mcp.tool()
    def run(task: str, confirm_mode: str = "never") -> str:
        """完整任务闭环：选型 -> 调用 -> 故障自动切换（不中断）-> 机械兜底 -> 入档"""
        result = switcher.run(task, confirm_mode=confirm_mode)
        return _dumps(
            {
                "ok": result.ok,
                "outcome": result.outcome,
                "model_ref": result.model_ref,
                "text": result.text,
                "attempts": result.attempts,
                "switches": result.switches,
                "error_class": result.error_class,
                "trace_id": result.trace_id,
            }
        )

    return mcp


def serve(config_path: Path | str = DEFAULT_CONFIG_PATH) -> None:
    """启动 MCP server（stdio transport），阻塞运行。"""

    switcher = ModelSwitcher(config_path)
    build_mcp_server(switcher).run()
