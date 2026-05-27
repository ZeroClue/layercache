"""Tests for the Prompt Registry."""

import json
from pathlib import Path

import pytest

from layercache.registry.prompt_registry import PromptRegistry, PromptTemplate


class TestPromptTemplate:
    def test_to_dict(self) -> None:
        template = PromptTemplate(
            name="test",
            version="1.0",
            description="Test template",
            l0_messages=[{"role": "system", "content": "Hello"}],
            l1_messages=[{"role": "system", "content": "Context"}],
        )
        d = template.to_dict()
        assert d["name"] == "test"
        assert d["version"] == "1.0"
        assert len(d["L0"]) == 1
        assert len(d["L1"]) == 1

    def test_from_dict(self) -> None:
        data = {
            "name": "test",
            "version": "2.0",
            "L0": [{"role": "system", "content": "Hello"}],
        }
        template = PromptTemplate.from_dict(data)
        assert template.name == "test"
        assert template.version == "2.0"
        assert len(template.l0_messages) == 1


class TestPromptRegistry:
    @pytest.fixture
    def registry_with_templates(self, tmp_path: Path) -> PromptRegistry:
        """Create a registry with test templates."""
        templates_dir = tmp_path / "prompts"
        templates_dir.mkdir()

        # Create a YAML template file
        yaml_file = templates_dir / "test_template.yaml"
        yaml_file.write_text("""
name: "test-assistant"
version: "1.0"
description: "Test assistant template"
L0:
  - role: "system"
    content: "You are a test assistant."
L1:
  - role: "system"
    content: "Additional context information."
""")

        return PromptRegistry(templates_dir=templates_dir)

    def test_load_templates_from_yaml(self, registry_with_templates: PromptRegistry) -> None:
        """Should load templates from YAML files."""
        template = registry_with_templates.get_template("test-assistant")
        assert template is not None
        assert len(template["L0"]) == 1
        assert len(template["L1"]) == 1

    def test_list_templates(self, registry_with_templates: PromptRegistry) -> None:
        templates = registry_with_templates.list_templates()
        assert len(templates) == 1
        assert templates[0]["name"] == "test-assistant"
        assert templates[0]["version"] == "1.0"

    def test_get_nonexistent_template(self, registry_with_templates: PromptRegistry) -> None:
        assert registry_with_templates.get_template("nonexistent") is None

    def test_register_template(self, tmp_path: Path) -> None:
        registry = PromptRegistry()
        template = PromptTemplate(
            name="custom",
            version="1.0",
            l0_messages=[{"role": "system", "content": "Custom system prompt"}],
        )
        registry.register_template(template)

        result = registry.get_template("custom")
        assert result is not None
        assert result["L0"][0]["content"] == "Custom system prompt"

    def test_delete_template(self, tmp_path: Path) -> None:
        registry = PromptRegistry()
        template = PromptTemplate(name="to-delete", version="1.0")
        registry.register_template(template)

        assert registry.delete_template("to-delete") is True
        assert registry.get_template("to-delete") is None

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        registry = PromptRegistry()
        assert registry.delete_template("nonexistent") is False

    def test_load_json_templates(self, tmp_path: Path) -> None:
        """Should load templates from JSON files."""
        templates_dir = tmp_path / "prompts"
        templates_dir.mkdir()

        json_file = templates_dir / "json_template.json"
        json_file.write_text(
            json.dumps(
                {
                    "name": "json-assistant",
                    "version": "1.0",
                    "description": "JSON template",
                    "L0": [{"role": "system", "content": "JSON system prompt"}],
                    "L1": [],
                }
            )
        )

        registry = PromptRegistry(templates_dir=templates_dir)
        template = registry.get_template("json-assistant")
        assert template is not None

    def test_load_multi_template_file(self, tmp_path: Path) -> None:
        """Should load multiple templates from a single file."""
        templates_dir = tmp_path / "prompts"
        templates_dir.mkdir()

        yaml_file = templates_dir / "multi.yaml"
        yaml_file.write_text("""
templates:
  - name: "template-a"
    version: "1.0"
    L0:
      - role: "system"
        content: "Template A"
  - name: "template-b"
    version: "1.0"
    L0:
      - role: "system"
        content: "Template B"
""")

        registry = PromptRegistry(templates_dir=templates_dir)
        assert registry.get_template("template-a") is not None
        assert registry.get_template("template-b") is not None

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty templates directory should not cause errors."""
        templates_dir = tmp_path / "empty"
        templates_dir.mkdir()
        registry = PromptRegistry(templates_dir=templates_dir)
        assert registry.list_templates() == []

    def test_reload(self, registry_with_templates: PromptRegistry) -> None:
        """Reload should refresh templates from disk."""
        original = registry_with_templates.get_template("test-assistant")
        assert original is not None

        # Reload should not fail
        registry_with_templates.reload()
        reloaded = registry_with_templates.get_template("test-assistant")
        assert reloaded is not None
