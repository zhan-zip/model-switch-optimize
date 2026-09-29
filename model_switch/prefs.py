"""模型标签偏好：config/model_prefs.md 读写。

人可读、可手改的 markdown 表格；程序可解析回字典。
三个区块：
  ## 模型标签 —— 每个模型的标签（mso init-check 生成）
  ## 任务偏好 —— 任务关键词 -> 模型（choose_model 确认沉淀）
  ## 切换偏好 —— 错误类别 -> 上次成功切换目标 + 命中次数（plan_recovery 沉淀，
                命中时程序直接切换省决策调用；可手改/删除）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_PREFS_PATH = Path("config/model_prefs.md")

SECTION_LABELS = "## 模型标签"
SECTION_TASKS = "## 任务偏好"
SECTION_PLANS = "## 切换偏好"

_HEADERS = {"模型", "任务关键词", "错误类别"}


def load_prefs(path: Path | str = DEFAULT_PREFS_PATH) -> dict[str, dict[str, Any]]:
    """解析偏好文件；不存在返回空结构（不报错）。"""

    path = Path(path)
    prefs: dict[str, dict[str, Any]] = {"model_labels": {}, "task_prefs": {}, "plan_prefs": {}}
    if not path.exists():
        return prefs
    section: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == SECTION_LABELS:
            section = "model_labels"
            continue
        if stripped == SECTION_TASKS:
            section = "task_prefs"
            continue
        if stripped == SECTION_PLANS:
            section = "plan_prefs"
            continue
        if section is None or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key, value = cells[0], cells[1]
        if not key or not value:
            continue  # 空行/空值
        if key.startswith("-") or key.startswith("#") or key in _HEADERS:
            continue  # 分隔线 / 注释 / 表头
        if section == "plan_prefs":
            hits = 1
            if len(cells) >= 3:
                try:
                    hits = max(1, int(cells[2]))
                except ValueError:
                    hits = 1
            prefs["plan_prefs"][key] = (value, hits)
        else:
            prefs[section][key] = value
    return prefs


def save_prefs(prefs: dict[str, dict[str, Any]], path: Path | str = DEFAULT_PREFS_PATH) -> Path:
    """写回 markdown（整文件重写，三区块全保留）。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 模型标签偏好（mso init-check 生成，可手改）",
        "",
        SECTION_LABELS,
        "",
        "| 模型 | 标签 |",
        "| --- | --- |",
    ]
    for ref, labels in sorted(prefs.get("model_labels", {}).items()):
        lines.append(f"| {ref} | {labels} |")
    lines += ["", SECTION_TASKS, "", "| 任务关键词 | 模型 |", "| --- | --- |"]
    for task, ref in sorted(prefs.get("task_prefs", {}).items()):
        lines.append(f"| {task} | {ref} |")
    lines += ["", SECTION_PLANS, "", "| 错误类别 | 切换目标 | 命中次数 |", "| --- | --- | --- |"]
    for error_class, hit in sorted(prefs.get("plan_prefs", {}).items()):
        ref, hits = hit if isinstance(hit, tuple) else (hit, 1)
        lines.append(f"| {error_class} | {ref} | {hits} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def add_task_pref(task: str, model_ref: str, path: Path | str = DEFAULT_PREFS_PATH) -> None:
    """沉淀一条任务偏好（choose_model 确认后调用）。"""

    prefs = load_prefs(path)
    prefs["task_prefs"][task] = model_ref
    save_prefs(prefs, path)


def add_plan_pref(error_class: str, model_ref: str, path: Path | str = DEFAULT_PREFS_PATH) -> None:
    """沉淀一条切换偏好（切换成功后由 pipeline 调用）。

    同目标累计命中次数；换目标视为修正，重置为 1。
    """

    prefs = load_prefs(path)
    existing = prefs["plan_prefs"].get(error_class)
    if existing is not None and existing[0] == model_ref:
        hits = existing[1] + 1
    else:
        hits = 1
    prefs["plan_prefs"][error_class] = (model_ref, hits)
    save_prefs(prefs, path)


def match_plan_pref(
    error_class: str,
    prefs: dict[str, dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_PREFS_PATH,
) -> tuple[str, int] | None:
    """切换偏好匹配：返回 (切换目标, 命中次数) 或 None。"""

    prefs = prefs if prefs is not None else load_prefs(path)
    hit = prefs.get("plan_prefs", {}).get(error_class)
    return hit if hit else None


def match_task_pref(
    task: str,
    prefs: dict[str, dict[str, str]] | None = None,
    path: Path | str = DEFAULT_PREFS_PATH,
) -> str | None:
    """任务偏好匹配：已存关键词是任务文本的子串即命中（长词优先）。"""

    prefs = prefs if prefs is not None else load_prefs(path)
    best_keyword = ""
    best_ref: str | None = None
    for keyword, ref in prefs.get("task_prefs", {}).items():
        if keyword and keyword in task and len(keyword) > len(best_keyword):
            best_keyword, best_ref = keyword, ref
    return best_ref
