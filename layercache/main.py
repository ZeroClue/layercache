"""LayerCache - Intelligent Prompt Enhancement & Token Caching Proxy.

Main FastAPI application that provides:
- OpenAI-compatible proxy endpoint (/v1/chat/completions)
- Cache metrics endpoint (/v1/cache/metrics, /metrics)
- Prompt registry management (/v1/prompts/templates)
- Health check endpoint (/health)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import litellm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from .adapters import detect_provider
from .adapters.anthropic_messages import (
    AnthropicStreamTranslator,
    anthropic_request_to_fields,
    openai_response_to_anthropic,
)
from .cache.embedder import get_embedder
from .cache.semantic import SemanticCache
from .canonicalizer import Canonicalizer
from .config import LayerCacheSettings
from .dashboard import router as dashboard_router
from .dashboard.router import _log_ring
from .enhancements import DynamicFewShotEnhancement, create_default_registry
from .metrics.collector import MetricsCollector
from .metrics.storage import MetricsDB
from .models import LayerCacheRequest
from .pipeline import RequestPipeline, validate_model_name
from .registry.prompt_registry import PromptRegistry
from .stratifier import Stratifier

logger = logging.getLogger("layercache")

# Derive session secret: env var > hash of proxy key > dev fallback
_SESSION_SECRET = os.environ.get("LAYERCACHE_SESSION_SECRET")
if not _SESSION_SECRET:
    try:
        with open("layercache.yaml") as f:
            import yaml

            _cfg = yaml.safe_load(f) or {}
        _proxy_key = _cfg.get("proxy", {}).get("proxy_api_key")
        if _proxy_key:
            _SESSION_SECRET = hashlib.sha256(_proxy_key.encode()).hexdigest()
        else:
            _SESSION_SECRET = hashlib.sha256(b"layercache-local-dev").hexdigest()
    except Exception:
        _SESSION_SECRET = hashlib.sha256(b"layercache-local-dev").hexdigest()

# Global instances (initialized in lifespan)
_settings: LayerCacheSettings | None = None
_pipeline: RequestPipeline | None = None
_metrics: MetricsCollector | None = None
_metrics_db: MetricsDB | None = None
_snapshot_task: Any = None
_semantic_cache: SemanticCache | None = None
_prompt_registry: PromptRegistry | None = None
_stratifier: Stratifier | None = None


def reload_config() -> dict[str, Any]:
    """Hot-reload layercache.yaml from disk.

    Re-reads the config file, validates it, and updates the running
    application state. Returns a dict with status and warnings.
    """
    global _settings
    warnings: list[str] = []

    config_path = "layercache.yaml"
    if not Path(config_path).exists():
        return {"status": "error", "error": f"Config file not found: {config_path}"}

    import yaml

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return {"status": "error", "error": f"YAML parsing failed: {e}"}

    try:
        new_settings = LayerCacheSettings.model_validate(raw)
    except Exception as e:
        return {"status": "error", "error": f"Validation failed: {e}"}

    old_settings = _settings
    _settings = new_settings

    # Apply log level
    logging.getLogger("layercache").setLevel(
        getattr(logging, _settings.proxy.log_level.upper(), logging.INFO)
    )

    # Update pipeline timeout/retries
    if _pipeline:
        provider = (
            _settings.providers.anthropic
            or _settings.providers.openai
            or _settings.providers.gemini
        )
        _pipeline._timeout = provider.timeout if provider else 120
        _pipeline._max_retries = provider.max_retries if provider else 3
        _pipeline._max_session_tokens = _settings.caching.max_session_tokens

    # Apply enhancement config changes
    if _pipeline and old_settings:
        old_enh_names = {e.name for e in old_settings.enhancements.registered}
        new_enh_names = {e.name for e in _settings.enhancements.registered}
        if old_enh_names != new_enh_names:
            warnings.append("Enhancement changes require a full restart to take effect")

    # Check for changes that need restart
    if old_settings:
        if old_settings.caching.semantic != _settings.caching.semantic:
            warnings.append("Semantic cache config changes require a full restart")

    logger.info("Configuration reloaded from %s", config_path)
    return {
        "status": "ok",
        "warnings": warnings,
        "config_path": config_path,
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize and cleanup resources."""
    global _settings, _pipeline, _metrics, _metrics_db, _snapshot_task
    global _semantic_cache, _prompt_registry, _stratifier

    # Load configuration (merge YAML + env vars)
    _settings = LayerCacheSettings.from_yaml("layercache.yaml")

    # Apply configured log level
    logging.getLogger("layercache").setLevel(
        getattr(logging, _settings.proxy.log_level.upper(), logging.INFO)
    )

    # Suppress LiteLLM's verbose logging
    litellm.suppress_debug_info = True

    # Validate that configured provider API keys are set
    missing_keys: list[str] = []
    for provider_name, provider_cfg in [
        ("anthropic", _settings.providers.anthropic),
        ("openai", _settings.providers.openai),
        ("gemini", _settings.providers.gemini),
    ]:
        if provider_cfg and provider_cfg.api_key_env:
            if not os.environ.get(provider_cfg.api_key_env):
                missing_keys.append(f"{provider_name}:{provider_cfg.api_key_env}")

    if missing_keys:
        logger.warning(
            "Provider API key(s) not set in environment: %s. "
            "Requests for these providers will fail with authentication errors.",
            ", ".join(missing_keys),
        )

    # Initialize metrics
    _metrics = MetricsCollector()
    _metrics_db = MetricsDB(db_path=_settings.caching.metrics.db_path)
    await _metrics_db.initialize()

    # Initialize prompt registry
    _prompt_registry = PromptRegistry(templates_dir="/data/prompts")

    # Initialize semantic cache
    if _settings.caching.semantic.enabled:
        embedder = get_embedder(_settings.caching.semantic.embedder)
        _semantic_cache = SemanticCache(
            db_path=_settings.caching.semantic.db_path,
            default_ttl=_settings.caching.semantic.default_ttl,
            similarity_threshold=_settings.caching.semantic.similarity_threshold,
            embedder=embedder,
        )
        await _semantic_cache.initialize()
    else:
        _semantic_cache = None

    # Initialize stratifier
    _stratifier = Stratifier()
    if _prompt_registry:
        _stratifier.set_registry(_prompt_registry)

    # Initialize canonicalizer
    canonicalizer = Canonicalizer()

    # Initialize enhancement registry
    enhancement_registry = create_default_registry()

    # Register dynamic few-shot if configured
    registered_names = [
        e.name
        for n in enhancement_registry.list_enhancements()
        if (e := enhancement_registry.get(n))
    ]
    for enh_config in _settings.enhancements.registered:
        if enh_config.name == "dynamic_few_shot" and "dynamic_few_shot" not in registered_names:
            few_shot = DynamicFewShotEnhancement(
                examples_path=enh_config.config.get("vector_store"),
                top_k=enh_config.config.get("top_k", 3),
                embedder=(
                    get_embedder(_settings.caching.semantic.embedder)
                    if _settings.caching.semantic.enabled
                    else None
                ),
            )
            enhancement_registry.register(few_shot)

    # Build the pipeline with timeout and retries from config
    provider = (
        _settings.providers.anthropic or _settings.providers.openai or _settings.providers.gemini
    )
    timeout = provider.timeout if provider else 120
    max_retries = provider.max_retries if provider else 3

    _pipeline = RequestPipeline(
        stratifier=_stratifier,
        canonicalizer=canonicalizer,
        enhancement_registry=enhancement_registry,
        semantic_cache=_semantic_cache,
        prompt_registry=_prompt_registry,
        metrics=_metrics,
        timeout=timeout,
        max_retries=max_retries,
        max_session_tokens=_settings.caching.max_session_tokens,
    )

    # Start background metrics snapshot task
    async def _snapshot_loop() -> None:
        interval = _settings.caching.metrics.snapshot_interval_seconds
        retention = _settings.caching.metrics.snapshot_retention_days
        consecutive_failures = 0
        next_run = time.time() + interval
        while True:
            try:
                now = time.time()
                sleep_sec = max(0.0, next_run - now)
                if sleep_sec > 0:
                    await asyncio.sleep(sleep_sec)
                next_run = time.time() + interval
                if _metrics and _metrics_db:
                    ts = int(time.time())
                    metrics_data = _metrics.get_metrics()
                    await _metrics_db.insert_snapshot(ts, metrics_data)
                    await _metrics_db.prune(retention_days=retention)
                    await _metrics_db.checkpoint()
                consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception:
                consecutive_failures += 1
                backoff = min(interval * (2 ** (consecutive_failures - 1)), 3600)
                if consecutive_failures == 1:
                    logger.exception(
                        "Metrics snapshot task failed (will retry in %ds)", backoff
                    )
                elif consecutive_failures < 10 or consecutive_failures % 10 == 0:
                    logger.warning(
                        "Metrics snapshot task failed (%d consecutive, retrying in %ds)",
                        consecutive_failures,
                        backoff,
                    )
                await asyncio.sleep(backoff)
                continue

    _snapshot_task = asyncio.create_task(_snapshot_loop())

    # Set shared state for dashboard access
    app.state.metrics = _metrics
    app.state.metrics_db = _metrics_db
    app.state.semantic_cache = _semantic_cache
    app.state.prompt_registry = _prompt_registry
    app.state.settings = _settings
    app.state.config_path = "layercache.yaml"
    def _reload_with_state() -> dict[str, Any]:
        result = reload_config()
        if result.get("status") == "ok":
            app.state.settings = _settings
        return result

    app.state.reload_config = _reload_with_state

    # Attach log ring buffer
    logging.getLogger("layercache").addHandler(_log_ring)

    logger.info("LayerCache initialized successfully")
    yield

    # Cleanup
    if _snapshot_task:
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except asyncio.CancelledError:
            pass
    if _metrics_db:
        await _metrics_db.close()
    if _semantic_cache:
        await _semantic_cache.close()
    embedder = get_embedder()
    embedder.shutdown()
    logger.info("LayerCache shutdown complete")


