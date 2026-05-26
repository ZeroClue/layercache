from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from layercache.config import LayerCacheSettings


class TestReloadConfig:
    """Unit tests for the reload_config() hot-reload function."""

    def test_reload_returns_ok_with_valid_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        from layercache.main import reload_config

        config_path = tmp_path / "layercache.yaml"
        config_path.write_text("proxy:\n  log_level: debug\n")
        monkeypatch.chdir(tmp_path)

        result = reload_config()
        assert result["status"] == "ok"
        assert "warnings" in result

    def test_reload_returns_error_when_file_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        from layercache.main import reload_config

        monkeypatch.chdir(tmp_path)
        result = reload_config()
        assert result["status"] == "error"
        assert "not found" in result["error"]

    def test_reload_returns_error_on_invalid_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        from layercache.main import reload_config

        config_path = tmp_path / "layercache.yaml"
        config_path.write_text(": invalid yaml {{")
        monkeypatch.chdir(tmp_path)

        result = reload_config()
        assert result["status"] == "error"
        assert "YAML parsing failed" in result["error"]

    def test_reload_updates_global_settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        import layercache.main as m

        config_path = tmp_path / "layercache.yaml"
        config_path.write_text("proxy:\n  log_level: debug\n")
        monkeypatch.chdir(tmp_path)

        m._settings = None
        result = m.reload_config()
        assert result["status"] == "ok"
        assert m._settings is not None
        assert m._settings.proxy.log_level == "debug"

    def test_reload_applies_log_level(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        import logging

        import layercache.main as m

        logger = logging.getLogger("layercache")
        logger.setLevel(logging.INFO)

        config_path = tmp_path / "layercache.yaml"
        config_path.write_text("proxy:\n  log_level: debug\n")
        monkeypatch.chdir(tmp_path)

        m._settings = None
        m.reload_config()
        assert logger.level <= logging.DEBUG

    def test_reload_warns_on_semantic_cache_change(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        import layercache.main as m

        config_path = tmp_path / "layercache.yaml"
        config_path.write_text(
            "caching:\n  semantic:\n    enabled: true\n    db_path: /tmp/test.db\n"
        )
        monkeypatch.chdir(tmp_path)

        m._settings = LayerCacheSettings.model_validate({
            "caching": {"semantic": {"enabled": False}},
        })
        result = m.reload_config()
        assert result["status"] == "ok"
        assert any("Semantic cache" in w for w in result["warnings"])

    def test_reload_handles_stale_global_settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        import layercache.main as m

        config_path = tmp_path / "layercache.yaml"
        config_path.write_text("proxy:\n  log_level: info\n")
        monkeypatch.chdir(tmp_path)

        m._settings = None
        result = m.reload_config()
        assert result["status"] == "ok"
        assert m._settings is not None

    def test_reload_missing_proxy_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        from layercache.main import reload_config

        config_path = tmp_path / "layercache.yaml"
        config_path.write_text("caching:\n  semantic:\n    enabled: false\n")
        monkeypatch.chdir(tmp_path)

        result = reload_config()
        assert result["status"] == "ok"


class TestConfigRoutes:
    """Integration tests for the dashboard config routes."""

    @pytest.fixture(autouse=True)
    def _setup_app(self, tmp_path: pytest.TempPathFactory) -> None:
        self.app = FastAPI()
        from layercache.dashboard.router import router

        self.app.include_router(router)
        self.app.state.settings = LayerCacheSettings(
            proxy={"proxy_api_key": None}  # no auth needed
        )
        self.config_path = tmp_path / "layercache.yaml"
        self.app.state.config_path = str(self.config_path)

        def _noop_reload() -> dict:
            return {"status": "ok", "warnings": []}

        self.app.state.reload_config = _noop_reload

        self.client = TestClient(self.app)

    def _save_post(self, data: dict | None = None, **kwargs: Any) -> Any:
        """Helper: POST to config/save with HTMX headers."""
        payload = data or {"config_yaml": "proxy:\n  log_level: debug\n", "mtime": "0"}
        headers = {"HX-Request": "true"}
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        return self.client.post("/dashboard/config/save", data=payload, headers=headers, **kwargs)

    def test_config_page_returns_200(self) -> None:
        response = self.client.get("/dashboard/config")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_config_save_success(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")

        response = self._save_post()
        assert response.status_code == 200
        assert "Config saved" in response.text
        assert "hx-swap-oob" in response.text

    def test_config_save_mtime_conflict(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")

        mtime = os.stat(str(self.config_path)).st_mtime - 10
        response = self._save_post({"config_yaml": "proxy:\n  log_level: debug\n", "mtime": str(mtime)})
        assert response.status_code == 409
        assert "modified by another process" in response.text

    def test_config_save_invalid_yaml(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")

        response = self._save_post({"config_yaml": ": invalid {{ yaml", "mtime": "0"})
        assert response.status_code == 400
        assert "Invalid" in response.text

    def test_config_save_atomic_write(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")
        original_stat = os.stat(str(self.config_path))
        original_inode = original_stat.st_ino

        response = self._save_post()
        assert response.status_code == 200

        new_stat = os.stat(str(self.config_path))
        new_content = self.config_path.read_text()
        assert new_content == "proxy:\n  log_level: debug\n"
        assert new_stat.st_ino != original_inode

    def test_config_save_file_not_found(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")
        os.unlink(str(self.config_path))
        response = self._save_post()
        assert response.status_code == 400
        assert "Config file not found" in response.text

    def test_config_save_read_only(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")
        os.chmod(str(self.config_path), 0o444)

        response = self._save_post()
        os.chmod(str(self.config_path), 0o644)
        assert response.status_code == 400
        assert "read-only" in response.text

    def test_config_save_csrf_rejected_without_htmx_header(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")

        response = self.client.post(
            "/dashboard/config/save",
            data={"config_yaml": "proxy:\n  log_level: debug\n", "mtime": "0"},
            headers={"HX-Request": "false"},
        )
        assert response.status_code == 403
        assert "CSRF" in response.text

    def test_config_save_rate_limiting(self) -> None:
        self.config_path.write_text("proxy:\n  log_level: info\n")
        from layercache.dashboard.router import _rate_limit_bucket, _RATE_LIMIT_MAX

        _rate_limit_bucket.clear()
        for _ in range(_RATE_LIMIT_MAX):
            resp = self._save_post({"config_yaml": "proxy:\n  log_level: debug\n", "mtime": "0"})
            assert resp.status_code == 200

        resp = self._save_post({"config_yaml": "proxy:\n  log_level: debug\n", "mtime": "0"})
        assert resp.status_code == 429
        _rate_limit_bucket.clear()


def test_schema_generation(tmp_path: Path) -> None:
    """JSON Schema can be generated from model."""
    from layercache.schema import generate_schema, write_schema

    schema = generate_schema()
    assert schema["$schema"] == "https://json-schema.org/draft-07/schema#"
    assert schema["title"] == "LayerCache Configuration"
    assert "$defs" in schema

    dest = write_schema(str(tmp_path / "test_schema.json"))
    assert dest.exists()
    with open(dest) as f:
        reloaded = json.load(f)
    assert reloaded["title"] == "LayerCache Configuration"


def test_yaml_has_schema_reference() -> None:
    """layercache.yaml should reference the schema file."""
    config_path = Path(__file__).parent.parent / "layercache.yaml"
    assert config_path.exists()
    content = config_path.read_text()
    assert "yaml-language-server: $schema=./layercache.schema.json" in content
