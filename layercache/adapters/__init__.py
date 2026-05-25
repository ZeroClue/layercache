"""Provider adapters package."""

from .anthropic import AnthropicAdapter
from .base import BaseAdapter
from .gemini import GeminiAdapter
from .openai import OpenAIAdapter

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


def detect_provider(model_name: str) -> str:
    """Detect the provider from a model name.

    Handles formats like:
    - "anthropic/claude-3-5-sonnet-20241022" (LiteLLM format with prefix)
    - "claude-3-5-sonnet-20241022" (direct model name)
    - "gpt-4o" (direct model name)
    """
    model_lower = model_name.lower()

    # Check for explicit prefix (provider/model format)
    if "/" in model_lower:
        prefix = model_lower.split("/")[0]
        for known_prefix, provider in PROVIDER_PREFIXES.items():
            if known_prefix == prefix:
                return provider

    # Check model name patterns (use startswith to avoid substring false positives)
    for known_prefix, provider in PROVIDER_PREFIXES.items():
        if model_lower.startswith(known_prefix):
            return provider

    # Default to OpenAI
    return "openai"


def get_adapter(provider_name: str) -> BaseAdapter:
    """Get an adapter instance for the given provider."""
    adapter_cls = ADAPTER_REGISTRY.get(provider_name)
    if adapter_cls is None:
        # Default to OpenAI adapter (automatic caching)
        return OpenAIAdapter()
    return adapter_cls()
