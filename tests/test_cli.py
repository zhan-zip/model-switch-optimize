"""CLI 测试：init 生成、覆盖保护、validate 合法/非法、JSON 输出、占位命令。"""
import json

import pytest

from model_switch.cli import main


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_creates_files(workspace):
    assert main(["init"]) == 0
    assert (workspace / "config" / "models.yaml").exists()
    for d in ("data/auth", "data/faults", "data/events"):
        assert (workspace / d).is_dir()


def test_init_refuses_overwrite_then_force(workspace):
    assert main(["init"]) == 0
    assert main(["init"]) == 1
    assert main(["init", "--force"]) == 0


def test_validate_ok_after_init(workspace, capsys):
    main(["init"])
    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "配置合法" in out
    assert "保底模型" in out


def test_validate_json_ok(workspace, capsys):
    main(["init"])
    capsys.readouterr()  # 清空 init 的输出
    assert main(["validate", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["summary"]["providers"] >= 1
    assert payload["models"]
    assert payload["errors"] == []


def test_validate_reports_errors(workspace, capsys):
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "models.yaml").write_text(
        "providers:\n"
        "  - name: a\n"
        "    base_url: https://a.example.com/v1\n"
        "    groups:\n"
        "      - { name: g, key_env: KEY_A, models: [m1] }\n"
        "fallback: { provider: a, group: g, model: missing, key_env: KEY_A }\n",
        encoding="utf-8",
    )
    assert main(["validate"]) == 1
    out = capsys.readouterr().out
    assert "fallback.model" in out


def test_validate_json_errors(workspace, capsys):
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "models.yaml").write_text("providers: []\n", encoding="utf-8")
    assert main(["validate", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["errors"]


def test_validate_missing_config(workspace, capsys):
    assert main(["validate"]) == 1
    assert "配置文件不存在" in capsys.readouterr().out


def test_pending_command_exit_code(workspace, capsys):
    assert main(["tools"]) == 2
    assert "后续" in capsys.readouterr().out
