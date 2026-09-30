# model-switch-optimize

轻量的多模型任务路由与故障转移中间件。

它可以统一管理多个服务商、分组和模型，根据任务选择合适的模型；当当前模型调用失败时，自动尝试切换到其他可用模型，尽量让任务继续完成。

## 主要能力

- 多服务商、多分组、多模型配置
- 指定保底模型
- 根据模型标签和任务内容进行选型
- 模型调用失败后自动切换
- 所有候选模型失败时按配置顺序机械兜底
- 故障记录、事件流和调用历史
- 模型暂停与冷却
- 故障模型恢复探测
- 切换偏好记忆，减少重复决策
- CLI、Python、Agent 和 MCP 多种接入方式
- 支持用户使用自己的模型地址和 API key

## 适用场景

- 使用多个模型服务的应用
- 需要提高任务完成率的 Agent
- 需要跨服务商切换模型的自动化任务
- 不希望在业务代码中重复编写模型重试和降级逻辑的项目

## 快速开始

### 1. 安装

```bash
python -m pip install -e .
```

如果系统找不到 `mso`，可以使用：

```bash
python -m model_switch.cli
```

### 2. 生成配置

```bash
mso init
```

### 3. 配置自己的模型

编辑 `config/models.yaml`：

```yaml
providers:
  - name: my-provider
    base_url: https://api.example.com/v1
    groups:
      - name: main
        key_env: MY_PROVIDER_KEY
        models:
          - model-a
          - model-b

fallback:
  provider: my-provider
  group: main
  model: model-a
  key_env: MY_PROVIDER_KEY
```

配置结构为：

```text
服务商 → 分组 → 模型
```

用户可以自行填写服务商名称、API 地址、分组名称、模型名称和保底模型。

### 4. 设置 API key

API key 只通过环境变量提供，`key_env` 只填写变量名。

PowerShell 示例：

```powershell
$env:MY_PROVIDER_KEY = "你的 API key"
```

### 5. 验证配置和模型

```bash
mso validate
mso tools --check --model my-provider/main/model-a
```

### 6. 执行任务

```bash
mso run "总结这段文本"
```

任务执行失败时，中间件会记录故障并尝试切换其他可用模型。

## 接入自己的项目

完整的安装、配置、调用示例和后台诊断说明见：

[`宿主对接与使用指南.md`](宿主对接与使用指南.md)

支持以下接入方式：

| 宿主类型 | 接入方式 |
| --- | --- |
| 普通程序 | CLI 子进程 `mso run --json "任务"` |
| Python 项目 | `from model_switch import ModelSwitcher` |
| Agent 托管模式 | Agent 调用 `run()`，由中间件负责选型和切换 |
| Agent 自决策模式 | 使用 `dispatch()` 调用六个基础工具 |
| MCP 宿主 | 启动 `mso mcp`，通过 MCP tools 接入 |

## 任务结果

使用 `mso run --json` 或 Python API 时，可以根据结果状态判断任务是否完成：

| 状态 | 含义 |
| --- | --- |
| `ok` | 第一次选择的模型成功 |
| `switched` | 发生模型切换后成功 |
| `fallback` | 进入机械兜底后成功 |
| `failed` | 所有候选模型都失败 |

## 故障处理

查看故障历史：

```bash
mso history
```

手动诊断服务商：

```bash
mso diagnose <provider>
```

探测故障模型是否恢复：

```bash
mso probe
```

宿主 Agent 可以在后台自行调度 `diagnose` 和 `probe`。中间件本体不会自动启动后台 Agent 或后台线程。

## 接口要求

用户配置的模型服务需要提供 OpenAI Chat Completions 兼容接口：

- 请求地址：`<base_url>/chat/completions`
- 认证方式：`Authorization: Bearer <API key>`
- 请求包含 `model` 和 `messages`
- 响应至少包含 `choices[0].message.content`

如果服务商使用其他协议、特殊认证 Header、特殊请求路径或特殊响应格式，目前不能直接使用，需要增加协议适配。

## 安全注意事项

- 不要把 API key 写入 `models.yaml`、代码、日志或 Git 仓库。
- `key_env` 只写环境变量名称。
- MCP 宿主启动的子进程必须能够读取对应的 key 环境变量。
- 高风险修复操作必须经过用户或宿主确认。

## 能力边界

- 主要负责任务路由、故障切换、机械兜底、故障记录和恢复探测。
- 不自动登录第三方控制台。
- 不自动修改用户 key 或服务商分组。
- 不自动充值。
- 所有模型都不可用时，任务仍可能失败。
