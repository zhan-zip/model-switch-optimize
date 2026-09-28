"""测试共享：可编程序列模型客户端（按调用顺序返回预置回复）。"""
from model_switch.client import CallResult


class SequenceClient:
    """按调用顺序返回预置回复的模型客户端（签名与 call_openai_compatible 一致）。

    replies 元素为 str（成功文本）或 CallResult（原样返回）；耗尽后返回 "[mock] ok"。
    """

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    def __call__(
        self,
        base_url,
        api_key,
        model,
        messages,
        *,
        timeout=30,
        max_tokens=None,
        opener=None,
    ) -> CallResult:
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        if self.replies:
            item = self.replies.pop(0)
            if isinstance(item, CallResult):
                return item
            return CallResult(ok=True, model=model, text=str(item), latency_ms=1, usage={})
        return CallResult(ok=True, model=model, text="[mock] ok", latency_ms=1, usage={})
