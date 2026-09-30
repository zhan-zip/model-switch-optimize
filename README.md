# model-switch-optimize · 多模型降级自修复管理系统

> 独立模块，单独可用。模型**故障自修复 + 自动切换**：决策由"模型"做，
> 程序只提供基础设施与工具箱。新增模型只需改配置，程序零改动。

**开发状态**：阶段1~5 已完成；阶段6 的 plan_prefs、PAUSE、stats 和测试隔离增强项已完成，当前剩余 Web 演示、软著材料和 init-check 远程确认程序化入口；浏览器 MCP 真实接入排在阶段6之后。

**当前质量状态**：阶段6 增强项已完成；观察点修复已完成；全量测试 `240 passed`。

## 快速开始

```bash
pip install -e .
# 如果 mso 不在 PATH，可使用 python -m model_switch.cli，或将 Python 用户级 Scripts 目录加入 PATH
mso init          # 生成配置模板 config/models.yaml + data 目录
mso validate      # 校验配置（--json 输出结构化结果）
mso tools         # 模型清单与状态
mso tools --check --model <服务商/分组/模型>   # 连通测试（真实调用，事件流落盘）
mso tools --check --mock                      # mock 演示（无真实 key 也能跑）
mso init-check    # 初始化 onboarding：连通 + 联网跑分 + 模型画像 + 标签落盘
                  #   --per-group 每分组代表抽样连通；--mock 无 key 演示；
                  #   --json 输出事件流（跳过终端确认）
mso run "任务"    # 完整闭环：选型 → 调用 → 故障自动切换（任务不中断）→ 机械兜底
                  #   启动时自动恢复探测队列中的故障模型（跳过最近探测失败的模型）
                  #   同类错误第二次起自动复用上次成功切换目标（切换偏好命中，程序直切）
                  #   --prompt "自定义提示词"：若提供，任务文本作为上下文拼接
                  #   --diagnose 故障后自动诊断；--confirm once 选型确认一次；
                  #   --json 事件流；--mock 无 key 演示故障切换闭环（演示故障入临时队列/临时偏好）
mso diagnose <服务商>   # 手动诊断：四项检查（可达/余额/分组/连通）→ 结论与建议
mso history       # 故障历史（data/faults/）
                  #   损坏/非法编码的旧档案会跳过；已经保存为 ???? 的历史文字无法恢复
mso probe         # 探测故障模型队列（默认单轮；--watch 循环；--mock 自包含演示恢复，
                  #   演示故障走临时队列，不写真实 data/probe）
mso auth          # 管理控制台账号（add/list/remove）
                  #   auth add 需要交互式终端；非交互环境会立即拒绝，避免 getpass 挂起
mso pause <服务商/分组/模型> [秒]   # 暂停模型（冷却：选型/切换/兜底/决策者跳过，默认 300s）
mso resume <服务商/分组/模型>      # 解除暂停
mso stats          # 查看 run 统计（各模型近 20 次成功率/耗时，样本不足 3 次不显示）
mso mcp           # 启动 MCP server（stdio，供 MCP 宿主零代码接入）
pytest            # 运行测试（先安装 dev 依赖：pip install -e ".[dev]"）
```

## 配置

三级结构：**服务商 → 分组 → 模型**；使用者只需填写模型清单 + 一个保底模型。

- key 一律走环境变量（`key_env` 只写变量名，不落明文）
- 配置顺序即机械兜底顺序
- 示例见 `mso init` 生成的模板

## 架构（三层）

```
决策中枢（动态模型：选型 / 切换 / 判因 / 定修复）
程序工具层（call_model · test_connectivity · apply_fix(人工门禁) · ...）
机械兜底（全部不可用时按配置顺序逐个试调）
```

## 开发阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 骨架与基础设施：config / events / safety / cli(init·validate) | ✅ |
| 2 | 工具层与 mock：model / client / health / tools / mocker | ✅ |
| 3 | 决策中枢：manager 三决策点 + onboarding(init-check) + 联网搜索 | ✅ |
| 4 | 故障自愈闭环：切换 / 诊断四项 / 人工门禁修复 / 规则库 / 机械兜底 | ✅ |
| 5 | 周期探测 + CLI 收尾(probe/auth) + 嵌入接口（mso run --json / Python 库）+ MCP 薄封装 | ✅ (MCP 已锁定 <2.0，误装 2.x 会给出清晰降级指引) |
| 6 | Web 演示 / 软著材料 / init-check 远程确认程序化入口 + 架构增强收尾（切换偏好沉淀 ✅ / PAUSE 冷却 ✅ / run 统计经验分 ✅ / 测试隔离 ✅） | 进行中（收尾项） |

## 嵌入与对接（已支持）

| 宿主形态 | 对接方式 |
|---|---|
| 任意语言程序 | CLI 子进程 `mso run --json "任务"`，解析 JSON 事件流 |
| Python 项目 | `from model_switch import ModelSwitcher` |
| agent 项目（托管） | agent 工具列表加"执行任务"工具，模块全权代理选型/切换 |
| agent 项目（自决策） | 六工具（TOOL_SPECS）注册进 function calling，统一入口 `dispatch(name, args)` |
| MCP 宿主 | MCP 薄封装（阶段5），零代码接入 |

高危操作走人工确认协议（`confirm_requested` → 宿主转发用户 → `confirm_granted/denied`）。

## 运行条件

- Python 3.10+；`pip install -e .` 一次安装
- 零数据库、零服务端、单进程可跑
- 依赖：PyYAML（运行）；mcp（运行，MCP 宿主接入）；pytest（开发）
