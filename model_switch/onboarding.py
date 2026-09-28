"""Onboarding（mso init-check）：连通 -> 跑分 -> 画像 -> 确认 -> 标签落盘。

- 连通：逐模型测试（全量或 --per-group 每分组代表），事件 onboarding_connectivity
- 跑分：按分组代表模型联网搜索（web_search 保底；失败不阻断，降级无跑分）
- 画像：决策模型生成"每个模型适合什么任务"标签（仅 LLM 上下文，不落盘）
- 确认：终端问答循环（满意 / 提修改意见重新生成）；--json 模式跳过交互，
  返回 needs_confirmation（宿主可从事件流/返回值取画像；远程确认无专门 CLI/MCP 入口，
  宿主注入 confirm_handler 或取 profile 后手改 model_prefs.md——留待阶段6）
- 落盘：标签写 config/model_prefs.md（保留已有任务偏好区），事件 onboarding_labels_saved
"""
from __future__ import annotations

from typing import Any, Callable

from .events import EventType
from .manager import DecisionManager, DecisionError
from .model import ModelRegistry, ModelStatus
from .prefs import DEFAULT_PREFS_PATH, load_prefs, save_prefs
from .safety import redact

ConfirmHandler = Callable[[str], tuple[bool, str]]  # (profile_text) -> (granted, user_note)

PROFILE_PROMPT = """\
你是模型画像生成者。为下列每个模型生成简短标签：它适合什么任务（多个标签用顿号分隔）。

模型清单与状态：
{table}

分组跑分摘要（联网搜索所得，可能缺失或不全）：
{benchmarks}

只输出一个 JSON 对象（不要 markdown 代码块、不要任何其他文字）：
{{"labels": {{"<模型完整引用，照抄清单写法>": "<标签1、标签2>", ...}}}}
必须覆盖清单中的每一个模型。"""

_OK_ANSWERS = ("y", "yes", "ok", "是", "满意", "好", "可以")


def run_onboarding(
    toolbox: Any,
    *,
    per_group: bool = False,
    confirm_handler: ConfirmHandler | None = None,
    json_mode: bool = False,
    prefs_path: Any = DEFAULT_PREFS_PATH,
    max_rounds: int = 5,
    manager: DecisionManager | None = None,
) -> dict[str, Any]:
    """执行完整 onboarding；返回 {"ok", "status", ...}。"""

    manager = manager or DecisionManager(toolbox, prefs_path=prefs_path)

    # 1. 连通测试（全量或每分组代表）
    targets = _group_leaders(toolbox.registry) if per_group else list(toolbox.registry.all())
    connectivity: dict[str, bool] = {}
    for status in targets:
        ref = str(status.ref)
        result = toolbox.test_connectivity(ref)[ref]
        connectivity[ref] = bool(result["ok"])
        _emit(toolbox, EventType.ONBOARDING_CONNECTIVITY, {"model_ref": ref, "ok": result["ok"]})
    ok_count = sum(1 for value in connectivity.values() if value)
    summary = {"total": len(targets), "ok": ok_count}

    # 2. 跑分搜索（每分组代表模型；失败不阻断）
    benchmarks: list[str] = []
    for status in _group_leaders(toolbox.registry):
        model = status.ref.model
        response = toolbox.web_search(f"{model} 大模型 跑分 评测")
        if response.get("ok"):
            digest = "; ".join(
                f"{item.get('title', '')} - {str(item.get('snippet', ''))[:120]}"
                for item in response.get("results", [])[:3]
            )
            if digest:
                benchmarks.append(f"- {status.ref.provider}/{status.ref.group}（代表 {model}）: {digest}")
        else:
            benchmarks.append(
                f"- {status.ref.provider}/{status.ref.group}（代表 {model}）: 跑分搜索不可用（{response.get('detail', '')}）"
            )

    # 3. 画像（决策模型生成标签）
    labels = _decide_profile(toolbox, manager, benchmarks)
    _emit(toolbox, EventType.ONBOARDING_PROFILE, {"profile": labels})

    # --json：不交互，画像随事件流输出，标签不落盘（远程确认留待阶段6/宿主）
    if json_mode:
        return {
            "ok": False,
            "status": "needs_confirmation",
            "profile": labels,
            "connectivity": summary,
            "detail": "--json 模式跳过终端确认；画像已随事件流输出，标签未落盘",
        }

    # 4. 确认循环（终端问答 / 注入的 handler）
    handler = confirm_handler or _terminal_confirm
    note = ""
    confirmed = False
    for _ in range(max_rounds):
        confirmed, note = handler(_format_profile(labels))
        if confirmed:
            _emit(
                toolbox,
                EventType.ONBOARDING_PROFILE_CONFIRMED,
                {"profile": labels, "user_note": note},
            )
            break
        labels = _decide_profile(toolbox, manager, benchmarks, previous=labels, note=note)
        _emit(toolbox, EventType.ONBOARDING_PROFILE, {"profile": labels, "user_note": note})
    if not confirmed:
        return {
            "ok": False,
            "status": "profile_not_confirmed",
            "profile": labels,
            "connectivity": summary,
            "detail": f"{max_rounds} 轮确认未通过，标签未保存；可重新运行 init-check",
        }

    # 5. 标签落盘（保留已有任务偏好区）
    known = {str(status.ref) for status in toolbox.registry.all()}
    saved = {ref: label for ref, label in labels.items() if ref in known}
    prefs = load_prefs(prefs_path)
    prefs["model_labels"].update(saved)
    save_prefs(prefs, prefs_path)
    for ref, label in saved.items():
        toolbox.registry.update(ref, labels=tuple(part.strip() for part in label.split("、") if part.strip()))
    _emit(
        toolbox,
        EventType.ONBOARDING_LABELS_SAVED,
        {"prefs_ref": str(prefs_path), "count": len(saved)},
    )
    return {
        "ok": True,
        "status": "ready",
        "labels": saved,
        "prefs_path": str(prefs_path),
        "connectivity": summary,
    }