app = FastAPI(
    title="LayerCache",
    description="Intelligent Prompt Enhancement & Token Caching Proxy",
    version="1.4.0",
    lifespan=lifespan,
)

# CORS — allow all origins (this is a proxy, not an origin-bound service)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dashboard session support
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET)

# Static files (dashboard CSS/JS, vendor libs)
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Dashboard UI routes
app.include_router(dashboard_router)


# --- Request ID Middleware ---


@app.middleware("http")
async def add_request_id(request: Request, call_next: Any) -> Response:
    """Inject a unique request ID for tracing."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# --- Auth Middleware ---


async def _verify_proxy_key(authorization: str | None) -> None:
    """Verify the proxy API key if configured."""
    if _settings and _settings.proxy.proxy_api_key:
        expected = f"Bearer {_settings.proxy.proxy_api_key}"
        if not authorization:
            raise HTTPException(status_code=401, detail="Proxy API key required")
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=403, detail="Invalid proxy API key")


async def _verify_proxy_or_dashboard(request: Request, authorization: str | None) -> None:
    """Verify proxy API key or authenticated dashboard session."""
    if not _settings or not _settings.proxy.proxy_api_key:
        return
    if authorization:
        expected = f"Bearer {_settings.proxy.proxy_api_key}"
        if hmac.compare_digest(authorization, expected):
            return
    session = getattr(request.state, "session", {})
    if session.get("authenticated", False):
        return
    raise HTTPException(status_code=401, detail="Authentication required")


def _resolve_provider_api_key(model: str) -> str:
    """Look up the provider API key from environment variables.

    In protected mode (proxy_api_key is set), the proxy does NOT forward
    the client's Bearer token to the provider. Instead it reads the
    provider key from its own environment.
    """
    provider = detect_provider(model)
    provider_config = getattr(_settings.providers, provider, None) if _settings else None
    if provider_config and provider_config.api_key_env:
        key = os.environ.get(provider_config.api_key_env)
        if key:
            return key
        logger.warning(
            "Provider %s API key (%s) is not set in environment",
            provider,
            provider_config.api_key_env,
        )
    else:
        logger.warning("No provider config found for %s (model=%s)", provider, model)
    return ""


# --- OpenAI-Compatible Endpoints ---


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse | StreamingResponse:
    """OpenAI-compatible chat completions endpoint.

    Accepts standard OpenAI payloads. LayerCache extensions can be passed
    in the request body (lc_enhancements, lc_template, lc_cache_ttl, etc.).
    """
    await _verify_proxy_key(authorization)

    # Reject oversized request bodies early (SSRF + OOM guard)
    body_size = len(await request.body())
    if body_size > 10 * 1024 * 1024:  # 10 MB
        raise HTTPException(status_code=413, detail="Request body too large")

    # Parse request body
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    # Extract model before building the request (needed for auth resolution + validation)
    model = body.get("model", "")

    # Validate model name early (prevents SSRF, returns 400 not 500)
    try:
        validate_model_name(model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Resolve the provider API key
    if _settings and _settings.proxy.proxy_api_key:
        # Protected mode: proxy auth is separate from the provider key.
        # The proxy reads its own configured provider keys from the environment.
        api_key = _resolve_provider_api_key(model)
    else:
        # Passthrough mode: the Bearer token / x-api-key IS the provider key.
        api_key = ""
        if authorization and authorization.startswith("Bearer "):
            api_key = authorization[7:]
        if not api_key:
            api_key = request.headers.get("x-api-key", "")

    # Reject empty API keys with a clear 401
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="No API key provided. "
            "In passthrough mode, send a Bearer token or X-API-Key header. "
            "In protected mode, configure the provider API key environment variable.",
        )

    # Build LayerCache request from the body
    try:
        lc_request = LayerCacheRequest(
            model=model,
            messages=body.get("messages", []),
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            max_tokens=body.get("max_tokens"),
            stream=body.get("stream", False),
            tools=body.get("tools"),
            tool_choice=body.get("tool_choice"),
            response_format=body.get("response_format"),
            # LayerCache extensions
            lc_template=body.get("lc_template"),
            lc_enhancements=body.get("lc_enhancements", []),
            lc_cache_ttl=body.get("lc_cache_ttl", 300),
            lc_layer_hints=body.get("lc_layer_hints"),
            lc_skip_semantic_cache=body.get("lc_skip_semantic_cache", False),
            lc_bypass_cache=body.get("lc_bypass_cache", False),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request: {e}")

    if lc_request.stream:
        return StreamingResponse(
            _handle_streaming(lc_request, api_key),
            media_type="text/event-stream",
        )
    else:
        response = await _pipeline.process_request(lc_request, api_key)
        return JSONResponse(content=response)


async def _handle_streaming(lc_request: LayerCacheRequest, api_key: str) -> AsyncIterator[str]:
    """Handle an OpenAI-format streaming request."""
    try:
        async for chunk in _pipeline.process_streaming_request(lc_request, api_key):
            if isinstance(chunk, dict):
                data = json.dumps(chunk, ensure_ascii=False)
                yield f"data: {data}\n\n"
            elif isinstance(chunk, str):
                yield f"data: {json.dumps({'choices': [{'delta': {'content': chunk}}]})}\n\n"
    except Exception:
        logger.exception("Streaming pipeline failed")
        error_body = json.dumps({"error": {"message": "Streaming error", "type": "stream_error"}})
        yield f"data: {error_body}\n\n"
    finally:
        yield "data: [DONE]\n\n"


# --- Anthropic-Compatible Endpoint ---


@app.post("/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse | StreamingResponse:
    """Anthropic-compatible /v1/messages endpoint.

    Accepts Anthropic Messages API format. Translates to LayerCache's
    internal pipeline and converts the response back to Anthropic format.

    Supports streaming via SSE events matching Anthropic's protocol.
    """
    await _verify_proxy_key(authorization)

    body_size = len(await request.body())
    if body_size > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Request body too large")

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    model = body.get("model", "")
    try:
        validate_model_name(model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if _settings and _settings.proxy.proxy_api_key:
        api_key = _resolve_provider_api_key(model)
    else:
        api_key = ""
        if authorization and authorization.startswith("Bearer "):
            api_key = authorization[7:]
        if not api_key:
            api_key = request.headers.get("x-api-key", "")

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="No API key provided. "
            "In passthrough mode, send a Bearer token or X-API-Key header. "
            "In protected mode, configure the provider API key environment variable.",
        )

    # Translate Anthropic request to pipeline format
    try:
        fields = anthropic_request_to_fields(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Anthropic request: {e}")

    # Anthropic requires max_tokens
    if "max_tokens" not in fields or not fields["max_tokens"]:
        raise HTTPException(
            status_code=400,
            detail="max_tokens is required for Anthropic-compatible requests",
        )

    lc_request = LayerCacheRequest(
        model=fields["model"],
        messages=fields["messages"],
        temperature=fields.get("temperature"),
        top_p=fields.get("top_p"),
        max_tokens=fields["max_tokens"],
        stream=fields.get("stream", False),
        tools=fields.get("tools"),
        tool_choice=fields.get("tool_choice"),
        user=fields.get("user"),
        stop=fields.get("stop"),
    )

    if lc_request.stream:
        return StreamingResponse(
            _handle_anthropic_stream(lc_request, api_key),
            media_type="text/event-stream",
        )
    else:
        response = await _pipeline.process_request(lc_request, api_key)
        anthropic_response = openai_response_to_anthropic(response)
        return JSONResponse(content=anthropic_response)


async def _handle_anthropic_stream(
    lc_request: LayerCacheRequest,
    api_key: str,
) -> AsyncIterator[str]:
    """Handle an Anthropic-format streaming request.

    Converts each pipeline chunk from OpenAI streaming format to
    Anthropic SSE events on the fly.
    """
    translator = AnthropicStreamTranslator(model=lc_request.model)

    def _sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        async for chunk in _pipeline.process_streaming_request(lc_request, api_key):
            if isinstance(chunk, dict):
                for event in translator.translate(chunk):
                    yield event
            elif isinstance(chunk, str):
                if not translator._has_emitted_start:
                    fake = {"choices": [{"delta": {"content": chunk}, "finish_reason": None}]}
                    for event in translator.translate(fake):
                        yield event
                else:
                    yield _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": chunk},
                        },
                    )

        if not translator._has_emitted_start:
            msg_id = f"msg_{id(translator)}"
            msg: dict[str, Any] = {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": lc_request.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
            yield _sse("message_start", {"type": "message_start", "message": msg})
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        if not translator._has_emitted_stop:
            yield _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            )
            yield _sse("message_stop", {"type": "message_stop"})
    except Exception:
        logger.exception("Anthropic streaming failed")
        error_body = json.dumps(
            {
                "type": "error",
                "error": {"type": "api_error", "message": "Streaming error"},
            }
        )
        yield f"event: error\ndata: {error_body}\n\n"


@app.get("/v1/models")
async def list_models(
    authorization: str | None = Header(None),
) -> JSONResponse:
    """List available models grouped by provider."""
    await _verify_proxy_key(authorization)

    try:
        # Group LiteLLM's known models by provider
        by_provider: dict[str, list[str]] = {}
        for provider, models in litellm.models_by_provider.items():
            by_provider[provider] = sorted(models)

        return JSONResponse(
            content={
                "configured_providers": _list_configured_providers(),
                "by_provider": by_provider,
                "total_models": len(litellm.model_list),
            }
        )
    except Exception as e:
        logger.error("Failed to list models: %s", e)
        raise HTTPException(status_code=500, detail="Failed to list models")


def _list_configured_providers() -> list[dict[str, str]]:
    """List providers that are configured in layercache.yaml."""
    if not _settings:
        return []
    configured: list[dict[str, str]] = []
    for name, cfg in [
        ("anthropic", _settings.providers.anthropic),
        ("openai", _settings.providers.openai),
        ("gemini", _settings.providers.gemini),
    ]:
        if cfg:
            key_set = bool(os.environ.get(cfg.api_key_env)) if cfg.api_key_env else False
            configured.append(
                {"name": name, "api_key_env": cfg.api_key_env or "", "key_set": key_set}
            )
    return configured


# --- Cache Metrics Endpoints ---


@app.get("/v1/cache/metrics")
async def cache_metrics(
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Return cache performance metrics as JSON."""
    await _verify_proxy_key(authorization)
    if _metrics is None:
        return JSONResponse(content={"error": "Metrics not initialized"})

    return JSONResponse(content=_metrics.get_metrics())


