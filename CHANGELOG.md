# Changelog

All notable changes to the LayerCache project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.0] - 2026-05-26

### Added
- **Metrics storage backend** (`metrics/storage.py`): persistent `metric_snapshots` table with WAL mode, snapshot insert, bucketed history queries, prune, and WAL checkpoint
- **Background snapshot task**: minute-aligned periodic counter dump with exponential backoff (capped at 3600s), WAL checkpoint after each prune
- **Metrics history endpoint** `GET /v1/cache/metrics/history` — bucketed time-series via `GROUP BY CAST(ts / ? AS INTEGER)`
- **Metrics status endpoint** `GET /v1/cache/metrics/status` — snapshot age tracking
- **Built-in Web Dashboard** (`layercache/dashboard/`):
  - 10 Jinja2 templates (base, login, overview with 7 stat cards + 3 Chart.js charts, models, cache, templates CRUD, config editor, logs)
  - 16 routes with session auth (proxy API key login, auto-pass when no key configured)
  - Static assets: `dashboard.css` (dark theme, responsive), `dashboard.js` (Chart.js re-init on HTMX swaps)
  - Vendored `htmx.min.js` (v2.0.4) + `chart.umd.min.js` (v4.4.7)
  - `LogRingBuffer` with threading lock for log tail
- **Config write-back** (`POST /dashboard/config/save`): atomic tempfile+rename, YAML syntax validation, Pydantic model validation, mtime conflict detection, HTMX OOB mtime update
- **Hot-reload** (`reload_config()`): re-reads `layercache.yaml`, validates via `LayerCacheSettings.model_validate()`, applies log level, pipeline timeout/retries, warns on changes needing restart
- **CSRF protection** on config save: rejects POSTs without `HX-Request: true` header
- **Rate limiting** on config save: 10 POSTs/min per IP
- **Read-only detection**: save button hidden when config file is not writable
- 17 new tests: `tests/test_config.py` for reload function, save endpoint, mtime conflict, atomic write, CSRF, rate limiting

### Changed
- `MetricsCollector` now uses `threading.Lock` for concurrent-safe access
- Pricing fuzzy match sorts keys by length descending so specific substrings win
- Snapshot loop: `max(0, ...)` guard on sleep, exponential backoff capped at 1h, log throttled
- `MetricsDB.checkpoint()` added; called after each prune cycle
- Config path centralised via `request.app.state.config_path`
- Dashboard count: 115 tests (from 98)

### Removed
- Dead `_mask_secrets` function from `layercache/dashboard/router.py`

### Fixed
- `reload_config()` YAML parse errors now caught (was outside try/except)
- `app.state.settings` now updated after hot-reload
- Config mtime hidden input uses `hx-swap-oob="outerHTML"` (innerHTML is a no-op on void `<input>`)
- Config save uses `tempfile.mkstemp()` instead of fixed `.tmp` filename
- Config save serialized with `asyncio.Lock()` to prevent concurrent-write races

## [1.4.0] - 2026-05-26

### Added
- **L2 session truncation** (`caching.max_session_tokens`): pipeline drops oldest conversation turns until the stable prefix fits within a token budget. Turn-group-aware — keeps complete user/assistant/tool clusters. Always preserves at least one turn. Default `null` (no truncation). Hot-reloadable.
- **Prefix threshold warning**: pipeline logs at INFO when L0+L1+L2 is below ~1024 tokens, rate-limited to once per hour per prefix hash. Uses `litellm.token_counter` per-model with chars/2 fallback.
- `AnthropicProviderConfig` with `use_auto_cache_control: bool = False` (field defined, not yet wired)

### Changed
- Pipeline expanded from 8 to 11 stages: canonicalize → truncate session → prefix check → enhance → inject markers → route → handle → store → background cache
- `BaseAdapter.inject_markers()` accepts optional `config` dict parameter

