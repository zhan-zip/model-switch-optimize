"""mock：无真实模型也能跑通全闭环并演示。

- MockModelClient   可编程模型客户端（按规则返回成功 / 指定错误分类）
- MockConsole       mock 控制台（console_check 四项事实预设 + 修复动作）
- MockWebSearch     mock 联网搜索（预设跑分数据）
- MockDecisionClient 按 prompt 关键词分流的决策中枢 mock（CLI --mock 演示闭环）

接入真实模型 / 真实 Playwright MCP 时，仅替换实现，流程不变。
"""
from __future__ import annotations

import json
from typing import Any

from .client import CallResult, ErrorClass

_MOCK_USAGE = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


class MockModelClient:
    """按规则返回结果的模型客户端，签名与 call_openai_compatible 一致。

    rules: {模型名: "ok" | "401" | "429" | "5xx" | "timeout" | "network"}
    未命中规则的模型按 default 处理；calls 记录全部调用便于断言。
    """

    def __init__(self, rules: dict[str, str] | None = None, default: str = "ok"):
        self.rules = dict(rules or {})
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        *,
        timeout: int = 30,
        max_tokens: int | None = None,
        opener: Any = None,
    ) -> CallResult:
        self.calls.append({"base_url": base_url, "model": model, "messages": messages})
        outcome = self.rules.get(model, self.default)
        if outcome == "ok":
            return CallResult(
                ok=True, model=model,
                text="[mock] ok",
                latency_ms=42, usage=dict(_MOCK_USAGE),
            )
        if outcome.startswith("{"):
            return CallResult(
                ok=True, model=model,
                text=outcome,
                latency_ms=42, usage=dict(_MOCK_USAGE),
            )
        return CallResult(ok=False, model=model, error_class=outcome,
                          detail=f"[mock] {outcome}")


class MockConsole:
    """mock 控制台：console_check 四项事实预设（网站可达 / 余额 / 分组 / 连通）+ 修复动作。"""

    def __init__(self, facts: dict[str, dict[str, Any]] | None = None):
        self.facts = facts or {}
        self.checks: list[str] = []
        self.repairs: list[dict[str, str]] = []

    def check(self, provider: str) -> dict[str, Any]:
        self.checks.append(provider)
        preset = self.facts.get(provider)
        if preset is not None:
            return {"ok": True, "provider": provider, "facts": preset}
        return {
            "ok": True,
            "provider": provider,
            "facts": {
                "reachable": {"ok": True, "evidence": "[mock] 网站可达"},
                "balance": {"ok": True, "evidence": "[mock] 余额充足"},
                "group": {"ok": True, "evidence": "[mock] key 分组正常"},
                "connectivity": {"ok": True, "evidence": "[mock] 模型连通"},
            },
        }

    def repair(self, provider: str, action: str) -> dict[str, Any]:
        self.repairs.append({"provider": provider, "action": action})
        return {
            "ok": True,
            "provider": provider,
            "action": action,
            "detail": "[mock] 修复动作已执行",
        }


class MockDecisionClient:
    """mock 决策中枢客户端：按 prompt 关键词分流返回对应决策 JSON。

    供 CLI --mock 演示完整闭环（选型 -> 故障 -> 切换 -> 成功 / 诊断结论）：
    - 连通探测（content=="ping"）：成功
    - 选型 prompt：返回 default_model（完整模型引用）
    - 切换 prompt：返回 switch_to（完整模型引用）
    - 画像 prompt：返回 labels_map（或每个模型统一 mock 标签）
    - 诊断 prompt：模拟结论（余额不足 -> 充值/换分组）
    - 普通任务调用：fail_models 命中返回指定错误类别，否则成功
    """

    def __init__(
        self,
        *,
        default_model: str = "",
        switch_to: str = "",
        fail_models: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        pause_seconds: int = 0,
    ):
        self.default_model = default_model
        self.switch_to = switch_to
        self.fail_models = dict(fail_models or {})
        self.labels = dict(labels or {})
        self.pause_seconds = pause_seconds
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        *,
        timeout: int = 30,
        max_tokens: int | None = None,
        opener: Any = None,
    ) -> CallResult:
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        content = messages[-1]["content"] if messages else ""
        text = self._route(content, model)
        if text.startswith("{"):
            return CallResult(
                ok=True, model=model, text=text,
                latency_ms=42, usage=dict(_MOCK_USAGE),
            )
        if text in ("401", "429", "5xx", "timeout", "network"):
            return CallResult(
                ok=False, model=model, error_class=text,
                detail=f"[mock] {text}", latency_ms=42,
            )
        if text == "ping-ok":
            return CallResult(
                ok=True, model=model, text="[mock] ok",
                latency_ms=42, usage=dict(_MOCK_USAGE),
            )
        return CallResult(
            ok=True, model=model, text=f"[mock] 任务完成：{content[:50]}",
            latency_ms=42, usage=dict(_MOCK_USAGE),
        )

    def _route(self, content: str, model: str) -> str:
        if content == "ping":
            return "ping-ok"
        if "选型决策者" in content:
            return json.dumps(
                {"model": self.default_model, "reason": "[mock] 按标签默认选择"},
                ensure_ascii=False,
            )
        if "故障切换决策者" in content:
            plan = {
                "switch_to": self.switch_to,
                "diagnose": False,
                "reason": "[mock] 立即切换顶上",
            }
            if self.pause_seconds:
                plan["pause_seconds"] = self.pause_seconds  # 演示失败模型冷却
            return json.dumps(plan, ensure_ascii=False)
        if "画像生成者" in content:
            labels = self.labels or {
                ref: "mock标签、演示用" for ref in _refs_from_profile_prompt(content)
            }
            return json.dumps({"labels": labels}, ensure_ascii=False)
        if "诊断结论决策者" in content:
            return json.dumps(
                {"conclusion": "[mock] 余额不足（演示结论）", "actions": ["充值", "换分组"]},
                ensure_ascii=False,
            )
        # 普通任务调用：fail_models 按模型名命中
        if model in self.fail_models:
            return self.fail_models[model]
        return "task-ok"


def _refs_from_profile_prompt(content: str) -> list[str]:
    """从画像 prompt 中提取模型引用行（- provider/group/model | ...）。"""

    refs: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and " | " in stripped:
            refs.append(stripped[2:].split(" | ")[0].strip())
    return refs


class MockWebSearch:
    """mock 联网搜索：按 query 返回预设结果（如跑分数据）。"""

    def __init__(self, results: dict[str, dict[str, Any]] | None = None):
        self.results = results or {}
        self.queries: list[str] = []

    def search(self, query: str) -> dict[str, Any]:
        self.queries.append(query)
        for key, value in self.results.items():
            if key in query:
                return {"ok": True, "query": query, "result": value}
        return {"ok": True, "query": query, "result": {"summary": "[mock] 无预设结果"}}


def mock_probe_reply() -> str:
    """mock 保底回复文本（阶段3 决策模型 mock 用）。"""

    return "[mock-decision] ok"
