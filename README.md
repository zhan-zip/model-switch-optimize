# model-switch-optimize · 多模型降级自修复管理系统

> 独立模块，单独可用。模型**故障自修复 + 自动切换**：决策由"模型"做，
> 程序只提供基础设施与工具箱。新增模型只需改配置，程序零改动。

**开发状态**：阶段1 已完成（骨架与基础设施）。

## 快速开始（阶段1）

```bash
pip install -e .
mso init          # 生成配置模板 config/models.yaml + data 目录
mso validate      # 校验配置（--json 输出结构化结果）
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
| 2 | 工具层与 mock：model / client / health / tools / mocker | ⬜ |
| 3 | 决策中枢：manager 三决策点 + onboarding(init-check) | ⬜ |
| 4 | 故障自愈闭环：切换 / 诊断四项 / 人工门禁修复 / 规则库 / 机械兜底 | ⬜ |
| 5 | 周期探测 + 完整 CLI(run/diagnose/probe/history/auth) + 嵌入接口 | ⬜ |
| 6 | 测试收尾 / Web 演示 / 软著材料 | ⬜ |

## 运行条件

- Python 3.10+；`pip install -e .` 一次安装
- 零数据库、零服务端、单进程可跑
- 依赖：PyYAML（运行）；pytest（开发）
