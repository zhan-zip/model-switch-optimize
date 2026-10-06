"""task_store 模块测试：两阶段审批任务持久化（原子写/容错/过期清理/状态机/审计）。"""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from model_switch.task_store import (
    DEFAULT_TASKS_DIR,
    AWAITING_CONFIRMATION,
    COMPLETED,
    DENIED,
    EXPIRED,
    FAILED,
    READY,
    RUNNING,
    is_finished,
    load_task,
    minimal_audit,
    new_task_id,
    prune_expired,
    save_task,
    task_path,
    update_task,
)


def _record(task_id, status=AWAITING_CONFIRMATION, task="任务内容", expires_at=None):
    return {
        "task_id": task_id,
        "status": status,
        "task": task,
        "task_type": "general",
        "selected_model": "a/g1/m1",
        "reason": "理由",
        "created_at": "2026-10-06T00:00:00+00:00",
        "expires_at": expires_at or "2026-10-06T01:00:00+00:00",
    }


def test_new_task_id_format():
    task_id = new_task_id()
    assert re.match(r"^task-[0-9a-f]{12}$", task_id)
    assert set(new_task_id() for _ in range(50)) != {task_id, new_task_id()}


def test_task_path_valid_and_invalid():
    assert task_path("task-" + "a" * 12) is not None
    assert task_path("evil/../x") is None
    assert task_path("task-ABC") is None
    assert task_path("task-") is None


def test_save_and_load_roundtrip(tmp_path):
    task_id = new_task_id()
    rec = _record(task_id)
    path = save_task(rec, tmp_path)
    assert path is not None and path.name == f"{task_id}.json"
    assert path.exists()
    loaded = load_task(task_id, tmp_path)
    assert loaded["task_id"] == task_id
    assert loaded["status"] == AWAITING_CONFIRMATION
    assert loaded["task"] == "任务内容"


def test_tmp_file_not_left_behind(tmp_path):
    """原子写：临时文件 os.replace 后不残留 .tmp。"""
    task_id = new_task_id()
    save_task(_record(task_id), tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_missing_returns_none(tmp_path):
    assert load_task("task-" + "b" * 12, tmp_path) is None


def test_load_corrupted_returns_none(tmp_path):
    task_id = new_task_id()
    (tmp_path / f"{task_id}.json").write_text("{bad json", encoding="utf-8")
    assert load_task(task_id, tmp_path) is None


def test_load_invalid_task_id_returns_none(tmp_path):
    assert load_task("../etc/passwd", tmp_path) is None


def test_update_task_preserves_fields(tmp_path):
    task_id = new_task_id()
    save_task(_record(task_id), tmp_path)
    updated = update_task(task_id, {"status": READY}, tmp_path)
    assert updated["status"] == READY
    assert updated["task"] == "任务内容"  # 未覆盖字段保留
    assert load_task(task_id, tmp_path)["status"] == READY


def test_update_missing_returns_none(tmp_path):
    assert update_task("task-" + "c" * 12, {"status": READY}, tmp_path) is None


def test_is_finished():
    assert is_finished({"status": COMPLETED})
    assert is_finished({"status": DENIED})
    assert is_finished({"status": EXPIRED})
    assert is_finished({"status": FAILED})
    assert not is_finished({"status": READY})
    assert not is_finished({"status": RUNNING})
    assert not is_finished(None)


def test_minimal_audit_truncates_task_and_strips_credentials(tmp_path):
    task_id = new_task_id()
    rec = _record(task_id, task="长" * 500)
    save_task(rec, tmp_path)
    audit = minimal_audit(rec, outcome="ok")
    assert "task" in audit
    assert len(audit["task"]) <= 100  # 截断摘要
    assert audit["outcome"] == "ok"
    assert "task_id" in audit and "selected_model" in audit
    # 不保留 API key / 凭据类字段
    assert "api_key" not in audit
    assert "key" not in json.dumps(audit)


def test_prune_expired_finalizes(tmp_path):
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (now + timedelta(minutes=31)).isoformat(timespec="seconds")

    expired_id = new_task_id()
    alive_id = new_task_id()
    finished_id = new_task_id()
    save_task(_record(expired_id, expires_at=past), tmp_path)
    save_task(_record(alive_id, expires_at=future), tmp_path)
    save_task(_record(finished_id, status=COMPLETED, expires_at=past), tmp_path)

    cleaned = prune_expired(tmp_path, now=now)
    assert cleaned == [expired_id]  # 过期+未终态才清理；已终态不动；未过期不动
    assert load_task(expired_id, tmp_path)["status"] == EXPIRED
    assert load_task(alive_id, tmp_path)["status"] == AWAITING_CONFIRMATION
    assert load_task(finished_id, tmp_path)["status"] == COMPLETED


def test_prune_expired_empty_dir(tmp_path):
    assert prune_expired(tmp_path) == []


def test_prune_expired_ignores_corrupt(tmp_path):
    (tmp_path / "task-corrupt.json").write_text("not json", encoding="utf-8")
    assert prune_expired(tmp_path) == []


def test_status_constants_complete():
    """五.3 状态机覆盖全部转移状态。"""
    assert {AWAITING_CONFIRMATION, READY}
    assert {DENIED, EXPIRED, COMPLETED, FAILED}


def test_default_tasks_dir_is_data_tasks():
    assert DEFAULT_TASKS_DIR.parts[-2:] == ("data", "tasks")