"""配置加载与校验。

三级配置结构：服务商(providers) -> 分组(groups) -> 模型(models)。
使用者只需填写模型清单与一个保底模型(fallback)；
key 一律走环境变量（key_env 只写变量名，不落明文）。
配置顺序即机械兜底顺序。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

DEFAULT_CONFIG_PATH = Path("config/models.yaml")

CONFIG_TEMPLATE = """\
# ============================================================
# 多模型降级自修复管理系统 · 模型配置
# ------------------------------------------------------------
# 使用者只需填写两样：
#   1) providers 模型清单（服务商 -> 分组 -> 模型 三级）
#   2) fallback  保底模型（所有模型不可用时最后兜底）
# 不需要写角色、写降级链、写规则；配置顺序即机械兜底顺序。
# key 一律存放于环境变量（key_env 只填变量名，不落明文）。
# ============================================================

providers:
  - name: provider-a                    # 服务商名称（自定义）
    base_url: https://your-provider-a.example.com/v1   # 自定义模型与地址
    groups:
      - name: default                   # 分组名称（自定义）
        key_env: KEY_A                  # 存放该分组 key 的环境变量名
        models: [gpt-4o, gpt-4o-mini]   # 该分组下可用的模型

  - name: provider-b
    base_url: https://your-provider-b.example.com/v1
    groups:
      - name: main
        key_env: KEY_B
        models: [deepseek-chat, deepseek-reasoner]

# 保底模型（必填）：决策模型也全不可用时，按配置顺序逐个试调，
# 全部失败前保底模型是最后一道防线。
fallback:
  provider: provider-b
  group: main
  model: deepseek-chat
  key_env: KEY_B    # 应与所指向分组(groups[].key_env)一致
