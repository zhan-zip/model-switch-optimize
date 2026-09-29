"""决策中枢：三决策点，决策由模型做。

程序职责：组装上下文 -> 调决策模型（保底优先，不可用按清单序降级换决策者）
-> 容错解析 JSON -> 校验防幻觉 -> 事件留痕。

三个决策点：
  choose_model        选型（任务 + 清单/状态/偏好/跑分 -> 选哪个模型）
  plan_recovery      故障切换决策（错误类型 + 可用模型 -> 切换目标 + 是否诊断）
  conclude_diagnosis  诊断结论（四项事实 + 内置规则库 -> 结论与建议，阶段4 供给事实）

选型确认策略：confirm_mode = always / once / never；
once：任务无匹配偏好时确认一次，确认后沉淀为任务偏好，下次免问。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable

from .events import EventType
from .prefs import (
    DEFAULT_PREFS_PATH,
    add_task_pref,
    load_prefs,
    match_plan_pref,
    match_task_pref,
)
from .safety import redact

JSON_RE = re.compile(r"\{.*\}", re.S)

ConfirmHandler = Callable[[str, str, str], bool]  # (operation, reason, evidence) -> granted

_STATE_TEXT = {True: "可用", False: "不可用", None: "未测"}

CHOOSE_PROMPT = """\
你是模型选型决策者。根据以下信息为任务选出最合适的一个模型。

任务：{task}

模型清单（完整引用 | 状态 | 标签 | 上次延迟）：
{table}
{extra}

只输出一个 JSON 对象（不要 markdown 代码块、不要任何其他文字）：
{{"model": "<清单中的模型完整引用，照抄清单写法>", "reason": "<一句话理由>"}}"""

PLAN_PROMPT = """\
你是故障切换决策者。一个模型调用失败，需要立刻选一个模型顶上（任务不中断），并判断是否需要后台诊断。

失败模型：{failed}
错误类别：{error_class}
错误详情：{detail}

其余模型清单（完整引用 | 状态 | 标签 | 上次延迟）：
{table}

只输出一个 JSON 对象（不要 markdown 代码块、不要任何其他文字）：
{{"switch_to": "<清单中的模型完整引用，照抄清单写法>", "diagnose": <true 或 false>, "reason": "<一句话理由>"}}"""

CONCLUDE_PROMPT = """\
你是诊断结论决策者。根据四项检查事实与内置规则库，给出故障原因结论与建议动作。

四项检查事实（网站可达 / 余额 / key 分组 / 模型连通）：
{facts}

内置规则库：
{rules}

