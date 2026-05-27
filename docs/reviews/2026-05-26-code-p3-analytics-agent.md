## Code Review: P3 Analytics Dashboard Implementation

### Verdict
❌ Request changes

### Required Changes

| File:Line | Issue | Fix |
|-----------|-------|-----|
| `aggregator.py:145,192` | References non-existent `metrics_requests` table | The table must be created in `storage.py` or aggregator must use existing `metric_snapshots` table |
| `aggregator.py:58-69` | No connection error handling if DB not initialized | Add try/except around `connect()` with graceful fallback |
| `router.py:556-601` | Hardcoded reference to `_metrics._db.db_path` accesses private attribute | Use public API or inject aggregator via app.state |
| `analytics.html:119` | `showToast()` called but function may not be globally available | Verify `dashboard.js` is loaded before analytics.html scripts execute |
| `aggregator.py:421-425` | `_next_hour()` doesn't handle timezone-naive datetimes correctly | Use `datetime.fromisoformat()` with proper UTC handling |

---

### Strengths

- **Clean dataclass design** — `HourlyRollup` and `DailyRollup` are well-structured with clear field types
- **Pre-computed rollups** — Background aggregation avoids real-time query costs (aligns with spec)
- **Parameterized queries** — All SQL uses `?` placeholders, preventing SQL injection
- **Type hints throughout** — Consistent use of type annotations for IDE support
- **Chart.js integration** — Four complementary charts provide good visual coverage
- **Auto-refresh** — 60-second polling keeps dashboard current without manual refresh
- **Time range selector** — 24h/7d/30d options match spec requirements

---

### Nitpicks

- `aggregator.py:14` — Unused `Any` import (only used in `get_token_savings` return type)
- `aggregator.py:393-410` — `get_token_savings()` could return a dataclass instead of dict
- `analytics.html:13-16` — Inline `onchange`/`onclick` handlers; consider HTMX or event listeners
- `analytics.html:306-317` — Session table truncates ID to 8 chars but doesn't add title tooltip
- `router.py:556` — Import inside function (`from ..main import _metrics`) should be at module top
- `analytics.html:1` — Missing `block nav_analytics` override to highlight active nav item

---

### Test Coverage

| Area | Status |
|------|--------|
| Unit tests | ❌ No tests exist for `MetricsAggregator` |
| Edge cases | ❌ Empty database, null metrics, timezone boundaries untested |
| Error paths | ❌ DB connection failures, query errors untested |
| Integration | ❌ No tests for `/dashboard/api/analytics` endpoint |

**Required tests:**
```python
# tests/test_aggregator.py
- test_compute_hourly_rollup_empty()
- test_compute_hourly_rollup_with_data()
- test_compute_daily_rollup_unique_sessions()
- test_get_cache_hit_rate_zero_division()
- test_get_token_savings_calculation()
- test_rollup_save_idempotent()
```

---

### Spec Alignment Check (v1.5.0-scale-context.md)

| Requirement | Status | Notes |
|-------------|--------|-------|
| **R3: Dashboard shows hit rates** | ⚠️ Partial | Shows overall hit rate, but NOT per agent/model/template as specified |
| **R3: Per-enhancement ROI** | ❌ Missing | No tracking of enhancement-specific metrics |
| **R3: Query performance <100ms** | ⚠️ Unverified | Pre-computed rollups should help, but no benchmarks |
| **R3: Redis key prefixes** | ❌ N/A | Implementation uses SQLite only, no Redis integration |
| **R3: Time series charts** | ✅ Implemented | Hit rate, requests, tokens, latency charts present |
| **R3: Top templates by savings** | ⚠️ Placeholder | Templates array is empty (`[]`) in API response |
| **R3: Agent comparison** | ⚠️ Placeholder | Sessions array is empty (`[]`) in API response |

**Critical gap:** The spec requires analytics "per agent/model/template" but the current implementation:
1. Doesn't track template usage in metrics (no `lc_template` field in snapshots)
2. Doesn't track enhancement usage per request
3. Doesn't correlate session IDs with agent identity

---

### Security Notes

| Issue | Severity | Recommendation |
|-------|----------|----------------|
| No auth check bypass | ✅ Secure | `_auth_check()` properly guards all endpoints |
| SQL injection | ✅ Secure | All queries use parameterized statements |
| XSS in charts | ⚠️ Low | Chart.js data comes from JSON API, but ensure `escape` in template |
| Session ID exposure | ⚠️ Medium | Session IDs shown in table; consider hashing for display |
| Rate limiting | ✅ Secure | Config save has rate limit, but analytics API is unlimited (acceptable for read-only) |

