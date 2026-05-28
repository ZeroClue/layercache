from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import threading
from collections import deque
from pathlib import Path
from time import time
from typing import Any

import litellm
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..registry.prompt_registry import PromptTemplate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Jinja2 globals available in all dashboard templates
templates.env.globals["app_version"] = __version__

# Serialize config saves to prevent races between concurrent POSTs
_config_save_lock = asyncio.Lock()

# Simple in-memory rate limiter for config save (local-only safety)
_rate_limit_bucket: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 10


class LogRingBuffer(logging.Handler):
    """Captures the last N log records for the dashboard logs page."""

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__()
        self.buffer: deque[logging.LogRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self.buffer.append(record)


_log_ring = LogRingBuffer()
_log_ring.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))


def _auth_check(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None)
    if not settings or not settings.proxy.proxy_api_key:
        return True
    session = getattr(request.state, "session", {})
    return session.get("authenticated", False)


def _redirect_login() -> RedirectResponse:
    return RedirectResponse(url="/dashboard/login", status_code=303)


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request) -> HTMLResponse | RedirectResponse:
    settings = getattr(request.app.state, "settings", None)
    if not settings or not settings.proxy.proxy_api_key:
        return RedirectResponse(url="/dashboard/", status_code=303)
    if _auth_check(request):
        return RedirectResponse(url="/dashboard/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": request.query_params.get("error", "")},
    )


@router.post("/login")
async def login_action(request: Request) -> RedirectResponse:
    settings = getattr(request.app.state, "settings", None)
    if not settings or not settings.proxy.proxy_api_key:
        return RedirectResponse(url="/dashboard/", status_code=303)

    form = await request.form()
    key = form.get("api_key", "")
    if not key or not hmac.compare_digest(key, settings.proxy.proxy_api_key):
        return RedirectResponse(url="/dashboard/login?error=Invalid+API+key", status_code=303)

    request.state.session.clear()
    request.state.session["authenticated"] = True
    return RedirectResponse(url="/dashboard/", status_code=303)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.state.session.clear()
    return RedirectResponse(url="/dashboard/login", status_code=303)