### Documentation
- README.md: version badge 1.4.0, pipeline diagram, features table, config reference
- AGENTS.md: 11-stage pipeline, gotchas for truncation and threshold warning
- CHANGELOG.md: this entry

## [1.2.0] - 2026-05-26

### Added
- **Anthropic `/v1/messages` endpoint** — full bidirectional wire-format translation
  - `anthropic_request_to_fields()` converts Anthropic request to internal pipeline format
  - `openai_response_to_anthropic()` converts pipeline response to Anthropic format
  - `AnthropicStreamTranslator` state machine converts per-chunk OpenAI streaming deltas to correct Anthropic SSE events
- **25 dedicated tests** for `/v1/messages` translation: request parsing (12), response formatting (5), streaming events (8)
- **`tool_choice` mapping**: `{"type": "any"}` → `"required"`, `{"type": "auto"}` → `"auto"`, `{"type": "tool", "name": "x"}` → `{"type": "function", "function": {"name": "x"}}`

### Changed
- `LayerCacheRequest` now accepts `user` and `stop` fields (required by Anthropic wire format)
- Pipeline `_build_payload()` forwards `user` and `stop` to LiteLLM

### Fixed
- **Duplicate termination events** — `_has_emitted_stop` flag gates post-loop `message_delta`/`message_stop`
- **`message_start` never fires for role-only chunk** — first chunk always emits `message_start` (even if only `{"role": "assistant"}`)
- **`_close_text_block` used for tool blocks** — added proper `_close_tool_block()` method
- **`json.loads` can raise on truncated tool JSON** — wrapped in `try/except json.JSONDecodeError`
- **README typo**: `anthropropic.py` → `anthropic.py`

### Security
- CORS config flagged as technically invalid (`allow_origins=["*"]` + `allow_credentials=True`) — not exploitable (proxy pattern, not browser-facing)

## [1.1.0] - 2026-05-26

