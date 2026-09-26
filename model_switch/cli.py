"""CLI 入口：mso <command>。

阶段1 实现：init（生成配置模板）· validate（校验配置）。
其余命令（init-check / run / tools / diagnose / probe / history / auth）
已注册占位，将在后续开发阶段实现。
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

DATA_DIRS = ("data/auth", "data/faults", "data/events")

_PENDING_COMMANDS: dict[str, str] = {
    "init-check": "初始化 onboarding（连通+跑分+画像+标签）",
    "run": "完整闭环处理任务（默认人读 / --json 事件流）",
    "tools": "查看模型清单与状态",
    "diagnose": "手动触发诊断",
    "probe": "手动探测恢复",
    "history": "查看故障历史",
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
    return cmd_pending(args.command)


if __name__ == "__main__":
    sys.exit(main())
