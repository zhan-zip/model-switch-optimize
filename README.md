# model-switch-optimize · 多模型降级自修复管理系统

> 独立模块，单独可用。模型**故障自修复 + 自动切换**：决策由"模型"做，
> 程序只提供基础设施与工具箱。新增模型只需改配置，程序零改动。

**开发状态**：阶段1~5 已完成；阶段6 的 plan_prefs、PAUSE、stats 和测试隔离增强项已完成，当前剩余 Web 演示、软著材料和 init-check 远程确认程序化入口；浏览器 MCP 真实接入排在阶段6之后。

**当前质量状态**：阶段6 增强项已完成；观察点修复已完成；全量测试 `240 passed`。

## 新用户入口

完整的接入步骤请先阅读：[`宿主对接与使用指南.md`](宿主对接与使用指南.md)。

这份 README 负责说明项目是什么和如何开始；使用指南负责说明如何接入自己的项目。新用户建议按下面顺序操作：

1. 安装项目并运行 `mso init`。
2. 编辑 `config/models.yaml`，填写自己的服务商、地址、分组、模型和保底模型。
3. 通过环境变量设置自己的 API key。
4. 运行 `mso validate` 和 `mso tools --check --model <服务商/分组/模型>`。
5. 根据项目类型选择 CLI、Python、Agent 托管、Agent 自决策或 MCP 接入方式。

项目不依赖仓库内的测试模型或测试 key。用户可以使用自己的模型和 key，但模型服务需要提供 OpenAI Chat Completions 兼容接口：请求路径为 `<base_url>/chat/completions`，使用 Bearer key 认证。

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

用户可以完全替换为自己的配置，例如：

```yaml
providers:
  - name: my-provider
    base_url: https://api.example.com/v1
    groups:
      - name: main
        key_env: MY_PROVIDER_KEY
        models: [model-a, model-b]

fallback:
  provider: my-provider
  group: main
  model: model-a
  key_env: MY_PROVIDER_KEY
```

然后设置对应环境变量并验证：

```powershell
$env:MY_PROVIDER_KEY = "你的 API key"
mso validate
mso tools --check --model my-provider/main/model-a
```

如果服务商不是 OpenAI Chat Completions 兼容协议，或者要求特殊认证 Header、特殊请求路径和特殊响应格式，目前不能直接使用，需要增加协议适配。

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

任务失败后的后台诊断由宿主调度：宿主发现 `fault_recorded` 后，可以在自己的后台任务中调用 `diagnose(provider)`，并定期调用 `probe()`。中间件本体不会自行启动后台 Agent 或后台线程。

## 运行条件

- Python 3.10+；`pip install -e .` 一次安装
- 零数据库、零服务端、单进程可跑
- 依赖：PyYAML（运行）；mcp（运行，MCP 宿主接入）；pytest（开发）

## 能力边界

- 主要目标是任务不中断、模型故障切换、机械兜底、故障记录和恢复探测。
- 真实控制台登录、自动修改 key、自动修改分组和自动充值不属于基础接入能力。
- `apply_fix` 等高风险操作必须经过宿主或用户确认。
- MCP 宿主启动的子进程必须能够读取配置中 `key_env` 对应的环境变量，否则会返回 `config` 错误。
- 本地 benchmark 和显式 `task_type` 分类仍属于后续优化计划，当前任务适配主要依靠模型标签和决策模型判断。
