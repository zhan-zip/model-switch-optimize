"""模型标签偏好：config/model_prefs.md 读写。

人可读、可手改的 markdown 表格；程序可解析回字典。
两个区块：
  ## 模型标签 —— 每个模型的标签（mso init-check 生成）
  ## 任务偏好 —— 任务关键词 -> 模型（choose_model 确认沉淀）
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_PREFS_PATH = Path("config/model_prefs.md")

SECTION_LABELS = "## 模型标签"
SECTION_TASKS = "## 任务偏好"

_HEADERS = {"模型", "任务关键词"}


def load_prefs(path: Path | str = DEFAULT_PREFS_PATH) -> dict[str, dict[str, str]]:
    """解析偏好文件；不存在返回空结构（不报错）。"""

    path = Path(path)
    prefs: dict[str, dict[str, str]] = {"model_labels": {}, "task_prefs": {}}
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
        prefs[section][key] = value
    return prefs


def save_prefs(prefs: dict[str, dict[str, str]], path: Path | str = DEFAULT_PREFS_PATH) -> Path:
    """写回 markdown（整文件重写，两区块全保留）。"""

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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def add_task_pref(task: str, model_ref: str, path: Path | str = DEFAULT_PREFS_PATH) -> None:
    """沉淀一条任务偏好（choose_model 确认后调用）。"""

    prefs = load_prefs(path)
    prefs["task_prefs"][task] = model_ref
    save_prefs(prefs, path)


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
