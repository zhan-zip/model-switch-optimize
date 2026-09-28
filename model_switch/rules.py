"""内置规则库：rules/diagnose.md 加载。

开发者维护、随程序发布，告知决策模型常见故障判断要点；
使用者不接触。文件缺失时返回空串（诊断降级为无规则模式）。
"""
from __future__ import annotations

from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"
DEFAULT_RULES_PATH = RULES_DIR / "diagnose.md"


def load_rules(path: Path | str | None = None) -> str:
    """读取规则库文本；不存在返回空串。"""

    target = Path(path) if path is not None else DEFAULT_RULES_PATH
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8").strip()
