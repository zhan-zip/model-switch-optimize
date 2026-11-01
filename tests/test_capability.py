"""capability 模块测试：三层来源合并 / 探测判定 / 存储 / 选型硬过滤 / CLI。

能力检测方案C：declared > probed > mapped > unknown；
探测保守原则（失败不定罪）；mock 分流零真实调用。
"""
import base64
import json

import pytest

from model_switch.capability import (
    CAPABILITIES,
    MOCK_CAPABILITY_OUTCOMES,
    capability_value,
    load_capabilities,
    probe_all,
    probe_capability,
    resolve_capabilities,
    save_capabilities,
    _solid_png_b64,
)
from model_switch.config import ConfigError, load_config
from model_switch.events import EventStream, EventType
from model_switch.manager import DecisionManager
from model_switch.model import ModelRegistry
from model_switch.rules import load_capability_rules, match_capability_rules
from model_switch.tools import Toolbox
from conftest import SequenceClient

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

DECLARED_YAML = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - name: g1
        key_env: KEY_A
        models:
          - m1
          - { name: m2, capabilities: [vision, tool_use] }
fallback: { provider: a, group: g1, model: m1, key_env: KEY_A }
"""


def _toolbox(tmp_path, client=None, stream=None, yaml=VALID_YAML, mock=True):
    path = tmp_path / "models.yaml"
    path.write_text(yaml, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)
    return Toolbox(
        config, registry, stream,
        model_client=client or SequenceClient(),
        mock=mock,
    )


# -- 映射表（mapped 层） ---------------------------------------------------


def test_capability_rules_default_exists_and_matches():
    rules = load_capability_rules()
    assert rules, "内置能力映射表应存在且非空"
    assert "vision" in match_capability_rules("gpt-4o")
    assert "vision" in match_capability_rules("claude-sonnet-5")
    assert "tool_use" in match_capability_rules("deepseek-chat")
    assert match_capability_rules("m1") == frozenset()  # 未知模型名无映射


def test_capability_rules_skips_headers(tmp_path):
    path = tmp_path / "caps.md"
    path.write_text(
        "| 模型名正则 | 能力 |\n| --- | --- |\n| ^foo | vision |\n",
        encoding="utf-8",
    )
    rules = load_capability_rules(path)
    assert len(rules) == 1
    assert "vision" in match_capability_rules("foo", rules)


def test_capability_rules_missing_file_returns_empty(tmp_path):
    assert load_capability_rules(tmp_path / "nope.md") == []


# -- 存储读写 ---------------------------------------------------------------


def test_capabilities_store_roundtrip(tmp_path):
    path = tmp_path / "capabilities.json"
    save_capabilities({"a/g1/m1": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}}, path)
    store = load_capabilities(path)
    assert store["a/g1/m1"]["vision"]["supported"] is False
    assert not path.with_suffix(".json.tmp").exists()  # 原子写无残留


def test_capabilities_store_fault_tolerant(tmp_path):
    assert load_capabilities(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_capabilities(bad) == {}
    bad.write_text('["array"]', encoding="utf-8")
    assert load_capabilities(bad) == {}  # 顶层非带 models 的对象


# -- 三层合并 ---------------------------------------------------------------


def test_resolve_priority_declared_over_all(tmp_path):
    box = _toolbox(tmp_path, yaml=DECLARED_YAML)
    status = box.registry.find("a/g1/m2")  # declared: vision, tool_use
    store = {"a/g1/m2": {"vision": {"supported": False, "source": "probed"}}}  # probed 冲突
    caps = resolve_capabilities(status, store=store)
    assert caps["vision"]["supported"] is True
    assert caps["vision"]["source"] == "declared"


def test_resolve_priority_probed_over_mapped(tmp_path):
    box = _toolbox(tmp_path)
    status = box.registry.find("a/g1/m1")  # m1 无映射
    rules = load_capability_rules()
    # 构造 mapped 命中的假 rules + probed False 的 store：probed 应胜出
    import re

    fake_rules = [(re.compile(r"^m1$"), frozenset({"vision"}))]
    store = {"a/g1/m1": {"vision": {"supported": False, "source": "probed"}}}
    caps = resolve_capabilities(status, store=store, rules=fake_rules)
    assert caps["vision"]["supported"] is False
    assert caps["vision"]["source"] == "probed"


def test_resolve_mapped_and_unknown(tmp_path):
    box = _toolbox(tmp_path)
    status = box.registry.find("a/g1/m1")
    import re

    fake_rules = [(re.compile(r"^m1$"), frozenset({"vision"}))]
    caps = resolve_capabilities(status, store={}, rules=fake_rules)
    assert caps["vision"] == {"supported": True, "source": "mapped", "tested_at": None}
    assert caps["tool_use"]["supported"] is None
    assert caps["tool_use"]["source"] == "unknown"


def test_capability_value_helper(tmp_path):
    box = _toolbox(tmp_path)
    status = box.registry.find("a/g1/m1")
    assert capability_value(status, "vision", store={}) is None  # unknown -> None
    store = {"a/g1/m1": {"vision": {"supported": True, "source": "probed"}}}
    assert capability_value(status, "vision", store=store) is True


# -- vision 探测素材 ---------------------------------------------------------


def test_solid_png_b64_valid():
    data = base64.b64decode(_solid_png_b64((255, 0, 0)))
    assert data.startswith(b"\x89PNG\r\n\x1a\n")  # PNG 签名
    assert len(data) > 50


# -- mock 探测分流 -----------------------------------------------------------


def test_probe_mock_default_true_and_preset(tmp_path):
    box = _toolbox(tmp_path)
    outcome = probe_capability(box, "a/g1/m1", "vision")
    assert outcome["supported"] is True  # 未预设默认 True
    MOCK_CAPABILITY_OUTCOMES["m1"] = {"vision": False, "structured_output": None}
    try:
        assert probe_capability(box, "a/g1/m1", "vision")["supported"] is False
        assert probe_capability(box, "a/g1/m1", "structured_output")["supported"] is None
    finally:
        del MOCK_CAPABILITY_OUTCOMES["m1"]


def test_probe_unknown_model_or_capability(tmp_path):
    box = _toolbox(tmp_path)
    assert probe_capability(box, "a/g1/nope", "vision")["supported"] is None
    assert probe_capability(box, "a/g1/m1", "nope")["supported"] is None


def test_probe_emits_event(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)
    probe_capability(box, "a/g1/m1", "vision")
    events = [e for e in stream if e.type == EventType.CAPABILITY_PROBED]
    assert len(events) == 1
    assert events[0].data["model_ref"] == "a/g1/m1"
    assert events[0].data["capability"] == "vision"


# -- 真实路径探测（注入假适配器） --------------------------------------------


class _FakeAdapter:
    """按请求特征返回固定回复的假适配器（vision 色词 / tools / response_format）。"""

    def __init__(self, vision_reply="红色", structured_reply='{"ok": true}'):
        self.vision_reply = vision_reply
        self.structured_reply = structured_reply
        self.calls = []

    def call(self, base_url, api_key, request, **kwargs):
        from model_switch.protocols.base import ModelResponse

        self.calls.append(request)
        content = request.messages[-1]["content"]
        if isinstance(content, list):  # vision 多模态
            return ModelResponse(ok=True, text=self.vision_reply, latency_ms=10)
        if request.response_format is not None:  # structured
            return ModelResponse(ok=True, text=self.structured_reply, latency_ms=10)
        if request.tools is not None:  # tool_use
            return ModelResponse(ok=True, text="好的", latency_ms=10)
        return ModelResponse(ok=True, text="ok", latency_ms=10)


def _real_toolbox(tmp_path, monkeypatch, adapter):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)
    box = Toolbox(config, registry, EventStream(), mock=False)
    monkeypatch.setenv("KEY_A", "sk-test")
    monkeypatch.setattr("model_switch.protocols.registry.get_adapter", lambda p: adapter)
    return box


def test_probe_real_vision_supported(tmp_path, monkeypatch):
    # 固定随机色（红色）：假适配器回复"红色"含匹配词 -> 支持
    monkeypatch.setattr("model_switch.capability.random.choice", lambda seq: "红色")
    box = _real_toolbox(tmp_path, monkeypatch, _FakeAdapter(vision_reply="红色"))
    assert probe_capability(box, "a/g1/m1", "vision")["supported"] is True


def test_probe_real_vision_gateway_strips_image(tmp_path, monkeypatch):
    # 网关剥图：模型说看不到图 -> 判定不支持（False，而非 None）
    box = _real_toolbox(tmp_path, monkeypatch, _FakeAdapter(vision_reply="我看不到任何图片"))
    assert probe_capability(box, "a/g1/m1", "vision")["supported"] is False


def test_probe_real_tool_use_ok(tmp_path, monkeypatch):
    adapter = _FakeAdapter()
    box = _real_toolbox(tmp_path, monkeypatch, adapter)
    assert probe_capability(box, "a/g1/m1", "tool_use")["supported"] is True
    assert adapter.calls and adapter.calls[0].tools is not None  # 请求携带 tools 定义


def test_probe_real_structured(tmp_path, monkeypatch):
    box = _real_toolbox(tmp_path, monkeypatch, _FakeAdapter(structured_reply='{"ok": true}'))
    assert probe_capability(box, "a/g1/m1", "structured_output")["supported"] is True
    box2 = _real_toolbox(tmp_path, monkeypatch, _FakeAdapter(structured_reply="这不是 JSON"))
    assert probe_capability(box2, "a/g1/m2", "structured_output")["supported"] is False


def test_probe_real_call_failure_returns_none(tmp_path, monkeypatch):
    class _FailAdapter:
        def call(self, *args, **kwargs):
            from model_switch.protocols.base import ModelResponse

            return ModelResponse(ok=False, error_class="timeout", detail="timed out")

    box = _real_toolbox(tmp_path, monkeypatch, _FailAdapter())
    outcome = probe_capability(box, "a/g1/m1", "vision")
    assert outcome["supported"] is None  # 失败不定罪


def test_probe_real_key_missing_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    box = Toolbox(config, ModelRegistry(config), EventStream(), mock=False)
    monkeypatch.delenv("KEY_A", raising=False)
    outcome = probe_capability(box, "a/g1/m1", "vision")
    assert outcome["supported"] is None
    assert "未设置" in outcome["detail"]


# -- probe_all：增量 / 全量 / 落盘 --------------------------------------------


def test_probe_all_mock_writes_store_and_skips_declared(tmp_path):
    box = _toolbox(tmp_path, yaml=DECLARED_YAML)  # m2 declared vision+tool_use
    path = tmp_path / "capabilities.json"
    results = probe_all(box, path=path)
    # m2 declared 项不探测但出现在结果（source=declared）
    assert results["a/g1/m2"]["vision"]["source"] == "declared"
    store = load_capabilities(path)
    # m1 未声明任何能力：三项全 mock 探测落盘 probed
    assert store["a/g1/m1"]["vision"]["supported"] is True
    assert store["a/g1/m1"]["vision"]["source"] == "probed"
    # m2 declared vision+tool_use：这两项不探测（store 无记录）；
    # structured_output 未声明 -> 探测落盘
    assert "vision" not in store["a/g1/m2"]
    assert "tool_use" not in store["a/g1/m2"]
    assert store["a/g1/m2"]["structured_output"]["source"] == "probed"


def test_probe_all_incremental_skips_existing(tmp_path):
    box = _toolbox(tmp_path)
    path = tmp_path / "capabilities.json"
    save_capabilities(
        {"a/g1/m1": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}},
        path,
    )
    results = probe_all(box, path=path, incremental=True)
    # 已有 probed 记录：增量模式不重测（保留 False）
    assert results["a/g1/m1"]["vision"]["supported"] is False
    assert results["a/g1/m1"]["tool_use"]["supported"] is True  # 未测项本次 mock 探测


def test_probe_all_refresh_retests(tmp_path):
    box = _toolbox(tmp_path)
    path = tmp_path / "capabilities.json"
    save_capabilities(
        {"a/g1/m1": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}},
        path,
    )
    results = probe_all(box, path=path, incremental=False)  # refresh：非 declared 全重测
    assert results["a/g1/m1"]["vision"]["supported"] is True  # mock 默认覆盖旧 False


# -- 选型能力硬过滤（manager） -----------------------------------------------


def _choose_json(model, task_type="general"):
    payload = {"model": model, "reason": "按标签"}
    if task_type:
        payload["task_type"] = task_type
    return json.dumps(payload, ensure_ascii=False)


def test_choose_model_validator_blocks_unsupported_vision(tmp_path):
    stream = EventStream()
    # 决策者 m3：第一次选 m1（vision=False 被拒），第二次修正选 m2
    client = SequenceClient([
        _choose_json("a/g1/m1", "vision"),
        _choose_json("a/g1/m2", "vision"),
    ])
    box = _toolbox(tmp_path, client=client, stream=stream)
    caps_path = tmp_path / "capabilities.json"
    save_capabilities(
        {"a/g1/m1": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}},
        caps_path,
    )
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md", capabilities_path=caps_path)
    decision = mgr.choose_model("识别这张图里的文字")
    assert decision.decision["model"] == "a/g1/m2"
    assert decision.decision["task_type"] == "vision"


def test_choose_model_unknown_capability_passes(tmp_path):
    # 未测（None）放行：validator 不拦
    client = SequenceClient([_choose_json("a/g1/m1", "vision")])
    box = _toolbox(tmp_path, client=client)
    mgr = DecisionManager(
        box,
        prefs_path=tmp_path / "prefs.md",
        capabilities_path=tmp_path / "capabilities.json",  # 无记录 -> unknown
    )
    decision = mgr.choose_model("识别这张图里的文字")
    assert decision.decision["model"] == "a/g1/m1"


def test_status_line_capability_column(tmp_path):
    box = _toolbox(tmp_path)
    caps_path = tmp_path / "capabilities.json"
    save_capabilities(
        {
            "a/g1/m1": {
                "vision": {"supported": False, "source": "probed", "tested_at": "t"},
                "tool_use": {"supported": True, "source": "probed", "tested_at": "t"},
            }
        },
        caps_path,
    )
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md", capabilities_path=caps_path)
    line = mgr._status_line(
        box.registry.find("a/g1/m1"), {}, caps_store=load_capabilities(caps_path)
    )
    assert "能力: " in line
    assert "视觉✗" in line and "工具✓" in line
    assert "结构" not in line.split("能力: ")[1]  # 未测项不显示（防噪音/误导）
    # 全未知模型不显示能力列
    line_unknown = mgr._status_line(
        box.registry.find("a/g1/m2"), {}, caps_store=load_capabilities(caps_path)
    )
    assert "能力: " not in line_unknown


# -- plan_recovery 候选过滤 ---------------------------------------------------


def test_plan_recovery_candidate_table_excludes_unsupported(tmp_path):
    # 决策者 m3 反复选 m2（vision=False）：validator 每次拒绝 -> 最终 DecisionError；
    # 同时验证发给 LLM 的候选表 prompt 不含确认不支持的 m2
    switch = json.dumps({"switch_to": "a/g1/m2", "diagnose": False, "reason": "顶上"})
    client = SequenceClient([switch, switch, switch])
    box = _toolbox(tmp_path, client=client)
    caps_path = tmp_path / "capabilities.json"
    save_capabilities(
        {"a/g1/m2": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}},
        caps_path,
    )
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md", capabilities_path=caps_path)
    with pytest.raises(Exception):
        mgr.plan_recovery("a/g1/m1", "401", required_capability="vision")
    prompts = [call["messages"][-1]["content"] for call in client.calls]
    assert prompts  # 决策调用已发出
    # 候选表行不含确认不支持者（反馈重试文本可能提及 m2，属预期——用于纠错）
    assert all("- a/g1/m2 |" not in text for text in prompts)


def test_plan_recovery_validator_rejects_unsupported_target(tmp_path):
    # 第一次决策选 m2（vision=False）-> validator 拒绝并反馈；第二次选 m3 -> 通过
    switch1 = json.dumps({"switch_to": "a/g1/m2", "diagnose": False, "reason": "顶上"})
    switch2 = json.dumps({"switch_to": "b/g1/m3", "diagnose": False, "reason": "换一个"})
    client = SequenceClient([switch1, switch2])
    box = _toolbox(tmp_path, client=client)
    caps_path = tmp_path / "capabilities.json"
    save_capabilities(
        {"a/g1/m2": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}},
        caps_path,
    )
    mgr = DecisionManager(box, prefs_path=tmp_path / "prefs.md", capabilities_path=caps_path)
    plan = mgr.plan_recovery("a/g1/m1", "401", required_capability="vision")
    assert plan.decision["switch_to"] == "b/g1/m3"


# -- pipeline 机械兜底候选过滤 -------------------------------------------------


def test_mechanical_excludes_unsupported(tmp_path):
    from model_switch.pipeline import Pipeline
    from model_switch.mocker import MockModelClient

    stream = EventStream()
    # m1 失败（401）-> m2 应被能力过滤跳过 -> m3 成功
    client = MockModelClient(rules={"m1": "401"})
    box = _toolbox(tmp_path, client=client, stream=stream)
    caps_path = tmp_path / "capabilities.json"
    save_capabilities(
        {"a/g1/m2": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}},
        caps_path,
    )
    pipeline = Pipeline(
        box,
        prefs_path=tmp_path / "prefs.md",
        pause_path=tmp_path / "pause.json",
        stats_path=tmp_path / "stats.json",
        capabilities_path=caps_path,
    )
    result = pipeline._mechanical("看图任务", set(), 0, 0, "trace-1", task_type="vision")
    assert result.ok
    assert result.model_ref == "b/g1/m3"  # m2 被能力过滤跳过
    fallback = [e for e in stream if e.type == EventType.MECHANICAL_FALLBACK]
    assert "a/g1/m2" not in fallback[0].data["tried_models"]


# -- 配置声明式解析 -----------------------------------------------------------


def test_config_declared_models_parsed(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(DECLARED_YAML, encoding="utf-8")
    config = load_config(path)
    group = config.providers[0].groups[0]
    assert group.models == ("m1", "m2")  # 兼容混合格式，顺序不变
    assert group.model_caps["m2"] == ("vision", "tool_use")
    assert group.model_caps.get("m1", ()) == ()
    registry = ModelRegistry(config)
    assert registry.find("a/g1/m2").capabilities_declared == ("vision", "tool_use")
    assert registry.find("a/g1/m1").capabilities_declared == ()
    assert "capabilities_declared" in registry.find("a/g1/m2").to_dict()


def test_config_declared_missing_name_raises(tmp_path):
    yaml = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - name: g1
        key_env: KEY_A
        models:
          - { capabilities: [vision] }
fallback: { provider: a, group: g1, model: m1, key_env: KEY_A }
"""
    # 缺 name 且清单无其他有效模型 -> ConfigError
    path = tmp_path / "models.yaml"
    path.write_text(yaml, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_config_declared_unknown_capability_raises(tmp_path):
    yaml = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - name: g1
        key_env: KEY_A
        models:
          - m1
          - { name: m2, capabilities: [telepathy] }
fallback: { provider: a, group: g1, model: m1, key_env: KEY_A }
"""
    path = tmp_path / "models.yaml"
    path.write_text(yaml, encoding="utf-8")
    with pytest.raises(ConfigError, match="未知能力"):
        load_config(path)


def test_config_declared_caps_not_list_raises(tmp_path):
    yaml = """\
providers:
  - name: a
    base_url: https://a.example.com/v1
    groups:
      - name: g1
        key_env: KEY_A
        models:
          - m1
          - { name: m2, capabilities: vision }
fallback: { provider: a, group: g1, model: m1, key_env: KEY_A }
"""
    path = tmp_path / "models.yaml"
    path.write_text(yaml, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


# -- onboarding 能力步骤 -------------------------------------------------------


def test_onboarding_skip_capabilities_all_unknown(tmp_path):
    from model_switch.onboarding import run_onboarding
    from model_switch.mocker import MockModelClient

    profile = json.dumps(
        {"labels": {ref: "标签" for ref in ("a/g1/m1", "a/g1/m2", "b/g1/m3")}},
        ensure_ascii=False,
    )
    box = _toolbox(tmp_path, client=MockModelClient(rules={"m3": profile}))
    result = run_onboarding(
        box,
        confirm_handler=lambda text: (True, "y"),
        prefs_path=tmp_path / "prefs.md",
        skip_capabilities=True,
        capabilities_path=tmp_path / "capabilities.json",
    )
    assert result["ok"] is True
    for caps in result["capabilities"].values():
        assert all(entry["supported"] is None for entry in caps.values())
    assert not (tmp_path / "capabilities.json").exists()  # 跳过时不落盘


def test_onboarding_probes_capabilities(tmp_path):
    from model_switch.onboarding import run_onboarding
    from model_switch.mocker import MockModelClient

    stream = EventStream()
    profile = json.dumps(
        {"labels": {ref: "标签" for ref in ("a/g1/m1", "a/g1/m2", "b/g1/m3")}},
        ensure_ascii=False,
    )
    box = _toolbox(tmp_path, client=MockModelClient(rules={"m3": profile}), stream=stream)
    result = run_onboarding(
        box,
        confirm_handler=lambda text: (True, "y"),
        prefs_path=tmp_path / "prefs.md",
        capabilities_path=tmp_path / "capabilities.json",
    )
    assert result["ok"] is True
    probed = [e for e in stream if e.type == EventType.CAPABILITY_PROBED]
    assert len(probed) == 3 * len(CAPABILITIES)  # 3 模型 × 3 能力（mock 无映射全未知）
    assert (tmp_path / "capabilities.json").exists()
    # 确认文本含能力表
    store = load_capabilities(tmp_path / "capabilities.json")
    assert store["a/g1/m1"]["vision"]["supported"] is True


# -- CLI capabilities 命令 ------------------------------------------------------


def test_cli_tools_shows_capability_line(tmp_path, capsys, monkeypatch):
    """mso tools 人读清单展示能力列（只显示已定论项）。"""
    from model_switch import cli

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    save_capabilities(
        {"a/g1/m1": {"vision": {"supported": False, "source": "probed", "tested_at": "t"}}},
        tmp_path / "data" / "capabilities.json",
    )
    code = cli.main(["tools", "--config", str(yaml_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "能力: 视觉✗" in out  # 已定论模型显示能力列
    assert out.count("└ 能力:") == 1  # 未测模型不显示能力行（防噪音）


def test_cli_capabilities_mock(tmp_path, capsys, monkeypatch):
    from model_switch import cli

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # mock 临时目录外无真实 data/ 写入
    code = cli.main(["capabilities", "--config", str(yaml_path), "--mock"])
    out = capsys.readouterr().out
    assert code == 0
    assert "模型能力" in out
    assert "a/g1/m1" in out
    # mock 预置：首个模型 vision=False（展示过滤素材）
    assert "vision=✗" in out


def test_cli_capabilities_json(tmp_path, capsys, monkeypatch):
    from model_switch import cli

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code = cli.main(
        ["capabilities", "--config", str(yaml_path), "--mock", "--json"]
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert payload["ok"] is True
    assert "a/g1/m1" in payload["capabilities"]
    assert payload["capabilities"]["a/g1/m1"]["vision"]["supported"] is False  # mock 预置


def test_cli_capabilities_unknown_model(tmp_path, capsys, monkeypatch):
    from model_switch import cli

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code = cli.main(
        ["capabilities", "--config", str(yaml_path), "--mock", "--model", "a/g1/nope"]
    )
    assert code == 1
    assert "未知模型" in capsys.readouterr().out


# -- switcher / MCP 接口 --------------------------------------------------------


def test_switcher_capabilities_interfaces(tmp_path):
    from model_switch import ModelSwitcher

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    caps_path = tmp_path / "capabilities.json"
    switcher = ModelSwitcher(
        yaml_path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", persist_events=False,
        capabilities_path=caps_path,
    )
    # 查询（不探测）：mock 隔离路径下无记录 -> 全 unknown
    caps = switcher.get_capabilities("a/g1/m1")
    assert caps["a/g1/m1"]["vision"]["supported"] is None
    # 探测：mock 全 True 落盘
    result = switcher.probe_capabilities("a/g1/m1")
    assert result["a/g1/m1"]["vision"]["supported"] is True
    assert load_capabilities(caps_path)["a/g1/m1"]["vision"]["supported"] is True


def test_mcp_model_capabilities_tool(tmp_path):
    from model_switch.mcp import build_mcp_server
    from model_switch import ModelSwitcher

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    switcher = ModelSwitcher(
        yaml_path, mock=True,
        faults_dir=tmp_path / "faults", probe_dir=tmp_path / "probe",
        tasks_dir=tmp_path / "tasks", persist_events=False,
        capabilities_path=tmp_path / "capabilities.json",
    )
    server = build_mcp_server(switcher)

    import asyncio

    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert "model_capabilities" in names

    raw = asyncio.run(server.call_tool("model_capabilities", {"model_ref": "a/g1/m1"}))
    content = raw[0] if isinstance(raw, tuple) else raw
    payload = json.loads("".join(getattr(i, "text", str(i)) for i in content))
    assert payload["ok"] is True
    assert "a/g1/m1" in payload["capabilities"]


# -- mock 零污染 ----------------------------------------------------------------


def test_cli_capabilities_mock_writes_no_real_store(tmp_path, capsys, monkeypatch):
    """CLI mock 探测写临时能力文件，不污染真实 data/capabilities.json。"""
    from model_switch import cli

    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # cwd 即测试目录：真实 data/ 不应出现 capabilities.json
    cli.main(["capabilities", "--config", str(yaml_path), "--mock", "--json"])
    capsys.readouterr()
    assert not (tmp_path / "data" / "capabilities.json").exists()