"""


class ConfigError(Exception):
    """配置无法加载或结构不合法。"""


@dataclass(frozen=True)
class Group:
    """一个 key 分组：同一 key_env 下的若干模型。"""

    name: str
    key_env: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class Provider:
    """一个自定义模型与地址（服务商）。"""

    name: str
    base_url: str
    groups: tuple[Group, ...]


@dataclass(frozen=True)
class Fallback:
    """保底模型引用与 key 环境变量名。"""

    provider: str
    group: str
    model: str
    key_env: str


@dataclass(frozen=True)
class ModelRef:
    """模型唯一寻址：服务商/分组/模型名。"""

    provider: str
    group: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.group}/{self.model}"


@dataclass(frozen=True)
class Config:
    providers: tuple[Provider, ...]
    fallback: Fallback
    source: Path | None = None
    probe_interval: int = 60  # 可选：周期探测间隔（秒），阶段5 probe.py

    def iter_model_refs(self) -> Iterator[ModelRef]:
        """按配置顺序产出全部模型（该顺序即机械兜底顺序）。"""

        for p in self.providers:
            for g in p.groups:
                for m in g.models:
                    yield ModelRef(p.name, g.name, m)

    def find_provider(self, name: str) -> Provider | None:
        for p in self.providers:
            if p.name == name:
                return p
        return None

    def find_group(self, provider: str, group: str) -> Group | None:
        p = self.find_provider(provider)
        if p is None:
            return None
        for g in p.groups:
            if g.name == group:
                return g
        return None

    def fallback_ref(self) -> ModelRef:
        return ModelRef(self.fallback.provider, self.fallback.group, self.fallback.model)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """加载并构造配置（只查结构，语义问题交给 validate_config）。"""

    path = Path(path)
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}（可先运行 mso init 生成模板）")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是映射(mapping)")
    return _build(raw, source=path)


def validate_config(config: Config) -> list[str]:
    """语义校验；返回错误列表（空列表 = 合法）。"""

    errors: list[str] = []
    if not config.providers:
        errors.append("providers 不能为空（至少配置一个服务商）")

    seen_providers: set[str] = set()
    for p in config.providers:
        if not p.name.strip():
            errors.append("存在 name 为空的服务商")
        if p.name in seen_providers:
            errors.append(f"服务商名重复：{p.name}")
        seen_providers.add(p.name)

        if not p.base_url.strip():
            errors.append(f"[{p.name}] base_url 为空")
        elif not p.base_url.startswith(("http://", "https://")):
            errors.append(f"[{p.name}] base_url 必须以 http:// 或 https:// 开头")

        if not p.groups:
            errors.append(f"[{p.name}] groups 不能为空")

        seen_groups: set[str] = set()
        for g in p.groups:
            if not g.name.strip():
                errors.append(f"[{p.name}] 存在 name 为空的分组")
            if g.name in seen_groups:
                errors.append(f"[{p.name}] 分组名重复：{g.name}")
            seen_groups.add(g.name)

            if not g.key_env.strip():
                errors.append(f"[{p.name}/{g.name}] key_env 为空（key 只走环境变量）")
            if not g.models:
                errors.append(f"[{p.name}/{g.name}] models 为空")

    fb = config.fallback
    if not fb.key_env.strip():
        errors.append("fallback.key_env 为空")
    provider = config.find_provider(fb.provider)
    if provider is None:
        errors.append(f"fallback.provider 不存在：{fb.provider}")
        return errors
    group = next((g for g in provider.groups if g.name == fb.group), None)
    if group is None:
        errors.append(f"fallback.group 不存在：{fb.provider}/{fb.group}")
        return errors
    if fb.model not in group.models:
        errors.append(f"fallback.model 不在 {fb.provider}/{fb.group} 的模型列表中：{fb.model}")
    if fb.key_env and fb.key_env != group.key_env:
        errors.append(
            f"fallback.key_env({fb.key_env}) 与分组 {fb.provider}/{fb.group} 的 "
            f"key_env({group.key_env}) 不一致"
        )
    return errors


def resolve_api_key(key_env: str) -> str | None:
    """从环境变量解析 key；未设置返回 None。值不落任何输出。"""

    value = os.environ.get(key_env)
    return value.strip() if value and value.strip() else None


def _build(raw: dict[str, Any], source: Path | None) -> Config:
    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, list) or not providers_raw:
        raise ConfigError("providers 必须是非空列表（至少一个服务商）")
    providers = tuple(_build_provider(i, p) for i, p in enumerate(providers_raw))

    fb_raw = raw.get("fallback")
    if not isinstance(fb_raw, dict):
        raise ConfigError("fallback 必填且为映射（保底模型）")
    missing = [k for k in ("provider", "group", "model", "key_env") if not str(fb_raw.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"fallback 缺少必填字段：{', '.join(missing)}")
    fallback = Fallback(
        provider=str(fb_raw["provider"]).strip(),
        group=str(fb_raw["group"]).strip(),
        model=str(fb_raw["model"]).strip(),
        key_env=str(fb_raw["key_env"]).strip(),
    )
    interval_raw = raw.get("probe_interval", 60)
    try:
        probe_interval = int(interval_raw)
    except (TypeError, ValueError):
        raise ConfigError(f"probe_interval 必须是整数（秒）：{interval_raw}")
    if probe_interval <= 0:
        raise ConfigError(f"probe_interval 必须为正整数（秒）：{probe_interval}")
    return Config(
        providers=providers, fallback=fallback, source=source, probe_interval=probe_interval
    )


def _build_provider(index: int, raw: Any) -> Provider:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers[{index}] 必须是映射")
    name = str(raw.get("name", "")).strip()
    base_url = str(raw.get("base_url", "")).strip()
    if not name:
        raise ConfigError(f"providers[{index}].name 必填")
    if not base_url:
        raise ConfigError(f"providers[{index}]（{name}）base_url 必填")

    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise ConfigError(f"[{name}] groups 必须是非空列表")
    groups: list[Group] = []
    for gi, g_raw in enumerate(groups_raw):
        if not isinstance(g_raw, dict):
            raise ConfigError(f"[{name}] groups[{gi}] 必须是映射")
        g_name = str(g_raw.get("name", "")).strip()
        g_key = str(g_raw.get("key_env", "")).strip()
        if not g_name:
            raise ConfigError(f"[{name}] groups[{gi}].name 必填")
        if not g_key:
            raise ConfigError(f"[{name}] groups[{gi}]（{g_name}）key_env 必填（key 只走环境变量）")
        models_raw = g_raw.get("models")
        if not isinstance(models_raw, list) or not models_raw:
            raise ConfigError(f"[{name}] groups[{gi}]（{g_name}）models 必须是非空列表")
        models = tuple(str(m).strip() for m in models_raw if str(m).strip())
        if not models:
            raise ConfigError(f"[{name}] groups[{gi}]（{g_name}）models 不能为空")
        groups.append(Group(name=g_name, key_env=g_key, models=models))
    return Provider(name=name, base_url=base_url, groups=tuple(groups))
