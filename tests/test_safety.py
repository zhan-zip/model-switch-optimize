"""safety 模块测试：文本脱敏、递归脱敏、敏感键掩码、环境变量名保留。"""
from model_switch.safety import is_sensitive_name, mask_env_value, redact, redact_text


def test_redact_text_sk_key():
    assert redact_text("key is sk-abcdef1234567890ghi") == "key is sk-abcd****"


def test_redact_text_bearer():
    assert redact_text("Authorization: Bearer abcdefghijklmno") == "Authorization: Bearer abcd****"


def test_plain_text_untouched():
    text = "调用 gpt-4o 失败，error_class=429"
    assert redact_text(text) == text


def test_redact_dict_sensitive_key():
    out = redact({"api_key": "sk-abcdefgh1234567890", "model": "gpt-4o"})
    assert out["api_key"].endswith("****")
    assert "abcdefgh" not in out["api_key"]
    assert out["model"] == "gpt-4o"


def test_redact_keeps_env_var_names():
    out = redact({"key_env": "KEY_A", "fallback_key_env": "FALLBACK_KEY"})
    assert out == {"key_env": "KEY_A", "fallback_key_env": "FALLBACK_KEY"}


def test_redact_nested_structures():
    payload = {
        "detail": "auth failed for sk-abcdef1234567890",
        "nested": [{"token": "x" * 40}],
    }
    out = redact(payload)
    assert "sk-abcdef" not in out["detail"]
    assert out["nested"][0]["token"].endswith("****")


def test_mask_env_value():
    assert mask_env_value(None) == "<未设置>"
    assert mask_env_value("") == "<未设置>"
    assert mask_env_value("abcdefghij123456") == "abcd****"


def test_redact_keeps_task_id_plaintext():
    """task_id（task-<12hex>）是两阶段审批公开关联键，不被 key 模式误伤。"""
    task_id = "task-af4e7b852dbd"
    assert redact_text(f"任务 {task_id} 已准备") == f"任务 {task_id} 已准备"
    out = redact({"task_id": task_id})
    assert out["task_id"] == task_id
    # 相邻真实 key 仍正常脱敏
    mixed = redact({"task_id": task_id, "api_key": "sk-abcdefghij123456"})
    assert mixed["task_id"] == task_id
    assert mixed["api_key"].endswith("****")


def test_is_sensitive_name():
    assert is_sensitive_name("API_KEY")
    assert is_sensitive_name("Authorization")
    assert not is_sensitive_name("key_env")
    assert not is_sensitive_name("model_ref")
