"""Provider adapters package.

The registry maps provider names to adapter classes. Detection is a
two-step process:

1. If a config override exists (from layercache.yaml ``adapter:`` field),
   use that adapter directly.
2. Otherwise, check the ``PROVIDER_PREFIXES`` table for a model-name
   match. The fallback is the OpenAI adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .anthropic import AnthropicAdapter
from .base import BaseAdapter
from .gemini import GeminiAdapter
from .openai import OpenAIAdapter

if TYPE_CHECKING:
    from ..config import ProvidersConfig

__all__ = [
    "BaseAdapter",
    "AnthropicAdapter",
    "OpenAIAdapter",
    "GeminiAdapter",
]

# Provider name -> adapter class mapping
ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "gemini": GeminiAdapter,
    "google": GeminiAdapter,  # Alias
}

# Known provider prefixes in model names (e.g., "anthropic/claude-3-5-sonnet")
PROVIDER_PREFIXES: dict[str, str] = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "gpt": "openai",
    "chatgpt": "openai",
    "gemini": "gemini",
    "google": "gemini",
    "palm": "gemini",
}


def detect_provider(
    model_name: str,
    providers_config: ProvidersConfig | None = None,
) -> str:
    """Detect the provider from a model name.

    When *providers_config* is supplied, configured adapters take
    precedence over the built-in prefix table.
    """
    model_lower = model_name.lower()

    # Check the config for an explicit adapter override.
    # First try exact match of the model prefix (before /), then
    # check if any config key is a prefix of the entire model name.
    if providers_config:
        if "/" in model_lower:
            prefix = model_lower.split("/")[0]
            if prefix in providers_config.root:
                return prefix
        for key in providers_config.root:
            if model_lower.startswith(key):
                return key

    # Check for explicit prefix (provider/model format)
    if "/" in model_lower:
        prefix = model_lower.split("/")[0]
        for known_prefix, provider in PROVIDER_PREFIXES.items():
            if known_prefix == prefix:
                return provider

    # Check model name patterns
    for known_prefix, provider in PROVIDER_PREFIXES.items():
        if model_lower.startswith(known_prefix):
            return provider

    # Check for Ollama Cloud models (suffixed with :cloud)
    if model_lower.endswith(":cloud") and providers_config:
        if "ollama-cloud" in providers_config.root:
            return "ollama-cloud"

    # Fallback: model has no provider prefix and no known pattern matched.
    # If "openai" is not explicitly configured with a base_url, check
    # other configured providers. This handles AI SDK providers that
    # strip their prefix from the model name (e.g. opencode-go sends
    # "deepseek-v4-flash" instead of "opencode-go/deepseek-v4-flash").
    if providers_config:
        openai_cfg = providers_config.root.get("openai")
        if not openai_cfg or not openai_cfg.base_url:
            known_openai_prefixes = {
                "gpt-",
                "chatgpt-",
                "o1-",
                "o3-",
                "o4-",
                "text-embedding-",
                "tts-",
                "whisper-",
            }
            if not any(model_lower.startswith(p) for p in known_openai_prefixes):
                for key, cfg in providers_config.root.items():
                    if key != "openai" and cfg.base_url:
                        return key

    # Default to OpenAI
    return "openai"


def get_adapter(
    provider_name: str,
    providers_config: ProvidersConfig | None = None,
) -> BaseAdapter:
    """Get an adapter instance for the given provider.

    If *providers_config* is supplied and contains an *adapter* override
    for *provider_name*, that override is used.
    """
    resolved = provider_name
    if providers_config and provider_name in providers_config.root:
        resolved = providers_config.adapter_for(provider_name)
    adapter_cls = ADAPTER_REGISTRY.get(resolved)
    if adapter_cls is None:
        return OpenAIAdapter()
    return adapter_cls()
