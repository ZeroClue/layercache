"""Cache tier hierarchy for multi-tier caching.

Defines the cache tiers (semantic, prefix, inference) and the hierarchy logic
for cascading cache lookups.
"""

from __future__ import annotations

from enum import StrEnum


class CacheTier(StrEnum):
    """Cache tiers in the multi-tier caching hierarchy.

    Tiers are ordered from most specific (semantic) to least specific (inference).
    Cache lookups cascade through tiers until a hit occurs or all tiers are exhausted.
    """

    SEMANTIC = "semantic"
    PREFIX = "prefix"
    INFERENCE = "inference"


class CacheTierHierarchy:
    """Manages the cache tier hierarchy and lookup order.

    Implements cascading lookup: semantic → prefix → inference.
    Each tier must be checked in order; a hit at any tier short-circuits the flow.
    """

    def __init__(self) -> None:
        self._lookup_order = [
            CacheTier.SEMANTIC,
            CacheTier.PREFIX,
            CacheTier.INFERENCE,
        ]

    def get_lookup_order(self) -> list[CacheTier]:
        """Return the ordered list of cache tiers for lookup.

        Returns:
            List of CacheTier values in lookup order.
        """
        return self._lookup_order.copy()

    def next_tier(self, current_tier: CacheTier) -> CacheTier | None:
        """Return the next tier in the hierarchy.

        Args:
            current_tier: The current cache tier.

        Returns:
            The next tier, or None if current is the final tier.
        """
        try:
            current_index = self._lookup_order.index(current_tier)
            if current_index + 1 < len(self._lookup_order):
                return self._lookup_order[current_index + 1]
        except ValueError:
            pass
        return None

    def is_final_tier(self, tier: CacheTier) -> bool:
        """Check if a tier is the final tier in the hierarchy.

        Args:
            tier: The cache tier to check.

        Returns:
            True if this is the final tier (inference), False otherwise.
        """
        return tier == CacheTier.INFERENCE

    def get_tier_index(self, tier: CacheTier) -> int:
        """Get the index of a tier in the hierarchy.

        Args:
            tier: The cache tier.

        Returns:
            The tier index (0 for semantic, 1 for prefix, 2 for inference).

        Raises:
            ValueError: If tier is not recognized.
        """
        return self._lookup_order.index(tier)
