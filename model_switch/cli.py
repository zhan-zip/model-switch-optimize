"""CLI 入口：mso <command>。

已实现：init（生成配置模板）· validate（校验配置）· tools（模型清单与状态）
· init-check（初始化 onboarding：连通+跑分+画像+标签）
· run（完整闭环：选型->调用->故障切换->机械兜底，可选自动诊断）
· diagnose（手动诊断：四项检查->结论）· history（故障历史）。
其余命令（probe / auth）已注册占位，将在后续开发阶段实现。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_TEMPLATE,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    load_config,
    validate_config,
)
from .diagnose import run_diagnose
from .events import EventStream, EventType
from .manager import DecisionError
from .mocker import MockConsole, MockDecisionClient, MockModelClient, MockWebSearch
from .model import ModelRegistry
from .onboarding import run_onboarding
from .pipeline import OUTCOME_FALLBACK, OUTCOME_OK, OUTCOME_SWITCHED, Pipeline
from .tools import Toolbox
from .websearch import default_web_search

DATA_DIRS = ("data/auth", "data/faults", "data/events")

_PENDING_COMMANDS: dict[str, str] = {
    "probe": "手动探测恢复",
    "auth": "管理控制台账号",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mso",
        description="多模型降级自修复管理系统（model-switch-optimize）",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    p_init = sub.add_parser("init", help="生成配置模板（config/models.yaml）与 data 目录")
    p_init.add_argument("--force", action="store_true", help="配置已存在时覆盖")

    p_validate = sub.add_parser("validate", help="校验配置")
    p_validate.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    p_validate.add_argument("--json", action="store_true", help="输出 JSON 结果")

    p_tools = sub.add_parser("tools", help="模型清单与状态")
    p_tools.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    p_tools.add_argument("--check", action="store_true", help="对模型做连通测试（真实调用）")
    p_tools.add_argument("--model", default=None, help="只测指定模型（服务商/分组/模型名）")
    p_tools.add_argument("--mock", action="store_true", help="使用 mock（无真实 key 也能演示）")
    p_tools.add_argument("--json", action="store_true", help="输出 JSON 结果")

    p_ic = sub.add_parser("init-check", help="初始化 onboarding（连通+跑分+画像+标签）")
    p_ic.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    p_ic.add_argument("--per-group", action="store_true", help="连通测试只测每分组代表模型（默认全量）")
    p_ic.add_argument("--mock", action="store_true", help="使用 mock（无真实 key 也能演示）")
    p_ic.add_argument("--json", action="store_true", help="输出 JSON 事件流（跳过终端确认，标签不落盘）")

    p_run = sub.add_parser("run", help="完整闭环处理任务（选型->调用->故障切换->机械兜底）")
    p_run.add_argument("task", help="任务文本")
    p_run.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    p_run.add_argument("--mock", action="store_true", help="使用 mock（无真实 key 演示故障切换闭环）")
    p_run.add_argument("--json", action="store_true", help="输出 JSON 事件流")
    p_run.add_argument("--diagnose", action="store_true", help="故障后自动诊断相关服务商（四项检查->结论）")
    p_run.add_argument("--confirm", default="never", choices=["never", "once", "always"], help="选型人工确认策略")

    p_diag = sub.add_parser("diagnose", help="手动诊断服务商（四项检查->结论）")
    p_diag.add_argument("provider", help="服务商名称")
    p_diag.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    p_diag.add_argument("--mock", action="store_true", help="使用 mock（演示四项事实与结论）")
    p_diag.add_argument("--json", action="store_true", help="输出 JSON 事件流")

    p_hist = sub.add_parser("history", help="查看故障历史（data/faults/）")
    p_hist.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    p_hist.add_argument("--json", action="store_true", help="输出 JSON 结果")

    for name, help_text in _PENDING_COMMANDS.items():
        sub.add_parser(name, help=f"{help_text}（后续阶段实现）")

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(DEFAULT_CONFIG_PATH)
    if path.exists() and not args.force:
        print(f"已存在 {path}；如需覆盖请使用 --force")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    print(f"已生成配置模板：{path}")
    for d in DATA_DIRS:
        target = Path(d)
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").write_text("", encoding="utf-8")
    print("已创建数据目录：" + " · ".join(DATA_DIRS))
    print()
    print("下一步：")
    print("  1) 编辑 config/models.yaml：填入你的自定义模型与地址、保底模型")
    print("  2) 设置环境变量中的 key（配置里 key_env 只写变量名，不落明文）")
    print("  3) 运行 mso validate 校验配置")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print_validate_result(as_json=args.json, ok=False, errors=[f"加载失败：{exc}"])
        return 1

    errors = validate_config(config)
    if errors:
        _print_validate_result(as_json=args.json, ok=False, errors=errors)
        return 1

    groups = sum(len(p.groups) for p in config.providers)
    model_refs = [str(ref) for ref in config.iter_model_refs()]
    _print_validate_result(
        as_json=args.json,
        ok=True,
        errors=[],
        summary={
            "providers": len(config.providers),
            "groups": groups,
            "models": len(model_refs),
            "fallback": str(config.fallback_ref()),
        },
        model_refs=model_refs,
    )
    return 0


def _print_validate_result(
    *,
    as_json: bool,
    ok: bool,
    errors: list[str],
    summary: dict[str, Any] | None = None,
    model_refs: list[str] | None = None,
) -> None:
    if as_json:
        payload: dict[str, Any] = {"ok": ok, "errors": errors}
        if ok:
            payload["summary"] = summary
            payload["models"] = model_refs
        print(json.dumps(payload, ensure_ascii=False))
        return

    if not ok:
        print(f"配置校验未通过（{len(errors)} 项问题）：")
        for i, err in enumerate(errors, 1):
            print(f"  ✗ {i}. {err}")
        return

    assert summary is not None and model_refs is not None
    print("配置合法 ✓")
    print(f"  服务商 {summary['providers']} · 分组 {summary['groups']} · 模型 {summary['models']}")
    print(f"  保底模型：{summary['fallback']}")
    print("  机械兜底顺序（即配置顺序）：")
    for i, ref in enumerate(model_refs, 1):
        print(f"    {i}. {ref}")


def cmd_tools(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print_line(args.json, {"ok": False, "errors": [f"加载失败：{exc}"]})
        return 1

    registry = ModelRegistry(config)
    if args.mock:
        stream = EventStream()
        toolbox = Toolbox(
            config, registry, stream,
            model_client=MockModelClient(),
            console=MockConsole(),
            web_search_impl=MockWebSearch().search,
            mock=True,
        )
    else:
        stream = EventStream(persist_dir=Path("data/events")) if args.check else None
        toolbox = Toolbox(config, registry, stream)

    if args.check:
        results = toolbox.test_connectivity(args.model)
        all_ok = bool(results) and all(item["ok"] for item in results.values())
        payload = {
            "ok": all_ok,
            "checks": results,
            "trace_id": stream.trace_id if stream else None,
        }
        if not args.json:
            print("连通测试：")
            for ref, item in results.items():
                if item["ok"]:
                    print(f"  ✓ {ref}  {item['latency_ms']}ms  tokens={item['usage'].get('total_tokens')}")
                else:
                    print(f"  ✗ {ref}  [{item['error_class']}] {item['detail']}")
            if stream is not None:
                print(f"（事件流已落盘 data/events/{stream.trace_id}.jsonl）")
        else:
            print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0 if all_ok else 1

    models = toolbox.list_models()
    if args.json:
        print(json.dumps({"ok": True, "models": models}, ensure_ascii=False))
        return 0
    print(f"模型清单（{len(models)} 个）：")
    for i, m in enumerate(models, 1):
        state = {True: "可用", False: "不可用", None: "未测"}[m["available"]]
        key_state = "key 已设置" if "****" in str(m["key_status"]) else "key 未设置"
        print(f"  {i}. {m['model_ref']}  [{key_state}]  状态: {state}")
        if m["last_error_class"]:
            print(f"     └ 上次错误: [{m['last_error_class']}] {m['last_error_detail']}")
    return 0


def _print_line(as_json: bool, payload: dict[str, Any]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for err in payload.get("errors", []):
            print(err)


# -- run / diagnose / history（阶段4） -------------------------------------


def _pipeline_toolbox(args: argparse.Namespace, stream: EventStream) -> Toolbox:
    """run / diagnose 共用：mock 决策演示（选型->故障->切换）或真实工具箱。"""

    config = load_config(args.config)
    registry = ModelRegistry(config)
    if args.mock:
        first = registry.all()[0]
        fallback = str(config.fallback_ref())
        client = MockDecisionClient(
            default_model=str(first.ref),
            switch_to=fallback,
            fail_models={first.ref.model: "401"},
        )
        return Toolbox(
            config, registry, stream,
            model_client=client,
            console=MockConsole(),
            web_search_impl=MockWebSearch().search,
            mock=True,
        )
    return Toolbox(config, registry, stream, web_search_impl=default_web_search)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        stream = EventStream(persist_dir=Path("data/events"))
        toolbox = _pipeline_toolbox(args, stream)
    except ConfigError as exc:
        _print_line(args.json, {"ok": False, "errors": [f"加载失败：{exc}"]})
        return 1

    pipeline = Pipeline(toolbox, confirm_mode=args.confirm)
    result = pipeline.run(args.task)

    diagnosis: list[dict[str, Any]] = []
    if args.diagnose:
        fault_providers = sorted(
            {
                event.data["model_ref"].split("/")[0]
                for event in stream
                if event.type == EventType.FAULT_RECORDED
            }
        )
        for provider in fault_providers:
            try:
                diagnosis.append(run_diagnose(toolbox, provider))
            except DecisionError as exc:
                diagnosis.append(
                    {"ok": False, "status": "decision_failed", "provider": provider, "detail": str(exc)}
                )

    if args.json:
        print(stream.to_json())
        return 0 if result.ok else 1

    titles = {
        OUTCOME_OK: "一次成功",
        OUTCOME_SWITCHED: "故障切换后完成（任务不中断）",
        OUTCOME_FALLBACK: "机械兜底完成",
    }
    if result.ok:
        print(f"任务结果：✓ {titles[result.outcome]}")
        print(f"  最终模型：{result.model_ref}")
        print(f"  尝试 {result.attempts} 次 · 切换 {result.switches} 次")
        print("  最终回复：")
        print(f"    {result.text.strip()[:500]}")
    else:
        print(f"任务结果：✗ 全部模型不可用（最后错误 [{result.error_class}]）")
        print(f"  尝试 {result.attempts} 次 · 切换 {result.switches} 次 · 故障已入档 data/faults/")

    for diag in diagnosis:
        print(f"── 诊断（{diag.get('provider')}）──")
        if diag.get("ok"):
            print(f"  结论：{diag['conclusion']}")
            print(f"  建议：{' · '.join(diag['actions'])}")
        else:
            print(f"  未完成（{diag.get('status')}）：{diag.get('detail', '')}")

    print(f"（事件流已落盘 data/events/{stream.trace_id}.jsonl）")
    return 0 if result.ok else 1


def cmd_diagnose(args: argparse.Namespace) -> int:
    try:
        stream = EventStream(persist_dir=Path("data/events"))
        toolbox = _pipeline_toolbox(args, stream)
    except ConfigError as exc:
        _print_line(args.json, {"ok": False, "errors": [f"加载失败：{exc}"]})
        return 1

    try:
        result = run_diagnose(toolbox, args.provider)
    except DecisionError as exc:
        if args.json:
            print(stream.to_json())
        else:
            print(f"诊断失败（决策模型全部不可用）：{exc}")
        return 1

    if args.json:
        print(stream.to_json())
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print(f"诊断未完成（{result.get('status')}）：{result.get('detail', '')}")
        if result.get("status") == "console_not_configured":
            print("  真实诊断依赖外部 Playwright MCP 控制台实现；演示可加 --mock")
        return 1

    print(f"诊断（{args.provider}）：")
    for name, item in result["facts"]["facts"].items():
        mark = "✓" if item.get("ok") else "✗"
        print(f"  {mark} {name}: {item.get('evidence', '')}")
    print(f"  结论：{result['conclusion']}")
    print(f"  建议：{' · '.join(result['actions'])}")
    print(f"（事件流已落盘 data/events/{stream.trace_id}.jsonl）")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print_line(args.json, {"ok": False, "errors": [f"加载失败：{exc}"]})
        return 1

    toolbox = Toolbox(config, ModelRegistry(config), None)
    records = toolbox.fault_history()
    if args.json:
        print(json.dumps({"ok": True, "count": len(records), "faults": records}, ensure_ascii=False))
        return 0

    if not records:
        print("暂无故障历史（data/faults/ 为空）")
        return 0
    print(f"故障历史（{len(records)} 条）：")
    for record in records:
        print(
            f"  #{record.get('seq', '?')} [{record.get('error_class', '')}] "
            f"{record.get('model_ref', '')} · {record.get('ts', '')}"
        )
        if record.get("task"):
            print(f"     任务：{record['task']}")
        if record.get("detail"):
            print(f"     详情：{str(record['detail'])[:120]}")
    return 0


def cmd_init_check(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print_line(args.json, {"ok": False, "errors": [f"加载失败：{exc}"]})
        return 1

    registry = ModelRegistry(config)
    stream = EventStream(persist_dir=Path("data/events"))
    if args.mock:
        fallback_model = config.fallback_ref().model  # MockModelClient 按模型名匹配
        profile_json = json.dumps(
            {"labels": {str(status.ref): "mock标签、演示用" for status in registry.all()}},
            ensure_ascii=False,
        )
        toolbox = Toolbox(
            config, registry, stream,
            model_client=MockModelClient(rules={fallback_model: profile_json}),
            console=MockConsole(),
            web_search_impl=MockWebSearch().search,
            mock=True,
        )
    else:
        toolbox = Toolbox(config, registry, stream, web_search_impl=default_web_search)

    result = run_onboarding(toolbox, per_group=args.per_group, json_mode=args.json)

    if args.json:
        print(stream.to_json())
        return 0  # needs_confirmation 属正常输出（画像在事件流中，宿主确认属阶段5）

    connectivity = result.get("connectivity", {})
    print(f"连通测试：{connectivity.get('ok', 0)}/{connectivity.get('total', 0)} 通过"
          + ("（每分组代表）" if args.per_group else ""))
    if result.get("status") == "ready":
        labels = result.get("labels", {})
        print(f"标签已保存：{result.get('prefs_path')}（{len(labels)} 个模型）")
        print("模块就绪：后续 mso run 将按标签选型（阶段4/5 实现）")
        print(f"（事件流已落盘 data/events/{stream.trace_id}.jsonl）")
        return 0
    if result.get("status") == "needs_confirmation":
        print(result.get("detail", ""))
        return 0
    print(f"onboarding 未完成：{result.get('detail', result.get('status'))}")
    return 1


def cmd_pending(command: str) -> int:
    print(f"命令 `{command}` 已注册，将在后续开发阶段实现（见 README 开发阶段表）。")
    return 2


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 避免 UnicodeEncodeError。"""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "tools":
        return cmd_tools(args)
    if args.command == "init-check":
        return cmd_init_check(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "diagnose":
        return cmd_diagnose(args)
    if args.command == "history":
        return cmd_history(args)
    return cmd_pending(args.command)


if __name__ == "__main__":
    sys.exit(main())
