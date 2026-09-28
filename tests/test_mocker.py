"""mocker 模块测试：可编程 mock 的行为（客户端 / 控制台 / 决策中枢分流）。"""
import json

from model_switch.mocker import (
    MockConsole,
    MockDecisionClient,
    MockModelClient,
    MockWebSearch,
)


def test_mock_client_default_ok():
    client = MockModelClient()
    result = client("https://x/v1", "sk-x", "m1", [{"role": "user", "content": "ping"}])
    assert result.ok is True
    assert result.usage["total_tokens"] > 0


def test_mock_client_rules():
    client = MockModelClient(rules={"bad-model": "401", "busy-model": "429"}, default="5xx")
    assert client("u", "k", "bad-model", []).error_class == "401"
    assert client("u", "k", "busy-model", []).error_class == "429"
    assert client("u", "k", "other", []).error_class == "5xx"


def test_mock_client_records_calls():
    client = MockModelClient()
    client("https://x/v1", "sk-x", "m1", [{"role": "user", "content": "a"}])
    client("https://x/v1", "sk-x", "m2", [])
    assert [c["model"] for c in client.calls] == ["m1", "m2"]


def test_mock_console_default_facts():
    console = MockConsole()
    result = console.check("provider-a")
    assert result["ok"] is True
    assert set(result["facts"]) == {"reachable", "balance", "group", "connectivity"}
    assert console.checks == ["provider-a"]


def test_mock_console_preset_facts():
    console = MockConsole(facts={"provider-a": {"reachable": {"ok": False, "evidence": "不可达"}}})
    result = console.check("provider-a")
    assert result["facts"]["reachable"]["ok"] is False


def test_mock_web_search_preset():
    search = MockWebSearch(results={"gpt-4o": {"score": 88}})
    result = search.search("gpt-4o 跑分")
    assert result["ok"] is True
    assert result["result"]["score"] == 88
    assert search.queries == ["gpt-4o 跑分"]


def test_mock_console_repair_records():
    console = MockConsole()
    result = console.repair("provider-a", "充值")
    assert result["ok"] is True
    assert console.repairs == [{"provider": "provider-a", "action": "充值"}]


# -- MockDecisionClient（按 prompt 关键词分流） ---------------------------


def _decision_client():
    return MockDecisionClient(
        default_model="a/g1/m1",
        switch_to="b/g1/m2",
        fail_models={"m1": "401"},
    )


def _call(client, content, model="mX", max_tokens=None):
    return client("u", "k", model, [{"role": "user", "content": content}], max_tokens=max_tokens)


def test_decision_client_probe_ok():
    client = _decision_client()
    result = _call(client, "ping", model="m1", max_tokens=8)
    assert result.ok is True


def test_decision_client_routes_choose_and_switch():
    client = _decision_client()
    chosen = json.loads(_call(client, "你是模型选型决策者……", model="m2").text)
    assert chosen["model"] == "a/g1/m1"
    plan = json.loads(_call(client, "你是故障切换决策者……", model="m2").text)
    assert plan["switch_to"] == "b/g1/m2"
    assert plan["diagnose"] is False


def test_decision_client_routes_diagnosis():
    client = _decision_client()
    conclusion = json.loads(_call(client, "你是诊断结论决策者……", model="m2").text)
    assert conclusion["conclusion"]
    assert conclusion["actions"] == ["充值", "换分组"]


def test_decision_client_routes_profile():
    client = MockDecisionClient(labels={"a/g1/m1": "通用、便宜"})
    profile = json.loads(_call(client, "你是模型画像生成者……", model="m2").text)
    assert profile["labels"] == {"a/g1/m1": "通用、便宜"}


def test_decision_client_task_fail_and_ok():
    client = _decision_client()
    failed = _call(client, "帮我写个爬虫", model="m1")
    assert failed.ok is False
    assert failed.error_class == "401"
    ok = _call(client, "帮我写个爬虫", model="m2")
    assert ok.ok is True
    assert "爬虫" in ok.text
