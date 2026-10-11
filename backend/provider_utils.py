"""Provider validation and utility functions."""

from typing import Optional
from .providers import PROVIDER_REGISTRY, ProviderInfo


def validate_provider(provider_key: str) -> bool:
    """Check if a provider key exists in the registry."""
    return provider_key in PROVIDER_REGISTRY


def get_provider_info(provider_key: str) -> Optional[ProviderInfo]:
    """Get provider information from the registry."""
    return PROVIDER_REGISTRY.get(provider_key)


def get_provider_display_name(provider_key: str) -> str:
    """Get the display name for a provider, fallback to key if not found."""
    info = get_provider_info(provider_key)
    return info.name if info else provider_key
