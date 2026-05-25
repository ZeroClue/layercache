"""Enhancements package."""

from .base import BaseEnhancement, EnhancementRegistry
from .chain_of_thought import ChainOfThoughtEnhancement
from .dynamic_few_shot import DynamicFewShotEnhancement
from .self_critique import SelfCritiqueEnhancement
from .structured_output import StructuredOutputEnhancement

__all__ = [
    "BaseEnhancement",
    "EnhancementRegistry",
    "ChainOfThoughtEnhancement",
    "StructuredOutputEnhancement",
    "SelfCritiqueEnhancement",
    "DynamicFewShotEnhancement",
]


def create_default_registry() -> EnhancementRegistry:
    """Create an enhancement registry pre-loaded with all built-in enhancements."""
    registry = EnhancementRegistry()
    registry.register(ChainOfThoughtEnhancement())
    registry.register(StructuredOutputEnhancement())
    registry.register(SelfCritiqueEnhancement())
    # DynamicFewShot needs embedder - registered later if configured
    return registry
