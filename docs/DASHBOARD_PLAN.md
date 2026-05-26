# Built-in Web Dashboard — Plan

## Data storage

Current metrics are purely in-memory (`MetricsCollector`). They reset to zero on every restart. A dashboard with time-series charts needs persistent storage.

### SQLite metric store (separate DB)

Use a dedicated `/data/metrics.db` (not the same DB as the semantic cache) to avoid write contention and keep concerns separated.

```sql
CREATE TABLE metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    labels TEXT DEFAULT '{}',          -- JSON, e.g. {"model": "anthropic/..."}
    UNIQUE(ts, name, labels)
);
```

WAL journal mode enabled on both `metrics.db` and `semantic_cache.db` for concurrent reads during writes. Currently neither DB uses WAL; enabling it on the cache DB also improves pipeline throughput (cache writes no longer block concurrent pipeline reads).

### Background snapshot task

- Runs every 60s (on the minute boundary, not pure sleep drift)
- Reads current counters from `MetricsCollector`
- Writes one row per metric + per-model breakdown into `metric_snapshots`
- Task is wrapped in try/except with log + retry; never silently dies
- Tracks `last_snapshot_ts` exposed via a lightweight `/v1/cache/metrics/status` endpoint so the dashboard can show "data age" warnings
- Prunes rows older than `snapshot_retention_days` (default 7) on each snapshot cycle

Snapshot stores **running counters** (`llm_requests_total: 1250`), not deltas.
The history endpoint computes rates as `(v2 - v1) / (t2 - t1)` for charting.
First snapshot after restart is discarded as a baseline anchor.

### Bucketing semantics

`GET /v1/cache/metrics/history?range=24h&resolution=5m`

- Each series: `SELECT ts, AVG(value) WHERE name=? AND labels=? GROUP BY floor(ts/?)*?`
- Gaps → null (Chart.js renders nulls as line breaks; caller can interpolate)
- No snapshot in bucket → null, not zero (zero would falsely show "0 requests")
- Counter series: first point after restart is discarded (no previous value to diff against)

---

## Architecture

### Stack

- **Server**: Jinja2 templates rendered by FastAPI
- **Frontend**: HTMX (CDN + vendored fallback) + Chart.js (CDN + vendored fallback)
- **Auth**: Dashboard requires proxy API key via login form → session cookie. Read-only view for Viewer role, admin for Config editing.
- **Static files**: FastAPI `StaticFiles` mount at `/static`

### Auth model

| Page | Access |
|------|--------|
| Overview, Models, Cache stats | Any authenticated user |
| Template CRUD, Config view | Admin only (proxy_api_key configured) |
| Log stream | Any authenticated user |

No `proxy_api_key` configured → dashboard is unrestricted (local-only by default).

### Config editing read-only detection

In Docker the config file is mounted `:ro`. The config page detects writability at startup and shows a banner: *"Config file is read-only. Edit layercache.yaml directly and reload."* The save button is hidden.

---

## Pages

### 1. Overview (`/dashboard`)

| Widget | Source |
|--------|--------|
| Request rate (5m avg) | `history?range=1h&resolution=1m` |
| Semantic cache hit rate % | `hits / (hits + misses)` |
| Tokens saved (cumulative) | latest snapshot value |
| Cost saved (USD) | latest snapshot value |
| Provider cache hit rate | latest snapshot value |
| Avg / P95 latency | latest + 24h trend |
| Data age warning | shown if `last_snapshot_ts > 90s` ago |

Charts: 24h line chart for request rate, cache hit rate, token usage — all from history endpoint.

### 2. Models (`/dashboard/models`)

Table from `/v1/models`: provider name, model count, API key configured (yes/no), click to expand per-provider model list.

### 3. Cache (`/dashboard/cache`)

Semantic cache stats, search by prefix hash, invalidate entry.

### 4. Templates (`/dashboard/templates`)

Prompt template CRUD wrapping `/v1/prompts/templates` API. Create/edit via form, delete with confirmation, reload from disk button.

### 5. Configuration (`/dashboard/config`)

Editable YAML editor with syntax validation, atomic save, and hot-reload. Banner if file is read-only. Save uses tempfile+rename with mtime conflict detection. Hot-reload applies log level, pipeline timeout/retries, and warns on changes requiring restart (semantic cache, enhancements).

### 6. Logs (`/dashboard/logs`)

Tail of recent log entries from a ring buffer.

---

## Files to create

```
layercache/
  metrics/
    collector.py        # + snapshot_to_db() method
    storage.py          # NEW: metric_snapshots table init, insert, query, prune
  dashboard/
    __init__.py          # router registration
    router.py            # all /dashboard routes (HTML)
    templates/
      base.html          # layout with sidebar nav + auth
      overview.html      # stat cards + Chart.js time-series
      models.html        # provider/model table
      cache.html         # cache stats + invalidation
      templates.html     # prompt template CRUD
      config.html        # config viewer with R/O detection
      logs.html          # log tail
  static/
    vendor/
      htmx.min.js        # vendored fallback
      chart.umd.min.js   # vendored fallback
    dashboard.css
    dashboard.js
  config.py              # + snapshot_retention_days, snapshot_interval_seconds
  main.py                # + StaticFiles mount, /history route, lifespan snapshot task
```

## Implementation order

### Phase 1: Storage backend (~5h)

1. Create `/data/metrics.db` with WAL mode, `metric_snapshots` table
2. `storage.py` — init, insert_snapshot(), query_history(), prune()
3. Background `_snapshot_metrics()` task in lifespan with error handling
4. `GET /v1/cache/metrics/history` endpoint with bucketed queries
5. `GET /v1/cache/metrics/status` for snapshot age tracking
6. Config: `snapshot_retention_days`, `snapshot_interval_seconds`

### Phase 2: Dashboard UI (~14h)

7. StaticFiles mount + base template with sidebar nav + login
8. Overview page: stat cards + Chart.js charts from history endpoint
9. Models page: table from `/v1/models`
10. Templates page: CRUD forms
11. Cache page: stats + search/invalidate
12. Config page: YAML display with R/O detection + secrets masking
13. Logs page: ring buffer tail

### Phase 3: Config editing ✅

14. Write-back endpoint with atomic write + mtime check ✅
15. YAML validation before write ✅
16. Hot-reload via `reload_config()` (log level, pipeline, restart warnings) ✅

### Cleanup Phase: Post-implementation polish ✅

17. Negative sleep guard, WAL checkpoint, backoff cap on snapshot loop
18. Pricing match: sort by key length descending
19. `MetricsCollector` threading lock for concurrent safety
20. CSRF (HTMX header check) + rate limiting on config save endpoint
21. Dead `_mask_secrets` removed
22. Config path centralised via `app.state.config_path`
23. Test coverage: 17 new tests for config reload, save, mtime, CSRF, rate limiting
24. Documentation: plans, changelog, agents.md, docstrings updated

## Dependencies

No new Python packages. HTMX and Chart.js loaded from CDN with vendored fallbacks in `static/vendor/`.
