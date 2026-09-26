"""模型运行时状态与注册表。

从 Config 构建全部模型的运行时状态（ModelStatus），
供工具层 / 决策中枢 / CLI 查询与更新。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Config, ModelRef, resolve_api_key
from .safety import mask_env_value


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

    @property
    def key_ready(self) -> bool:
        return resolve_api_key(self.key_env) is not None

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
