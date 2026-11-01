# 模型能力映射表（开发者维护，随程序发布）

> 供能力三层来源中的 mapped 层使用：按模型名正则匹配已知模型的能力。
> 使用者不接触；新模型发布后由开发者补充。能力项：vision / tool_use / structured_output。
> 说明：本表只是兜底提示（优先级 declared > probed > mapped）——转发网关可能名实不符，
> 真实链路能力以主动探测（mso capabilities）或用户声明（models.yaml capabilities）为准。

## 映射规则

| 模型名正则 | 能力 |
| --- | --- |
| ^gpt-4o | vision, tool_use, structured_output |
| ^gpt-4\.1 | vision, tool_use, structured_output |
| ^gpt-5 | vision, tool_use, structured_output |
| ^gpt-6 | vision, tool_use, structured_output |
| ^o[13] | tool_use, structured_output |
| ^claude- | vision, tool_use |
| ^gemini- | vision, tool_use |
| ^deepseek-chat$ | tool_use |
| ^deepseek-reasoner$ | tool_use |
| ^qwen | vision, tool_use |
| ^glm- | vision, tool_use |
