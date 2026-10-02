"""阶段 0 兼容性基线：旧配置和旧 stats.json 可以继续读取。"""

from pathlib import Path

from model_switch.config import load_config
from model_switch.model import ModelRegistry
from model_switch.stats import load_stats, summary


FIXTURES = Path(__file__).parent / "fixtures"


def test_load_legacy_config_without_protocol():
    config = load_config(FIXTURES / "legacy_models_v1.yaml")
    assert [provider.name for provider in config.providers] == ["openai-compatible", "local-ollama"]
    assert str(config.fallback_ref()) == "openai-compatible/default/gpt-4o-mini"


def test_legacy_config_models_are_available_in_registry():
    config = load_config(FIXTURES / "legacy_models_v1.yaml")
    registry = ModelRegistry(config)
    assert registry.find("openai-compatible/default/gpt-4o") is not None
    assert registry.find("local-ollama/local/llama3") is not None


def test_load_legacy_stats_array_format():
    stats = load_stats(FIXTURES / "legacy_stats_v1.json")
    entry = stats["openai-compatible/default/gpt-4o"]
    assert isinstance(entry["recent"], list)
    assert len(entry["recent"]) == 10
    assert entry["recent"][0] == [True, 823, 0]


def test_legacy_stats_summary_still_works():
    stats = load_stats(FIXTURES / "legacy_stats_v1.json")
    result = summary("openai-compatible/default/gpt-4o", stats=stats)
    assert result == {"n": 10, "ok": 9, "rate": 90, "avg_latency_ms": 996}


def test_load_stats_accepts_mixed_old_and_new_entries(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text(
        '{"old": [[true, 100, 0]], "new": {"recent": [[false, 200, 1]]}}',
        encoding="utf-8",
    )
    stats = load_stats(path)
    assert stats["old"]["recent"] == [[True, 100, 0]]
    assert stats["new"]["recent"] == [[False, 200, 1]]
