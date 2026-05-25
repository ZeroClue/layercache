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
_executor: ProcessPoolExecutor | None = None


def _init_embedder(model_name: str) -> Any:
    """Initialize the FastEmbed model in a subprocess."""
    try:
        from fastembed import TextEmbedding
        return TextEmbedding(model_name)
    except ImportError:
        logger.warning(
            "fastembed not installed, using fallback embedding. "
            "Install with: pip install fastembed"
        )
        return None


def _embed_texts_sync(model_name: str, texts: list[str]) -> list[list[float]]:
    """Synchronous embedding function for use in executor."""
    try:
        from fastembed import TextEmbedding
        embedder = TextEmbedding(model_name)
        embeddings = list(embedder.embed(texts))
        return [e.tolist() for e in embeddings]
    except Exception as e:
        logger.error("Embedding failed: %s", e)
        # Return zero vectors as fallback
        return [[0.0] * 384 for _ in texts]


def _embed_single_sync(model_name: str, text: str) -> list[float]:
    """Embed a single text synchronously."""
    results = _embed_texts_sync(model_name, [text])
    return results[0] if results else [0.0] * 384


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
        """
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor,
                partial(_embed_single_sync, self.model_name, text),
            )
            return result
        except Exception as e:
            logger.error("Async embedding failed: %s", e)
            return [0.0] * self._dimension

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed.

        Returns:
            List of embedding vectors.
        """
        if not texts:
            return []

        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(
                self._executor,
                partial(_embed_texts_sync, self.model_name, texts),
            )
            return results
        except Exception as e:
            logger.error("Async batch embedding failed: %s", e)
            return [[0.0] * self._dimension for _ in texts]

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
