"""模型能力检测：vision / tool_use / structured_output 三项。

三层来源与优先级（高 -> 低）：
  declared  用户在 models.yaml 中显式声明（capabilities 字段）
  probed    主动探测结果（真实调用验证，存 data/capabilities.json）
  mapped    内置模型名映射表（rules/capabilities.md，仅兜底——网关可能名实不符）
  unknown   以上皆无（supported=None，选型放行不硬拦）

探测判定（保守原则，与"不因一次失败永久禁用"一致）：
  - ok 且回复符合要求 -> supported=True
  - ok 但回复不符合（说看不到图 / 非 JSON）-> supported=False（网关剥离图片等）
  - 调用失败（网络/鉴权/超时/5xx）-> supported=None（不定罪）
每次探测记 stats（call_kind=probe）并发 capability_probed 事件。

vision 探测原理：发送一张纯色 PNG（每次从调色板随机取色），
问"这是什么颜色"；回复含该颜色的中英文词 -> 支持。用固定文案陷阱缓解
网关剥图后模型瞎猜的概率（瞎猜命中概率 1/4）。
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EventType
from .rules import load_capability_rules, match_capability_rules
from .safety import redact

CAPABILITIES = ("vision", "tool_use", "structured_output")
CAPABILITIES_VERSION = 1
DEFAULT_CAPABILITIES_PATH = Path("data/capabilities.json")

# 任务类型 -> 选型硬过滤所需能力（供 manager/pipeline 使用）：
# 确认不支持（supported=False）的模型被拒绝；None（未测）放行
TASK_TYPE_CAPABILITY = {
    "vision": "vision",
    "tool_use": "tool_use",
    "structured": "structured_output",
}

SOURCE_DECLARED = "declared"
SOURCE_PROBED = "probed"
SOURCE_MAPPED = "mapped"
SOURCE_UNKNOWN = "unknown"

# -- vision 探测素材 -------------------------------------------------------

# 调色板：颜色名 -> (RGB, 中英文匹配词)
VISION_PALETTE: dict[str, tuple[tuple[int, int, int], tuple[str, ...]]] = {
    "红色": ((255, 0, 0), ("红", "red")),
    "绿色": ((0, 200, 0), ("绿", "green")),
    "蓝色": ((0, 100, 255), ("蓝", "blue")),
    "黄色": ((255, 220, 0), ("黄", "yellow")),
}

VISION_PROBE_TEXT = "这张图片是什么颜色？只用一个词回答。"

_TOOL_PROBE_MESSAGES = [
    {"role": "user", "content": "北京今天天气怎么样？"},
]
_TOOL_PROBE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

_STRUCTURED_PROBE_MESSAGES = [
    {"role": "user", "content": '只输出一个 JSON 对象：{"ok": true}'},
]
_STRUCTURED_RESPONSE_FORMAT = {"type": "json_object"}

# mock 探测分流（按模型名）：{模型名: {能力: True/False/None}}；未预设的模型默认 True。
# 供 CLI --mock 演示与测试构造"确认不支持/未知"场景。
MOCK_CAPABILITY_OUTCOMES: dict[str, dict[str, bool | None]] = {}


def _solid_png_b64(rgb: tuple[int, int, int], size: int = 8) -> str:
    """生成纯色 PNG 的 base64（标准库 zlib+struct，零依赖）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


def _vision_payload() -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """构造 vision 探测消息与该颜色的匹配词；返回 (messages, 匹配词)。"""

    color = random.choice(list(VISION_PALETTE))
    rgb, words = VISION_PALETTE[color]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROBE_TEXT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_solid_png_b64(rgb)}"},
                },
            ],
        }
    ]
    return messages, words


# -- 存储读写 ---------------------------------------------------------------