### Added
- CORS middleware (all origins allowed — proxy pattern)
- Request ID middleware (`X-Request-ID` header, propagated to error responses and logs)
- Provider API key validation at startup (logs warning if configured keys are missing)
- Background Gemini CachedContent creation (`create_cached_content` via Gemini API with `X-Goog-Api-Key` header)
- Error handler for fire-and-forget background tasks (`_log_task_error` callback)
- `asyncio.CancelledError` handling in streaming path (logs client disconnect warnings)
- Token metrics recording for streaming requests (parses final chunk's `usage` data)
- Configurable `log_level` from `layercache.yaml` is now applied at startup
- Configurable `timeout` and `max_retries` from `ProviderConfig` are now passed to LiteLLM
- Warning log when config YAML file is missing (`config.py`)

### Changed
- **Security**: Gemini CachedContent API key moved from URL query parameter to `X-Goog-Api-Key` header
- **Security**: Empty API key now returns HTTP 401 with a clear error message (was opaque LiteLLM auth failure)
- **Embedder**: FastEmbed model now cached in subprocess via `_subprocess_embedders` dict (was re-initialized on every call)
- **Embedder**: Removed silent zero-vector fallback on embedding failures (exceptions now propagate)
- **Embedder**: Missing `fastembed` now raises `ImportError` at construction (was silent failure)
- **Pipeline**: `temp_prompt` canonicalized before semantic cache lookup (fixes permanent cache misses for non-normalized whitespace)
- **Pricing**: Model pricing map updated (fixed sort order for correct cache read vs input pricing)
- **Config**: Moved `import yaml` and `from pydantic` above `logger` statement to satisfy E402
- **Content hash**: Replaced MD5 with SHA-256[:16] (`models.py`)
- **Prometheus endpoint**: Changed from `JSONResponse` to plain `Response` with `text/plain` media type

### Fixed
- Dead floating expression in `metrics/collector.py` (`(input_tokens / 1_000_000) * pricing["input"]`)
- `_apply_enhancements` early-return bug (properly checks `few_shot is not None` before async dispatch)
- `_verify_proxy_key` docstring (`auth` → `proxy_api_key` in config comment)
- Dead `CacheMetrics` model removed from `models.py`

### Removed
- Dead `CacheMetrics` Pydantic model
- Dead `_init_embedder` function from `embedder.py`
- Orphan `import asyncio` inside `_stream_cached_response` (now at module level)

## [1.0.0] - 2025-05-26

### Added

#### Sprint 0: Project Setup
- Initialized Python project with `pyproject.toml` (Hatch build system)
- Configured `ruff` for linting/formatting and `mypy` for type checking
- Added core dependencies: FastAPI, uvicorn, LiteLLM, Pydantic v2, aiosqlite, FastEmbed, Prometheus client, PyYAML
- Created `requirements.txt` with all runtime and dev dependencies
- Implemented basic passthrough proxy with `POST /v1/chat/completions` endpoint
- Added health check endpoint at `GET /health`

#### Sprint 1: Stratifier & Canonicalizer
- **Stratifier** (`stratifier.py`): Implemented heuristic L0-L4 message classification engine
  - System messages at index 0 are classified as L0 (System/Persona)
  - System messages with tool definitions or contextual content are classified as L1 (Context)
  - Assistant and tool messages are classified as L2 (Session/History)
  - Final user messages are classified as L4 (User Input)
  - Non-final user messages are classified as L2 (Session)
  - Support for explicit layer hints via `lc_layer_hints` parameter
  - Support for template-based stratification via `lc_template` parameter
  - Context detection heuristic for system messages (tool keywords, length threshold)
- **Canonicalizer** (`canonicalizer.py`): Implemented deterministic prompt normalization
  - Whitespace normalization: `strip()`, collapse triple newlines, collapse multiple spaces
  - Trailing whitespace removal per line
  - JSON schema minification with sorted keys
  - Alphabetical sorting of tools array by `function.name`
  - Multimodal content array support (text blocks canonicalized, images preserved)
  - Deterministic reassembly guarantee (identical input always produces identical output)
- **Core Data Models** (`models.py`):
  - `LayerType` enum (SYSTEM, CONTEXT, SESSION, ENHANCEMENT, USER) with cacheability metadata
  - `StratifiedMessage` with layer assignment, role, content, original index, and metadata
  - `StratifiedPrompt` with per-layer message storage, `reassemble()` for L0-L4 flattening, and `prefix_hash()` for cache keying
  - `LayerCacheRequest` extending standard OpenAI fields with LayerCache extensions
  - `CacheMetrics` and `CacheEntry` models for observability

#### Sprint 2: Provider Cache Marker Injection
- **Adapter Pattern** (`adapters/base.py`): Abstract `BaseAdapter` with `inject_markers()` and `extract_cache_metrics()` interface
- **Anthropic Adapter** (`adapters/anthropic.py`):
  - Injects `"cache_control": {"type": "ephemeral"}` at L0, L1, and L2 layer boundaries
  - Handles both string and multimodal (list) content formats
  - Injects cache markers on system prompt content blocks
  - Extracts `cache_read_input_tokens` and `cache_creation_input_tokens` from Anthropic response `usage`
- **OpenAI Adapter** (`adapters/openai.py`):
  - Ensures L0-L2 content is placed at the beginning for automatic prefix caching
  - No explicit markers needed; canonicalization is the key responsibility
  - Extracts `cached_tokens` from OpenAI response `usage`
- **Gemini Adapter** (`adapters/gemini.py`):
  - Manages `CachedContent` resource lifecycle with prefix hash mapping
  - First request sends full content and triggers async cache creation
  - Subsequent requests use cached content and only send L2+ messages
  - Converts OpenAI-format messages to Gemini `contents` format (role mapping)
  - Extracts `cachedContentTokenCount` from Gemini `usageMetadata`
- **Provider Detection** (`adapters/__init__.py`):
  - Automatic provider detection from model names (supports prefix format like `anthropic/claude-3-5-sonnet` and bare names like `gpt-4o`)
  - Adapter registry with factory function

#### Sprint 3: Enhancement Engine
- **BaseEnhancement** (`enhancements/base.py`): Abstract base class with `apply(prompt) -> prompt` contract
  - `EnhancementRegistry` for managing enhancement plugins
  - Helper methods for adding user/assistant message pairs at L3
  - Strict L3-only injection rule (never modifies L0-L2)
- **Chain of Thought** (`enhancements/chain_of_thought.py`): Step-by-step reasoning instruction injected as user/assistant pair at L3
- **Structured Output** (`enhancements/structured_output.py`): JSON format enforcement with optional schema inclusion
- **Self Critique** (`enhancements/self_critique.py`): Three-phase instruction (Initial Analysis, Critique, Final Response) at L3
- **Cache Safety Verification**: Enhancements are guaranteed to never change the prefix hash of L0-L2

#### Sprint 4: Prompt Registry & Dynamic Few-Shots
- **Prompt Registry** (`registry/prompt_registry.py`):
  - File-based template storage (YAML and JSON supported)
  - Named, versioned templates with L0 (System) and L1 (Context) layers
  - Hot-reload support via `reload()` method
  - Multi-template file support (single file with `templates` array)
  - CRUD operations: get, list, register, delete
- **Dynamic Few-Shot** (`enhancements/dynamic_few_shot.py`):
  - Local vector store with cosine similarity search
  - Embeds user query (L4) and retrieves top-K most relevant examples
  - Async embedding computation support (`apply_async`)
  - Synchronous fallback when embedder is unavailable
  - JSON-based example storage with optional pre-computed embeddings
- **Management API**:
  - `GET /v1/prompts/templates` — List all registered templates
  - `POST /v1/prompts/templates` — Create or update a template
  - `DELETE /v1/prompts/templates/{name}` — Delete a template
  - `POST /v1/prompts/reload` — Reload all templates from disk
- **Sample Data**:
  - `code-assistant.yaml` — Coding assistant template with safety rules and output format
  - `writer.yaml` — Creative writing template with style guidelines
  - `examples.json` — Python programming few-shot examples (list reversal, tuples, exceptions)

#### Sprint 5: Semantic Cache
- **Embedder** (`cache/embedder.py`):
  - FastEmbed wrapper using `BAAI/bge-small-en-v1.5` (384-dimensional embeddings)
  - Async embedding generation via `ProcessPoolExecutor` to avoid blocking the event loop
  - Single text and batch embedding support
  - Graceful fallback with zero vectors on failure
  - Global singleton management via `get_embedder()`
- **Semantic Cache** (`cache/semantic.py`):
  - SQLite-backed storage via `aiosqlite` for full async operation
  - Dual-key strategy: SHA-256 prefix hash (exact match) + query embedding (cosine similarity > 0.95)
  - Configurable TTL with automatic expiration
  - `lookup()` — Find cached response matching both prefix hash and query similarity
  - `store()` — Cache new responses with prefix hash, embedding, and TTL
  - `invalidate()` — Remove entries by prefix hash or all entries
  - `cleanup_expired()` — Garbage collection for expired entries
  - `stats()` — Cache statistics (total entries, valid entries)
  - Indexes on `prefix_hash` and `ttl_expires_at` for query performance

#### Sprint 6: Observability & Metrics
- **MetricsCollector** (`metrics/collector.py`):
  - Request counting (total, per-model)
  - Token usage tracking (input, output, cache read, cache creation)
  - Semantic cache hit/miss counting
  - Latency tracking (average, P95) with configurable sample limit
  - Cost estimation using per-model pricing tables (Anthropic, OpenAI, Gemini)
  - Cost savings calculation based on cached vs. full input token pricing
  - Per-model breakdown with request counts and cache hit rates
- **Prometheus Metrics**:
  - `lc_llm_requests_total` — Counter for total LLM requests
  - `lc_semantic_cache_hits_total` — Counter for semantic cache hits
  - `lc_semantic_cache_misses_total` — Counter for semantic cache misses
  - `lc_tokens_saved_total` — Counter for total tokens saved
  - `lc_cache_read_tokens_total` — Counter for provider cached tokens
  - `lc_input_tokens_total` — Counter for total input tokens
  - `lc_output_tokens_total` — Counter for total output tokens
  - `lc_cost_saved_usd` — Counter for estimated cost savings
  - `lc_request_duration_seconds` — Summary (avg, P95)
- **Metrics Endpoints**:
  - `GET /v1/cache/metrics` — JSON dashboard with all aggregated metrics
  - `GET /metrics` — Prometheus text exposition format
- **RequestTimer** context manager for precise duration measurement

#### Sprint 7: Streaming & Configuration
- **Streaming Support**:
  - `POST /v1/chat/completions` with `stream: true` returns SSE stream
  - Streaming proxy for LLM provider responses via LiteLLM async streaming
  - Semantic cache hit streaming — cached responses streamed back with artificial chunk delays
  - SSE format with `data:` prefixed JSON lines and `[DONE]` terminator
- **Configuration System** (`config.py`):
  - YAML-based configuration via Pydantic Settings
  - Structured config: `ProxyConfig`, `ProvidersConfig`, `CachingConfig`, `EnhancementsConfig`
  - `LayerCacheSettings.from_yaml()` for file-based loading
  - Default fallback when no config file is present
- **Authentication Middleware**:
  - Optional proxy API key verification via `proxy_api_key` config
  - Supports Bearer token and `x-api-key` header
  - Returns 401/403 on auth failure

#### Sprint 8: Hardening & Deployment
- **Error Handling**:
  - Global exception handler returning structured error responses
  - Semantic cache failures fail open (request proceeds normally)
  - Embedding failures skip semantic caching gracefully
  - Comprehensive logging throughout the pipeline
- **Docker**:
  - Multi-stage Dockerfile based on `python:3.11-slim`
  - FastEmbed model pre-downloaded during build (eliminates cold-start latency)
  - Health check configured (`/health` endpoint, 30s interval)
  - `.dockerignore` for clean builds
- **Docker Compose**:
  - Single-service configuration with volume mounts
  - Environment variable passthrough for API keys
  - Restart policy and health check integration
- **Configuration File**: Default `layercache.yaml` with all documented options
- **Documentation**: Comprehensive project documentation (see docs/ directory)

### Test Suite
- 73 unit tests covering all major components
- **test_stratifier.py** (17 tests): Heuristic classification, layer hints, template mode, reassembly, prefix hashing
- **test_canonicalizer.py** (8 tests): Whitespace normalization, tool canonicalization, determinism, multimodal content
- **test_enhancements.py** (12 tests): CoT, structured output, self-critique, registry, cache safety verification
- **test_adapters.py** (12 tests): Anthropic markers, OpenAI ordering, Gemini cache lifecycle, provider detection
- **test_semantic_cache.py** (10 tests): Store/lookup, TTL expiration, prefix mismatch, invalidation, cleanup
- **test_metrics.py** (7 tests): Request recording, semantic tracking, hit rates, cost estimation, Prometheus output
- **test_registry.py** (12 tests): YAML/JSON loading, multi-template files, CRUD, reload, empty directory handling

---

## [Unreleased]

### Planned (Post-V1)

See the [ROADMAP.md](docs/ROADMAP.md) for the full prioritized plan. Key themes:
- **V2**: Redis distributed cache, Git-synced prompt registry, Prometheus/Grafana dashboards, WebSocket support, Helm chart, client rate limiting
- **V3**: Multi-modal CLIP caching, A/B testing framework, custom embedding models, plugin marketplace, multi-region deployment
