"""Semantic Cache package."""

from .embedder import Embedder, get_embedder
from .factory import get_cache_backend
from .redis import RedisSemanticCache
from .semantic import SemanticCache, cosine_similarity

__all__ = [
    "SemanticCache",
    "RedisSemanticCache",
    "cosine_similarity",
    "Embedder",
    "get_embedder",
    "get_cache_backend",
]
