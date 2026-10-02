"""阶段 0：兼容性测�?- 确保旧版配置和统计数据可以正常加载�?
这些测试验证向后兼容性，确保未来升级不会破坏现有用户的配置和数据�?"""
import json
from pathlib import Path

import pytest

from model_switch.config import load_config
from model_switch.stats import load_stats


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_load_legacy_config_v1():
    """旧版配置（无 protocol 字段）可以正常加载�?""
    config_path = FIXTURES_DIR / "legacy_models_v1.yaml"

    config = load_config(config_path)

    # 基本结构正确
    assert len(config.providers) == 2
    assert config.providers[0].name == "openai-compatible"
    assert config.providers[1].name == "local-ollama"

    # fallback 正确
    fallback = config.fallback_ref()
    assert str(fallback) == "openai-compatible/default/gpt-4o-mini"


def test_legacy_config_has_no_protocol_field():
    """确认旧版配置确实没有 protocol 字段（用于测试默认值逻辑）�?""
    config_path = FIXTURES_DIR / "legacy_models_v1.yaml"

    # 读取原始 YAML 内容
    content = config_path.read_text(encoding="utf-8")

    # 确认没有 protocol 关键�?    assert "protocol" not in content.lower()


def test_load_legacy_stats_v1():
    """旧版 stats.json（数组格式）可以正常加载�?""
    stats_path = FIXTURES_DIR / "legacy_stats_v1.json"

    stats = load_stats(stats_path)

    # 应该能读取到旧格式数�?    assert "openai-compatible/default/gpt-4o" in stats
    assert "openai-compatible/default/gpt-4o-mini" in stats
    assert "local-ollama/local/llama3" in stats

    # 验证数据结构 - 迁移后统一�?{"recent": [...]}
    gpt4o_stats = stats["openai-compatible/default/gpt-4o"]
    assert isinstance(gpt4o_stats, dict)
    assert "recent" in gpt4o_stats
    assert len(gpt4o_stats["recent"]) == 10

    # 验证每条记录格式 [ok, latency_ms, switches]
    first_record = gpt4o_stats["recent"][0]
    assert len(first_record) == 3
    assert isinstance(first_record[0], bool)  # ok
    assert isinstance(first_record[1], int)   # latency_ms
    assert isinstance(first_record[2], int)   # switches


def test_legacy_stats_format_is_array():
    """确认旧版 stats 确实是数组格式（不是对象格式）�?""
    stats_path = FIXTURES_DIR / "legacy_stats_v1.json"

    raw_data = json.loads(stats_path.read_text(encoding="utf-8"))

    # 每个模型的统计应该是数组
    for model_ref, records in raw_data.items():
        assert isinstance(records, list)
        if records:
            # 每条记录应该是三元素数组 [ok, latency_ms, switches]
            assert isinstance(records[0], list)
            assert len(records[0]) == 3


def test_legacy_config_models_are_accessible(tmp_path):
    """旧版配置中的模型可以�?ModelRegistry 正确识别�?""
    config_path = FIXTURES_DIR / "legacy_models_v1.yaml"

    config = load_config(config_path)
    from model_switch.model import ModelRegistry

    registry = ModelRegistry(config)

    # 所有模型都能找�?    assert registry.find("openai-compatible/default/gpt-4o") is not None
    assert registry.find("openai-compatible/default/gpt-4o-mini") is not None
    assert registry.find("local-ollama/local/llama3") is not None
    assert registry.find("local-ollama/local/mistral") is not None

    # 总共 4 个模�?    all_models = registry.all()
    assert len(all_models) == 4


def test_legacy_stats_survival_rate_calculation():
    """旧版统计数据可以正确计算成功率�?""
    stats_path = FIXTURES_DIR / "legacy_stats_v1.json"

    stats = load_stats(stats_path)

    # 手动计算 gpt-4o 的成功率（迁移后�?{"recent": [...]})
    gpt4o_records = stats["openai-compatible/default/gpt-4o"]["recent"]
    success_count = sum(1 for record in gpt4o_records if record[0])
    success_rate = success_count / len(gpt4o_records)

    # 10 条记录中 9 条成�?    assert success_rate == 0.9

    # llama3 的成功率
    llama3_records = stats["local-ollama/local/llama3"]["recent"]
    llama3_success = sum(1 for record in llama3_records if record[0])
    llama3_rate = llama3_success / len(llama3_records)

    # 4 条记录中 2 条成�?    assert llama3_rate == 0.5


def test_future_stats_v2_will_be_backward_compatible():
    """预留测试：未来的 stats v2 格式应该能兼容读�?v1�?
    这个测试现在通过，因�?load_stats 已经可以读取旧格式�?    当实施阶�?2（stats v2）时，这个测试应该继续通过�?    """
    stats_path = FIXTURES_DIR / "legacy_stats_v1.json"

    # 当前可以加载
    stats = load_stats(stats_path)
    assert stats is not None
    assert len(stats) > 0

    # 未来升级后，这个测试应该继续通过
    # 如果这个测试失败，说明破坏了向后兼容�?