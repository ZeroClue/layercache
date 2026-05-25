"""LayerCache - Intelligent Prompt Enhancement & Token Caching Proxy.

Main FastAPI application that provides:
- OpenAI-compatible proxy endpoint (/v1/chat/completions)
- Cache metrics endpoint (/v1/cache/metrics, /metrics)
- Prompt registry management (/v1/prompts/templates)
- Health check endpoint (/health)
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import litellm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .cache.embedder import get_embedder
from .cache.semantic import SemanticCache
from .canonicalizer import Canonicalizer
from .config import LayerCacheSettings
from .enhancements import DynamicFewShotEnhancement, create_default_registry
from .metrics.collector import MetricsCollector
from .models import LayerCacheRequest
from .pipeline import RequestPipeline
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

    # Load configuration
    _settings = LayerCacheSettings()

    # Suppress LiteLLM's verbose logging
    litellm.suppress_debug_info = True

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
        if (
            enh_config.name == "dynamic_few_shot"
            and "dynamic_few_shot" not in registered_names
        ):
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

    # Build the pipeline
    _pipeline = RequestPipeline(
        stratifier=_stratifier,
        canonicalizer=canonicalizer,
        enhancement_registry=enhancement_registry,
        semantic_cache=_semantic_cache,
        prompt_registry=_prompt_registry,
        metrics=_metrics,
    )

    logger.info("LayerCache initialized successfully")
    yield

    # Cleanup
    if _semantic_cache:
        await _semantic_cache.close()
    logger.info("LayerCache shutdown complete")


app = FastAPI(
    title="LayerCache",
    description="Intelligent Prompt Enhancement & Token Caching Proxy",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Auth Middleware ---

async def _verify_proxy_key(authorization: str | None) -> None:
    """Verify the proxy API key if configured."""
    if _settings and _settings.proxy.proxy_api_key:
        if not authorization:
            raise HTTPException(status_code=401, detail="Proxy API key required")
        if authorization != f"Bearer {_settings.proxy.proxy_api_key}":
            raise HTTPException(status_code=403, detail="Invalid proxy API key")


# --- OpenAI-Compatible Endpoints ---

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse | StreamingResponse:
    """OpenAI-compatible chat completions endpoint.

    Accepts standard OpenAI payloads. LayerCache extensions can be passed
    in the request body (lc_enhancements, lc_template, lc_cache_ttl, etc.).
    """
    await _verify_proxy_key(authorization)

    # Parse request body
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

    # Extract provider API key from Authorization header
    # The proxy forwards this to the LLM provider
    api_key = ""
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]

    # Also check for x-api-key header
    x_api_key = request.headers.get("x-api-key")
    if x_api_key and not api_key:
        api_key = x_api_key

    # Build LayerCache request from the body
    try:
        lc_request = LayerCacheRequest(
            model=body.get("model", ""),
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
    """Handle a streaming request."""
    async for chunk in _pipeline.process_streaming_request(lc_request, api_key):
        # Format as Server-Sent Events
        if isinstance(chunk, dict):
            data = json.dumps(chunk, ensure_ascii=False)
            yield f"data: {data}\n\n"
        elif isinstance(chunk, str):
            yield f"data: {json.dumps({'choices': [{'delta': {'content': chunk}}]})}\n\n"

    yield "data: [DONE]\n\n"


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
) -> JSONResponse:
    """Return Prometheus-compatible metrics."""
    if _metrics is None:
        return JSONResponse(
            content="# LayerCache metrics not initialized\n",
            media_type="text/plain",
        )

    metrics_text = _metrics.get_prometheus_metrics()
    return JSONResponse(content=metrics_text, media_type="text/plain")


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
        "version": "1.0.0",
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
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "type": "server_error"}},
    )
