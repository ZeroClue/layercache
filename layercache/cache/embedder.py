"""Embedding service using FastEmbed for lightweight local text embeddings.

Used by the Semantic Cache and Dynamic Few-Shot enhancement to generate
query embeddings without external API calls.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

# Global embedder instance (lazy-loaded)
_embedder_instance: Any = None
_model_name: str = "BAAI/bge-small-en-v1.5"

# Subprocess-level model cache — persists across calls within each worker
_subprocess_embedders: dict[str, Any] = {}


def _get_embedder(model_name: str) -> Any:
    """Get or create a FastEmbed model, cached in the subprocess."""
    if model_name not in _subprocess_embedders:
        from fastembed import TextEmbedding

        _subprocess_embedders[model_name] = TextEmbedding(model_name)
    return _subprocess_embedders[model_name]


def _embed_texts_sync(model_name: str, texts: list[str]) -> list[list[float]]:
    """Synchronous embedding function for use in executor."""
    embedder = _get_embedder(model_name)
    embeddings = list(embedder.embed(texts))
    return [e.tolist() for e in embeddings]


def _embed_single_sync(model_name: str, text: str) -> list[float]:
    """Embed a single text synchronously."""
    results = _embed_texts_sync(model_name, [text])
    return results[0]


class Embedder:
    """Wrapper around FastEmbed providing async embedding generation.

    Uses a ProcessPoolExecutor to avoid blocking the async event loop
    during CPU-bound embedding computation.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        max_workers: int = 2,
    ) -> None:
        # Verify fastembed is available at construction time
        try:
            from fastembed import TextEmbedding  # noqa: F401
        except ImportError:
            raise ImportError(
                "fastembed is required for semantic caching. Install with: pip install fastembed"
            )

        self.model_name = model_name
        self._executor = ProcessPoolExecutor(max_workers=max_workers)
        self._dimension = 384  # bge-small-en-v1.5 dimension

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string.

        Runs in a subprocess to avoid blocking the event loop.

        Args:
            text: The text to embed.

        Returns:
            Embedding vector as a list of floats.

        Raises:
            RuntimeError: If the subprocess task fails.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._executor,
            partial(_embed_single_sync, self.model_name, text),
        )
        return result

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed.

        Returns:
            List of embedding vectors.

        Raises:
            RuntimeError: If the subprocess task fails.
        """
        if not texts:
            return []

        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            self._executor,
            partial(_embed_texts_sync, self.model_name, texts),
        )
        return results

    @property
    def dimension(self) -> int:
        """Return the embedding dimension."""
        return self._dimension

    def shutdown(self) -> None:
        """Shutdown the executor."""
        if self._executor:
            self._executor.shutdown(wait=False)


def get_embedder(model_name: str = "BAAI/bge-small-en-v1.5") -> Embedder:
    """Get or create a global Embedder instance."""
    global _embedder_instance, _model_name
    if _embedder_instance is None or _model_name != model_name:
        _embedder_instance = Embedder(model_name=model_name)
        _model_name = model_name
    return _embedder_instance
