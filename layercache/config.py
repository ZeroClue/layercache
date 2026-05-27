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

    api_key_env: str = Field(description="Env var name holding the provider API key")
    base_url: str | None = Field(default=None, description="Override the default API base URL")
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
                    key,
                    validated,
                    ", ".join(sorted(VALID_ADAPTERS)),
                )
                return "openai"
            return validated
        if key in VALID_ADAPTERS:
            return key
        return "openai"


class MultiTierConfig(BaseModel):
    """Multi-tier caching configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable multi-tier cache hierarchy (semantic → prefix → inference)",
    )
    validation_latency_budget_ms: int = Field(
        default=50,
        ge=1,
        description="Maximum validation latency budget in milliseconds (p95)",
    )
    probation_threshold: int = Field(
        default=10,
        ge=1,
        description="Number of successful validations before promotion from probation",
    )
    probation_auto_promotion_seconds: int = Field(
        default=3600,
        ge=60,
        description="Seconds before auto-promotion from probation regardless of count",
    )
    max_probation_entries: int = Field(
        default=1000,
        ge=100,
        description="Maximum entries to keep in probation (LRU eviction)",
    )


class SemanticCacheConfig(BaseModel):
    """Semantic cache configuration."""

    enabled: bool = Field(default=True, description="Enable embedding-based semantic cache")
    backend: str = Field(
        default="sqlite",
        pattern="^(sqlite|redis)$",
        description="Cache backend (sqlite or redis)",
    )
    db_path: str = Field(
        default="/data/semantic_cache.db",
        description="Path to the SQLite database file",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL (used when backend=redis)",
    )
    redis_pool_size: int = Field(default=10, ge=1, le=100, description="Redis connection pool size")
    redis_timeout: float = Field(default=5.0, gt=0, description="Redis socket timeout in seconds")
    default_ttl: int = Field(
        default=3600, ge=0, description="Default TTL in seconds (0 = no expiry)"
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
    session_isolation: bool = Field(
        default=True,
        description="Isolate cache entries by session ID (prevents cross-session pollution)",
    )
    session_id_header: str = Field(
        default="X-Session-ID",
        description="HTTP header name for session ID (auto-generated if missing)",
    )
    session_id_auto_generate: bool = Field(
        default=True,
        description="Auto-generate session ID if not provided by client",
    )
    multi_tier: MultiTierConfig = Field(
        default_factory=MultiTierConfig,
        description="Multi-tier caching hierarchy settings",
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
    truncation_strategy: str = Field(
        default="recent",
        pattern="^(recent|important|trim|semantic)$",
        description="Truncation strategy for session management (semantic deferred to v1.6)",
    )
    token_counter: str = Field(
        default="tiktoken",
        pattern="^(tiktoken|char_estimate)$",
        description="Token counting method (tiktoken = accurate, char_estimate = fast approx)",
    )
    litellm_model: str = Field(
        default="gpt-4o",
        description="Model for LiteLLM trim_messages (trim strategy)",
    )


class EnhancementConfig(BaseModel):
    """Single enhancement plugin configuration."""

    name: str = Field(description="Reference name used in lc_enhancements requests")
    class_path: str = Field(description="Python dotted path to the enhancement class")
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

    proxy: ProxyConfig = Field(default_factory=ProxyConfig, description="Proxy server settings")
    providers: ProvidersConfig = Field(
        default_factory=ProvidersConfig,
        description="LLM provider settings (dict of name -> config)",
    )
    caching: CachingConfig = Field(default_factory=CachingConfig, description="Caching behaviour")
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
