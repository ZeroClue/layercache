# Fix Plan: P3 Analytics Dashboard Issues

**Date:** 2026-05-26  
**Agent:** deepseek-v4-flash (LayerCache Senior Fixing Agent)  
**Review Reference:** `docs/reviews/2026-05-26-code-p3-analytics-agent.md`

---

## Critical Issues Identified

| # | Issue | Severity | Files Affected |
|---|-------|----------|----------------|
| 1 | Missing `metrics_requests` table | **BLOCKING** | `storage.py`, `aggregator.py` |
| 2 | Schema mismatch between storage and aggregator | **BLOCKING** | `storage.py`, `aggregator.py` |
| 3 | Synchronous DB calls blocking async event loop | HIGH | `aggregator.py`, `router.py` |
| 4 | Hardcoded dependency on `_metrics._db.db_path` | MEDIUM | `router.py` |
| 5 | No input validation on `hours` parameter | MEDIUM | `router.py` |

---

## Fix Strategy

### Issue 1 & 2: Create `metrics_requests` Table + Schema Alignment

**Problem:** Aggregator queries `metrics_requests` table that doesn't exist. Column names don't match.

**Solution:** 
1. Add `metrics_requests` table to `storage.py` with proper schema
2. Modify `MetricsCollector.record_request()` to accept additional params (session_id, semantic_cache_hit)
3. Update pipeline to write per-request metrics
4. Align aggregator queries with new schema

**Schema:**
```sql
CREATE TABLE metrics_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_id TEXT,
    model TEXT NOT NULL,
    semantic_cache_hit INTEGER DEFAULT 0,
    duration_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    template_name TEXT,
    enhancements TEXT DEFAULT '[]'
)
```

### Issue 3: Refactor Aggregator to Async (aiosqlite)

**Problem:** Synchronous sqlite3 blocks async event loop.

**Solution:**
1. Replace `sqlite3` with `aiosqlite` in `aggregator.py`
2. Make all methods `async def`
3. Use `async with self._db.execute(...)` pattern
4. Update router to `await` aggregator calls

### Issue 4: Decouple from `_metrics` Global

**Problem:** `router.py:556` accesses private `_metrics._db.db_path`.

**Solution:**
1. Initialize `MetricsAggregator` in `main.py` lifespan
2. Store in `app.state.metrics_aggregator`
3. Inject via `request.app.state.metrics_aggregator` in router
4. Remove hardcoded import and private attribute access

### Issue 5: Add Input Validation

**Problem:** `hours` parameter accepts arbitrary integers.

**Solution:**
```python
hours = max(1, min(hours, 8760))  # Clamp to 1h - 365d
```

---

## Implementation Order

1. **storage.py** - Add `metrics_requests` table + `insert_request()` method
2. **collector.py** - Extend `record_request()` signature
3. **aggregator.py** - Full async refactor with aiosqlite
4. **main.py** - Initialize aggregator in lifespan, store in app.state
5. **router.py** - Use app.state injection, add validation
6. **pipeline.py** - Write per-request metrics after each request
7. **tests** - Add comprehensive test coverage

---

## Test Plan

### `tests/test_aggregator.py` (6+ tests)
- `test_compute_hourly_rollup_empty()`
- `test_compute_hourly_rollup_with_data()`
- `test_compute_daily_rollup_unique_sessions()`
- `test_get_cache_hit_rate_zero_division()`
- `test_get_token_savings_calculation()`
- `test_rollup_save_idempotent()`

### `tests/test_analytics_api.py` (Integration)
- `test_analytics_api_default_hours()`
- `test_analytics_api_custom_hours()`
- `test_analytics_api_hours_validation()`
- `test_analytics_api_empty_database()`
- `test_analytics_api_with_data()`

---

## Files to Modify

| File | Lines Changed | Type |
|------|---------------|------|
| `layercache/metrics/storage.py` | +50 | Add table + method |
| `layercache/metrics/aggregator.py` | ~200 | Async refactor |
| `layercache/metrics/collector.py` | +20 | Extend signature |
| `layercache/main.py` | +30 | Initialize aggregator |
| `layercache/dashboard/router.py` | +40 | Decouple + validate |
| `layercache/pipeline.py` | +30 | Write metrics |
| `tests/test_aggregator.py` | +150 | New file |
| `tests/test_analytics_api.py` | +120 | New file |

---

## Success Criteria

- [ ] All 5 critical issues resolved
- [ ] Tests pass with 80%+ coverage on new code
- [ ] No regressions in existing tests
- [ ] Lint + typecheck clean (`ruff`, `mypy`)
- [ ] Fix summary document created

---

## Risk Mitigation

- **Backward compatibility:** Preserve existing `metric_snapshots` table and functionality
- **Migration:** New `metrics_requests` table is additive, no schema migrations needed
- **Performance:** Async I/O prevents blocking; indexes on `created_at` and `session_id`

---

**Estimated Implementation Time:** 2-3 hours  
**Priority:** P3 (blocking analytics dashboard functionality)
