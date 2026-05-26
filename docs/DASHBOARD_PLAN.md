# Built-in Web Dashboard — Plan v2

## Data storage: the key gap

Current metrics are purely in-memory (`MetricsCollector`). They reset to zero on every restart. A dashboard with time-series charts ("last 24h", "last 7 days") needs persistent storage.

### Option A: SQLite snapshots (recommended)

- SQLite is already a dependency (aiosqlite for semantic cache)
- No new infra, works identically in Docker and bare-metal
- Background asyncio task snapshots current metrics every 60s
- API endpoint returns time-bucketed data for charting
- Auto-prune data older than configurable retention (default 7 days)

**New table in the existing SQLite DB:**
```sql
CREATE TABLE metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,              -- unix epoch seconds
    name TEXT NOT NULL,                -- metric name
    value REAL NOT NULL,
    labels TEXT DEFAULT '{}',          -- JSON, e.g. {"model": "claude-..."}
    UNIQUE(ts, name, labels)
);

CREATE INDEX idx_snapshots_ts ON metric_snapshots(ts);
```

**Snapshot schema** (one row per counter + current running values):
| Metric | Example |
|--------|---------|
| `llm_requests_total` | 1250 |
| `semantic_cache_hits_total` | 180 |
| `semantic_cache_misses_total` | 1070 |
| `tokens_input_total` | 1250000 |
| `tokens_output_total` | 625000 |
| `tokens_cache_read_total` | 815000 |
| `cost_saved_usd` | 21.72 |
| `latency_avg_seconds` | 1.234 |
| `latency_p95_seconds` | 3.456 |
| `cache_hit_rate` | 0.65 |

Per-model snapshots use the `labels` column: `{"model": "anthropic/claude-3-5-sonnet-20241022"}`.

**New API endpoints:**
- `GET /v1/cache/metrics/history?range=24h&resolution=5m` — returns bucketed time-series

**Background task:**
- `asyncio.create_task` during lifespan, coroutine sleeps 60s between snapshots
- Uses existing `MetricsCollector.get_metrics()` for the snapshot values
- Prunes rows older than `snapshot_retention_days` (configurable, default 7)

### Option B: Prometheus-only

Skip the SQLite snapshots. The built-in dashboard queries Prometheus directly via its HTTP API. This requires Prometheus to be running (no bare-metal dashboard) but gives you real PromQL power.

**Decision**: Go with Option A (SQLite) as the default. Option B is complementary — the Grafana sidecar already handles the Prometheus path. SQLite makes the dashboard work for everyone.

---

## Implementation order (updated)

### Phase 1: Storage backend (~4h)

1. Add `metric_snapshots` table to semantic cache DB initialization
2. Add `_snapshot_metrics()` background task to lifespan
3. Add `GET /v1/cache/metrics/history` endpoint with time bucketing
4. Add `snapshot_retention_days` to config

### Phase 2: Dashboard UI (~12h)

5. **Router + base template** — sidebar nav, auth wrapper, page skeleton
6. **Overview page** — stat cards + Chart.js time-series (reads from history endpoint)
7. **Models page** — table from `/v1/models`
8. **Templates page** — CRUD forms wrapping existing API
9. **Cache page** — stats + search/invalidate from semantic cache API
10. **Config page** — read-only YAML display with secrets masked
11. **Logs page** — ring buffer tail (requires new backend stream)

### Phase 3: Config editing (future)

12. Write-back support for `layercache.yaml` — mutation endpoint + validation
13. Apply config changes without full restart (signal-based reload)

---

## Files to create/modify

```
layercache/
  metrics/
    collector.py       # + snapshot_to_db(), get_history()
    storage.py         # NEW: metric_snapshots table CRUD
  dashboard/
    __init__.py
    router.py
    templates/
      base.html
      overview.html
      models.html
      cache.html
      templates.html
      config.html
      logs.html
  config.py            # + snapshot_retention_days field
  main.py              # + lifespan background task, /history route
  static/
    dashboard.css
    dashboard.js
```

## Dependencies

No new Python packages — aiosqlite, Jinja2 already available. HTMX + Chart.js loaded from CDN in the HTML.
