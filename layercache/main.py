"""LayerCache - Intelligent Prompt Enhancement & Token Caching Proxy.

Main FastAPI application that provides:
- OpenAI-compatible proxy endpoint (/v1/chat/completions)
- Cache metrics endpoint (/v1/cache/metrics, /metrics)
- Prompt registry management (/v1/prompts/templates)
- Health check endpoint (/health)
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import litellm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

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
from .enhancements import DynamicFewShotEnhancement, create_default_registry
from .metrics.collector import MetricsCollector
from .models import LayerCacheRequest
from .pipeline import RequestPipeline, validate_model_name
from .registry.prompt_registry import PromptRegistry
from .stratifier import Stratifier

logger = logging.getLogger("layercache")

# Global instances (initialized in lifespan)
_settings: LayerCacheSettings | None = None
_pipeline: RequestPipeline | None = None
_metrics: MetricsCollector | None = None
_semantic_cache: SemanticCache | None = None
_prompt_registry: PromptRegistry | None = None
_stratifier: Stratifier | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize and cleanup resources."""
    global _settings, _pipeline, _metrics, _semantic_cache, _prompt_registry, _stratifier

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
    )

    logger.info("LayerCache initialized successfully")
    yield

    # Cleanup
    if _semantic_cache:
        await _semantic_cache.close()
    embedder = get_embedder()
    embedder.shutdown()
    logger.info("LayerCache shutdown complete")


app = FastAPI(
    title="LayerCache",
    description="Intelligent Prompt Enhancement & Token Caching Proxy",
    version="1.2.0",
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
    """List available models (proxied to LiteLLM)."""
    await _verify_proxy_key(authorization)
    try:
        models = await litellm.model_list()
        return JSONResponse(content=models)
    except Exception as e:
        logger.error("Failed to list models: %s", e)
        raise HTTPException(status_code=500, detail="Failed to list models")


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
