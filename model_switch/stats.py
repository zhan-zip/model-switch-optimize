"""run 结果统计：data/stats.json 按模型聚合（滚动窗口经验分）。

阶段2（v2）：记录从数组升级为对象，包含失败类别、Token、时间、调用用途与协议。
- record_run：每次模型调用后记录该次结果（ok / latency / switches / error_class /
  tokens / call_kind / protocol / task_type / ts）
- summary：聚合近 window 次成功率/平均耗时；样本 < min_samples 返回 None（不显示）；
  同时兼容旧数组记录（v1）与新对象记录（v2）
- choose_model 注入选型 prompt（与标签并列的"经验"列）
- 只存 recent 滚动数组（total/ok/耗时由 recent 推导，避免双写不一致）
- 失败调用也记录（成功率才有效；故障明细另见 data/faults/）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATS_PATH = Path("data/stats.json")

STATS_VERSION = 2       # 阶段2 新增：stats 文件版本
WINDOW = 20             # 滚动窗口：每模型最多保留最近 20 次
MIN_SAMPLES = 3         # 样本不足不显示

_CALL_KINDS = ("task", "probe", "decision", "benchmark", "diagnosis")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry_recent(entry: Any) -> list[Any] | None:
    """从 entry（v1 数组或 v1.5/v2 对象）提取 recent 列表；非法返回 None。"""
    if isinstance(entry, list):
        return [item for item in entry if isinstance(item, (list, tuple, dict))]
    if isinstance(entry, dict) and isinstance(entry.get("recent"), list):
        return [item for item in entry["recent"] if isinstance(item, (list, tuple, dict))]
    return None


def load_stats(path: Path | str = DEFAULT_STATS_PATH) -> dict[str, dict[str, Any]]:
    """读统计表 {model_ref: {"recent": [...]}}；不存在/损坏返回空。

    兼容三种磁盘格式：
    - v1：{ref: [[ok, latency, switches], ...]}
    - v1.5：{ref: {"recent": [[ok, latency, switches], ...]}}
    - v2（阶段2）：{"version": 2, "models": {ref: {"recent": [对象记录]}}}
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

    if data.get("version") == STATS_VERSION and isinstance(data.get("models"), dict):
        # v2 磁盘格式：{"version": 2, "models": {...}}
        for ref, entry in data["models"].items():
            recent = _entry_recent(entry)
            if recent is not None:
                stats[str(ref)] = {"recent": recent}
    else:
        # v1 / v1.5 旧格式：{ref: entry}
        for ref, entry in data.items():
            recent = _entry_recent(entry)
            if recent is not None:
                stats[str(ref)] = {"recent": recent}
    return stats


