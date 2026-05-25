"""Semantic Cache package."""

from .embedder import Embedder, get_embedder
from .semantic import SemanticCache, cosine_similarity

__all__ = [
    "SemanticCache",
    "cosine_similarity",
    "Embedder",
    "get_embedder",
]