@router.get("", response_class=HTMLResponse, response_model=None)
@router.get("/", response_class=HTMLResponse, response_model=None)
async def overview(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    aggregator = getattr(request.app.state, "metrics_aggregator", None)
    metrics_db = getattr(request.app.state, "metrics_db", None)

    stats: dict[str, Any] = {
        "llm_requests_total": 0,
        "semantic_cache_hit_rate": 0.0,
        "provider_token_cache_hit_rate": 0.0,
        "estimated_tokens_saved": 0,
        "estimated_cost_saved_usd": 0.0,
        "avg_request_duration_seconds": 0.0,
        "p95_request_duration_seconds": 0.0,
    }
    data_age: int | None = None

    if aggregator:
        try:
            daily = await aggregator.get_recent_daily(limit=90)
            total_requests = sum(d.total_requests for d in daily)
            total_hits = sum(d.cache_hits for d in daily)
            total_misses = sum(d.cache_misses for d in daily)
            total_input = sum(d.total_input_tokens for d in daily)
            total_output = sum(d.total_output_tokens for d in daily)
            total_cache_read = sum(d.cache_read_tokens for d in daily)
            latencies = [d.avg_latency_ms for d in daily if d.avg_latency_ms > 0]

            stats["llm_requests_total"] = total_requests
            semantic_total = total_hits + total_misses
            stats["semantic_cache_hit_rate"] = (
                total_hits / semantic_total if semantic_total > 0 else 0.0
            )
            stats["provider_token_cache_hit_rate"] = (
                total_cache_read / total_input if total_input > 0 else 0.0
            )
            stats["estimated_tokens_saved"] = total_cache_read
            # Rough cost estimate: $3/M input, $15/M output, $0.30/M cached read
            pricing_input = 3.0
            pricing_output = 15.0
            pricing_cache_read = 0.30
            cost_saved = (total_cache_read / 1_000_000) * (pricing_input - pricing_cache_read)
            total_cost = (total_input / 1_000_000) * pricing_input + (
                total_output / 1_000_000
            ) * pricing_output
            stats["estimated_cost_saved_usd"] = round(cost_saved, 4)
            stats["estimated_total_cost_usd"] = round(total_cost, 4)

            if latencies:
                stats["avg_request_duration_seconds"] = round(
                    sum(latencies) / len(latencies) / 1000, 4
                )
                sorted_lat = sorted(latencies)
                p95_idx = min(int(len(sorted_lat) * 0.95), len(sorted_lat) - 1)
                stats["p95_request_duration_seconds"] = round(sorted_lat[p95_idx] / 1000, 4)
        except Exception:
            logger.exception("Failed to load overview stats from aggregator")

    if metrics_db:
        try:
            age = await metrics_db.snapshot_age()
            data_age = age
        except Exception:
            pass

    return templates.TemplateResponse(
        request=request,
        name="overview.html",
        context={"stats": stats, "data_age": data_age},
    )


@router.get("/models", response_class=HTMLResponse, response_model=None)
async def models_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    settings = getattr(request.app.state, "settings", None)

    # Build config lookup
    configured_map: dict[str, dict[str, Any]] = {}
    if settings:
        for name, cfg in settings.providers.root.items():
            key_set = bool(os.environ.get(cfg.api_key_env)) if cfg.api_key_env else False
            configured_map[name] = {
                "name": name,
                "key_set": key_set,
                "adapter": settings.providers.adapter_for(name),
            }

    # Build LiteLLM provider list
    by_provider: dict[str, list[str]] = {}
    for provider, models in litellm.models_by_provider.items():
        by_provider[provider] = sorted(models)

    # Merge: configured providers first, then all LiteLLM providers
    show_all = request.query_params.get("all") == "1"
    seen = set()
    providers: list[dict[str, Any]] = []

    adapter_hints = {
        "anthropic": "ephemeral cache_control",
        "openai": "auto prefix",
        "gemini": "CachedContent API",
    }

    for name in sorted(configured_map):
        seen.add(name)
        info = configured_map[name]
        models = by_provider.get(name, [])
        info["model_count"] = len(models)
        info["models"] = models
        info["adapter_note"] = adapter_hints.get(info["adapter"])
        providers.append(info)

    if show_all:
        for name in sorted(by_provider):
            if name in seen:
                continue
            seen.add(name)
            adapter = "openai"
            if name in ("anthropic", "claude"):
                adapter = "anthropic"
            elif name in ("gemini", "google"):
                adapter = "gemini"
            models = by_provider[name]
            providers.append(
                {
                    "name": name,
                    "adapter": adapter,
                    "adapter_note": adapter_hints.get(adapter, "auto prefix"),
                    "key_set": None,
                    "model_count": len(models),
                    "models": models,
                }
            )

    return templates.TemplateResponse(
        request=request,
        name="models.html",
        context={
            "providers": providers,
            "show_all_link": show_all is False,
            "total_providers": len(by_provider),
            "known_models": len(litellm.model_list),
        },
    )


@router.get("/cache", response_class=HTMLResponse, response_model=None)
async def cache_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    semantic_cache = getattr(request.app.state, "semantic_cache", None)
    stats: dict[str, Any] = {"total_entries": 0, "valid_entries": 0}
    if semantic_cache:
        try:
            stats = await semantic_cache.stats()
        except Exception:
            logger.exception("Failed to read cache stats")

    aggregator = getattr(request.app.state, "metrics_aggregator", None)
    bucket_count = 0
    avg_turns = 0.0
    cache_lookups = 0
    if aggregator:
        try:
            daily = await aggregator.get_recent_daily(limit=90)
            cache_lookups = sum(d.total_requests for d in daily)
            # Prefix hash bucket metrics from the most recent snapshot
            metrics_db = getattr(request.app.state, "metrics_db", None)
            if metrics_db:
                now_ts = int(time())
                snapshots = await metrics_db.query_history(
                    "prefix_hash_bucket_count", now_ts - 86400, now_ts, bucket_seconds=86400
                )
                latest_bucket = snapshots[-1] if snapshots else None
                if latest_bucket and latest_bucket["avg"] is not None:
                    bucket_count = int(latest_bucket["avg"])

                turns_snapshots = await metrics_db.query_history(
                    "avg_turns_per_bucket", now_ts - 86400, now_ts, bucket_seconds=86400
                )
                latest_turns = turns_snapshots[-1] if turns_snapshots else None
                if latest_turns and latest_turns["avg"] is not None:
                    avg_turns = latest_turns["avg"]
        except Exception:
            logger.exception("Failed to load cache page stats from aggregator")

    return templates.TemplateResponse(
        request=request,
        name="cache.html",
        context={
            "cache_stats": stats,
            "bucket_count": bucket_count,
            "avg_turns": avg_turns,
            "cache_lookups": cache_lookups,
        },
    )


@router.get("/templates", response_class=HTMLResponse, response_model=None)
async def templates_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    prompt_registry = getattr(request.app.state, "prompt_registry", None)
    template_list: list[dict[str, Any]] = []
    if prompt_registry:
        template_list = prompt_registry.list_templates()

    return templates.TemplateResponse(
        request=request,
        name="templates.html",
        context={"templates": template_list},
    )


@router.get("/templates/new", response_class=HTMLResponse, response_model=None)
async def template_new_form(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    return templates.TemplateResponse(
        request=request,
        name="template_form.html",
        context={"template": {}, "action": "/dashboard/templates/save"},
    )


@router.get("/templates/{name}/edit", response_class=HTMLResponse, response_model=None)
async def template_edit_form(request: Request, name: str) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    prompt_registry = getattr(request.app.state, "prompt_registry", None)
    if not prompt_registry:
        return HTMLResponse(status_code=404, content="Registry unavailable")

    tpl = prompt_registry.get_template(name)
    if not tpl:
        return HTMLResponse(status_code=404, content="Template not found")

    return templates.TemplateResponse(
        request=request,
        name="template_form.html",
        context={
            "template": tpl,
            "action": f"/dashboard/templates/save/{name}",
        },
    )


@router.post("/templates/save/{name:path}", response_model=None)
async def template_update(request: Request, name: str) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    prompt_registry = getattr(request.app.state, "prompt_registry", None)
    if not prompt_registry:
        return HTMLResponse(status_code=503, content="Registry unavailable")

    if not name.strip():
        return HTMLResponse(status_code=400, content="Template name is required")
    if re.search(r"[#/\"\'\\]", name):
        return HTMLResponse(status_code=400, content="Template name must not contain # / \" ' \\")

    form = await request.form()
    tpl = PromptTemplate(
        name=name,
        version=form.get("version", "1.0"),
        description=form.get("description", ""),
        template=form.get("template", ""),
    )
    prompt_registry.register_template(tpl)
    return RedirectResponse(url="/dashboard/templates", status_code=303)


@router.post("/templates/save", response_model=None)
async def template_create(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    prompt_registry = getattr(request.app.state, "prompt_registry", None)
    if not prompt_registry:
        return HTMLResponse(status_code=503, content="Registry unavailable")

    form = await request.form()
    name = form.get("name", "")
    if not name.strip():
        return HTMLResponse(status_code=400, content="Template name is required")
    if re.search(r"[#/\"\'\\]", name):
        return HTMLResponse(status_code=400, content="Template name must not contain # / \" ' \\")

    tpl = PromptTemplate(
        name=name,
        version=form.get("version", "1.0"),
        description=form.get("description", ""),
        template=form.get("template", ""),
    )
    prompt_registry.register_template(tpl)
    return RedirectResponse(url="/dashboard/templates", status_code=303)


@router.delete("/templates/{name}", response_model=None)
async def template_delete(request: Request, name: str) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    prompt_registry = getattr(request.app.state, "prompt_registry", None)
    if not prompt_registry:
        return HTMLResponse(status_code=503, content="Registry unavailable")

    if not prompt_registry.delete_template(name):
        return HTMLResponse(status_code=404, content="Template not found")
    return HTMLResponse(status_code=200, content="")


@router.post("/templates/reload")
async def template_reload(request: Request) -> RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    prompt_registry = getattr(request.app.state, "prompt_registry", None)
    if prompt_registry:
        prompt_registry.reload()

    return RedirectResponse(url="/dashboard/templates", status_code=303)


@router.get("/config", response_class=HTMLResponse, response_model=None)
async def config_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    config_content = ""
    writable = False
    mtime: float = 0
    config_path = getattr(request.app.state, "config_path", "layercache.yaml")
    if os.path.exists(config_path):
        st = os.stat(config_path)
        with open(config_path, encoding="utf-8") as f:
            config_content = f.read()
        writable = os.access(config_path, os.W_OK)
        mtime = st.st_mtime

    return templates.TemplateResponse(
        request=request,
        name="config.html",
        context={
            "config_raw": config_content,
            "writable": writable,
            "config_path": config_path,
            "mtime": mtime,
        },
    )


@router.post("/config/save", response_model=None)
async def config_save(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    # CSRF: require HTMX header — non-HTMX POSTs are not from our forms
    if request.headers.get("HX-Request") != "true":
        return HTMLResponse(status_code=403, content="CSRF: direct POST not allowed")

    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    now = time()
    bucket = _rate_limit_bucket.setdefault(client_ip, [])
    cutoff = now - _RATE_LIMIT_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _RATE_LIMIT_MAX:
        return HTMLResponse(status_code=429, content="Too many requests. Try again later.")
    bucket.append(now)

    config_path = getattr(request.app.state, "config_path", "layercache.yaml")

    if not os.path.exists(config_path):
        return HTMLResponse(status_code=400, content="Config file not found")

    if not os.access(config_path, os.W_OK):
        return HTMLResponse(status_code=400, content="Config file is read-only")

    form = await request.form()
    raw_mtime = form.get("mtime", "0")
    new_yaml_raw = form.get("config_yaml", "")
    new_yaml = str(new_yaml_raw) if not isinstance(new_yaml_raw, str) else new_yaml_raw
    client_mtime = float(str(raw_mtime)) if not isinstance(raw_mtime, float) else raw_mtime

    async with _config_save_lock:
        # mtime conflict check
        current_mtime = os.stat(config_path).st_mtime
        if client_mtime > 0 and abs(current_mtime - client_mtime) > 0.1:
            msg = (
                '<div class="alert alert-error">'
                "Config was modified by another process. Reload and try again."
                "</div>"
            )
            return HTMLResponse(status_code=409, content=msg)

        # Validate YAML
        import yaml

        try:
            parsed = yaml.safe_load(new_yaml)
            if parsed is None:
                parsed = {}
        except yaml.YAMLError as e:
            return HTMLResponse(
                status_code=400,
                content=f'<div class="alert alert-error">Invalid YAML: {e}</div>',
            )

        # Validate against settings model
        from layercache.config import LayerCacheSettings

        try:
            LayerCacheSettings.model_validate(parsed)
        except Exception as e:
            return HTMLResponse(
                status_code=400,
                content=f'<div class="alert alert-error">Validation error: {e}</div>',
            )

        # Atomic write: mkstemp → rename
        import tempfile

        fd, tmp_path = tempfile.mkstemp(
            suffix=".yaml",
            prefix="layercache_",
            dir=os.path.dirname(config_path) or None,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_yaml)
            os.replace(tmp_path, config_path)
        except OSError as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return HTMLResponse(
                status_code=500,
                content=f'<div class="alert alert-error">Write failed: {e}</div>',
            )

    # Hot-reload
    reload_fn = getattr(request.app.state, "reload_config", None)
    warnings: list[str] = []
    if reload_fn:
        result = reload_fn()
        if result.get("status") == "error":
            err = result.get("error", "unknown error")
            msg = (
                '<div class="alert alert-error">'
                f"Config saved but reload failed: {err}. Restart required."
                "</div>"
            )
            return HTMLResponse(status_code=500, content=msg)
        warnings = result.get("warnings", [])

    new_mtime = os.stat(config_path).st_mtime
    warning_html = ""
    if warnings:
        warning_html = (
            '<div class="alert alert-warning">' + "<br>".join(f"⚠ {w}" for w in warnings) + "</div>"
        )

    oob_input = (
        f'<input type="hidden" id="cfg-mtime" name="mtime"'
        f' value="{new_mtime}" hx-swap-oob="outerHTML">'
    )
    return HTMLResponse(
        content=(
            '<div class="alert alert-ok">Config saved successfully.</div>'
            + warning_html
            + oob_input
        ),
    )


@router.get("/logs", response_class=HTMLResponse, response_model=None)
async def logs_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not _auth_check(request):
        return _redirect_login()

    with _log_ring._lock:
        records = list(_log_ring.buffer)
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={"log_records": records},
    )


@router.get("/analytics", response_class=HTMLResponse, response_model=None)
async def analytics_page(request: Request) -> HTMLResponse | RedirectResponse:
    """Analytics dashboard page."""
    if not _auth_check(request):
        return _redirect_login()

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={},
    )


@router.get("/api/analytics", response_model=None)
async def analytics_api(request: Request, hours: int = 24) -> dict[str, Any]:
    """Analytics API endpoint for chart data.

    Args:
        hours: Number of hours to look back (default 24).

    Returns:
        Analytics data including summary, time series, templates, and sessions.
    """
    hours = max(1, min(hours, 8760))

    aggregator = getattr(request.app.state, "metrics_aggregator", None)
    if not aggregator:
        return {
            "summary": {
                "hit_rate": 0.0,
                "tokens_saved": 0,
                "avg_latency": 0,
                "total_requests": 0,
            },
            "time_series": [],
            "templates": [],
            "sessions": [],
        }

    try:
        hit_rate = await aggregator.get_cache_hit_rate(hours)
        token_savings = await aggregator.get_token_savings(hours)

        hourly_data = await aggregator.get_recent_hourly(limit=hours)
        time_series = [
            {
                "hour": d.hour,
                "hit_rate": (d.cache_hits / d.total_requests * 100) if d.total_requests > 0 else 0,
                "total_requests": d.total_requests,
                "cache_hits": d.cache_hits,
                "cache_misses": d.cache_misses,
                "avg_latency": d.avg_latency_ms,
                "input_tokens": d.total_input_tokens,
                "output_tokens": d.total_output_tokens,
                "cache_read_tokens": d.cache_read_tokens,
            }
            for d in reversed(hourly_data)
        ]

        templates = []
        sessions = []

        return {
            "summary": {
                "hit_rate": hit_rate,
                "tokens_saved": token_savings["input_tokens_saved"],
                "avg_latency": time_series[-1]["avg_latency"] if time_series else 0,
                "total_requests": sum(d["total_requests"] for d in time_series),
            },
            "time_series": time_series,
            "templates": templates,
            "sessions": sessions,
        }
    except Exception as e:
        logger.error("Analytics API failed: %s", e)
        return {
            "summary": {
                "hit_rate": 0.0,
                "tokens_saved": 0,
                "avg_latency": 0,
                "total_requests": 0,
            },
            "time_series": [],
            "templates": [],
            "sessions": [],
            "error": str(e),
        }