def save_stats(stats: dict[str, dict[str, Any]], path: Path | str = DEFAULT_STATS_PATH) -> Path:
    """写盘并统一升级为 v2 顶层格式；非法条目忽略；旧数组记录就地转对象记录。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    models: dict[str, Any] = {}
    for ref, entry in stats.items():
        if not isinstance(entry, dict):
            continue
        recent = entry.get("recent")
        if not isinstance(recent, list):
            continue
        converted: list[Any] = []
        for item in recent:
            if isinstance(item, dict):
                converted.append(item)
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                # v1 数组 [ok, latency, switches] -> v2 对象记录（缺字段 None）
                converted.append(
                    {
                        "ok": bool(item[0]),
                        "latency_ms": int(item[1]),
                        "switches": int(item[2]),
                        "error_class": None,
                        "input_tokens": None,
                        "output_tokens": None,
                        "total_tokens": None,
                        "call_kind": "task",
                        "protocol": "openai_chat",
                        "task_type": "general",
                        "ts": "",
                    }
                )
        if converted or recent:
            models[str(ref)] = {"recent": converted}
    doc = {"version": STATS_VERSION, "models": models}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _new_record(
    *,
    ok: bool,
    latency_ms: int,
    switches: int,
    error_class: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    call_kind: str,
    protocol: str,
    task_type: str,
    ts: str,
) -> dict[str, Any]:
    """构造一条 v2 对象记录（缺省 Token/错误存 None，不误写 0 或空串）。"""
    return {
        "ok": bool(ok),
        "latency_ms": int(latency_ms),
        "switches": int(switches),
        "error_class": error_class,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "call_kind": call_kind,
        "protocol": protocol,
        "task_type": task_type,
        "ts": ts,
    }


def record_run(
    model_ref: str,
    ok: bool,
    latency_ms: int,
    switches: int,
    path: Path | str = DEFAULT_STATS_PATH,
    *,
    call_kind: str = "task",
    task_type: str = "general",
    protocol: str = "openai_chat",
    error_class: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    ts: str | None = None,
) -> None:
    """记录一次模型调用结果（滚动窗口裁剪）；写盘失败不阻断主流程由调用方兜底。

    保持前 5 个位置参数不变（旧调用兼容），新增字段走可选关键字参数。
    """

    if call_kind not in _CALL_KINDS:
        call_kind = "task"
    stats = load_stats(path)
    recent = stats.setdefault(model_ref, {"recent": []})["recent"]
    recent.append(
        _new_record(
            ok=ok,
            latency_ms=latency_ms,
            switches=switches,
            error_class=error_class,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            call_kind=call_kind,
            protocol=protocol,
            task_type=task_type,
            ts=ts or _now_iso(),
        )
    )
    del recent[:-WINDOW]
    save_stats(stats, path)


# -- 记录读取辅助（兼容 v1 数组与 v2 对象） -----------------------------


def _ok_of(record: Any) -> bool:
    return bool(record[0]) if isinstance(record, (list, tuple)) else bool(record["ok"])


def _latency_of(record: Any) -> int:
    return int(record[1]) if isinstance(record, (list, tuple)) else int(record.get("latency_ms", 0) or 0)


def _switches_of(record: Any) -> int:
    return int(record[2]) if isinstance(record, (list, tuple)) else int(record.get("switches", 0) or 0)


def _tok_of(record: Any, key: str) -> int | None:
    if isinstance(record, dict):
        value = record.get(key)
        return int(value) if value is not None else None
    return None


def _recent_records(stats: dict[str, dict[str, Any]] | None, model_ref: str) -> list[Any]:
    if not stats:
        return []
    entry = stats.get(model_ref)
    if not entry:
        return []
    return list(entry.get("recent", []))


def summary(
    model_ref: str,
    stats: dict[str, dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_STATS_PATH,
    *,
    min_samples: int = MIN_SAMPLES,
    window: int = WINDOW,
) -> dict[str, int] | None:
    """聚合经验分：{n, ok, rate(%), avg_latency_ms}；样本不足返回 None（不显示）。

    兼容 v1 数组记录与 v2 对象记录。
    """

    if stats is None:
        stats = load_stats(path)
    recent = _recent_records(stats, model_ref)[-window:]
    if len(recent) < min_samples:
        return None
    n = len(recent)
    ok_n = sum(1 for item in recent if _ok_of(item))
    latencies = [_latency_of(item) for item in recent if _ok_of(item)]
    if not latencies:
        latencies = [_latency_of(item) for item in recent]  # 无成功样本时用全部耗时
    avg = sum(latencies) // len(latencies) if latencies else 0
    return {"n": n, "ok": ok_n, "rate": round(ok_n * 100 / n), "avg_latency_ms": avg}


def all_summaries(
    model_refs: list[str],
    stats: dict[str, dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_STATS_PATH,
) -> dict[str, dict[str, int] | None]:
    """批量经验分（mso stats 展示用）：{model_ref: summary 或 None}。"""

    if stats is None:
        stats = load_stats(path)
    return {ref: summary(ref, stats=stats) for ref in model_refs}


# -- 阶段2：健康摘要与健康分级 -------------------------------------------


def _p50_latency(latencies: list[int]) -> int | None:
    if not latencies:
        return None
    return sorted(latencies)[(len(latencies) - 1) // 2]


def _p95_latency(latencies: list[int]) -> int | None:
    if not latencies:
        return None
    ordered = sorted(latencies)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def health_summary(
    model_ref: str,
    stats: dict[str, dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_STATS_PATH,
    *,
    call_kind: str | None = "task",
    window: int = WINDOW,
    min_samples: int = MIN_SAMPLES,
) -> dict[str, Any] | None:
    """健康摘要聚合（阶段2）：成功率/连续成败/错误分布/延迟分位/Token/最近调用/最近探测。

    只统计指定 call_kind（默认 task），任务与探测不混算。
    样本不足返回 None。
    """

    if stats is None:
        stats = load_stats(path)
    recent = _recent_records(stats, model_ref)[-window:]
    if call_kind is None:
        records = recent
    else:
        records = [
            r for r in recent
            if (r["call_kind"] if isinstance(r, dict) else "task") == call_kind
        ]
    if len(records) < min_samples:
        return None
    n = len(records)
    ok_n = sum(1 for r in records if _ok_of(r))
    fail_n = n - ok_n

    consecutive_ok = 0
    consecutive_fail = 0
    for r in reversed(records):
        if _ok_of(r):
            if consecutive_fail:
                break
            consecutive_ok += 1
        else:
            if consecutive_ok:
                break
            consecutive_fail += 1

    error_distribution: dict[str, int] = {}
    for r in records:
        if _ok_of(r):
            continue
        cls = r.get("error_class") if isinstance(r, dict) else None
        key = str(cls) if cls else "unknown"
        error_distribution[key] = error_distribution.get(key, 0) + 1

    ok_latencies = [_latency_of(r) for r in records if _ok_of(r)]
    all_latencies = [_latency_of(r) for r in records]
    avg_latency_ms = sum(all_latencies) // len(all_latencies) if all_latencies else 0
    p50 = _p50_latency(ok_latencies) if ok_latencies else _p50_latency(all_latencies)
    p95 = _p95_latency(ok_latencies) if ok_latencies else _p95_latency(all_latencies)

    input_tokens = [_tok_of(r, "input_tokens") for r in records]
    output_tokens = [_tok_of(r, "output_tokens") for r in records]
    total_tokens = [_tok_of(r, "total_tokens") for r in records]
    token_missing = sum(1 for v in total_tokens if v is None)
    tokens = {
        "input_total": sum(v for v in input_tokens if v is not None),
        "output_total": sum(v for v in output_tokens if v is not None),
        "total_total": sum(v for v in total_tokens if v is not None),
        "avg_per_call": (sum(v for v in total_tokens if v is not None) // max(1, n - token_missing))
        if n - token_missing
        else 0,
        "missing_count": token_missing,
    }

    last_call_ts = ""
    last_ok_ts = ""
    for r in reversed(records):
        if isinstance(r, dict) and r.get("ts"):
            if not last_call_ts:
                last_call_ts = r["ts"]
            if _ok_of(r) and not last_ok_ts:
                last_ok_ts = r["ts"]
        if last_call_ts and last_ok_ts:
            break

    last_probe: Any = None
    probe_records = [
        r for r in _recent_records(stats, model_ref)[-window:]
        if isinstance(r, dict) and r.get("call_kind") == "probe"
    ]
    if probe_records:
        probe_records.sort(key=lambda r: r.get("ts") or "", reverse=True)
        p = probe_records[0]
        last_probe = {"ok": bool(p.get("ok")), "ts": p.get("ts", "")}

    return {
        "n": n,
        "ok_n": ok_n,
        "fail_n": fail_n,
        "rate": round(ok_n * 100 / n),
        "consecutive_ok": consecutive_ok,
        "consecutive_fail": consecutive_fail,
        "error_distribution": error_distribution,
        "avg_latency_ms": avg_latency_ms,
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "tokens": tokens,
        "last_call_ts": last_call_ts,
        "last_ok_ts": last_ok_ts,
        "last_probe": last_probe,
        "data_completeness": round((n - token_missing) * 100 / n) if n else 0,
        "call_kind": call_kind,
    }


HEALTH_UNKNOWN = "unknown"
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNHEALTHY = "unhealthy"


def health_status(
    model_ref: str,
    stats: dict[str, dict[str, Any]] | None = None,
    path: Path | str = DEFAULT_STATS_PATH,
    *,
    call_kind: str | None = "task",
    window: int = WINDOW,
    min_samples: int = MIN_SAMPLES,
) -> str:
    """健康分级（阶段2，按优化计划 10.5 节默认阈值，不自动暂停模型）。

    规则：
    - 样本 < min_samples -> unknown
    - 连续失败 >= 3 -> unhealthy
    - 最近错误 401/config 且尚未恢复 -> unhealthy
    - 近 window 次成功率 < 50% -> unhealthy
    - 成功率 < 80% -> degraded
    - 近 window 次 429 >= 2 -> degraded
    - 其余 -> healthy
    """

    if stats is None:
        stats = load_stats(path)
    summary_dict = health_summary(
        model_ref, stats=stats, call_kind=call_kind, window=window, min_samples=min_samples
    )
    if summary_dict is None:
        return HEALTH_UNKNOWN
    if summary_dict["consecutive_fail"] >= 3:
        return HEALTH_UNHEALTHY
    last_error = ""
    recent = [
        r for r in _recent_records(stats, model_ref)[-window:]
        if isinstance(r, dict) and (call_kind is None or r.get("call_kind") == call_kind)
    ]
    if recent:
        last_record = recent[-1]
        if isinstance(last_record, dict) and not last_record.get("ok"):
            last_error = str(last_record.get("error_class") or "")
    if last_error in ("401", "config"):
        return HEALTH_UNHEALTHY
    if summary_dict["rate"] < 50:
        return HEALTH_UNHEALTHY
    if summary_dict["rate"] < 80:
        return HEALTH_DEGRADED
    rate_limit_count = sum(
        1
        for r in recent
        if isinstance(r, dict) and r.get("error_class") == "429"
    )
    if rate_limit_count >= 2:
        return HEALTH_DEGRADED
    return HEALTH_HEALTHY