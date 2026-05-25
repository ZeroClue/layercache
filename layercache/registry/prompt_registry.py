"""Prompt Registry - Manages named, versioned prompt templates.

The Prompt Registry stores L0 (System) and L1 (Context) prompt templates
that clients can reference by name. This guarantees 100% prefix match
across all requests using the same template.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class PromptTemplate:
    """A named, versioned prompt template containing L0 and L1 layers."""

    def __init__(
        self,
        name: str,
        version: str = "1.0",
        description: str = "",
        l0_messages: list[dict[str, str]] | None = None,
        l1_messages: list[dict[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.l0_messages = l0_messages or []
        self.l1_messages = l1_messages or []
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize template to dictionary."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "L0": self.l0_messages,
            "L1": self.l1_messages,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptTemplate:
        """Deserialize template from dictionary."""
        return cls(
            name=data.get("name", "unnamed"),
            version=data.get("version", "1.0"),
            description=data.get("description", ""),
            l0_messages=data.get("L0", []),
            l1_messages=data.get("L1", []),
            metadata=data.get("metadata", {}),
        )


class PromptRegistry:
    """File-based prompt template registry with hot-reload support.

    Templates are stored as YAML or JSON files in a directory.
    The registry watches for file changes and reloads automatically.
    """

    def __init__(self, templates_dir: str | Path | None = None) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        self._templates_dir = Path(templates_dir) if templates_dir else None
        self._load_all()

    def _load_all(self) -> None:
        """Load all templates from the templates directory."""
        if not self._templates_dir or not self._templates_dir.exists():
            logger.info("Templates directory not found: %s", self._templates_dir)
            return

        for file_path in sorted(self._templates_dir.glob("*.yaml")):
            self._load_file(file_path)
        for file_path in sorted(self._templates_dir.glob("*.yml")):
            self._load_file(file_path)
        for file_path in sorted(self._templates_dir.glob("*.json")):
            self._load_file(file_path)

        logger.info("Loaded %d prompt templates from %s", len(self._templates), self._templates_dir)

    def _load_file(self, file_path: Path) -> None:
        """Load templates from a single file.

        Supports both single template and multi-template files.
        """
        try:
            with open(file_path, encoding="utf-8") as f:
                if file_path.suffix in (".yaml", ".yml"):
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)

            if data is None:
                return

            if isinstance(data, list):
                for item in data:
                    template = PromptTemplate.from_dict(item)
                    self._templates[template.name] = template
            elif isinstance(data, dict):
                if "templates" in data:
                    # Multi-template file with "templates" key
                    for item in data["templates"]:
                        template = PromptTemplate.from_dict(item)
                        self._templates[template.name] = template
                else:
                    # Single template file
                    template = PromptTemplate.from_dict(data)
                    self._templates[template.name] = template

        except Exception as e:
            logger.error("Failed to load template from %s: %s", file_path, e)

    def get_template(self, name: str, version: str | None = None) -> dict[str, Any] | None:
        """Get a template by name (and optional version).

        Returns the template data as a dict with 'L0' and 'L1' keys.
        """
        template = self._templates.get(name)
        if template is None:
            return None

        if version and template.version != version:
            # Look for specific version
            for t in self._templates.values():
                if t.name == name and t.version == version:
                    return t.to_dict()
            return None

        return template.to_dict()

    def list_templates(self) -> list[dict[str, str]]:
        """List all registered templates."""
        return [
            {
                "name": t.name,
                "version": t.version,
                "description": t.description,
            }
            for t in self._templates.values()
        ]

    def register_template(self, template: PromptTemplate) -> None:
        """Register a new template or update an existing one."""
        self._templates[template.name] = template

    def delete_template(self, name: str) -> bool:
        """Delete a template by name."""
        if name in self._templates:
            del self._templates[name]
            return True
        return False

    def reload(self) -> None:
        """Reload all templates from disk."""
        self._templates.clear()
        self._load_all()
