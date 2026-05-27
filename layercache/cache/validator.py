"""Cache validation layer for multi-tier caching.

Provides intent hash computation, entity extraction, and validation decision tree
for determining cache validity. Target latency: <50ms (p95).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    """Result of a cache validation check.

    Attributes:
        is_match: Whether the validation passed.
        validation_stage: Which stage determined the result (intent_hash, entities).
        latency_ms: Time taken for validation in milliseconds.
        details: Additional details about the validation.
    """

    is_match: bool
    validation_stage: str
    latency_ms: float = 0.0
    details: dict[str, str | list[str]] = field(default_factory=dict)


class IntentHashValidator:
    """Validates cache entries using intent hash comparison.

    Intent hash is computed as SHA-256 of the normalized query:
    - Lowercase
    - Strip extra whitespace
    - Sort entities for order-independence
    """

    def __init__(self) -> None:
        self._entity_extractor = EntityExtractor()

    def compute_intent_hash(self, query: str) -> str:
        """Compute SHA-256 intent hash of a normalized query.

        Normalization steps:
        1. Convert to lowercase
        2. Strip leading/trailing whitespace
        3. Normalize internal whitespace
        4. Extract and sort entities

        Args:
            query: The user query string.

        Returns:
            SHA-256 hex digest of the normalized query.
        """
        normalized = self._normalize_query(query)
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _normalize_query(self, query: str) -> str:
        """Normalize a query for consistent hashing.

        Args:
            query: The raw query string.

        Returns:
            Normalized query string.
        """
        normalized = query.lower().strip()
        normalized = re.sub(r"\s+", " ", normalized)

        entities = self._entity_extractor.extract(query)
        if entities:
            sorted_entities = sorted(set(entities))
            entity_str = ",".join(sorted_entities)
            normalized_query_text = normalized
            for entity in sorted(sorted_entities, key=len, reverse=True):
                normalized_query_text = normalized_query_text.replace(entity.lower(), "<ENTITY>")
            normalized = f"{normalized_query_text} [entities:{entity_str}]"

        return normalized

    def validate(self, cached_query: str, incoming_query: str) -> ValidationResult:
        """Validate if an incoming query matches a cached query.

        Args:
            cached_query: The query from the cache entry.
            incoming_query: The new incoming query.

        Returns:
            ValidationResult indicating match status and latency.
        """
        start_time = time.time()

        cached_hash = self.compute_intent_hash(cached_query)
        incoming_hash = self.compute_intent_hash(incoming_query)

        latency_ms = (time.time() - start_time) * 1000

        is_match = cached_hash == incoming_hash

        return ValidationResult(
            is_match=is_match,
            validation_stage="intent_hash",
            latency_ms=latency_ms,
            details={
                "cached_hash": cached_hash[:16],
                "incoming_hash": incoming_hash[:16],
            },
        )


class EntityExtractor:
    """Extracts entities from queries for validation.

    Uses regex-based extraction (v1.6) for:
    - URLs
    - Numbers
    - Code snippets
    - Dates

    Future versions (v1.7) may use LLM-based extraction.
    """

    def __init__(self) -> None:
        self._patterns = {
            "url": re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]*[^\s<>\"{}|\\^`\[\]]?.?"),
            "number": re.compile(r"\b\d+(?:\.\d+)?\b"),
            "code_snippet": re.compile(r"\b(?:def|class|function|if|for|while)\s+\w+"),
            "date": re.compile(r"\d{4}-\d{2}-\d{2}"),
            "word_entity": re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b"),
        }

    def extract(self, query: str) -> list[str]:
        """Extract entities from a query.

        Args:
            query: The query string to extract entities from.

        Returns:
            List of extracted entities (deduplicated, sorted).
        """
        if not query:
            return []

        entities: list[str] = []

        for pattern_name, pattern in self._patterns.items():
            matches = pattern.findall(query)
            for match in matches:
                cleaned = match.rstrip("?.!,;:")
                if cleaned:
                    entities.append(cleaned)

        return sorted(set(entities))
