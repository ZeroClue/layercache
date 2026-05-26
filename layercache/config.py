"""LayerCache configuration management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, RootModel
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Valid adapter names (must match keys in adapters/__init__.py ADAPTER_REGISTRY)
VALID_ADAPTERS = frozenset({"anthropic", "openai", "gemini"})


class ProxyConfig(BaseModel):
    """Proxy server configuration."""

    host: str = Field(default="0.0.0.0", description="Bind address for the HTTP server")
    port: int = Field(default=8000, ge=1, le=65535, description="Port for the HTTP server")
    proxy_api_key: str | None = Field(
        default=None, description="Bearer token for client authentication"
    )
    log_level: str = Field(default="info", description="Log level (debug, info, warning, error)")


class ProviderConfig(BaseModel):
    """Single LLM provider configuration."""

    api_key_env: str = Field(
        description="Env var name holding the provider API key"
    )
    base_url: str | None = Field(
        default=None, description="Override the default API base URL"
    )
    default_model: str | None = Field(
        default=None, description="Default model if the request omits the model field"
    )
    max_retries: int = Field(
        default=3, ge=0, le=10, description="Maximum retries on transient failures"
    )
    timeout: int = Field(default=120, ge=1, description="Request timeout in seconds")
    adapter: str | None = Field(
        default=None,
        description=f"Cache adapter ({', '.join(sorted(VALID_ADAPTERS))}); "
        "auto-detected from model name if unset",
    )


class ProvidersConfig(RootModel[dict[str, ProviderConfig]]):
    """All provider configurations — a dict of provider name -> config.

    Keys are arbitrary labels (e.g. \"anthropic\", \"openai\", \"deepseek\").
    Each value configures how the proxy connects to that provider.
    """

    root: dict[str, ProviderConfig] = {}

    def first(self) -> ProviderConfig | None:
        """Return the first configured provider, or None."""
        for v in self.root.values():
            return v
        return None

    def adapter_for(self, key: str) -> str:
        """Resolve the effective adapter name for a provider key.

        Returns the explicit adapter if set, otherwise the key itself
        (convention: provider keys match adapter names for first-party
        providers like \"anthropic\", \"openai\", \"gemini\").
        """
        cfg = self.root.get(key)
        if cfg and cfg.adapter:
            validated = cfg.adapter
            if validated not in VALID_ADAPTERS:
                logger.warning(
                    "Provider %r has unknown adapter %r, falling back to openai. "
                    "Valid adapters: %s",
                    key, validated, ", ".join(sorted(VALID_ADAPTERS)),
                )
                return "openai"
            return validated
        if key in VALID_ADAPTERS:
            return key
        return "openai"


class SemanticCacheConfig(BaseModel):
    """Semantic cache configuration."""

    enabled: bool = Field(
        default=True, description="Enable embedding-based semantic cache"
    )
    backend: str = Field(
        default="sqlite",
        pattern="^(sqlite|redis)$",
        description="Cache backend (sqlite or redis)",
    )
    db_path: str = Field(
        default="/data/semantic_cache.db",
        description="Path to the SQLite database file",
    )
    default_ttl: int = Field(
        default=300, ge=0, description="Default TTL in seconds (0 = no expiry)"
    )
    similarity_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity for a cache hit",
    )
    embedder: str = Field(
        default="BAAI/bge-small-en-v1.5",
        description="FastEmbed model for query embeddings",
    )


class MetricsConfig(BaseModel):
    """Metrics snapshot storage configuration."""

    db_path: str = Field(
        default="/data/metrics.db",
        description="Path to the metrics SQLite database",
    )
    snapshot_interval_seconds: int = Field(
        default=60, gt=0, description="Seconds between metric snapshots"
    )
    snapshot_retention_days: int = Field(
        default=7, gt=0, description="Days to retain metric snapshots"
    )


class CachingConfig(BaseModel):
    """Caching configuration."""

    semantic: SemanticCacheConfig = Field(
        default_factory=SemanticCacheConfig, description="Semantic cache settings"
    )
    metrics: MetricsConfig = Field(
        default_factory=MetricsConfig,
        description="Metrics snapshot storage settings",
    )
    max_session_tokens: int | None = Field(
        default=None,
        description="Max L2 tokens before truncation (null = no limit)",
    )


class EnhancementConfig(BaseModel):
    """Single enhancement plugin configuration."""

    name: str = Field(
        description="Reference name used in lc_enhancements requests"
    )
    class_path: str = Field(
        description="Python dotted path to the enhancement class"
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments passed to the constructor",
    )


class EnhancementsConfig(BaseModel):
    """Enhancements configuration."""

    registered: list[EnhancementConfig] = Field(
        default_factory=list, description="Registered enhancement plugins"
    )


class LayerCacheSettings(BaseSettings):
    """Top-level LayerCache configuration."""

    proxy: ProxyConfig = Field(
        default_factory=ProxyConfig, description="Proxy server settings"
    )
    providers: ProvidersConfig = Field(
        default_factory=ProvidersConfig,
        description="LLM provider settings (dict of name -> config)",
    )
    caching: CachingConfig = Field(
        default_factory=CachingConfig, description="Caching behaviour"
    )
    enhancements: EnhancementsConfig = Field(
        default_factory=EnhancementsConfig,
        description="Prompt enhancement plugins",
    )

    @classmethod
    def from_yaml(cls, path: str | Path) -> LayerCacheSettings:
        """Load configuration from a YAML file."""
        config_path = Path(path)
        if not config_path.exists():
            logger.warning("Config file %s not found, using defaults", path)
            return cls()

        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        return cls.model_validate(raw)