@app.get("/metrics")
async def prometheus_metrics(
    authorization: str | None = Header(None),
) -> Response:
    """Return Prometheus-compatible metrics."""
    await _verify_proxy_key(authorization)
    if _metrics is None:
        return Response(
            content="# LayerCache metrics not initialized\n",
            media_type="text/plain",
        )

    metrics_text = _metrics.get_prometheus_metrics()
    return Response(content=metrics_text, media_type="text/plain")


@app.get("/v1/cache/metrics/history")
async def cache_metrics_history(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Return bucketed time-series history for all tracked metrics.

    Query params:
      range: time range in seconds (default 3600 = 1h)
      resolution: bucket size in seconds (default 300 = 5m)
    """
    await _verify_proxy_or_dashboard(request, authorization)
    if _metrics_db is None:
        return JSONResponse(content={"error": "Metrics storage not initialized"})

    now = int(time.time())
    try:
        range_seconds = int(request.query_params.get("range", 3600))
        bucket_seconds = int(request.query_params.get("resolution", 300))
    except ValueError:
        raise HTTPException(status_code=400, detail="range and resolution must be integers")
    if range_seconds < 1 or bucket_seconds < 1:
        raise HTTPException(status_code=400, detail="range and resolution must be >= 1")
    if range_seconds > 86400 * 365:
        range_seconds = 86400 * 365
    start_ts = now - range_seconds

    counters = await _metrics_db.query_counters_with_labels(start_ts, now)
    series: list[dict[str, Any]] = []
    for counter in counters:
        labels = json.loads(counter["labels"])
        data = await _metrics_db.query_history(
            name=counter["name"],
            start_ts=start_ts,
            end_ts=now,
            bucket_seconds=bucket_seconds,
            labels_filter=counter["labels"],
        )
        if data:
            series.append(
                {
                    "name": counter["name"],
                    "labels": labels,
                    "buckets": data,
                }
            )

    return JSONResponse(
        content={
            "range_seconds": range_seconds,
            "bucket_seconds": bucket_seconds,
            "series": series,
        }
    )


@app.get("/v1/cache/metrics/status")
async def cache_metrics_status(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Return snapshot age and storage status."""
    await _verify_proxy_or_dashboard(request, authorization)
    if _metrics_db is None:
        return JSONResponse(content={"enabled": False})

    age = await _metrics_db.snapshot_age()
    return JSONResponse(
        content={
            "enabled": True,
            "snapshot_age_seconds": age,
            "stale": age is not None and age > 90,
        }
    )


# --- Prompt Registry Management ---


@app.get("/v1/prompts/templates")
async def list_prompt_templates(
    authorization: str | None = Header(None),
) -> JSONResponse:
    """List all registered prompt templates."""
    await _verify_proxy_key(authorization)
    if _prompt_registry is None:
        return JSONResponse(content=[])

    return JSONResponse(content=_prompt_registry.list_templates())


@app.post("/v1/prompts/templates")
async def create_prompt_template(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Create or update a prompt template."""
    await _verify_proxy_key(authorization)

    if _prompt_registry is None:
        raise HTTPException(status_code=500, detail="Prompt registry not available")

    try:
        body = await request.json()
        from .registry.prompt_registry import PromptTemplate

        template = PromptTemplate.from_dict(body)
        _prompt_registry.register_template(template)
        return JSONResponse(
            content={"status": "ok", "name": template.name, "version": template.version}
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/v1/prompts/templates/{template_name}")
async def delete_prompt_template(
    template_name: str,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Delete a prompt template."""
    await _verify_proxy_key(authorization)

    if _prompt_registry is None:
        raise HTTPException(status_code=500, detail="Prompt registry not available")

    deleted = _prompt_registry.delete_template(template_name)
    if deleted:
        return JSONResponse(content={"status": "ok", "deleted": template_name})
    raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")


@app.post("/v1/prompts/reload")
async def reload_prompt_templates(
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Reload all prompt templates from disk."""
    await _verify_proxy_key(authorization)

    if _prompt_registry is None:
        raise HTTPException(status_code=500, detail="Prompt registry not available")

    _prompt_registry.reload()
    return JSONResponse(content={"status": "ok", "templates": _prompt_registry.list_templates()})


# --- Health Check ---


@app.get("/health")
async def health_check() -> JSONResponse:
    """Health check endpoint."""
    status = {
        "status": "healthy",
        "version": app.version,
        "semantic_cache": _semantic_cache is not None,
    }

    if _semantic_cache:
        try:
            stats = await _semantic_cache.stats()
            status["semantic_cache_stats"] = stats
        except Exception:
            status["semantic_cache"] = False

    return JSONResponse(content=status)


# --- Error Handlers ---


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler for unhandled errors."""
    request_id = getattr(request.state, "request_id", "unknown")

    # LiteLLM provider/auth errors -> 4xx, not 500
    if hasattr(litellm, "BadRequestError") and isinstance(exc, litellm.BadRequestError):
        status = 400
        message = str(exc)
        logger.warning("LiteLLM bad request (request_id=%s): %s", request_id, message)
    elif hasattr(litellm, "AuthenticationError") and isinstance(exc, litellm.AuthenticationError):
        status = 401
        message = str(exc)
        logger.warning("LiteLLM auth error (request_id=%s): %s", request_id, message)
    elif hasattr(litellm, "RateLimitError") and isinstance(exc, litellm.RateLimitError):
        status = 429
        message = str(exc)
        logger.warning("LiteLLM rate limit (request_id=%s): %s", request_id, message)
    else:
        status = 500
        message = "Internal server error"
        logger.error("Unhandled exception (request_id=%s): %s", request_id, exc, exc_info=True)

    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "server_error",
                "request_id": request_id,
            }
        },
    )
