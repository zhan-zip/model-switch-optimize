"""内置规则库：rules/diagnose.md 与 rules/capabilities.md 加载。

开发者维护、随程序发布；使用者不接触。文件缺失时降级（诊断空串 / 能力空表）。
"""
from __future__ import annotations

import re
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"
DEFAULT_RULES_PATH = RULES_DIR / "diagnose.md"
DEFAULT_CAPABILITY_RULES_PATH = RULES_DIR / "capabilities.md"


def load_rules(path: Path | str | None = None) -> str:
    """读取规则库文本；不存在返回空串。"""

    target = Path(path) if path is not None else DEFAULT_RULES_PATH
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8").strip()


def load_capability_rules(
    path: Path | str | None = None,
) -> list[tuple[re.Pattern[str], frozenset[str]]]:
    """读取能力映射表：[(模型名正则, 能力集合), ...]。

    rules/capabilities.md 的映射规则表（两列：模型名正则 | 逗号分隔能力）；
    缺失/损坏返回空表（mapped 层降级为无映射）。
    """

    target = Path(path) if path is not None else DEFAULT_CAPABILITY_RULES_PATH
    if not target.exists():
        return []
    rules: list[tuple[re.Pattern[str], frozenset[str]]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        pattern, caps_raw = cells[0], cells[1]
        if not pattern or pattern.startswith("-") or pattern.startswith("#") or pattern == "模型名正则":
            continue  # 分隔线 / 注释 / 表头
        caps = frozenset(
            part.strip() for part in caps_raw.split(",") if part.strip()
        )
        try:
            rules.append((re.compile(pattern), caps))
        except re.error:
            continue  # 坏正则条目跳过
    return rules


def match_capability_rules(
    model_name: str,
    rules: list[tuple[re.Pattern[str], frozenset[str]]] | None = None,
) -> frozenset[str]:
    """按模型名匹配能力（全部命中规则的并集；无命中返回空集）。"""

    rules = load_capability_rules() if rules is None else rules
    matched: set[str] = set()
    for pattern, caps in rules:
        if pattern.search(model_name):
            matched.update(caps)
    return frozenset(matched)
