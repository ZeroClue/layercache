# Built-in Web Dashboard — Plan

## Goals

A FastAPI-served web UI at `/dashboard` for managing LayerCache and viewing real-time performance. No separate server, no Node.js build step — just Python.

## Architecture

- **Server**: Jinja2 templates rendered by FastAPI (already has Jinja2 as a Starlette dependency)
- **Frontend**: Minimal HTML + HTMX for interactivity + Chart.js for live charts
- **Data**: Reads from in-memory `MetricsCollector` + hit the existing REST APIs (`/v1/cache/metrics`, `/v1/models`, `/v1/prompts/templates`)
- **Polling**: HTMX `hx-trigger` with `every:5s` for live updates

## Pages

### 1. Overview (`/dashboard`)

| Widget | Source |
|--------|--------|
| Request rate (sparkline) | `lc_llm_requests_total` rate over time |
| Semantic cache hit rate % | `hits / (hits + misses)` |
| Tokens saved (cumulative) | `estimated_tokens_saved` |
| Cost saved (USD) | `estimated_cost_saved_usd` |
| Provider cache hit rate | `provider_token_cache_hit_rate` |
| Avg/P95 latency | `avg/p95_request_duration_seconds` |

Charts: 7-day line chart for request rate, cache hit rate, token usage.

### 2. Models (`/dashboard/models`)

Table of all known providers with:
- Provider name, model count
- API key configured? (yes/no)
- Link to view per-provider model list

### 3. Cache (`/dashboard/cache`)

Semantic cache management:
- Stats (total entries, valid entries)
- Search/invalidate by prefix hash
- TTL configuration display

### 4. Templates (`/dashboard/templates`)

Prompt template CRUD (wraps existing `/v1/prompts/templates` API):
- List templates
- Create/edit via form
- Delete with confirmation
- Reload from disk button

### 5. Configuration (`/dashboard/config`)

Read-only view of current `layercache.yaml` (sensitive keys masked).
Future: inline editing with validation.

### 6. Logs (`/dashboard/logs`)

Tail of recent log entries (last N lines from a ring buffer).

## Files to create

```
layercache/dashboard/
  __init__.py          # router registration
  router.py            # /dashboard routes (HTML responses)
  templates/
    base.html          # layout with nav sidebar
    overview.html      # main metrics
    models.html        # provider/model browser
    cache.html         # cache management
    templates.html     # prompt template CRUD
    config.html        # config viewer
    logs.html          # live log tail
static/
  dashboard.css        # minimal styles
  dashboard.js         # Chart.js init + HTMX extensions
```

## Implementation order

1. **Router + base template** — sidebar nav, auth wrapper, page skeleton
2. **Overview page** — 6 stat cards + Chart.js time-series charts
3. **Models page** — table from `/v1/models` endpoint
4. **Templates page** — CRUD forms wrapping existing API
5. **Cache page** — stats + search/invalidate
6. **Config page** — YAML display with secret masking
7. **Logs page** — ring buffer tail (requires new backend stream)

## Dependencies added

None new — Jinja2, HTMX (CDN), Chart.js (CDN). No build step.

## Effort estimate

- Router + base template: ~2h
- Overview page: ~3h
- Models page: ~1h
- Templates page: ~2h
- Cache page: ~1h
- Config page: ~1h
- Logs page: ~2h (new backend ring buffer)
- **Total: ~12h**
