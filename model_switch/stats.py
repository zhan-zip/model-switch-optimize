"""run 结果统计：data/stats.json 按模型聚合（滚动窗口经验分）。

- record_run：每次模型调用后记录该次结果（ok / latency / 上下文切换数）
- summary：聚合近 window 次成功率/平均耗时；样本 < min_samples 返回 None（不显示，
  单机样本少时统计不可靠——这是设计约定，见交接文档十三.21-② 效果边界）
- choose_model 注入选型 prompt（与标签并列的"经验"列）
- 只存 recent 滚动数组（total/ok/耗时由 recent 推导，避免双写不一致）
- 失败调用也记录（成功率才有效；故障明细另见 data/faults/）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_STATS_PATH = Path("data/stats.json")

WINDOW = 20        # 滚动窗口：每模型最多保留最近 20 次
MIN_SAMPLES = 3    # 样本不足不显示


def load_stats(path: Path | str = DEFAULT_STATS_PATH) -> dict[str, dict[str, Any]]:
    """读统计表 {model_ref: {"recent": [[ok, latency, switches], ...]}}；不存在/损坏返回空。
    兼容旧格式：如果 entry 是数组而非对象，自动迁移为 {"recent": array}。
    """

    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    stats: dict[str, dict[str, Any]] = {}
    for ref, entry in data.items():
        # 兼容旧格式：entry 直接是数组 [[ok, latency, switches], ...]
        if isinstance(entry, list):
            stats[str(ref)] = {"recent": [list(item) for item in entry if isinstance(item, (list, tuple))]}
        # 新格式：entry 是对象 {"recent": [...]}
        elif isinstance(entry, dict) and isinstance(entry.get("recent"), list):
            stats[str(ref)] = {"recent": [list(item) for item in entry["recent"] if isinstance(item, (list, tuple))]}
        # 其他格式跳过
    return stats


def save_stats(stats: dict[str, dict[str, Any]], path: Path | str = DEFAULT_STATS_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def record_run(
    model_ref: str,
    ok: bool,
    latency_ms: int,
    switches: int,
    path: Path | str = DEFAULT_STATS_PATH,
) -> None:
    """记录一次模型调用结果（滚动窗口裁剪）；写盘失败不阻断主流程由调用方兜底。"""

    stats = load_stats(path)
    recent = stats.setdefault(model_ref, {"recent": []})["recent"]
    recent.append([bool(ok), int(latency_ms), int(switches)])
    del recent[:-WINDOW]
    save_stats(stats, path)


def summary(
    model_ref: str,
    stats: dict[str, dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_STATS_PATH,
    *,
    min_samples: int = MIN_SAMPLES,
    window: int = WINDOW,
) -> dict[str, int] | None:
    """聚合经验分：{n, ok, rate(%), avg_latency_ms}；样本不足返回 None（不显示）。"""

    stats = stats if stats is not None else load_stats(path)
    recent = stats.get(model_ref, {}).get("recent", [])[-window:]
    if len(recent) < min_samples:
        return None
    n = len(recent)
    ok_n = sum(1 for item in recent if item[0])
    latencies = [item[1] for item in recent if item[0]] or [item[1] for item in recent]
    avg = sum(latencies) // len(latencies) if latencies else 0
    return {"n": n, "ok": ok_n, "rate": round(ok_n * 100 / n), "avg_latency_ms": avg}


def all_summaries(
    model_refs: list[str],
    stats: dict[str, dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_STATS_PATH,
) -> dict[str, dict[str, int] | None]:
    """批量经验分（mso stats 展示用）：{model_ref: summary 或 None}。"""

    stats = stats if stats is not None else load_stats(path)
    return {ref: summary(ref, stats=stats) for ref in model_refs}
