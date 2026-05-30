# AGENTS.md — LayerCache

## Setup & dev loop

```bash
pip install -e ".[dev]"      # install with dev extras
uvicorn layercache.main:app --host 0.0.0.0 --port 8000   # run server
docker-compose up -d         # Docker deploy
```

## Tests

```bash
pytest tests/ -v
pytest tests/test_stratifier.py -v   # single file
```
`asyncio_mode = "auto"` in `pyproject.toml`. No external services; all tests mock the embedder and cache DB.

## Lint & typecheck (run both before committing)

```bash
ruff check layercache/
ruff format layercache/
mypy layercache/
```
Config in `pyproject.toml`: line-length 100, mypy strict, ruff selects `E,F,I,N,W,UP`.

## Architecture essentials

- FastAPI async proxy, entrypoint `layercache/main.py:app`
- 11-stage `RequestPipeline` (semantic cache lookup → stratify L0-L4 → canonicalize → **truncate session** → **prefix threshold check** → enhance at L3 → inject provider markers → LiteLLM route → handle response → store in cache → background cache creation)
- Claude Code Pro / Ollama Cloud traffic through `/v1/messages` uses **direct httpx proxy** to the upstream (Anthropic or Ollama API), bypassing the pipeline and LiteLLM entirely. No message translation needed.
- Provider adapters in `layercache/adapters/`: Anthropic (explicit `cache_control`), OpenAI (automatic prefix caching), Gemini (`CachedContent` API). Detected from model name prefixes (anthropic/claude, gpt, gemini). Can be overridden per provider via `providers.{name}.adapter` in config.
- `detect_provider()` and `get_adapter()` accept optional `ProvidersConfig` for config-aware resolution. Pipeline wires `self._providers_config` through both streaming and non-streaming paths.
- Enhancement plugins in `layercache/enhancements/` inject only at L3; they never modify L0-L2 (prefix hash invariant)
- Semantic cache: SQLite via aiosqlite + FastEmbed (`BAAI/bge-small-en-v1.5`, 384d) in ProcessPoolExecutor
- Metrics: Prometheus + JSON dashboard at `/metrics` and `/v1/cache/metrics`. Dashboard reads from persistent DB rollups, not in-memory counters.

## Package structure

```
layercache/main.py           — FastAPI app, endpoints, lifespan
layercache/pipeline.py       — RequestPipeline orchestrator
layercache/models.py         — StratifiedPrompt, LayerCacheRequest, CacheEntry
layercache/stratifier.py     — L0-L4 classification (heuristic/template/hints)
layercache/canonicalizer.py  — Whitespace, JSON, tool canonicalization
layercache/config.py         — Pydantic settings from layercache.yaml
layercache/schema.py         — JSON Schema generator for IDE autocompletion
layercache/adapters/         — Anthropic/OpenAI/Gemini cache markers + /v1/messages shim
layercache/enhancements/     — CoT, structured output, self-critique, dynamic few-shot
layercache/cache/            — Embedder + SQLite semantic cache
layercache/metrics/          — MetricsCollector, MetricsDB, MetricsDB checkpoint()
layercache/registry/         — Prompt template registry (YAML/JSON)
layercache/dashboard/        — Web dashboard (Jinja2 + HTMX + Chart.js)
```

## Gotchas

- Config at `layercache.yaml` defaults to `/data/semantic_cache.db` (Docker path); local dev may need to override or set `caching.semantic.db_path`
- `providers` in config is a dict of arbitrary keys (not just anthropic/openai/gemini). Each key is a `ProviderConfig` with `api_key_env`, `base_url`, `default_model`, `timeout`, `max_retries`, and optional `adapter` override.
- Dockerfile pre-downloads FastEmbed model during build (`BAAI/bge-small-en-v1.5`, ~400MB)
- No CI workflows or pre-commit hooks; quality is manual
- Prompt templates live in `/data/prompts/` (YAML/JSON); sample templates at `data/prompts/`
- LayerCache request extensions go in `extra_body`: `lc_template`, `lc_enhancements`, `lc_cache_ttl`, `lc_layer_hints`, `lc_skip_semantic_cache`, `lc_bypass_cache`
- Dashboard config save requires `HX-Request: true` header (CSRF check) — all HTMX forms include this automatically
- Config save is rate-limited to 10 POSTs/min per IP (local-only safety)
- Metrics snapshot loop uses exponential backoff capped at 3600s; WAL checkpoint runs after each prune
- `reload_config()` updates log level and pipeline timeout/retries; semantic cache changes require full restart
- `MetricsCollector` uses a `threading.Lock` for concurrent access from request handlers and the snapshot loop
- `max_session_tokens` in config: truncates L2 to fit within token budget (provider-agnostic); also hot-reloadable
- Prefix threshold warning logged at INFO once per hour per prefix hash when L0+L1+L2 is below ~1024 tokens
- `layercache.schema.json` must be regenerated after changing `config.py` fields: run `layercache-schema` from the project root
- **detect_provider() hole**: `default_model` preference can route `-free` suffixed models to wrong provider. If someone sends `deepseek-v4-flash-free` through `opencode-go`, it routes to `opencode-go` instead of `opencode` (Zen) because the fallback returns the first provider with `base_url`. Currently not an issue since Go doesn't have `-free` models.