**Missing:**
- No input validation on `hours` parameter (allows arbitrary integers)
- Should add bounds: `max(1, min(hours, 8760))` to limit 1 hour to 365 days

---

### Performance Notes

| Aspect | Status | Recommendation |
|--------|--------|----------------|
| N+1 queries | ✅ Good | Single query per rollup computation |
| Connection pooling | ⚠️ Missing | Creates new connection per request; should use shared pool |
| Async/await | ⚠️ Mixed | Aggregator is synchronous; should be async for non-blocking |
| Blocking calls | ❌ Issue | `aggregator.connect()` and queries block event loop |
| Index usage | ✅ Good | Indexes on `hour` and `date` columns created |

**Critical:** The synchronous `MetricsAggregator` blocks the async event loop during database operations. Should refactor to use `aiosqlite`:

```python
# Current (blocking)
cursor = self._conn.cursor()
cursor.execute(...)

# Recommended (async)
async with self._db.execute(...) as cursor:
    await cursor.fetchall()
```

---

### Architecture Concerns

1. **Tight coupling to `_metrics` global** (`router.py:556`)
   - Accessing private `_metrics._db.db_path` creates fragile dependency
   - Should inject via `request.app.state.metrics_aggregator`

2. **No rollup automation**
   - Spec says "Pre-compute rollups in background snapshot loop"
   - No evidence of hourly/daily rollup computation in snapshot loop
   - Rollups computed on-demand in API endpoint (defeats purpose)

3. **Missing `metrics_requests` table**
   - Aggregator queries `metrics_requests` table that doesn't exist
   - `storage.py` only creates `metric_snapshots` table
   - This is a **blocking bug** — code will crash on first use

4. **Schema mismatch**
   - Aggregator expects: `semantic_cache_hit`, `duration_ms`, `input_tokens`, `session_id`
   - Storage writes: `semantic_cache_hits_total`, `total_input_tokens`, no session_id
   - Field names and structure don't align

---

### Recommendations

1. **Fix blocking bugs first:**
   - Create `metrics_requests` table OR refactor aggregator to use `metric_snapshots`
   - Align column names between storage and aggregator

2. **Refactor to async:**
   ```python
   class MetricsAggregator:
       async def connect(self) -> None:
           self._db = await aiosqlite.connect(self.db_path)
       
       async def compute_hourly_rollup(self, hour: str) -> HourlyRollup | None:
           async with self._db.execute(...) as cursor:
               row = await cursor.fetchone()
   ```

3. **Add rollup scheduler:**
   - In `MetricsCollector.snapshot_loop()`, compute hourly rollup when minute == 0
   - Compute daily rollup when hour == 0 and minute == 0

4. **Track template/enhancement metadata:**
   - Add `template_name` and `enhancements` columns to `metric_snapshots`
   - Populate from `lc_template` and `lc_enhancements` request extensions

5. **Add input validation:**
   ```python
   @router.get("/api/analytics")
   async def analytics_api(request: Request, hours: int = 24) -> dict[str, Any]:
       hours = max(1, min(hours, 8760))  # Clamp to 1h - 365d
   ```

6. **Add nav highlighting:**
   ```html
   {% block nav_analytics %}active{% endblock %}
   ```

---

### Missing Features (Spec Gaps)

Per v1.5.0 spec R3, these are missing:

1. **Per-template performance** — API returns empty `templates: []`
2. **Per-enhancement ROI** — No enhancement tracking in metrics
3. **Agent comparison** — No agent identity tracking (only session IDs)
4. **Model breakdown** — Model data exists in `metric_snapshots` but not exposed in analytics API
5. **Redis backend** — Aggregator only supports SQLite

---

### Summary

The analytics dashboard has a solid foundation with well-structured rollup dataclasses and good chart visualizations. However, it has **critical implementation gaps** that prevent it from working:

1. **Missing table** — `metrics_requests` doesn't exist
2. **Schema mismatch** — Column names don't match between storage and aggregator
3. **Blocking I/O** — Synchronous DB calls in async context
4. **Empty data** — Template and session arrays are unimplemented
5. **No tests** — Zero test coverage for new code

**Recommendation:** Address the blocking bugs (table creation, schema alignment) before deployment. Then refactor to async and add rollup automation. Finally, implement template/enhancement tracking to meet spec requirements.
