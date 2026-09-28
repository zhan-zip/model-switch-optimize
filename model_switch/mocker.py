"""mock：无真实模型也能跑通全闭环并演示。

- MockModelClient  可编程模型客户端（按规则返回成功 / 指定错误分类）
- MockConsole      mock 控制台（console_check 四项事实预设）
- MockWebSearch    mock 联网搜索（预设跑分数据）

接入真实模型 / 真实 Playwright MCP 时，仅替换实现，流程不变。
"""
from __future__ import annotations

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
    """mock 控制台：console_check 四项事实预设（网站可达 / 余额 / 分组 / 连通）。"""

    def __init__(self, facts: dict[str, dict[str, Any]] | None = None):
        self.facts = facts or {}
        self.checks: list[str] = []

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
