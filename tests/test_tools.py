"""tools 模块测试：工具箱、事件流联动、统一分发。"""
import json

from model_switch.client import CallResult, ErrorClass
from model_switch.config import load_config
from model_switch.events import EventStream, EventType
from model_switch.mocker import MockConsole, MockModelClient, MockWebSearch
from model_switch.model import ModelRegistry
from model_switch.tools import Toolbox

VALID_YAML = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - { name: g1, key_env: KEY_A, models: [m1, m2] }
  - name: b
    base_url: https://b.example.com/v1
    groups:
      - { name: g1, key_env: KEY_B, models: [m3] }
fallback: { provider: b, group: g1, model: m3, key_env: KEY_B }
"""


def _toolbox(tmp_path, monkeypatch, rules=None, stream=None):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setenv("KEY_A", "sk-a")
    monkeypatch.setenv("KEY_B", "sk-b")
    config = load_config(path)
    registry = ModelRegistry(config)
    mock_client = MockModelClient(rules=rules)
    faults_dir = tmp_path / "faults"
    faults_dir.mkdir(parents=True, exist_ok=True)
    return Toolbox(
        config, registry, stream,
        model_client=mock_client,
        console=MockConsole(),
        web_search_impl=MockWebSearch().search,
        faults_dir=faults_dir,
    )


def test_call_model_success_emits_events(tmp_path, monkeypatch):
    stream = EventStream()
    box = _toolbox(tmp_path, monkeypatch, stream=stream)
    result = box.call_model("a/g1/m1", [{"role": "user", "content": "hi"}])
    assert result.ok is True
    types = [e.type for e in stream]
    assert types == ["model_called", "model_responded"]
    assert stream.events[0].data["model_ref"] == "a/g1/m1"
    assert box.registry.find("a/g1/m1").available is True


def test_call_model_failure_emits_and_updates(tmp_path, monkeypatch):
    stream = EventStream()
    box = _toolbox(tmp_path, monkeypatch, rules={"m1": "401"}, stream=stream)
    result = box.call_model("a/g1/m1", [{"role": "user", "content": "hi"}])
    assert result.ok is False
    assert result.error_class == "401"
    types = [e.type for e in stream]
    assert types == ["model_called", "model_failed"]
    status = box.registry.find("a/g1/m1")
    assert status.available is False
    assert status.last_error_class == "401"


def test_call_unknown_model_returns_config_error(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch)
    result = box.call_model("x/y/z", [{"role": "user", "content": "hi"}])
    assert result.ok is False
    assert result.error_class == ErrorClass.CONFIG


def test_call_model_without_key_returns_config_error(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch)
    monkeypatch.delenv("KEY_A", raising=False)  # 构造后再删除，避免 helper 再设置
    result = box.call_model("a/g1/m1", [{"role": "user", "content": "hi"}])
    assert result.ok is False
    assert result.error_class == ErrorClass.CONFIG
    assert "KEY_A" in result.detail


def test_list_models(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch)
    models = box.list_models()
    assert [m["model_ref"] for m in models] == ["a/g1/m1", "a/g1/m2", "b/g1/m3"]
    assert all("model_ref" in m and "key_status" in m for m in models)


def test_test_connectivity_all_and_single(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch, rules={"m2": "429"})
    results = box.test_connectivity()
    assert set(results) == {"a/g1/m1", "a/g1/m2", "b/g1/m3"}
    assert results["a/g1/m1"]["ok"] is True
    assert results["a/g1/m2"]["error_class"] == "429"
    single = box.test_connectivity("b/g1/m3")
    assert list(single) == ["b/g1/m3"]
    assert single["b/g1/m3"]["ok"] is True


def test_console_check_with_and_without_console(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch)
    facts = box.console_check("a")
    assert facts["ok"] is True
    assert set(facts["facts"]) == {"reachable", "balance", "group", "connectivity"}

    path = tmp_path / "models.yaml"
    config = load_config(path)
    bare = Toolbox(config, ModelRegistry(config), None)
    result = bare.console_check("a")
    assert result["ok"] is False
    assert result["status"] == "console_not_configured"


def test_web_search_with_mock(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch)
    result = box.web_search("gpt-4o benchmark 跑分")
    assert result["ok"] is True


def test_fault_history_empty_and_records(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch)
    assert box.fault_history() == []
    faults_dir = tmp_path / "faults"
    # faults_dir already created by Toolbox._toolbox
    (faults_dir / "001.json").write_text(
        json.dumps({"model_ref": "a/g1/m1", "error_class": "401"}), encoding="utf-8"
    )
    box2 = Toolbox(box.config, box.registry, None, faults_dir=faults_dir)
    records = box2.fault_history()
    assert len(records) == 1
    assert records[0]["model_ref"] == "a/g1/m1"


def test_fault_history_skips_corrupted_files(tmp_path, monkeypatch):
    """测试 fault_history 容错：跳过损坏/乱码的 JSON 文件"""
    box = _toolbox(tmp_path, monkeypatch)
    faults_dir = tmp_path / "faults"
    # 正常文件
    (faults_dir / "001.json").write_text(
        json.dumps({"model_ref": "a/g1/m1", "error_class": "401"}), encoding="utf-8"
    )
    # 乱码文件（模拟 GBK 写入 UTF-8 读取）
    (faults_dir / "002.json").write_bytes(b"\xff\xfe\x00\x00")
    # 损坏的 JSON
    (faults_dir / "003.json").write_text("{invalid json", encoding="utf-8")
    # 正常文件
    (faults_dir / "004.json").write_text(
        json.dumps({"model_ref": "b/g1/m2", "error_class": "500"}), encoding="utf-8"
    )
    
    box2 = Toolbox(box.config, box.registry, None, faults_dir=faults_dir)
    records = box2.fault_history()
    # 应该只返回 2 条正常记录，跳过损坏的
    assert len(records) == 2
    assert records[0]["model_ref"] == "a/g1/m1"
    assert records[1]["model_ref"] == "b/g1/m2"


def test_dispatch_routes_all_tools(tmp_path, monkeypatch):
    box = _toolbox(tmp_path, monkeypatch)
    assert box.dispatch("call_model", {"model_ref": "a/g1/m1",
                                       "messages": [{"role": "user", "content": "hi"}]})["ok"] is True
    assert box.dispatch("list_models")["ok"] is True
    assert box.dispatch("test_connectivity", {"model_ref": "b/g1/m3"})["ok"] is True
    assert box.dispatch("console_check", {"provider": "a"})["ok"] is True
    assert box.dispatch("web_search", {"query": "跑分"})["ok"] is True
    assert box.dispatch("fault_history")["ok"] is True
    assert box.dispatch("nope")["ok"] is False


def test_events_are_redacted(tmp_path, monkeypatch):
    stream = EventStream()
    box = _toolbox(tmp_path, monkeypatch, stream=stream)
    box.call_model("a/g1/m1", [{"role": "user", "content": "hi"}])
    dumped = stream.to_json()
    assert "sk-a" not in dumped
    assert "****" in dumped or "sk-" not in dumped
