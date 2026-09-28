"""onboarding 模块测试：init-check 全流程（mock 决策 / 确认循环 / 标签落盘）。"""
import json

from model_switch.events import EventStream, EventType
from model_switch.mocker import MockConsole, MockModelClient, MockWebSearch
from model_switch.config import load_config
from model_switch.model import ModelRegistry
from model_switch.onboarding import run_onboarding
from model_switch.prefs import load_prefs
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

PROFILE = {"a/g1/m1": "通用、便宜", "a/g1/m2": "快速", "b/g1/m3": "保底、稳定"}


def _profile_json():
    return json.dumps({"labels": dict(PROFILE)}, ensure_ascii=False)


def _toolbox(tmp_path, stream=None, rules=None, web_search=True):
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)
    client = MockModelClient(rules={"m3": _profile_json(), **(rules or {})})
    return Toolbox(
        config, registry, stream,
        model_client=client,
        console=MockConsole(),
        web_search_impl=MockWebSearch().search if web_search else None,
        mock=True,
    )


def _always_ok(text):
    return True, "y"


def test_full_flow_ready_and_labels_saved(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)
    prefs_path = tmp_path / "model_prefs.md"
    result = run_onboarding(box, confirm_handler=_always_ok, prefs_path=prefs_path)

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["connectivity"] == {"total": 3, "ok": 3}
    prefs = load_prefs(prefs_path)
    assert prefs["model_labels"] == PROFILE
    assert box.registry.find("a/g1/m1").labels == ("通用", "便宜")

    types = [e.type for e in stream]
    assert types.count(EventType.ONBOARDING_CONNECTIVITY) == 3
    assert EventType.ONBOARDING_PROFILE in types
    assert EventType.ONBOARDING_PROFILE_CONFIRMED in types
    assert EventType.ONBOARDING_LABELS_SAVED in types
    assert types.index(EventType.ONBOARDING_LABELS_SAVED) > types.index(
        EventType.ONBOARDING_PROFILE_CONFIRMED
    )


def test_revision_round_carries_user_note(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)
    answers = iter([(False, "m1 的标签改成 便宜快速"), (True, "y")])

    def handler(text):
        return next(answers)

    result = run_onboarding(box, confirm_handler=handler, prefs_path=tmp_path / "p.md")
    assert result["ok"] is True
    # 第二轮画像决策的 prompt 携带用户意见
    profile_calls = [
        call for call in box._client.calls if "模型画像生成者" in call["messages"][0]["content"]
    ]
    assert len(profile_calls) == 2
    assert "m1 的标签改成 便宜快速" in profile_calls[1]["messages"][0]["content"]
    assert EventType.ONBOARDING_PROFILE_CONFIRMED in [e.type for e in stream]


def test_per_group_only_probes_leaders(tmp_path):
    box = _toolbox(tmp_path)
    result = run_onboarding(
        box, per_group=True, confirm_handler=_always_ok, prefs_path=tmp_path / "p.md"
    )
    assert result["connectivity"] == {"total": 2, "ok": 2}
    probes = [c for c in box._client.calls if c["messages"][0]["content"] == "ping"]
    assert len(probes) == 2  # a/g1/m1 + b/g1/m3（每分组代表）


def test_json_mode_needs_confirmation_without_save(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream)
    prefs_path = tmp_path / "p.md"
    result = run_onboarding(box, json_mode=True, prefs_path=prefs_path)

    assert result["ok"] is False
    assert result["status"] == "needs_confirmation"
    assert result["profile"] == PROFILE
    assert not prefs_path.exists()
    assert EventType.ONBOARDING_LABELS_SAVED not in [e.type for e in stream]


def test_max_rounds_not_confirmed_no_save(tmp_path):
    box = _toolbox(tmp_path)
    prefs_path = tmp_path / "p.md"
    result = run_onboarding(
        box, confirm_handler=lambda text: (False, "还不满意"), prefs_path=prefs_path, max_rounds=2
    )
    assert result["ok"] is False
    assert result["status"] == "profile_not_confirmed"
    assert not prefs_path.exists()


def test_web_search_unavailable_does_not_block(tmp_path):
    stream = EventStream()
    box = _toolbox(tmp_path, stream=stream, web_search=False)
    result = run_onboarding(box, confirm_handler=_always_ok, prefs_path=tmp_path / "p.md")
    assert result["ok"] is True  # 跑分搜索失败不阻断流程
    profile_calls = [
        call for call in box._client.calls if "模型画像生成者" in call["messages"][0]["content"]
    ]
    assert "跑分搜索不可用" in profile_calls[0]["messages"][0]["content"]


def test_labels_preserve_existing_task_prefs(tmp_path):
    prefs_path = tmp_path / "p.md"
    from model_switch.prefs import save_prefs

    save_prefs({"model_labels": {}, "task_prefs": {"老任务": "a/g1/m1"}}, prefs_path)
    box = _toolbox(tmp_path)
    result = run_onboarding(box, confirm_handler=_always_ok, prefs_path=prefs_path)
    assert result["ok"] is True
    prefs = load_prefs(prefs_path)
    assert prefs["task_prefs"]["老任务"] == "a/g1/m1"  # 保留
    assert prefs["model_labels"] == PROFILE  # 新增


def test_sequence_client_shares_one_stream(tmp_path):
    """manager 复用 toolbox 的 client：onboarding 全程同一 SequenceClient 也可驱动。"""
    stream = EventStream()
    path = tmp_path / "models.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    config = load_config(path)
    registry = ModelRegistry(config)
    client = SequenceClient(
        [
            "[mock] ok",  # probe m1
            "[mock] ok",  # probe m2
            "[mock] ok",  # probe m3
            json.dumps({"labels": dict(PROFILE)}, ensure_ascii=False),  # 画像决策
        ]
    )
    box = Toolbox(config, registry, stream, model_client=client, mock=True)
    result = run_onboarding(box, confirm_handler=_always_ok, prefs_path=tmp_path / "p.md")
    assert result["ok"] is True
    assert client.replies == []  # 回复全部按序消耗