def load_capabilities(path: Path | str = DEFAULT_CAPABILITIES_PATH) -> dict[str, dict[str, Any]]:
    """读能力表 {model_ref: {kind: {supported, source, tested_at}}}；不存在/损坏返回空。"""

    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def save_capabilities(
    models: dict[str, dict[str, Any]], path: Path | str = DEFAULT_CAPABILITIES_PATH
) -> None:
    """原子写能力表（临时文件 + os.replace，与 pause/stats 同风格）。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {"version": CAPABILITIES_VERSION, "models": models},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# -- 三层合并 ---------------------------------------------------------------


def resolve_capabilities(
    status: Any,
    *,
    store: dict[str, dict[str, Any]] | None = None,
    rules: list | None = None,
) -> dict[str, dict[str, Any]]:
    """三层合并：declared > probed > mapped > unknown。

    返回 {kind: {supported: True/False/None, source, tested_at}}。
    declared 只能声明"支持"（不支持无法在 yaml 声明，走探测）。
    """

    store = {} if store is None else store
    ref = str(status.ref)
    declared = set(getattr(status, "capabilities_declared", ()) or ())
    mapped = match_capability_rules(status.ref.model, rules)
    probed = store.get(ref) or {}

    result: dict[str, dict[str, Any]] = {}
    for kind in CAPABILITIES:
        if kind in declared:
            result[kind] = {"supported": True, "source": SOURCE_DECLARED, "tested_at": None}
            continue
        entry = probed.get(kind)
        if isinstance(entry, dict) and entry.get("supported") is not None:
            result[kind] = {
                "supported": entry.get("supported"),
                "source": entry.get("source", SOURCE_PROBED),
                "tested_at": entry.get("tested_at"),
            }
            continue
        if kind in mapped:
            result[kind] = {"supported": True, "source": SOURCE_MAPPED, "tested_at": None}
            continue
        result[kind] = {"supported": None, "source": SOURCE_UNKNOWN, "tested_at": None}
    return result


def capability_value(
    status: Any,
    kind: str,
    *,
    store: dict[str, dict[str, Any]] | None = None,
    rules: list | None = None,
) -> bool | None:
    """单能力快捷查询：True/False/None（None=未知，选型放行）。"""

    caps = resolve_capabilities(status, store=store, rules=rules)
    entry = caps.get(kind) or {}
    value = entry.get("supported")
    return value if isinstance(value, bool) else None


# -- 探测 -------------------------------------------------------------------


def probe_capability(
    toolbox: Any,
    model_ref: str,
    capability: str,
    *,
    timeout: int = 20,
) -> dict[str, Any]:
    """探测单个模型单项能力；返回 {capability, supported, source, tested_at, detail}。

    mock 模式：按 MOCK_CAPABILITY_OUTCOMES 分流（未预设默认 True），不发真实调用。
    真实模式：经协议适配器发极小探测请求（与 health.test_connectivity 同风格）。
    探测调用写 stats（call_kind=probe）；mock 不写（零污染，与连通测试一致）。
    """

    if capability not in CAPABILITIES:
        return _probe_result(capability, None, "未知能力项：" + capability)

    status = toolbox.registry.find(model_ref)
    if status is None:
        return _probe_result(capability, None, f"未知模型：{model_ref}")

    tested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if getattr(toolbox, "mock", False):
        outcome = MOCK_CAPABILITY_OUTCOMES.get(status.ref.model, {}).get(capability, True)
        _emit_probed(toolbox, model_ref, capability, outcome)
        return _probe_result(
            capability, outcome, "[mock] 预设探测结果", tested_at=tested_at
        )

    from .client import response_to_result
    from .config import resolve_api_key
    from .protocols.base import ModelRequest
    from .protocols.registry import get_adapter

    api_key = resolve_api_key(status.key_env)
    if api_key is None:
        return _probe_result(
            capability, None,
            f"环境变量 {status.key_env} 未设置（key 只走环境变量，无法探测）",
        )

    tools: list | None = None
    response_format: dict | None = None
    if capability == "vision":
        messages, words = _vision_payload()
        max_tokens = 16
    elif capability == "tool_use":
        messages, words = _TOOL_PROBE_MESSAGES, ()
        tools = _TOOL_PROBE_TOOLS
        max_tokens = 16
    else:
        messages, words = _STRUCTURED_PROBE_MESSAGES, ()
        response_format = _STRUCTURED_RESPONSE_FORMAT
        max_tokens = 16

    adapter = get_adapter(status.protocol)
    request = ModelRequest(
        model=status.ref.model,
        messages=messages,
        timeout=timeout,
        max_tokens=max_tokens,
        tools=tools,
        response_format=response_format,
    )
    response = adapter.call(status.base_url, api_key, request, **status.options)
    result = response_to_result(response, model_ref)

    toolbox._record_probe_stats(status, result)

    if not result.ok:
        # 保守：调用失败不定罪（网络/鉴权/超时均可能；与"不因一次失败永久禁用"一致）
        return _probe_result(
            capability, None,
            f"探测调用失败（[{result.error_class}] {result.detail[:120]}）",
            tested_at=tested_at,
        )

    if capability == "vision":
        supported = any(word in result.text.lower() for word in words)
        detail = f"回复：{result.text.strip()[:60]}"
    elif capability == "tool_use":
        supported = True  # 不报错即链路接受 tools 参数（无论是否真实调用）
        detail = f"回复：{result.text.strip()[:60]}"
    else:
        supported = _extract_json_loose(result.text) is not None
        detail = f"回复：{result.text.strip()[:60]}"

    _emit_probed(toolbox, model_ref, capability, supported)
    return _probe_result(capability, supported, detail, tested_at=tested_at)


def probe_all(
    toolbox: Any,
    model_ref: str | None = None,
    *,
    path: Path | str = DEFAULT_CAPABILITIES_PATH,
    incremental: bool = True,
    timeout: int = 20,
) -> dict[str, dict[str, Any]]:
    """批量探测并写回能力表；返回全部模型的 resolve 结果（含未探测项）。

    - model_ref 省略探测全部模型；incremental=True 只探测 unknown 项
      （declared/probed/mapped 已有来源的跳过，省调用）；
      incremental=False 重测全部非 declared 项（--refresh 验证网关名实）。
    - 探测结果合并写回 store（原子写）；单项失败不阻断其余（保守 None）。
    """

    store = load_capabilities(path)
    rules = load_capability_rules()
    targets = (
        [toolbox.registry.find(model_ref)]
        if model_ref
        else list(toolbox.registry.all())
    )
    for status in targets:
        if status is None:
            continue
        caps = resolve_capabilities(status, store=store, rules=rules)
        for kind in CAPABILITIES:
            entry = caps[kind]
            if entry["source"] == SOURCE_DECLARED:
                continue  # 用户显式声明：不探测（尊重声明省调用）
            if incremental and entry["source"] != SOURCE_UNKNOWN:
                continue  # 增量模式：已有来源不重测
            outcome = probe_capability(toolbox, str(status.ref), kind, timeout=timeout)
            if outcome["supported"] is None:
                continue  # 定论失败不落盘（保持原来源，避免 unknown 覆盖 mapped）
            store.setdefault(str(status.ref), {})[kind] = {
                "supported": outcome["supported"],
                "source": SOURCE_PROBED,
                "tested_at": outcome["tested_at"],
            }
    try:
        save_capabilities(store, path)
    except OSError:
        pass  # 落盘失败不阻断（本进程 resolve 仍可用内存态）

    return {
        str(status.ref): resolve_capabilities(status, store=store, rules=rules)
        for status in toolbox.registry.all()
    }


def _probe_result(
    capability: str, supported: bool | None, detail: str, *, tested_at: str | None = None
) -> dict[str, Any]:
    return {
        "capability": capability,
        "supported": supported,
        "source": SOURCE_PROBED if supported is not None else SOURCE_UNKNOWN,
        "tested_at": tested_at,
        "detail": detail,
    }


def _emit_probed(toolbox: Any, model_ref: str, capability: str, supported: bool | None) -> None:
    if toolbox.stream is not None:
        toolbox.stream.emit(
            EventType.CAPABILITY_PROBED,
            redact(
                {
                    "model_ref": model_ref,
                    "capability": capability,
                    "supported": supported,
                }
            ),
        )


def _extract_json_loose(text: str) -> dict[str, Any] | None:
    """从回复中宽松提取 JSON（structured 探测判定用）。"""

    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None