def _decide_profile(
    toolbox: Any,
    manager: DecisionManager,
    benchmarks: list[str],
    *,
    previous: dict[str, str] | None = None,
    note: str = "",
) -> dict[str, str]:
    """让决策模型生成/修改画像标签；返回 {模型引用: 标签}。"""

    prefs = load_prefs(manager.prefs_path)
    table = manager._model_table(prefs)
    prompt = PROFILE_PROMPT.format(
        table=table,
        benchmarks="\n".join(benchmarks) or "（无跑分数据）",
    )
    if previous and note:
        prompt += (
            f"\n\n用户对上一版画像的意见：{note}\n"
            f"上一版画像：{previous}\n请据此修改标签。"
        )

    def validator(data: dict[str, Any]) -> str:
        labels = data.get("labels")
        if not isinstance(labels, dict) or not labels:
            return "labels 必须是非空映射（模型引用 -> 标签）"
        known = {str(status.ref) for status in toolbox.registry.all()}
        hit = [ref for ref in labels if ref in known]
        if not hit:
            return "labels 的键必须是清单内的模型完整引用"
        return ""

    try:
        decision = manager.decide("onboarding_profile", prompt, validator=validator)
    except DecisionError as exc:
        raise DecisionError(f"画像生成失败：{exc}") from exc
    return {str(ref): str(label) for ref, label in decision.decision["labels"].items()}


def _group_leaders(registry: ModelRegistry) -> list[ModelStatus]:
    """每个分组的首个模型（配置顺序）。"""

    seen: set[tuple[str, str]] = set()
    leaders: list[ModelStatus] = []
    for status in registry.all():
        key = (status.ref.provider, status.ref.group)
        if key not in seen:
            seen.add(key)
            leaders.append(status)
    return leaders


def _format_profile(labels: dict[str, str]) -> str:
    lines = ["模型画像（决策模型生成）："]
    for ref, label in sorted(labels.items()):
        lines.append(f"  {ref}  ->  {label}")
    return "\n".join(lines)


def _terminal_confirm(profile_text: str) -> tuple[bool, str]:
    """终端问答：y 保存；其他文字作为修改意见重新生成。"""

    print(profile_text)
    try:
        answer = input("对以上模型画像满意吗？（y=保存标签；输入修改意见=重新生成）: ").strip()
    except EOFError:
        return False, ""
    if answer.lower() in _OK_ANSWERS:
        return True, "y"
    return False, answer


def _emit(toolbox: Any, type_: str, data: dict[str, Any]) -> None:
    if toolbox.stream is not None:
        toolbox.stream.emit(type_, redact(data))
