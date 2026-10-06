"""两阶段审批的任务状态持久化：data/tasks/task-<id>.json。

prepare 写盘（awaiting_confirmation / ready）-> execute 校验
（approved / denied / expired）-> running -> completed / failed。
- 原子写：临时文件 + os.replace，避免半截文件。
- 加载容错：文件不存在 / 损坏 / 非法 JSON 返回 None。
- 终态后保留最小审计记录：task 截断为摘要，不保留任务全文与任何凭据。
- 记录不保存 API key（任务文本脱敏由 pipeline 负责）。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_TASKS_DIR = Path("data/tasks")

TASKS_VERSION = 1
TASK_TTL_SECONDS = 1800  # 默认 30 分钟有效期

# 状态机（一次迁移，无回退）
AWAITING_CONFIRMATION = "awaiting_confirmation"
READY = "ready"                        # never / once命中偏好 -> 自动批准
APPROVED = "approved"                  # execute 确认通过（短暂态）
DENIED = "denied"                      # 拒绝：任务不执行
EXPIRED = "expired"                    # 超过 expires_at
RUNNING = "running"                    # 执行中（并发/重复执行保护）
COMPLETED = "completed"
FAILED = "failed"

FINISHED = frozenset({DENIED, EXPIRED, COMPLETED, FAILED})

_TASK_ID_RE = re.compile(r"^task-[0-9a-f]{12}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    """生成 task-<12位hex>（少量碰撞可忽略；任务文件追加式）。"""
    return f"task-{uuid.uuid4().hex[:12]}"


def task_path(task_id: str, tasks_dir: Path | str = DEFAULT_TASKS_DIR) -> Path | None:
    """返回任务文件路径；task_id 非法（防路径穿越）返回 None。"""
    if not _TASK_ID_RE.match(task_id):
        return None
    return Path(tasks_dir) / f"{task_id}.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写：临时文件 + os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_task(
    record: dict[str, Any], tasks_dir: Path | str = DEFAULT_TASKS_DIR
) -> Path | None:
    """写入一条任务记录（原子写）；task_id 非法返回 None。"""
    task_id = str(record.get("task_id", ""))
    path = task_path(task_id, tasks_dir)
    if path is None:
        return None
    _atomic_write_json(path, record)
    return path


def load_task(task_id: str, tasks_dir: Path | str = DEFAULT_TASKS_DIR) -> dict[str, Any] | None:
    """读取任务记录；不存在 / 损坏 / 非法 task_id 返回 None。"""
    path = task_path(task_id, tasks_dir)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("version", TASKS_VERSION)
    return data


def update_task(
    task_id: str,
    fields: dict[str, Any],
    tasks_dir: Path | str = DEFAULT_TASKS_DIR,
) -> dict[str, Any] | None:
    """读改写（保留未覆盖字段）；任务不存在返回 None。"""
    record = load_task(task_id, tasks_dir)
    if record is None:
        return None
    record.update(fields)
    save_task(record, tasks_dir)
    return record


def is_finished(record: dict[str, Any] | None) -> bool:
    """是否为终态（denied / expired / completed / failed 不可再执行）。"""
    return record is not None and record.get("status") in FINISHED


def minimal_audit(record: dict[str, Any], *, outcome: str = "") -> dict[str, Any]:
    """终态最小审计记录：保留标识与结果，任务文本截断为摘要（约100字）。

    不保留任务全文、API key 与任何认证凭据。
    """
    return {
        "version": TASKS_VERSION,
        "task_id": record.get("task_id"),
        "status": record.get("status"),
        "task_type": record.get("task_type", "general"),
        "selected_model": record.get("selected_model", ""),
        "reason": record.get("reason", ""),
        "task": str(record.get("task", ""))[:100],
        "outcome": outcome or record.get("outcome", ""),
        "created_at": record.get("created_at", ""),
        "expires_at": record.get("expires_at", ""),
        "completed_at": _now_iso(),
    }


def prune_expired(
    tasks_dir: Path | str = DEFAULT_TASKS_DIR, *, now: datetime | None = None
) -> list[str]:
    """批量清理过期的非终态任务（标为 expired + 最小审计）。

    返回被清理的 task_id 列表；逐文件容错（单文件损坏不阻断）。
    """
    now = now or datetime.now(timezone.utc)
    cleaned: list[str] = []
    directory = Path(tasks_dir)
    if not directory.exists():
        return cleaned
    for path in directory.glob("task-*.json"):
        task_id = path.stem
        record = load_task(task_id, directory)
        if record is None or is_finished(record):
            continue
        try:
            expires_at = datetime.fromisoformat(str(record["expires_at"]))
        except (KeyError, ValueError, TypeError):
            continue
        if expires_at < now:
            record.update({"status": EXPIRED})
            save_task(minimal_audit(record, outcome="expired"), directory)
            cleaned.append(task_id)
    return cleaned


def expires_at_from(created_at: str, ttl: int = TASK_TTL_SECONDS) -> str:
    """由创建时间推有效期终点（缺省 30 分钟）。"""
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        created = datetime.now(timezone.utc)
    return (created + timedelta(seconds=ttl)).isoformat(timespec="seconds")