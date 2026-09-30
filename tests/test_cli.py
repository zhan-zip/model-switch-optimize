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


def test_unknown_command_reports_error():
    with pytest.raises(SystemExit) as exc:
        main(["nope"])
    assert exc.value.code == 2


# -- tools 命令（阶段2） -----------------------------------------------


def test_tools_lists_models(workspace, capsys):
    main(["init"])
    assert main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "模型清单" in out
    assert "provider-a/default/gpt-4o" in out
    assert "未测" in out


def test_tools_json(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["tools", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["models"]) == 4


def test_tools_check_mock_all_ok(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["tools", "--check", "--mock", "--model", "provider-a/default/gpt-4o"]) == 0
    out = capsys.readouterr().out
    assert "✓ provider-a/default/gpt-4o" in out


def test_tools_check_mock_failure_exit_code(workspace, capsys):
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "models.yaml").write_text(
        "providers:\n"
        "  - name: a\n"
        "    base_url: https://a.example.com/v1\n"
        "    groups:\n"
        "      - { name: g, key_env: KEY_A, models: [m1] }\n"
        "fallback: { provider: a, group: g, model: m1, key_env: KEY_A }\n",
        encoding="utf-8",
    )
    assert main(["tools", "--check", "--mock", "--model", "a/g/m99"]) == 1
    out = capsys.readouterr().out
    assert "未知模型" in out


def test_tools_missing_config(workspace, capsys):
    assert main(["tools"]) == 1
    assert "加载失败" in capsys.readouterr().out


# -- init-check 命令（阶段3） ---------------------------------------------


def test_init_check_mock_json(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["init-check", "--mock", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert types.count("onboarding_connectivity") == 4  # init 模板 4 模型全量
    assert "onboarding_profile" in types
    assert "onboarding_labels_saved" not in types  # --json 跳过确认，不落盘


def test_init_check_mock_interactive(workspace, capsys, monkeypatch):
    main(["init"])
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["init-check", "--mock"]) == 0
    out = capsys.readouterr().out
    assert "连通测试：4/4" in out
    assert "标签已保存" in out
    assert "模块就绪" in out
    assert (workspace / "config" / "model_prefs.md").exists()


def test_init_check_mock_per_group(workspace, capsys, monkeypatch):
    main(["init"])
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(["init-check", "--mock", "--per-group"]) == 0
    out = capsys.readouterr().out
    assert "连通测试：2/2 通过（每分组代表）" in out


def test_init_check_missing_config(workspace, capsys):
    assert main(["init-check", "--mock"]) == 1
    assert "加载失败" in capsys.readouterr().out


# -- run / diagnose / history 命令（阶段4） ----------------------------------


def test_run_mock_switch_flow(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "写个爬虫", "--mock"]) == 0
    out = capsys.readouterr().out
    assert "故障切换后完成" in out  # mock：选型第一个 -> 401 -> 切保底 -> 成功
    assert "最终模型" in out
    assert "尝试 2 次" in out
    assert "事件流已落盘" in out