只输出一个 JSON 对象（不要 markdown 代码块、不要任何其他文字）：
{{"conclusion": "<结论一句话>", "actions": ["<建议动作1>", "<建议动作2>"]}}"""


class DecisionError(Exception):
    """决策失败（决策模型全部不可用 / 输出无法解析或校验）。"""


@dataclass(frozen=True)
class Decision:
    """一次决策的结果。"""

    point: str
    decision: dict[str, Any]
    decision_model: str
    confirmed: bool | None = None  # None = 未走确认（never / 非高危）


class DecisionManager:
    """决策中枢：持有工具箱，三决策点 + 决策者选定与降级。"""

    def __init__(
        self,
        toolbox: Any,
        *,
        confirm_handler: ConfirmHandler | None = None,
        max_retries: int = 1,
        prefs_path: Any = DEFAULT_PREFS_PATH,
    ):
        self.toolbox = toolbox
        self.confirm_handler = confirm_handler
        self.max_retries = max_retries
        self.prefs_path = prefs_path

    # -- 决策者选定 ---------------------------------------------------

    def pick_decision_model(self, *, skip: frozenset[str] | set[str] = frozenset()) -> str:
        """保底模型优先担任决策者；明确不可用（或已试过）按清单序降级。"""

        fallback = str(self.toolbox.config.fallback_ref())
        order = [fallback] + [
            str(status.ref) for status in self.toolbox.registry.all() if str(status.ref) != fallback
        ]
        for ref in order:
            if ref in skip:
                continue
            status = self.toolbox.registry.find(ref)
            if status is None:
                continue
            if status.available is False:
                continue  # 明确不可用跳过（未测/可用均可尝试）
            return ref
        raise DecisionError("无可用决策模型（全部明确不可用或已尝试）")

    # -- 核心决策通道 ---------------------------------------------------

    def decide(
        self,
        point: str,
        prompt: str,
        *,
        validator: Callable[[dict[str, Any]], str] | None = None,
    ) -> Decision:
        """发起一次决策：发事件 -> 逐决策者尝试（失败换下一个）-> 返回 Decision。"""

        self._emit(EventType.DECISION_REQUESTED, {"decision_point": point, "context_chars": len(prompt)})
        tried: set[str] = set()
        last_error = ""
        for _ in range(len(self.toolbox.registry.all()) + 1):
            try:
                ref = self.pick_decision_model(skip=tried)
            except DecisionError:
                break
            tried.add(ref)
            self._emit(
                EventType.DECISION_MODEL_PICKED,
                {"model_ref": ref, "reason": "保底优先；不可用按清单序降级"},
            )
            try:
                data = self._ask(ref, prompt, validator=validator)
            except DecisionError as exc:
                last_error = str(exc)
                continue
            decision = Decision(point=point, decision=data, decision_model=ref)
            self._emit(
                EventType.DECISION_MADE,
                {"point": point, "choice": data, "reason": data.get("reason", ""), "decision_model": ref},
            )
            return decision
        raise DecisionError(f"决策点 {point} 失败：全部决策者尝试未果。{last_error}")

    def _ask(
        self,
        ref: str,
        prompt: str,
        *,
        validator: Callable[[dict[str, Any]], str] | None = None,
    ) -> dict[str, Any]:
        """调决策模型并解析校验 JSON；失败把原因反馈给决策模型重试。"""

        current = prompt
        problem = ""
        for _ in range(1 + self.max_retries):
            result = self.toolbox.call_model(ref, [{"role": "user", "content": current}])
            if not result.ok:
                raise DecisionError(f"决策模型 {ref} 调用失败：[{result.error_class}] {result.detail}")
            data = extract_json(result.text)
            if data is None:
                problem = "输出不是合法 JSON"
            else:
                problem = validator(data) if validator is not None else ""
                if not problem:
                    return data
                problem = f"输出未通过校验：{problem}"
            current = (
                prompt
                + f"\n\n你上一次的输出有问题（{problem}）。"
                + "请严格按格式只输出一个 JSON 对象，不要任何其他文字。"
            )
        raise DecisionError(f"决策模型 {ref} 连续 {1 + self.max_retries} 次未给出可解析且合法的 JSON（{problem}）")

    # -- 三个决策点 ---------------------------------------------------

    def choose_model(
        self,
        task: str,
        *,
        confirm_mode: str = "never",
        extra_context: str = "",
    ) -> Decision:
        """选型：任务 + 清单/状态/偏好 -> 选哪个模型；可选人工确认。"""

        prefs = load_prefs(self.prefs_path)
        extra = extra_context
        matched = match_task_pref(task, prefs)
        if matched:
            extra = (extra + "\n" if extra else "") + f"任务偏好命中：{matched}"
        prompt = CHOOSE_PROMPT.format(
            task=task,
            table=self._model_table(prefs),
            extra="\n" + extra if extra else "",
        )

        def validator(data: dict[str, Any]) -> str:
            model = str(data.get("model", "")).strip()
            if not model:
                return "缺少 model 字段"
            status = self.toolbox.registry.find(model)
            if status is None:
                return f"model 必须是清单内的完整引用：{model}"
            if status.available is False:
                return f"model {model} 当前不可用（已知故障或探测中），请选择其他模型"
            if not str(data.get("reason", "")).strip():
                return "缺少 reason 字段"
            return ""

        decision = self.decide("choose_model", prompt, validator=validator)
        confirmed: bool | None = None
        if confirm_mode == "always":
            confirmed = self._confirm(decision)
        elif confirm_mode == "once" and matched is None:
            confirmed = self._confirm(decision)
            if confirmed:
                add_task_pref(task, str(decision.decision["model"]), self.prefs_path)
        return replace(decision, confirmed=confirmed)

    def plan_recovery(
        self,
        failed_ref: str,
        error_class: str,
        *,
        detail: str = "",
        use_plan_pref: bool = True,
    ) -> Decision:
        """故障切换决策：错误类型 + 可用模型 -> 切换目标 + 是否诊断。

        切换偏好命中（同错类上次成功切换的目标仍可用）时程序直接切换
        （decision_model="program"，不调 LLM 省决策成本）；否则走 LLM 决策。
        偏好沉淀由 pipeline 在切换成功后回写（此处只读）。
        """

        others = [
            status
            for status in self.toolbox.registry.all()
            if str(status.ref) != failed_ref and status.available is not False
        ]

        # 切换偏好命中：程序直接决策（目标必须仍在可用候选集内）
        if use_plan_pref:
            hit = match_plan_pref(error_class, path=self.prefs_path)
            if hit is not None:
                target, hits = hit
                if target in {str(status.ref) for status in others}:
                    self._emit(
                        EventType.PLAN_PREF_HIT,
                        {"error_class": error_class, "switch_to": target, "hits": hits},
                    )
                    return Decision(
                        point="plan_recovery",
                        decision={
                            "switch_to": target,
                            "diagnose": True,
                            "reason": f"切换偏好命中（{hits} 次）：同错类上次成功切换",
                        },
                        decision_model="program",
                    )
            self._emit(EventType.PLAN_PREF_MISS, {"error_class": error_class})

        table = "\n".join(self._status_line(status, {}) for status in others) or "（无）"
        prompt = PLAN_PROMPT.format(
            failed=failed_ref,
            error_class=error_class,
            detail=detail or "（无详情）",
            table=table,
        )

        def validator(data: dict[str, Any]) -> str:
            switch_to = str(data.get("switch_to", "")).strip()
            if not switch_to:
                return "缺少 switch_to 字段"
            if switch_to == failed_ref:
                return "switch_to 不能是刚失败的模型"
            if self.toolbox.registry.find(switch_to) is None:
                return f"switch_to 必须是清单内的完整引用：{switch_to}"
            if not isinstance(data.get("diagnose"), bool):
                return "diagnose 字段必须是 true/false"
            if not str(data.get("reason", "")).strip():
                return "缺少 reason 字段"
            return ""

        return self.decide("plan_recovery", prompt, validator=validator)

    def conclude_diagnosis(self, facts: dict[str, Any], *, rules: str = "") -> Decision:
        """诊断结论：四项事实 + 内置规则库 -> 结论与建议动作（阶段4 供给事实）。"""

        prompt = CONCLUDE_PROMPT.format(
            facts=json.dumps(facts, ensure_ascii=False, default=str),
            rules=rules or "（暂无，凭常识判断）",
        )

        def validator(data: dict[str, Any]) -> str:
            if not str(data.get("conclusion", "")).strip():
                return "缺少 conclusion 字段"
            actions = data.get("actions")
            if not isinstance(actions, list) or not actions:
                return "actions 必须是非空列表"
            return ""

        return self.decide("conclude_diagnosis", prompt, validator=validator)

    # -- 确认 ---------------------------------------------------------

    def _confirm(self, decision: Decision) -> bool:
        """发起人工确认（confirm_requested -> handler -> granted/denied）。"""

        operation = f"选用模型 {decision.decision.get('model', '')} 执行任务"
        reason = str(decision.decision.get("reason", ""))
        self._emit(
            EventType.CONFIRM_REQUESTED,
            {"operation": operation, "reason": reason, "evidence": f"决策者：{decision.decision_model}"},
        )
        if self.confirm_handler is None:
            granted = True  # 未注入确认通道：视为通过（选型非高危；高危操作在阶段4 强制确认）
        else:
            granted = bool(self.confirm_handler(operation, reason, decision.decision_model))
        event = EventType.CONFIRM_GRANTED if granted else EventType.CONFIRM_DENIED
        self._emit(event, {"operation": operation})
        return granted

    # -- 上下文组装 ---------------------------------------------------

    def _model_table(self, prefs: dict[str, dict[str, str]]) -> str:
        labels = prefs.get("model_labels", {})
        return "\n".join(self._status_line(status, labels) for status in self.toolbox.registry.all())

    def _status_line(self, status: Any, labels: dict[str, str]) -> str:
        label = labels.get(str(status.ref), "-")
        latency = f"{status.last_latency_ms}ms" if status.last_latency_ms else "-"
        return (
            f"- {status.ref} | {_STATE_TEXT[status.available]} | "
            f"标签: {label} | 上次延迟: {latency}"
        )

    # -- 事件 ---------------------------------------------------------

    def _emit(self, type_: str, data: dict[str, Any]) -> None:
        if self.toolbox.stream is not None:
            self.toolbox.stream.emit(type_, redact(data))


def extract_json(text: str) -> dict[str, Any] | None:
    """从容错文本提取 JSON：直接 loads -> 失败则取首个 {...} 再 loads。"""

    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    match = JSON_RE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None
