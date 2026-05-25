"""Dynamic Few-Shot enhancement.

Retrieves the most relevant few-shot examples from a local vector store
based on the user's query (L4) and injects them at L3.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from ..models import StratifiedPrompt
from .base import BaseEnhancement

logger = logging.getLogger(__name__)


class DynamicFewShotEnhancement(BaseEnhancement):
    """Dynamically retrieves and injects relevant few-shot examples.

    Uses a lightweight local vector store (FAISS-compatible numpy arrays)
    to find the top-K most relevant examples based on the user query embedding.

    Examples are stored in a JSON file with embeddings pre-computed.
    """

    name = "dynamic_few_shot"

    def __init__(
        self,
        examples_path: str | Path | None = None,
        top_k: int = 3,
        embedder: Any = None,
    ) -> None:
        """Initialize the dynamic few-shot enhancement.

        Args:
            examples_path: Path to JSON file with few-shot examples.
            top_k: Number of examples to retrieve.
            embedder: Embedding function or model for query encoding.
        """
        self._examples_path = Path(examples_path) if examples_path else None
        self._top_k = top_k
        self._embedder = embedder
        self._examples: list[dict[str, Any]] = []
        self._embeddings: np.ndarray | None = None
        self._load_examples()

    def _load_examples(self) -> None:
        """Load few-shot examples from the JSON file."""
        if not self._examples_path or not self._examples_path.exists():
            logger.warning(
                "Few-shot examples file not found at %s, dynamic few-shot disabled",
                self._examples_path,
            )
            return

        try:
            with open(self._examples_path, encoding="utf-8") as f:
                data = json.load(f)

            self._examples = data if isinstance(data, list) else data.get("examples", [])

            # Load pre-computed embeddings if available
            if self._examples and "embedding" in self._examples[0]:
                embeddings = []
                for ex in self._examples:
                    emb = ex.get("embedding", [])
                    if emb:
                        embeddings.append(emb)
                if embeddings:
                    self._embeddings = np.array(embeddings, dtype=np.float32)

            logger.info(
                "Loaded %d few-shot examples from %s",
                len(self._examples),
                self._examples_path,
            )
        except Exception as e:
            logger.error("Failed to load few-shot examples: %s", e)

    async def _compute_query_embedding(self, query: str) -> np.ndarray | None:
        """Compute embedding for the user query."""
        if self._embedder is None:
            return None

        try:
            if hasattr(self._embedder, "embed"):
                result = self._embedder.embed(query)
                if isinstance(result, list):
                    return np.array(result, dtype=np.float32)
                elif hasattr(result, "tolist"):
                    return np.array(result.tolist(), dtype=np.float32)
            elif callable(self._embedder):
                result = self._embedder(query)
                return np.array(result, dtype=np.float32)
        except Exception as e:
            logger.error("Failed to compute query embedding: %s", e)

        return None

    def _find_similar_examples(self, query_embedding: np.ndarray) -> list[dict[str, Any]]:
        """Find the top-K most similar examples using cosine similarity."""
        if self._embeddings is None or len(self._examples) == 0:
            return self._examples[: self._top_k]

        # Compute cosine similarity
        query_norm = np.linalg.norm(query_embedding)
        if query_norm == 0:
            return self._examples[: self._top_k]

        query_normalized = query_embedding / query_norm
        embedding_norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        embedding_norms = np.where(embedding_norms == 0, 1, embedding_norms)
        embeddings_normalized = self._embeddings / embedding_norms

        similarities = np.dot(embeddings_normalized, query_normalized)

        # Get top-K indices
        top_indices = np.argsort(similarities)[::-1][: self._top_k]

        return [self._examples[i] for i in top_indices if i < len(self._examples)]

    async def apply_async(self, prompt: StratifiedPrompt, **kwargs: Any) -> StratifiedPrompt:
        """Async version of apply for embedding computation."""
        query = prompt.get_user_query()
        if not query or not self._examples:
            return prompt

        # Try to get query embedding
        query_embedding = await self._compute_query_embedding(query)

        if query_embedding is not None:
            examples = self._find_similar_examples(query_embedding)
        else:
            # Fall back to first K examples if no embedding available
            examples = self._examples[: self._top_k]

        # Inject examples at the beginning of L3 (before user query)
        for i, ex in enumerate(reversed(examples)):
            input_text = ex.get("input", ex.get("question", ex.get("user", "")))
            output_text = ex.get("output", ex.get("answer", ex.get("assistant", "")))

            if input_text and output_text:
                self._add_enhancement_pair(
                    prompt,
                    user_content=f"Example {len(examples) - i}:\n{input_text}",
                    assistant_content=output_text,
                    insert_at_start=True,
                )

        if examples:
            self._add_enhancement_message(
                prompt,
                role="user",
                content="Using the above examples as reference, please answer the following:",
                insert_at_start=True,
            )
            self._add_enhancement_message(
                prompt,
                role="assistant",
                content="Understood, I will use the provided examples as reference.",
                insert_at_start=True,
            )

        return prompt

    def apply(self, prompt: StratifiedPrompt, **kwargs: Any) -> StratifiedPrompt:
        """Apply dynamic few-shot examples at L3.

        Note: For async embedding computation, use `apply_async` instead.
        This synchronous version falls back to simple retrieval.
        """
        query = prompt.get_user_query()
        if not query or not self._examples:
            return prompt

        # Synchronous fallback: use first K examples
        examples = self._examples[: self._top_k]

        for i, ex in enumerate(reversed(examples)):
            input_text = ex.get("input", ex.get("question", ex.get("user", "")))
            output_text = ex.get("output", ex.get("answer", ex.get("assistant", "")))

            if input_text and output_text:
                self._add_enhancement_pair(
                    prompt,
                    user_content=f"Example {len(examples) - i}:\n{input_text}",
                    assistant_content=output_text,
                    insert_at_start=True,
                )

        if examples:
            self._add_enhancement_message(
                prompt,
                role="user",
                content="Using the above examples as reference, please answer the following:",
                insert_at_start=True,
            )
            self._add_enhancement_message(
                prompt,
                role="assistant",
                content="Understood, I will use the provided examples as reference.",
                insert_at_start=True,
            )

        return prompt
