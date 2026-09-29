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
from .stats import DEFAULT_STATS_PATH
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
        self.toolbox = Toolbox(
            self.config, self.registry, self.stream,
            model_client=model_client or call_openai_compatible,
            console=console,
            web_search_impl=web_search_impl,
            faults_dir=Path(faults_dir),
            mock=mock,
        )
        self.manager = DecisionManager(self.toolbox, confirm_handler=confirm_handler)
        # mock 演示：切换偏好/暂停表/统计只写临时文件，不污染真实运行态
        if mock:
            import tempfile

            prefs_path = Path(tempfile.mkdtemp(prefix="mso-mock-prefs-")) / "model_prefs.md"
            pause_path = Path(tempfile.mkdtemp(prefix="mso-mock-pause-")) / "pause.json"
            stats_path = Path(tempfile.mkdtemp(prefix="mso-mock-stats-")) / "stats.json"
        else:
            prefs_path = DEFAULT_PREFS_PATH
            pause_path = DEFAULT_PAUSE_PATH
            stats_path = DEFAULT_STATS_PATH
        self.pipeline = Pipeline(
            self.toolbox,
            manager=self.manager,
            probe_dir=probe_dir,
            prefs_path=prefs_path,
            pause_path=pause_path,
            stats_path=stats_path,
        )

    # -- 闭环 ---------------------------------------------------------

    def run(self, task: str, *, confirm_mode: str = "never", diagnose: bool = False) -> RunResult:
        """完整闭环；diagnose=True 时故障后自动诊断相关服务商。"""

        self.pipeline.confirm_mode = confirm_mode
        result = self.pipeline.run(task)
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
