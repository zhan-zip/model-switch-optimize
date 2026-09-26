"""mocker 模块测试：可编程 mock 的行为。"""
from model_switch.mocker import MockConsole, MockModelClient, MockWebSearch


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
