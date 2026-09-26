"""多模型降级自修复管理系统（model-switch-optimize）。

决策由"模型"做，程序只提供基础设施与工具箱。

阶段1（骨架与基础设施）：
- config  三级配置（服务商 -> 分组 -> 模型）加载与校验
- events  事件流（trace_id / seq / cause 追溯链）
- safety  全链路脱敏
- cli     mso init（生成配置模板）· mso validate（校验配置）
"""
__version__ = "1.0.0"
