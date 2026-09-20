"""柳如烟 provider 配置模块。

负责：
- provider 元数据 schema（ProviderConfig / ModelEntry）
- 内置 provider 种子（builtin.py）
- 加载 / 保存 providers 配置（store.py，含向下兼容迁移）
- 模型发现（P2 fetchers.py）

设计参考：
- _research/cherry-studio-main/src/renderer/src/aiCore/services/listModels.ts
- _research/QwenPaw-main/src/qwenpaw/providers/provider_manager.py
"""
from .schema import ProviderConfig, ModelEntry
from .builtin import BUILTIN_PROVIDERS
from .store import load_providers_config, save_providers_config
# 老 backend/providers.py（静态 PROVIDER_REGISTRY）搬到这里后，通过 re-export
# 让 backend/provider_utils.py 的 `from .providers import PROVIDER_REGISTRY,
# ProviderInfo` 继续生效。避免一次大范围 import 路径迁移。
from .legacy_registry import PROVIDER_REGISTRY, ProviderInfo

__all__ = [
    "ProviderConfig",
    "ModelEntry",
    "BUILTIN_PROVIDERS",
    "load_providers_config",
    "save_providers_config",
    "PROVIDER_REGISTRY",
    "ProviderInfo",
]
