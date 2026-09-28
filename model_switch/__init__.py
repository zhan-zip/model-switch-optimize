"""多模型降级自修复管理系统（model-switch-optimize）。

决策由"模型"做，程序只提供基础设施与工具箱。

阶段1（骨架与基础设施）：
- config  三级配置（服务商 -> 分组 -> 模型）加载与校验
- events  事件流（trace_id / seq / cause 追溯链）
- safety  全链路脱敏
- cli     mso init（生成配置模板）· mso validate（校验配置）

阶段2（工具层与 mock）：
- model   ModelStatus / ModelRegistry（运行时状态）
- client  OpenAI 兼容调用（urllib）· ErrorClass 错误分类
- health  test_connectivity 连通测试
- tools   Toolbox 六工具 + dispatch 统一分发
- mocker  Mock 三件套（无真实 key 跑通全闭环）

阶段3（决策中枢与 onboarding）：
- manager     三决策点（choose_model / plan_recovery / conclude_diagnosis）
- onboarding  init-check 流程（连通 -> 跑分 -> 画像 -> 标签）
- prefs       model_prefs.md 读写（模型标签 + 任务偏好）
- websearch   DuckDuckGo 简版联网搜索（保底）
- cli         mso tools · mso init-check

阶段4（故障自愈闭环）：
- pipeline    run 闭环（选型 -> 调用 -> 故障切换 -> 机械兜底 -> 入档）
- diagnose    诊断编排（四项事实 -> 规则库 -> 结论）
- fix         apply_fix 人工门禁修复（确认 -> 执行 -> 重测 -> 恢复）
- console_ops 控制台操作契约（Playwright MCP 占位）
- history     故障写入侧（fault-<序号>.json 脱敏落盘）
- rules       内置规则库（rules/diagnose.md）
- cli         mso run · mso diagnose · mso history

阶段5~6 见 README 开发阶段表（周期探测 / 嵌入接口与 MCP / 收尾与软著材料）。
"""
from .switcher import ModelSwitcher

__all__ = ["ModelSwitcher"]
__version__ = "1.0.0"
