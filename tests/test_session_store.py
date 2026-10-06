"""session_store 模块测试：会话偏好持久化（原子写/容错/路径穿越防御/清除）。"""
from model_switch.session_store import (
    DEFAULT_SESSIONS_DIR,
    clear_session,
    list_sessions,
    load_session,
    save_session,
)


def test_save_and_load_roundtrip(tmp_path):
    path = save_session(
        "user-abc", preferred_model="a/g1/m1", strict_model=True, sessions_dir=tmp_path
    )
    assert path is not None and path.name == "user-abc.json"
    loaded = load_session("user-abc", tmp_path)
    assert loaded["session_id"] == "user-abc"
    assert loaded["preferred_model"] == "a/g1/m1"
    assert loaded["strict_model"] is True
    assert loaded["updated_at"]


def test_save_without_strict_defaults_false(tmp_path):
    save_session("s1", preferred_model="a/g1/m2", sessions_dir=tmp_path)
    loaded = load_session("s1", tmp_path)
    assert loaded["strict_model"] is False


def test_load_missing_returns_empty(tmp_path):
    assert load_session("nobody", tmp_path) == {}


def test_load_corrupted_returns_empty(tmp_path):
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
    assert load_session("bad", tmp_path) == {}


def test_invalid_session_id_returns_none(tmp_path):
    assert save_session("../evil", preferred_model="x", sessions_dir=tmp_path) is None
    assert save_session("a/b", preferred_model="x", sessions_dir=tmp_path) is None
    assert save_session("", preferred_model="x", sessions_dir=tmp_path) is None
    assert save_session("x" * 65, preferred_model="x", sessions_dir=tmp_path) is None
    assert load_session("../../etc", tmp_path) == {}
    assert clear_session("../evil", tmp_path) is False


def test_clear_removes_file(tmp_path):
    save_session("s1", preferred_model="a/g1/m1", sessions_dir=tmp_path)
    assert clear_session("s1", tmp_path) is True
    assert load_session("s1", tmp_path) == {}
    assert clear_session("s1", tmp_path) is False  # 幂等：再清返回 False


def test_atomic_no_tmp_leftover(tmp_path):
    save_session("s1", preferred_model="a/g1/m1", sessions_dir=tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []


def test_list_sessions_sorted(tmp_path):
    assert list_sessions(tmp_path) == []
    save_session("zeta", preferred_model="a/g1/m1", sessions_dir=tmp_path)
    save_session("alpha", preferred_model="b/g1/m3", sessions_dir=tmp_path)
    assert list_sessions(tmp_path) == ["alpha", "zeta"]
    # 非法 session_id 文件名（如路径穿越片段）跳过
    (tmp_path / "..bad..json.json").write_text("{}", encoding="utf-8")
    assert list_sessions(tmp_path) == ["alpha", "zeta"]


def test_update_overwrites(tmp_path):
    save_session("s1", preferred_model="a/g1/m1", sessions_dir=tmp_path)
    save_session("s1", preferred_model="a/g1/m2", sessions_dir=tmp_path)
    assert load_session("s1", tmp_path)["preferred_model"] == "a/g1/m2"


def test_default_sessions_dir_is_data_sessions():
    assert DEFAULT_SESSIONS_DIR.parts[-2:] == ("data", "sessions")