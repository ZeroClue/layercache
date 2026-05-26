"""LayerCache configuration management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class ProxyConfig(BaseModel):
    """Proxy server configuration."""

    host: str = Field(default="0.0.0.0", description="Bind address for the HTTP server")
    port: int = Field(default=8000, ge=1, le=65535, description="Port for the HTTP server")
    proxy_api_key: str | None = Field(default=None, description="Optional bearer token to authenticate proxy clients")
    log_level: str = Field(default="info", description="Log level (debug, info, warning, error)")


class ProviderConfig(BaseModel):
    """Single LLM provider configuration."""

    api_key_env: str = Field(description="Environment variable name that holds the API key")
    base_url: str | None = Field(default=None, description="Override the default API base URL for this provider")
    default_model: str | None = Field(default=None, description="Default model to use when the request omits the model field")
    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum number of retries on transient failures")
    timeout: int = Field(default=120, ge=1, description="Request timeout in seconds")


class AnthropicProviderConfig(ProviderConfig):
    """Anthropic-specific provider configuration."""

    use_auto_cache_control: bool = Field(default=False, description="[P1 deferred] Automatically inject cache_control markers on system and historical messages")


class ProvidersConfig(BaseModel):
    """All provider configurations."""

    anthropic: AnthropicProviderConfig | None = Field(default=None, description="Anthropic provider settings")
    openai: ProviderConfig | None = Field(default=None, description="OpenAI provider settings")
    gemini: ProviderConfig | None = Field(default=None, description="Google Gemini provider settings")


class SemanticCacheConfig(BaseModel):
    """Semantic cache configuration."""

    enabled: bool = Field(default=True, description="Enable the semantic cache (embedding-based query matching)")
    backend: str = Field(default="sqlite", description="Cache backend (currently only sqlite)")
    db_path: str = Field(default="/data/semantic_cache.db", description="Path to the SQLite database file")
    default_ttl: int = Field(default=300, ge=0, description="Default TTL in seconds for cache entries (0 = no expiry)")
    similarity_threshold: float = Field(default=0.95, ge=0.0, le=1.0, description="Minimum cosine similarity score for a semantic cache hit")
    embedder: str = Field(default="BAAI/bge-small-en-v1.5", description="FastEmbed model name for generating query embeddings")


class MetricsConfig(BaseModel):
    """Metrics snapshot storage configuration."""

    db_path: str = Field(default="/data/metrics.db", description="Path to the metrics SQLite database")
    snapshot_interval_seconds: int = Field(default=60, gt=0, description="Seconds between background metric snapshots")
    snapshot_retention_days: int = Field(default=7, gt=0, description="Days to retain metric snapshots before pruning")


class CachingConfig(BaseModel):
    """Caching configuration."""

    semantic: SemanticCacheConfig = Field(default_factory=SemanticCacheConfig, description="Semantic cache settings")
    metrics: MetricsConfig = Field(default_factory=MetricsConfig, description="Metrics snapshot storage settings")
    max_session_tokens: int | None = Field(default=None, description="Optional: truncate L2 to fit within this token budget (null = no truncation)")


class EnhancementConfig(BaseModel):
    """Single enhancement plugin configuration."""

    name: str = Field(description="Unique name used to reference this enhancement in requests (lc_enhancements)")
    class_path: str = Field(description="Python dotted path to the enhancement class, e.g. layercache.enhancements.chain_of_thought.ChainOfThoughtEnhancement")
    config: dict[str, Any] = Field(default_factory=dict, description="Arbitrary keyword arguments passed to the enhancement constructor")


class EnhancementsConfig(BaseModel):
    """Enhancements configuration."""

    registered: list[EnhancementConfig] = Field(default_factory=list, description="List of registered enhancement plugins")


class LayerCacheSettings(BaseSettings):
    """Top-level LayerCache configuration."""

    proxy: ProxyConfig = Field(default_factory=ProxyConfig, description="Proxy server settings")
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig, description="LLM provider settings")
    caching: CachingConfig = Field(default_factory=CachingConfig, description="Caching behaviour")
    enhancements: EnhancementsConfig = Field(default_factory=EnhancementsConfig, description="Prompt enhancement plugins")

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
