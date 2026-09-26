"""程序工具层：模型 / 宿主 agent 均可调用的工具箱。

六个工具：call_model · list_models · test_connectivity · console_check ·
web_search(保底) · fault_history（apply_fix 属阶段4，带人工门禁）。

统一 dispatch(name, args) 分发，供决策中枢 / 宿主 agent 按工具规格调用。
mock 场景：构造 Toolbox 时注入 MockModelClient / MockConsole / MockWebSearch，
无真实模型也能跑通全闭环并演示。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .client import CallResult, ErrorClass, ModelClient, call_openai_compatible
from .config import Config
from .events import EventStream, EventType
from .health import PROBE_MESSAGES, PROBE_MAX_TOKENS, test_connectivity
from .model import ModelRegistry, ModelStatus
from .safety import redact

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "call_model",
        "description": "调用指定模型进行对话",
        "parameters": {
            "model_ref": "服务商/分组/模型名",
            "messages": [{"role": "user|assistant|system", "content": "..."}],
            "timeout": "超时秒数（可选，默认 30）",
        },
    },
    {
        "name": "list_models",
        "description": "列出全部模型清单与运行时状态",
        "parameters": {},
    },
    {
        "name": "test_connectivity",
        "description": "对指定模型（或全部模型）做连通测试",
        "parameters": {"model_ref": "服务商/分组/模型名（省略则测全部）"},
    },
    {
        "name": "console_check",
        "description": "控制台四项检查（网站可达 / 余额 / key 分组 / 模型连通），需注入控制台实现（Playwright MCP / mock）",
        "parameters": {"provider": "服务商名称"},
    },
    {
        "name": "web_search",
        "description": "联网搜索（跑分数据等）；宿主联网优先，本工具为保底",
        "parameters": {"query": "搜索词"},
    },
    {
        "name": "fault_history",
        "description": "读取故障历史（data/faults/）",
        "parameters": {},
    },
]

FAULTS_DIR = Path("data/faults")


class Toolbox:
    """工具箱：持有配置 / 注册表 / 事件流，统一供模型与程序调用。"""

    def __init__(
        self,
        config: Config,
        registry: ModelRegistry,
        stream: EventStream | None = None,
        *,
        model_client: ModelClient = call_openai_compatible,
        console: Any = None,
        web_search_impl: Callable[[str], dict[str, Any]] | None = None,
        faults_dir: Path = FAULTS_DIR,
        mock: bool = False,
    ):
        self.config = config
        self.registry = registry
        self.stream = stream
        self._client = model_client
        self._console = console
        self._web_search_impl = web_search_impl
        self._faults_dir = Path(faults_dir)
        self.mock = mock  # mock 模式：跳过 key 检查（无真实 key 也能跑通全闭环）

    # -- 模型调用 ----------------------------------------------------

    def call_model(
        self,
        model_ref: str,
        messages: list[dict[str, str]],
        timeout: int = 30,
    ) -> CallResult:
        status = self.registry.find(model_ref)
        if status is None:
            return CallResult(
                ok=False, model=model_ref,
                error_class=ErrorClass.CONFIG,
                detail=f"未知模型：{model_ref}（可用 mso tools 查看清单）",
            )
        from .config import resolve_api_key

        if self.mock:
            api_key = "[mock-key]"
        else:
            api_key = resolve_api_key(status.key_env)
            if api_key is None:
                return CallResult(
                    ok=False, model=model_ref,
                    error_class=ErrorClass.CONFIG,
                    detail=f"环境变量 {status.key_env} 未设置（key 只走环境变量）",
                )

        self._emit(EventType.MODEL_CALLED, {"model_ref": model_ref, "attempt": 1})
        result = self._client(
            status.base_url,
            api_key,
            status.ref.model,
            messages,
            timeout=timeout,
        )
        result = CallResult(
            ok=result.ok, model=model_ref, text=result.text,
            error_class=result.error_class, detail=result.detail,
            latency_ms=result.latency_ms, usage=result.usage,
        )
        self.registry.update(
            model_ref,
            available=result.ok,
            last_latency_ms=result.latency_ms,
            last_error_class="" if result.ok else result.error_class,
            last_error_detail="" if result.ok else result.detail,
        )
        if result.ok:
            self._emit(
                EventType.MODEL_RESPONDED,
                {"model_ref": model_ref, "latency_ms": result.latency_ms,
                 "tokens": result.usage.get("total_tokens")},
            )
        else:
            self._emit(
                EventType.MODEL_FAILED,
                {"model_ref": model_ref, "error_class": result.error_class,
                 "detail": result.detail},
            )
        return result

    # -- 清单与连通测试 ----------------------------------------------

    def list_models(self) -> list[dict[str, Any]]:
        return self.registry.to_list()

    def test_connectivity(self, model_ref: str | None = None, timeout: int = 20) -> dict[str, dict[str, Any]]:
        """测单个模型（model_ref）或全部；返回 {model_ref: CallResult.to_dict()}。"""

        targets: list[ModelStatus]
        if model_ref is None:
            targets = list(self.registry.all())
        else:
            status = self.registry.find(model_ref)
            if status is None:
                return {model_ref: CallResult(
                    ok=False, model=model_ref,
                    error_class=ErrorClass.CONFIG,
                    detail=f"未知模型：{model_ref}",
                ).to_dict()}
            targets = [status]

        results: dict[str, dict[str, Any]] = {}
        for status in targets:
            ref_str = str(status.ref)
            self._emit(EventType.MODEL_CALLED, {"model_ref": ref_str, "attempt": 1, "probe": True})
            if self.mock:
                result = self._client(
                    status.base_url,
                    "[mock-key]",
                    status.ref.model,
                    PROBE_MESSAGES,
                    timeout=timeout,
                    max_tokens=PROBE_MAX_TOKENS,
                )
            else:
                result = test_connectivity(status, client=self._client, timeout=timeout)
            result = CallResult(
                ok=result.ok, model=ref_str, text=result.text,
                error_class=result.error_class, detail=result.detail,
                latency_ms=result.latency_ms, usage=result.usage,
            )
            self.registry.update(
                ref_str,
                available=result.ok,
                last_latency_ms=result.latency_ms,
                last_error_class="" if result.ok else result.error_class,
                last_error_detail="" if result.ok else result.detail,
            )
            if result.ok:
                self._emit(
                    EventType.MODEL_RESPONDED,
                    {"model_ref": ref_str, "latency_ms": result.latency_ms,
                     "tokens": result.usage.get("total_tokens"), "probe": True},
                )
            else:
                self._emit(
                    EventType.MODEL_FAILED,
                    {"model_ref": ref_str, "error_class": result.error_class,
                     "detail": result.detail, "probe": True},
                )
            results[ref_str] = result.to_dict()
        return results

    # -- 控制台 / 联网 / 故障历史 --------------------------------------

    def console_check(self, provider: str) -> dict[str, Any]:
        """控制台四项检查；未注入控制台实现时返回 not_ready。"""

        if self._console is None:
            return {
                "ok": False,
                "status": "console_not_configured",
                "detail": "未配置控制台实现（真实场景接入外部 Playwright MCP，演示用 mock）",
                "provider": provider,
            }
        return self._console.check(provider)

    def web_search(self, query: str) -> dict[str, Any]:
        """联网搜索（保底）：宿主联网优先；未注入实现时返回 not_available。"""

        if self._web_search_impl is None:
            return {
                "ok": False,
                "status": "web_search_not_available",
                "detail": "宿主联网优先；当前环境未提供联网实现（可注入 mock 演示）",
                "query": query,
            }
        return self._web_search_impl(query)

    def fault_history(self) -> list[dict[str, Any]]:
        """读取故障历史（data/faults/*.json，按文件名排序）。"""

        if not self._faults_dir.exists():
            return []
        records = []
        for path in sorted(self._faults_dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return records

    # -- 统一分发（供决策模型 / 宿主 agent 调用）-----------------------

    def dispatch(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(args or {})
        if name == "call_model":
            result = self.call_model(
                str(args.get("model_ref", "")),
                list(args.get("messages") or []),
                int(args.get("timeout", 30)),
            )
            return {"tool": name, "ok": result.ok, "result": result.to_dict()}
        if name == "list_models":
            return {"tool": name, "ok": True, "result": self.list_models()}
        if name == "test_connectivity":
            ref = args.get("model_ref")
            results = self.test_connectivity(str(ref) if ref else None)
            return {"tool": name, "ok": all(r["ok"] for r in results.values()), "result": results}
        if name == "console_check":
            result = self.console_check(str(args.get("provider", "")))
            return {"tool": name, "ok": bool(result.get("ok")), "result": result}
        if name == "web_search":
            result = self.web_search(str(args.get("query", "")))
            return {"tool": name, "ok": bool(result.get("ok")), "result": result}
        if name == "fault_history":
            return {"tool": name, "ok": True, "result": self.fault_history()}
        return {"tool": name, "ok": False, "result": {"error": f"未知工具：{name}"}}

    # -- 内部 ---------------------------------------------------------

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        if self.stream is not None:
            self.stream.emit(type_, redact(data))
