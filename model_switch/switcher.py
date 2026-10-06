"""嵌入接口：Python 库模式一行接入（宿主场景 2/3/4）。

- ModelSwitcher.run(task)                    完整闭环（场景2/3：模块全权代理）
- ModelSwitcher.toolbox / .dispatch(name, args)  工具模式（场景4：宿主自决策）
- ModelSwitcher.manager                       三决策点（宿主直接调用）
- .diagnose(provider) / .probe(...)          诊断与探测
- confirm_handler 注入人工确认（高危操作）；console / web_search_impl / model_client
  注入真实或 mock 实现（宿主测试无 key 跑通全闭环）

用法：
    from model_switch import ModelSwitcher
    switcher = ModelSwitcher(confirm_handler=my_confirm)   # confirm_handler 可选
    result = switcher.run("写个爬虫")                        # RunResult(ok/outcome/model_ref/text/...)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .client import call_openai_compatible
from .config import DEFAULT_CONFIG_PATH, load_config
from .diagnose import run_diagnose
from .events import EventStream, EventType
from .manager import DecisionManager, DecisionError
from .mocker import MockConsole, MockDecisionClient, MockWebSearch
from .model import ModelRegistry
from .pipeline import Pipeline, RunResult
from .model import DEFAULT_PAUSE_PATH
from .prefs import DEFAULT_PREFS_PATH
from .probe import DEFAULT_PROBE_DIR, ProbeQueue, probe_once, probe_watch
from .session_store import DEFAULT_SESSIONS_DIR, clear_session, load_session, save_session
from .stats import DEFAULT_STATS_PATH
from .task_store import DEFAULT_TASKS_DIR
from .tools import Toolbox
from .websearch import default_web_search


class ModelSwitcher:
    """库模式入口：持有工具箱 / 决策中枢 / 闭环管线，一行嵌入。"""

    def __init__(
        self,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        *,
        confirm_handler: Callable[[str, str, str], bool] | None = None,
        mock: bool = False,
        console: Any = None,
        web_search_impl: Callable[[str], dict] | None = None,
        model_client: Any = None,
        faults_dir: Path | str = "data/faults",
        probe_dir: Path | str = DEFAULT_PROBE_DIR,
        persist_events: bool = True,
        tasks_dir: Path | str = DEFAULT_TASKS_DIR,
        sessions_dir: Path | str = DEFAULT_SESSIONS_DIR,
    ):
        self.config = load_config(config_path)
        self.registry = ModelRegistry(self.config)
        self.stream = EventStream(
            persist_dir=Path("data/events") if persist_events else None
        )
        if model_client is None and mock:
            model_client = MockDecisionClient(
                default_model=str(self.registry.all()[0].ref),
                switch_to=str(self.config.fallback_ref()),
            )
        if console is None and mock:
            console = MockConsole()
        if web_search_impl is None:
            web_search_impl = MockWebSearch().search if mock else default_web_search
        # mock 演示：切换偏好/暂停表/统计只写临时文件，不污染真实运行态
        if mock:
            import tempfile

            stats_path = Path(tempfile.mkdtemp(prefix="mso-mock-stats-")) / "stats.json"
        else:
            stats_path = DEFAULT_STATS_PATH
        self.toolbox = Toolbox(
            self.config, self.registry, self.stream,
            model_client=model_client or call_openai_compatible,
            console=console,
            web_search_impl=web_search_impl,
            faults_dir=Path(faults_dir),
            stats_path=stats_path,
            mock=mock,
        )
        self.manager = DecisionManager(self.toolbox, confirm_handler=confirm_handler)
        if mock:
            import tempfile

            prefs_path = Path(tempfile.mkdtemp(prefix="mso-mock-prefs-")) / "model_prefs.md"
            pause_path = Path(tempfile.mkdtemp(prefix="mso-mock-pause-")) / "pause.json"
            # 会话偏好影响后续选型决策（属影响运行态状态）：mock 默认走临时目录隔离；
            # 但显式传入 sessions_dir 时尊重调用方（测试注入 tmp、宿主自定义存储）
            if str(Path(sessions_dir)) == str(DEFAULT_SESSIONS_DIR):
                sessions_dir = Path(tempfile.mkdtemp(prefix="mso-mock-sessions-"))
            else:
                sessions_dir = Path(sessions_dir)
        else:
            prefs_path = DEFAULT_PREFS_PATH
            pause_path = DEFAULT_PAUSE_PATH
            sessions_dir = Path(sessions_dir)
        self.pipeline = Pipeline(
            self.toolbox,
            manager=self.manager,
            probe_dir=probe_dir,
            prefs_path=prefs_path,
            pause_path=pause_path,
            stats_path=stats_path,
            tasks_dir=tasks_dir,
            sessions_dir=sessions_dir,
        )

    # -- 闭环 ---------------------------------------------------------

    def run(
        self,
        task: str,
        *,
        confirm_mode: str = "never",
        diagnose: bool = False,
        session_id: str | None = None,
    ) -> RunResult:
        """完整闭环；diagnose=True 时故障后自动诊断相关服务商；session_id 命中会话偏好。"""

        self.pipeline.confirm_mode = confirm_mode
        result = self.pipeline.run(task, session_id=session_id)
        if diagnose and self.stream is not None:
            fault_providers = sorted(
                {
                    event.data["model_ref"].split("/")[0]
                    for event in self.stream
                    if event.type == EventType.FAULT_RECORDED
                }
            )
            for provider in fault_providers:
                try:
                    run_diagnose(self.toolbox, provider, manager=self.manager)
                except DecisionError:
                    continue  # 诊断失败不阻断结果返回
        return result

    def prepare(
        self,
        task: str,
        *,
        confirm_mode: str = "never",
        session_id: str | None = None,
        preferred_model: str | None = None,
        strict_model: bool = False,
    ) -> dict[str, Any]:
        """两阶段审批·准备：只选型不执行，任务状态落盘 data/tasks/。

        返回 {task_id, status, task_type, selected_model, reason, expires_at, ok}；
        status 为 awaiting_confirmation（等 execute 确认）或 ready（自动批准）。
        session_id/preferred_model/strict_model：会话偏好接入（阶段4）。
        """
        return self.pipeline.prepare(
            task,
            confirm_mode=confirm_mode,
            session_id=session_id,
            preferred_model=preferred_model,
            strict_model=strict_model,
        )

    def execute(
        self,
        task_id: str,
        *,
        user_confirmed: bool = False,
        model_override: str | None = None,
    ) -> RunResult:
        """两阶段审批·执行：校验状态 -> 批准/拒绝/过期 -> 执行（故障仍自动切换）。

        user_confirmed=true 批准 awaiting 任务；model_override 覆盖推荐模型（重新校验）。
        """
        return self.pipeline.execute(
            task_id, user_confirmed=user_confirmed, model_override=model_override
        )

    # -- 直通接口 ------------------------------------------------------

    def dispatch(self, name: str, args: dict | None = None) -> dict[str, Any]:
        """工具模式（场景4）：六工具统一分发直通。"""

        return self.toolbox.dispatch(name, args)

    def diagnose(self, provider: str) -> dict[str, Any]:
        """手动诊断一个服务商（四项事实 -> 规则库 -> 结论）。"""

        return run_diagnose(self.toolbox, provider, manager=self.manager)

    def probe(self, *, watch: bool = False, interval: int | None = None) -> dict[str, Any]:
        """探测故障队列（watch=True 循环，宿主后台任务可调）；用构造时的 probe_dir。"""

        if watch:
            return probe_watch(
                self.toolbox, interval=interval, queue=self.pipeline.probe_queue
            )
        return probe_once(self.toolbox, self.pipeline.probe_queue)

    def choose_model(self, task: str, *, confirm_mode: str = "never"):
        """三决策点直通：选型。"""

        return self.manager.choose_model(task, confirm_mode=confirm_mode)

    def health(self, model_ref: str | None = None) -> dict[str, Any]:
        """健康摘要查询（阶段2）：model_ref 省略返回全部模型。

        返回 {model_ref: {summary, health, paused}}；无记录模型 summary 为 None、health=unknown。
        """

        from .stats import DEFAULT_STATS_PATH, health_status, health_summary, load_stats

        try:
            stats = load_stats(self.pipeline.stats_path or DEFAULT_STATS_PATH)
        except OSError:
            stats = {}
        refs = [model_ref] if model_ref else [str(s.ref) for s in self.toolbox.registry.all()]
        result: dict[str, Any] = {}
        for ref in refs:
            status = self.toolbox.registry.find(ref)
            summary_dict = health_summary(ref, stats=stats)
            health = health_status(ref, stats=stats)
            result[ref] = {
                "summary": summary_dict,
                "health": "paused" if (status is not None and status.is_paused()) else health,
                "paused": status is not None and status.is_paused(),
            }
        return result

    # -- 会话模型偏好（阶段4）--------------------------------------------

    def _preferred_sessions_dir(self):
        return self.pipeline.sessions_dir

    def set_preferred_model(
        self,
        session_id: str,
        model_ref: str,
        *,
        strict_model: bool = False,
    ) -> dict[str, Any]:
        """设置会话偏好模型（软偏好）；strict=True 为硬限制（失败不切换）。

        准入校验（8.2）：模型必须存在且当前可入选型（available 不能明确不可用 / 未暂停）。
        校验失败返回 {"ok": False, "error": ...} 且不落盘、不发事件。
        """
        status = self.toolbox.registry.find(model_ref)
        if status is None:
            return {"ok": False, "error": f"模型不在清单内：{model_ref}"}
        if status.available is False:
            return {"ok": False, "error": f"模型当前不可用（已知故障或探测中）：{model_ref}"}
        if status.is_paused():
            return {"ok": False, "error": f"模型当前暂停中（冷却），不能作为会话偏好：{model_ref}"}
        path = save_session(
            session_id,
            preferred_model=model_ref,
            strict_model=strict_model,
            sessions_dir=self._preferred_sessions_dir(),
        )
        if path is None:
            return {"ok": False, "error": f"非法 session_id：{session_id}"}
        self._emit_pref_event(
            EventType.PREFERRED_MODEL_SET,
            {"session_id": session_id, "model_ref": model_ref, "strict": bool(strict_model)},
        )
        return {
            "ok": True,
            "session_id": session_id,
            "preferred_model": model_ref,
            "strict_model": bool(strict_model),
        }

    def get_preferred_model(self, session_id: str) -> dict[str, Any]:
        """查询会话偏好；未设置返回 preferred_model=None。"""
        session = load_session(session_id, self._preferred_sessions_dir())
        return {
            "session_id": session_id,
            "preferred_model": session.get("preferred_model") or None,
            "strict_model": bool(session.get("strict_model", False)),
        }

    def clear_preferred_model(self, session_id: str) -> dict[str, Any]:
        """清除会话偏好（恢复自动选择）。"""
        cleared = clear_session(session_id, self._preferred_sessions_dir())
        self._emit_pref_event(
            EventType.PREFERRED_MODEL_CLEARED, {"session_id": session_id}
        )
        return {"ok": True, "session_id": session_id, "cleared": cleared}

    def _emit_pref_event(self, type_: str, data: dict[str, Any]) -> None:
        if self.stream is not None:
            self.stream.emit(type_, data)
