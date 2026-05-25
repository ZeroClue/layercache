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

    host: str = "0.0.0.0"
    port: int = 8000
    proxy_api_key: str | None = None
    log_level: str = "info"


class ProviderConfig(BaseModel):
    """Single LLM provider configuration."""

    api_key_env: str
    base_url: str | None = None
    default_model: str | None = None
    max_retries: int = 3
    timeout: int = 120


class ProvidersConfig(BaseModel):
    """All provider configurations."""

    anthropic: ProviderConfig | None = None
    openai: ProviderConfig | None = None
    gemini: ProviderConfig | None = None


class SemanticCacheConfig(BaseModel):
    """Semantic cache configuration."""

    enabled: bool = True
    backend: str = "sqlite"
    db_path: str = "/data/semantic_cache.db"
    default_ttl: int = 300
    similarity_threshold: float = 0.95
    embedder: str = "BAAI/bge-small-en-v1.5"


class CachingConfig(BaseModel):
    """Caching configuration."""

    semantic: SemanticCacheConfig = Field(default_factory=SemanticCacheConfig)


class EnhancementConfig(BaseModel):
    """Single enhancement plugin configuration."""

    name: str
    class_path: str
    config: dict[str, Any] = Field(default_factory=dict)


class EnhancementsConfig(BaseModel):
    """Enhancements configuration."""

    registered: list[EnhancementConfig] = Field(default_factory=list)


class LayerCacheSettings(BaseSettings):
    """Top-level LayerCache configuration."""

    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    caching: CachingConfig = Field(default_factory=CachingConfig)
    enhancements: EnhancementsConfig = Field(default_factory=EnhancementsConfig)

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
