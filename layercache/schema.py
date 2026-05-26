"""JSON Schema generation for layercache.yaml configuration."""

from __future__ import annotations

import json
from pathlib import Path

from .config import LayerCacheSettings


def generate_schema() -> dict[str, object]:
    """Generate a JSON Schema document from the LayerCacheSettings model.

    The schema is annotated with a $comment so YAML editors can associate
    it with layercache.yaml automatically.
    """
    schema = LayerCacheSettings.model_json_schema()
    schema["$id"] = "https://layercache.ai/schemas/layercache.schema.json"
    schema["$schema"] = "https://json-schema.org/draft-07/schema#"
    schema["title"] = "LayerCache Configuration"
    schema["description"] = "Configuration schema for layercache.yaml"
    return schema


def write_schema(path: str | Path = "layercache.schema.json") -> Path:
    """Generate and write the schema to *path*."""
    schema = generate_schema()
    dest = Path(path)
    dest.write_text(json.dumps(schema, indent=2) + "\n")
    return dest


if __name__ == "__main__":
    dest = write_schema()
    print(f"Schema written to {dest}")


def cli() -> None:
    """CLI entry point for 'layercache-schema'."""
    dest = write_schema()
    print(f"Schema written to {dest}")