def test_run_mock_json_event_stream(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "写个爬虫", "--mock", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert "pipeline_started" in types
    assert "model_failed" in types
    assert "switch_triggered" in types
    assert "switched_to" in types
    assert "fault_recorded" in types
    assert types[-1] == "pipeline_finished"
    assert types[0] == "pipeline_started"


def test_run_mock_with_diagnose(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "写个爬虫", "--mock", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "诊断（provider-a）" in out
    assert "结论" in out
    assert "充值" in out  # mock 诊断建议动作


def test_run_missing_config(workspace, capsys):
    assert main(["run", "任务", "--mock"]) == 1
    assert "加载失败" in capsys.readouterr().out


def test_diagnose_mock(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["diagnose", "provider-a", "--mock"]) == 0
    out = capsys.readouterr().out
    assert "诊断（provider-a）" in out
    for name in ("reachable", "balance", "group", "connectivity"):
        assert name in out
    assert "结论" in out
    assert "建议" in out


def test_diagnose_real_without_console(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["diagnose", "provider-a"]) == 1  # 真实模式未注入控制台 -> 未完成
    out = capsys.readouterr().out
    assert "console_not_configured" in out
    assert "--mock" in out


def test_diagnose_missing_config(workspace, capsys):
    assert main(["diagnose", "x", "--mock"]) == 1
    assert "加载失败" in capsys.readouterr().out


def test_history_empty(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["history"]) == 0
    assert "暂无故障历史" in capsys.readouterr().out


def test_history_after_run_fault(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "写个爬虫", "--mock"]) == 0  # 产生一条故障记录
    capsys.readouterr()
    assert main(["history"]) == 0
    out = capsys.readouterr().out
    assert "故障历史（1 条）" in out
    assert "provider-a/default/gpt-4o" in out
    assert "[401]" in out
    assert "写个爬虫" in out


def test_history_json(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "任务", "--mock"]) == 0
    capsys.readouterr()
    assert main(["history", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["faults"][0]["error_class"] == "401"


# -- probe / auth 命令（阶段5） --------------------------------------------


def test_probe_empty_queue(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["probe"]) == 0
    assert "探测队列为空" in capsys.readouterr().out


def test_probe_mock_recovers(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["probe", "--mock"]) == 0
    out = capsys.readouterr().out
    assert "已入队演示故障" in out
    assert "✓ 已恢复" in out
    assert "回归可用池" in out


def test_probe_mock_json(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["probe", "--mock", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["probed"] == 1
    assert len(payload["recovered"]) == 1


def test_probe_missing_config(workspace, capsys):
    assert main(["probe", "--mock"]) == 1
    assert "加载失败" in capsys.readouterr().out


def test_run_mock_writes_no_real_probe_queue(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "写个爬虫", "--mock"]) == 0
    # mock 演示故障只入临时探测队列，不写真实 data/probe
    assert not (workspace / "data" / "probe" / "queue.json").exists()


def test_probe_mock_writes_no_real_probe_queue(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["probe", "--mock"]) == 0
    out = capsys.readouterr().out
    assert "✓ 已恢复" in out
    assert not (workspace / "data" / "probe" / "queue.json").exists()


def test_auth_add_masks_password(workspace, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "user@example.com")
    monkeypatch.setattr("model_switch.cli.getpass.getpass", lambda prompt: "secret-pw-123")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # 模拟交互终端
    assert main(["auth", "add", "provider-a"]) == 0
    out = capsys.readouterr().out
    assert "secret-pw-123" not in out  # 密码不回显
    path = workspace / "data" / "auth" / "provider-a.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["account"] == "user@example.com"
    assert payload["password"] == "secret-pw-123"  # 文件内存明文（data/auth 已 gitignore）


def test_auth_list_and_remove(workspace, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "user@example.com")
    monkeypatch.setattr("model_switch.cli.getpass.getpass", lambda prompt: "secret-pw-123")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # 模拟交互终端
    main(["auth", "add", "provider-a"])
    capsys.readouterr()
    assert main(["auth", "list"]) == 0
    out = capsys.readouterr().out
    assert "provider-a" in out
    assert "user@example.com" not in out  # 掩码显示
    assert "us****" in out
    assert main(["auth", "remove", "provider-a"]) == 0
    assert main(["auth", "remove", "provider-a"]) == 1


def test_auth_list_empty(workspace, capsys):
    assert main(["auth", "list"]) == 0
    assert "暂无账号" in capsys.readouterr().out


def test_auth_add_no_tty(workspace, capsys, monkeypatch):
    """测试无 tty 环境下 auth add 正确拒绝"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["auth", "add", "provider-a"]) == 1
    out = capsys.readouterr().out
    assert "需要交互式终端" in out or "无 tty 环境" in out


# -- pause / resume 命令（阶段6 增强项 ① PAUSE） ---------------------------


def test_pause_then_resume_persists(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    ref = "provider-a/default/gpt-4o"
    assert main(["pause", ref]) == 0  # 默认 300s
    assert "已暂停" in capsys.readouterr().out
    path = workspace / "data" / "pause.json"
    assert ref in json.loads(path.read_text(encoding="utf-8"))

    assert main(["resume", ref]) == 0
    assert ref not in json.loads(path.read_text(encoding="utf-8"))
    assert main(["resume", ref]) == 1  # 未在暂停中


def test_pause_seconds_bounds_and_unknown_model(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["pause", "provider-a/default/gpt-4o", "0"]) == 1  # 下界
    assert main(["pause", "provider-a/default/gpt-4o", "99999"]) == 1  # 上界
    assert main(["pause", "x/y/z"]) == 1  # 未知模型


def test_tools_shows_paused_state(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    main(["pause", "provider-a/default/gpt-4o", "300"])
    capsys.readouterr()
    assert main(["tools"]) == 0
    assert "暂停中（冷却）" in capsys.readouterr().out


def test_run_mock_writes_no_real_pause_state(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "写个爬虫", "--mock"]) == 0
    # mock 演示冷却只写临时暂停表，不写真实 data/pause.json
    assert not (workspace / "data" / "pause.json").exists()


def test_stats_empty_then_with_data(workspace, capsys):
    from model_switch.stats import record_run

    main(["init"])
    capsys.readouterr()
    assert main(["stats"]) == 0
    assert "暂无统计数据" in capsys.readouterr().out

    # 预置 3 次成功记录（真实 stats 路径，相对 workspace）
    for _ in range(3):
        record_run("provider-a/default/gpt-4o", True, 123, 0)
    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "provider-a/default/gpt-4o" in out
    assert "成功率 100%" in out
    assert "均耗时 123ms" in out


def test_run_mock_writes_no_real_stats(workspace, capsys):
    main(["init"])
    capsys.readouterr()
    assert main(["run", "写个爬虫", "--mock"]) == 0
    # mock 演示统计只写临时文件，不写真实 data/stats.json
    assert not (workspace / "data" / "stats.json").exists()
