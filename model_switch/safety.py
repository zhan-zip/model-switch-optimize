"""全链路脱敏。

key 只走环境变量；任何输出（事件、日志、CLI 回显）前必须经过本模块。
- redact_text   掩码文本中的常见 key 串（保留前几位便于辨认，其余 ****）
- redact        递归脱敏 str / dict / list；敏感键名的长值整体掩码
- mask_env_value 展示某环境变量（key）状态：只露前缀

task_id（task-<12位hex>）是两阶段审批的公开关联键，明文保留：
其前缀含 "sk-" 子串会被 key 模式误伤，故先占位保护再脱敏、最后还原。
"""
from __future__ import annotations

import re
from typing import Any

_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}"),
    re.compile(r"(gsk_[A-Za-z0-9]{4})[A-Za-z0-9]{4,}"),
    re.compile(r"(Bearer\s+[A-Za-z0-9._\-]{4})[A-Za-z0-9._\-]{4,}", re.IGNORECASE),
)

# task_id 明文保护：task- 后必跟 12 位 hex（两阶段审批公开关联键）
_TASK_ID_RE = re.compile(r"task-[0-9a-f]{12}")
_TASK_ID_PLACEHOLDER = "\x00mso-task-id\x00"

_SENSITIVE_KEYS = frozenset({
    "key", "api_key", "apikey", "token", "access_token", "refresh_token",
    "secret", "client_secret", "password", "authorization", "credentials", "cookie",
})

_SHORT_VALUE_KEEP = 12  # 环境变量名等短标识符（如 KEY_A / FALLBACK_KEY）不视为真实 key


def redact_text(text: str) -> str:
    """掩码文本中的常见 key 串（task_id 先占位保护，避免 "task-" 被误伤）。"""

    ids: list[str] = []
    text = _TASK_ID_RE.sub(lambda m: ids.append(m.group(0)) or _TASK_ID_PLACEHOLDER, text)
    for pattern in _TEXT_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "****", text)
    if ids:
        iterator = iter(ids)
        text = re.sub(
            re.escape(_TASK_ID_PLACEHOLDER),
            lambda m: next(iterator, m.group(0)),
            text,
        )
    return text


def is_sensitive_name(name: Any) -> bool:
    """是否为敏感键名（精确匹配，key_env 等环境变量名不算敏感值）。"""

    return str(name).lower() in _SENSITIVE_KEYS


def _mask_value(value: str) -> str:
    if len(value) <= _SHORT_VALUE_KEEP:
        return value
    return value[:4] + "****"


def redact(obj: Any) -> Any:
    """递归脱敏；事件 data 落盘/输出前统一调用。"""

    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {
            k: (_mask_value(v) if is_sensitive_name(k) and isinstance(v, str) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    return obj


def mask_env_value(value: str | None) -> str:
    """展示某环境变量（key）状态：只露前4位。"""

    if not value:
        return "<未设置>"
    return value[:4] + "****"
