"""v2 · H · P2-A · OutgoingPluginBuilder

按 (provider_id, model, base_url) 三维 predicate 装配 plugin 链。

设计参考：cherry-studio PluginBuilder 的 reverse 语义——先注册（push）的
靠前应用，等价于"最外层 wrapping"。本实现用简单的注册顺序 + predicate
过滤，不做 reverse（因为 outgoing plugin 不是 wrapping 关系，是顺序应用，
Pipeline 风格更直观）。

为什么用 predicate callable 而非 dict 路由：
- 灵活：predicate 可以匹配 model 通配符（如 "claude-opus-4-*"）、base_url
  子串（"xfyun.cn"）、provider 别名等组合条件
- 单元测试友好：每个 predicate 独立可测
- 跟 cherry-studio 的 "if (model.startsWith('qwen3-'))" 风格对齐

预定义 predicate helpers 在 `predicates.py`（待实现，按需扩展）；
本切片只提供 builder 骨架，具体 plugin 注册留 P2-B/D 切片做。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from .protocol import OutgoingPlugin

# (provider_id, model, base_url) → bool；True 表示该 plugin 应用于本请求
PluginPredicate = Callable[[str, str, Optional[str]], bool]


class OutgoingPluginBuilder:
    """按请求维度装配 outgoing plugin 链。

    用法::

        builder = OutgoingPluginBuilder()
        builder.register(
            predicate=lambda p, m, b: p == "anthropic",
            plugin=AnthropicCachePlugin(),
        )
        builder.register(
            predicate=lambda p, m, b: m.startswith("qwen3-"),
            plugin=QwenThinkingPlugin(),
        )

        plugins = builder.build("anthropic", "claude-opus-4-7", None)
        # → [AnthropicCachePlugin instance]

    注册顺序 = 应用顺序（先注册的先调用）。如果两个 plugin 都改 extras["system"]
    后注册的会看到前注册的结果——caller 注册时要意识到顺序。

    线程安全：本类不带锁。生产用法是启动期一次性注册，运行期只读 build；
    如果你要在运行期 register/unregister，自己加锁。
    """

    def __init__(self) -> None:
        self._registrations: List[Tuple[PluginPredicate, OutgoingPlugin]] = []

    def register(
        self,
        predicate: PluginPredicate,
        plugin: OutgoingPlugin,
    ) -> None:
        """注册一个 plugin + 路由 predicate。"""
        self._registrations.append((predicate, plugin))

    def build(
        self,
        provider_id: str,
        model: str,
        base_url: Optional[str] = None,
    ) -> List[OutgoingPlugin]:
        """按 (provider_id, model, base_url) 路由出 plugin 链（按注册顺序）。

        Returns:
            list[OutgoingPlugin] —— 可能空列表（无匹配 plugin）。caller 应该
            优雅处理：`for p in plugins: req = p.transform_request(req, ctx)`
            空列表时 req 不变。
        """
        return [p for pred, p in self._registrations if pred(provider_id, model, base_url)]

    def clear(self) -> None:
        """清空所有注册（测试 / 配置重载用）。"""
        self._registrations.clear()

    def list_plugin_names(self) -> List[str]:
        """诊断用：列出所有已注册 plugin 的 name。"""
        return [p.name for _pred, p in self._registrations]


# ── 进程内默认 builder（按需 lazy 注册）────────────────────────────────


_default_builder: Optional[OutgoingPluginBuilder] = None


def get_default_outgoing_plugin_builder() -> OutgoingPluginBuilder:
    """拿进程内共享 OutgoingPluginBuilder 单例（lazy）。

    P2-B/D 切片在导入时自动 register 自己负责的 plugin。caller（ApiClient）
    用本函数拿 builder，build(provider, model, base_url) 拿到具体的 plugin
    链按顺序 transform_request。
    """
    global _default_builder
    if _default_builder is None:
        _default_builder = OutgoingPluginBuilder()
    return _default_builder


def reset_default_outgoing_plugin_builder() -> None:
    """清掉默认单例（仅测试用）。"""
    global _default_builder
    _default_builder = None


__all__ = [
    "OutgoingPluginBuilder",
    "PluginPredicate",
    "get_default_outgoing_plugin_builder",
    "reset_default_outgoing_plugin_builder",
]
