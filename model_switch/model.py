"""模型运行时状态与注册表。

从 Config 构建全部模型的运行时状态（ModelStatus），
供工具层 / 决策中枢 / CLI 查询与更新。

暂停（冷却）机制：paused_until 记录截止时间，暂停中不可被
选型 / 切换 / 机械兜底 / 决策者选中；到期自动解除（比较时间）。
跨进程持久化：data/pause.json（启动时恢复+过期清扫）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config, ModelRef, resolve_api_key
from .safety import mask_env_value

DEFAULT_PAUSE_PATH = Path("data/pause.json")

MAX_PAUSE_SECONDS = 86400


@dataclass
class ModelStatus:
    """单个模型的运行时状态。"""

    ref: ModelRef
    base_url: str
    key_env: str
    available: bool | None = None      # None = 未测
    last_error_class: str = ""
    last_error_detail: str = ""
    last_latency_ms: int = 0
    last_checked: str = ""
    labels: tuple[str, ...] = ()      # 阶段3 onboarding 打标签用
    paused_until: str | None = None  # 暂停（冷却）截止 ISO 时间；None = 未暂停

    @property
    def key_ready(self) -> bool:
        return resolve_api_key(self.key_env) is not None

    def pause(self, seconds: int) -> None:
        """暂停（冷却）N 秒：到期自动解除（评估时比较时间）。"""
        self.paused_until = (
            datetime.now(timezone.utc) + timedelta(seconds=seconds)
        ).isoformat(timespec="seconds")

    def resume(self) -> None:
        self.paused_until = None

    def is_paused(self) -> bool:
        if not self.paused_until:
            return False
        try:
            return datetime.now(timezone.utc) < datetime.fromisoformat(self.paused_until)
        except ValueError:
            return False  # 坏格式视为未暂停

    def mark(self, ok: bool, *, latency_ms: int = 0, error_class: str = "", error_detail: str = "") -> None:
        self.available = ok
        self.last_latency_ms = latency_ms
        self.last_error_class = error_class
        self.last_error_detail = error_detail
        self.last_checked = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_ref": str(self.ref),
            "base_url": self.base_url,
            "key_env": self.key_env,
            "key_status": mask_env_value(resolve_api_key(self.key_env)),
            "available": self.available,
            "paused": self.is_paused(),
            "paused_until": self.paused_until,
            "last_error_class": self.last_error_class,
            "last_error_detail": self.last_error_detail,
            "last_latency_ms": self.last_latency_ms,
            "last_checked": self.last_checked,
        }


class ModelRegistry:
    """全部模型的运行时注册表：构建 / 查找 / 更新。"""

    def __init__(self, config: Config):
        self._config = config
        self._statuses: dict[str, ModelStatus] = {}
        for provider in config.providers:
            for group in provider.groups:
                for model in group.models:
                    ref = ModelRef(provider.name, group.name, model)
                    self._statuses[str(ref)] = ModelStatus(
                        ref=ref, base_url=provider.base_url, key_env=group.key_env
                    )

    @property
    def config(self) -> Config:
        return self._config

    def all(self) -> tuple[ModelStatus, ...]:
        return tuple(self._statuses.values())

    def find(self, ref_str: str) -> ModelStatus | None:
        return self._statuses.get(ref_str)

    def update(self, ref_str: str, **fields: Any) -> ModelStatus | None:
        status = self._statuses.get(ref_str)
        if status is None:
            return None
        for key, value in fields.items():
            if hasattr(status, key):
                setattr(status, key, value)
        return status

    def to_list(self) -> list[dict[str, Any]]:
        return [status.to_dict() for status in self._statuses.values()]


# -- 暂停（冷却）持久化 --------------------------------------------------


def load_pause_state(path: Path | str = DEFAULT_PAUSE_PATH) -> dict[str, str]:
    """读持久暂停表 {model_ref: paused_until ISO}；不存在/损坏返回空。"""

    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_pause_state(pauses: dict[str, str], path: Path | str = DEFAULT_PAUSE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pauses, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_pause_state(
    pauses: dict[str, str], *, now: datetime | None = None
) -> dict[str, str]:
    """清扫过期暂停条目（到期自动解除的实现）；坏格式条目一并清除。"""

    now = now or datetime.now(timezone.utc)
    kept: dict[str, str] = {}
    for ref, until in pauses.items():
        try:
            if datetime.fromisoformat(str(until)) > now:
                kept[ref] = str(until)
        except ValueError:
            continue
    return kept


def restore_pause_state(
    registry: "ModelRegistry", path: Path | str = DEFAULT_PAUSE_PATH
) -> dict[str, str]:
    """把持久暂停表恢复进 registry（先清扫过期；未知模型条目一并清除）；返回有效暂停表。"""

    pauses = prune_pause_state(load_pause_state(path))
    kept: dict[str, str] = {}
    for ref, until in pauses.items():
        status = registry.find(ref)
        if status is None:
            continue  # 未知模型（配置已删）：清扫
        status.paused_until = until
        kept[ref] = until
    return kept
