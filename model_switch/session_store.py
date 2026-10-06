"""会话偏好持久化：data/sessions/<session_id>.json。

阶段4：会话模型偏好（preferred_model 软偏好 / strict_model 显式硬限制）。
- 原子写（临时文件 + os.replace），避免半截文件。
- 加载容错：缺失/损坏/非法 session_id 返回空偏好（对齐 pause/probe 容错风格）。
- session_id 白名单字符（[A-Za-z0-9_-]），防路径穿越。
- CLI 独立模式使用本存储；宿主可自行管理会话偏好（核心 API 接受显式传入）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SESSIONS_DIR = Path("data/sessions")

SESSIONS_VERSION = 1

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_path(session_id: str, sessions_dir: Path | str) -> Path | None:
    """返回会话文件路径；session_id 非法（防路径穿越/超长）返回 None。"""
    if not _SESSION_ID_RE.match(session_id):
        return None
    return Path(sessions_dir) / f"{session_id}.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_session(
    session_id: str,
    *,
    preferred_model: str | None,
    strict_model: bool = False,
    sessions_dir: Path | str = DEFAULT_SESSIONS_DIR,
) -> Path | None:
    """写入（或更新）一条会话偏好，返回文件路径；非法 session_id 返回 None。"""
    path = _session_path(session_id, sessions_dir)
    if path is None:
        return None
    record = {
        "version": SESSIONS_VERSION,
        "session_id": session_id,
        "preferred_model": preferred_model,
        "strict_model": bool(strict_model),
        "updated_at": _now_iso(),
    }
    _atomic_write_json(path, record)
    return path


def load_session(
    session_id: str, sessions_dir: Path | str = DEFAULT_SESSIONS_DIR
) -> dict[str, Any]:
    """读取会话偏好；缺失/损坏/非法 session_id 返回空结构（不报错）。"""
    path = _session_path(session_id, sessions_dir)
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def clear_session(
    session_id: str, sessions_dir: Path | str = DEFAULT_SESSIONS_DIR
) -> bool:
    """清除会话偏好（文件不存在视为已清除）；返回是否确实删除。"""
    path = _session_path(session_id, sessions_dir)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def list_sessions(sessions_dir: Path | str = DEFAULT_SESSIONS_DIR) -> list[str]:
    """列出全部已保存的会话偏好（按 session_id 排序）。"""
    directory = Path(sessions_dir)
    if not directory.exists():
        return []
    ids: list[str] = []
    for path in directory.glob("*.json"):
        if path.stem == "" or not _SESSION_ID_RE.match(path.stem):
            continue
        ids.append(path.stem)
    return sorted(ids)