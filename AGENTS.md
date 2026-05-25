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
- 8-stage `RequestPipeline` (semantic cache lookup → stratify L0-L4 → canonicalize → enhance at L3 → inject provider markers → LiteLLM route → handle response → store in cache)
- Provider adapters in `layercache/adapters/`: Anthropic (explicit `cache_control`), OpenAI (automatic prefix caching), Gemini (`CachedContent` API). Detected from model name prefixes (anthropic/claude, gpt, gemini).
- Enhancement plugins in `layercache/enhancements/` inject only at L3; they never modify L0-L2 (prefix hash invariant)
- Semantic cache: SQLite via aiosqlite + FastEmbed (`BAAI/bge-small-en-v1.5`, 384d) in ProcessPoolExecutor
- Metrics: Prometheus + JSON dashboard at `/metrics` and `/v1/cache/metrics`

## Package structure

```
layercache/main.py           — FastAPI app, endpoints, lifespan
layercache/pipeline.py       — RequestPipeline orchestrator
layercache/models.py         — StratifiedPrompt, LayerCacheRequest, CacheEntry
layercache/stratifier.py     — L0-L4 classification (heuristic/template/hints)
layercache/canonicalizer.py  — Whitespace, JSON, tool canonicalization
layercache/config.py         — Pydantic settings from layercache.yaml
layercache/adapters/         — Anthropic, OpenAI, Gemini cache markers
layercache/enhancements/     — CoT, structured output, self-critique, dynamic few-shot
layercache/cache/            — Embedder + SQLite semantic cache
layercache/metrics/          — MetricsCollector + RequestTimer
layercache/registry/         — Prompt template registry (YAML/JSON)
```

## Gotchas

- README typo: `anthropropic.py` — actual file is `layercache/adapters/anthropic.py`
- Config at `layercache.yaml` defaults to `/data/semantic_cache.db` (Docker path); local dev may need to override or set `caching.semantic.db_path`
- Dockerfile pre-downloads FastEmbed model during build (`BAAI/bge-small-en-v1.5`, ~400MB)
- No CI workflows or pre-commit hooks; quality is manual
- Prompt templates live in `/data/prompts/` (YAML/JSON); sample templates at `data/prompts/`
- LayerCache request extensions go in `extra_body`: `lc_template`, `lc_enhancements`, `lc_cache_ttl`, `lc_layer_hints`, `lc_skip_semantic_cache`, `lc_bypass_cache`
