# Migrating from SQLite to Redis Backend

**LayerCache v1.5.0**

This guide provides step-by-step instructions for migrating LayerCache deployments from SQLite to Redis backend. The migration can be performed with zero downtime using a gradual cutover approach, or during a maintenance window for simpler execution.

---

## Table of Contents

1. [Overview](#overview)
2. [Pre-Migration Checklist](#pre-migration-checklist)
3. [Migration Steps](#migration-steps)
4. [Data Export](#data-export)
5. [Data Import](#data-import)
6. [Configuration Changes](#configuration-changes)
7. [Testing](#testing)
8. [Rollback](#rollback)
9. [Post-Migration](#post-migration)
10. [FAQ](#faq)

---

## 1. Overview

### Migration Scenarios

LayerCache v1.4.0 and earlier use SQLite as the sole backend for semantic caching. Version 1.5.0 introduces Redis as an alternative backend with automatic SQLite fallback, enabling:

- **Multi-agent concurrent access** without file locking issues
- **Horizontal scaling** across multiple LayerCache instances
- **Improved performance** under high concurrency
- **Centralized cache management** for distributed deployments

### Downtime Expectations

| Approach | Downtime | Complexity | Recommended For |
|----------|----------|------------|-----------------|
| **Zero-Downtime Cutover** | None | Medium | Production environments, high-availability requirements |
| **Maintenance Window** | 5-15 minutes | Low | Single-instance deployments, development environments |

### Important Limitations

**Semantic cache entries are not directly migratable between backends.** The SQLite and Redis implementations use different internal key structures:

- SQLite: Single table with BLOB storage for embeddings
- Redis: Multiple key types (hash entries, sorted set indexes, TTL keys)

Additionally, semantic cache keys are derived from:
1. **Prefix hash** (SHA-256 of L0+L1+L2 content)
2. **Query embedding** (vector from FastEmbed model)

This means cache entries are inherently tied to the specific prompt structure and cannot be meaningfully exported/imported across different system configurations. **Expect cache warm-up period after migration.**

---

## 2. Pre-Migration Checklist

Complete these steps before beginning the migration:

### Infrastructure Requirements

- [ ] Redis server installed and accessible (Redis 6.0+ recommended)
- [ ] Redis connection string configured (format: `redis://host:port/db`)
- [ ] Network connectivity verified between LayerCache and Redis
- [ ] Redis authentication credentials prepared (if using AUTH)
- [ ] Redis memory capacity sized for expected cache volume

### Backup Requirements

- [ ] Current SQLite database location identified
- [ ] Backup storage space available (2x database size minimum)
- [ ] Backup script tested and verified
- [ ] Rollback procedure documented and tested

### Testing Requirements

- [ ] Staging environment prepared (recommended)
- [ ] Load testing plan prepared
- [ ] Smoke test checklist created
- [ ] Monitoring dashboards accessible

### Redis Verification

```bash
# Verify Redis is running and accessible
redis-cli -h <redis-host> -p <redis-port> ping
# Expected output: PONG

# Check Redis version (6.0+ recommended)
redis-cli -h <redis-host> -p <redis-port> info server | grep redis_version

# Verify available memory
redis-cli -h <redis-host> -p <redis-port> info memory | grep used_memory_human
```

### Backup Current Configuration

```bash
# Backup current layercache.yaml
cp /path/to/layercache.yaml /path/to/layercache.yaml.backup.$(date +%Y%m%d-%H%M%S)

# Verify backup exists
ls -la /path/to/layercache.yaml.backup.*
```

---

## 3. Migration Steps

### Approach A: Zero-Downtime Cutover (Recommended for Production)

This approach uses LayerCache's built-in fallback mechanism to gradually transition traffic.

#### Step 1: Deploy Redis Alongside SQLite

```bash
# Start Redis server (example using Docker)
docker run -d --name layercache-redis \
  -p 6379:6379 \
  -v /data/redis:/data \
  --restart unless-stopped \
  redis:7-alpine

# Verify Redis is healthy
docker exec layercache-redis redis-cli ping
```

#### Step 2: Update Configuration with Fallback

Edit `layercache.yaml` to add Redis configuration while keeping SQLite as primary:

```yaml
caching:
  semantic:
    enabled: true
    backend: sqlite  # Keep SQLite as primary initially
    db_path: /data/semantic_cache.db
    # Add Redis configuration for future cutover
    redis_url: redis://localhost:6379/0
    redis_pool_size: 10
    redis_timeout: 5.0
    default_ttl: 3600
    similarity_threshold: 0.95
```

#### Step 3: Deploy Updated Configuration

```bash
# Reload LayerCache (graceful restart)
# Method depends on your deployment:

# Docker Compose
docker-compose up -d --no-deps layercache

# Systemd
sudo systemctl reload layercache

# Kubernetes
kubectl rollout restart deployment/layercache
```

#### Step 4: Monitor for Issues

Watch logs for any Redis connection warnings:

```bash
# Tail LayerCache logs
journalctl -u layercache -f --since "5 minutes ago"

# Or Docker logs
docker logs -f layercache 2>&1 | grep -i "redis\|cache"
```

#### Step 5: Switch Backend to Redis

Once confident in Redis connectivity, update `layercache.yaml`:

```diff
 caching:
   semantic:
     enabled: true
-    backend: sqlite
+    backend: redis
     db_path: /data/semantic_cache.db
     redis_url: redis://localhost:6379/0
```

#### Step 6: Deploy and Verify

```bash
# Deploy with new backend
docker-compose up -d --no-deps layercache

# Watch for successful initialization
docker logs -f layercache 2>&1 | grep "Using Redis cache backend"
# Expected: "Using Redis cache backend at redis://localhost:6379/0"
```

### Approach B: Maintenance Window (Simpler)

For single-instance deployments where brief downtime is acceptable:

#### Step 1: Stop LayerCache

```bash
# Docker Compose
docker-compose stop layercache

# Systemd
sudo systemctl stop layercache
```

#### Step 2: Update Configuration

Edit `layercache.yaml`:

```diff
 caching:
   semantic:
     enabled: true
-    backend: sqlite
+    backend: redis
     db_path: /data/semantic_cache.db
+    redis_url: redis://localhost:6379/0
+    redis_pool_size: 10
+    redis_timeout: 5.0
     default_ttl: 3600
     similarity_threshold: 0.95
```

#### Step 3: Start Redis (if not already running)

```bash
docker run -d --name layercache-redis \
  -p 6379:6379 \
  -v /data/redis:/data \
  --restart unless-stopped \
  redis:7-alpine
```

#### Step 4: Start LayerCache

```bash
# Docker Compose
docker-compose start layercache

# Systemd
sudo systemctl start layercache
```

#### Step 5: Verify Startup

```bash
# Check logs for successful initialization
docker logs layercache 2>&1 | tail -20

# Verify health endpoint
curl -s http://localhost:8000/health | jq .
```

---

## 4. Data Export

**Note:** Due to the embedding-based nature of semantic cache keys, exported data cannot be directly imported into Redis. However, you may want to export for archival or analysis purposes.

### Export SQLite Cache Entries

```python
#!/usr/bin/env python3
"""Export SQLite semantic cache entries to JSON for archival."""

import aiosqlite
import json
import asyncio
from pathlib import Path
from datetime import datetime

async def export_cache(db_path: str, output_path: str):
    """Export all cache entries to JSON."""

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT id, prefix_hash, query_text, response_payload,
                   model, ttl_expires_at, created_at
            FROM semantic_cache
            ORDER BY created_at DESC
        """)

        entries = []
        async for row in cursor:
            entries.append({
                "id": row["id"],
                "prefix_hash": row["prefix_hash"],
                "query_text": row["query_text"],
                "response_payload": json.loads(row["response_payload"]),
                "model": row["model"],
                "ttl_expires_at": row["ttl_expires_at"],
                "created_at": row["created_at"],
            })

        with open(output_path, "w") as f:
            json.dump({
                "exported_at": datetime.utcnow().isoformat(),
                "source_db": db_path,
                "entry_count": len(entries),
                "entries": entries,
            }, f, indent=2)

        print(f"Exported {len(entries)} entries to {output_path}")

if __name__ == "__main__":
    asyncio.run(export_cache(
        "/data/semantic_cache.db",
        "/data/cache_export.json"
    ))
```

### Export Cache Statistics

```bash
# Get current cache statistics before migration
curl -s http://localhost:8000/v1/cache/metrics | jq .

# Save to file for comparison
curl -s http://localhost:8000/v1/cache/metrics > /data/pre_migration_stats.json
```

### Backup SQLite Database File

```bash
# Create compressed backup
sqlite3 /data/semantic_cache.db ".backup '/data/semantic_cache.backup.$(date +%Y%m%d-%H%M%S).db'"

# Or using standard file copy (ensure database is not actively writing)
cp /data/semantic_cache.db /data/semantic_cache.backup.$(date +%Y%m%d-%H%M%S).db

# Verify backup integrity
sqlite3 /data/semantic_cache.backup.*.db "PRAGMA integrity_check;"
```

---

## 5. Data Import

### Why Direct Import Is Not Supported

The semantic cache cannot migrate entries between SQLite and Redis because:

1. **Embedding-dependent keys**: Cache lookup requires both prefix hash match AND query embedding similarity. Embeddings are computed at query time using FastEmbed.

2. **Different internal structures**:
   - SQLite: Single table with JSON-encoded embeddings in BLOB column
   - Redis: Multiple key types (`layercache:cache:*`, `layercache:index:*`, `layercache:ttl:*`)

3. **Entry ID generation**: Cache entry IDs include timestamps, making them non-transferable.

### Recommended Approach: Cache Warm-Up

Instead of data migration, allow the cache to naturally repopulate:

1. **Monitor cache hit rate** after migration
2. **Expect initial cache miss spike** as Redis cache is empty
3. **Cache will repopulate** automatically as requests are processed
4. **Typical warm-up time**: 1-4 hours depending on traffic volume

### Optional: Pre-warm Critical Cache Entries

If certain prompts are critical, you can pre-warm them:

```python
#!/usr/bin/env python3
"""Pre-warm cache with known important queries."""

import httpx
import asyncio

CRITICAL_QUERIES = [
    {"model": "gpt-4", "messages": [{"role": "user", "content": "What is our return policy?"}]},
    {"model": "gpt-4", "messages": [{"role": "user", "content": "How do I reset my password?"}]},
    # Add your critical queries here
]

# Replace with value from `proxy_api_key` in layercache.yaml or LAYERCACHE_API_KEY env var
API_KEY = "your-api-key"

async def warm_cache(query: dict, endpoint: str, api_key: str):
    """Send a query to warm the cache."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            endpoint,
            json=query,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        return response.status_code == 200

async def main():
    endpoint = "http://localhost:8000/v1/chat/completions"
    api_key = "your-api-key"

    results = await asyncio.gather(*[
        warm_cache(q, endpoint, api_key) for q in CRITICAL_QUERIES
    ])

    print(f"Warmed {sum(results)}/{len(results)} queries successfully")

# asyncio.run(main())
```

---

## 6. Configuration Changes

### Complete Configuration Diff

```diff
 # layercache.yaml

 caching:
   semantic:
     enabled: true
-    backend: sqlite
+    backend: redis

     # SQLite path (kept as fallback)
     db_path: /data/semantic_cache.db

+    # Redis configuration (new)
+    redis_url: redis://localhost:6379/0
+    redis_pool_size: 10
+    redis_timeout: 5.0
+
     # These settings apply to both backends
     default_ttl: 3600
     similarity_threshold: 0.95
     embedder: BAAI/bge-small-en-v1.5
      session_id_header: X-Session-ID
      session_id_auto_generate: false
```

### Redis Connection String Formats

| Scenario | Connection String |
|----------|-------------------|
| Local, no auth | `redis://localhost:6379/0` |
| Remote, no auth | `redis://redis-host:6379/0` |
| With password | `redis://:password@redis-host:6379/0` |
| With username/password | `redis://username:password@redis-host:6379/0` |
| SSL/TLS | `rediss://:password@redis-host:6379/0` |
| Redis Cluster | `redis://host1:6379,host2:6379,host3:6379` |

### Docker Compose Example

```yaml
version: '3.8'

services:
  layercache:
    image: layercache/layercache:v1.5.0
    ports:
      - "8000:8000"
    volumes:
      - ./layercache.yaml:/app/layercache.yaml
      - /data:/data
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    depends_on:
      - redis
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    command: redis-server --appendonly yes
    restart: unless-stopped

volumes:
  redis_data:
```

---

## 7. Testing

### Smoke Test Checklist

After migration, verify the following:

- [ ] LayerCache starts without errors
- [ ] Health endpoint returns 200 OK
- [ ] Cache metrics endpoint accessible
- [ ] Test request returns valid response
- [ ] Cache hit occurs on repeated identical request
- [ ] No errors in logs related to cache backend

### Verification Commands

```bash
# 1. Check health endpoint
curl -s http://localhost:8000/health | jq .
# Expected: {"status": "healthy", ...}

# 2. Check cache metrics
curl -s http://localhost:8000/v1/cache/metrics | jq .
# Expected: Shows Redis backend stats

# 3. Verify backend in logs
docker logs layercache 2>&1 | grep -E "Using (Redis|SQLite) cache backend"
# Expected: "Using Redis cache backend at redis://localhost:6379/0"

# 4. Test cache functionality
# Send a test request
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Test query for cache migration"}]
  }' | jq .

# Send the same request again (should be cached)
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Test query for cache migration"}]
  }' | jq '.lc_cache_hit'
# Expected: true (if lc_cache_hit field is returned)
```

### Load Testing (Optional)

```bash
# Install wrk if not available
# apt-get install wrk or brew install wrk

# Run load test (adjust URL and duration)
wrk -t4 -c100 -d60s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"load test"}]}'
```

### Monitoring During Migration

Watch these metrics for anomalies:

```bash
# Cache hit rate (should recover after warm-up period)
watch -n 5 'curl -s http://localhost:8000/v1/cache/metrics | jq ".cache.hit_rate"'

# Error rate in logs
tail -f /var/log/layercache/error.log | grep -c "ERROR"

# Redis memory usage
redis-cli info memory | grep used_memory_human
```

---

## 8. Rollback

If issues arise after migration, revert to SQLite using these steps.

### Quick Rollback (Immediate)

```bash
# 1. Stop LayerCache
docker-compose stop layercache

# 2. Revert configuration
cp /path/to/layercache.yaml.backup.* /path/to/layercache.yaml

# 3. Verify configuration shows SQLite backend
grep "backend:" /path/to/layercache.yaml
# Expected: backend: sqlite

# 4. Start LayerCache
docker-compose start layercache

# 5. Verify rollback
docker logs layercache 2>&1 | grep "Using SQLite cache backend"
```

### Rollback Configuration Diff

```diff
 caching:
   semantic:
     enabled: true
-    backend: redis
+    backend: sqlite
     db_path: /data/semantic_cache.db
-    redis_url: redis://localhost:6379/0
-    redis_pool_size: 10
-    redis_timeout: 5.0
     default_ttl: 3600
     similarity_threshold: 0.95
```

### Verify Rollback Success

```bash
# Check health
curl -s http://localhost:8000/health | jq .

# Verify cache functionality
curl -s http://localhost:8000/v1/cache/metrics | jq .

# Check logs for SQLite initialization
docker logs layercache 2>&1 | grep "Using SQLite cache backend"
# Expected: "Using SQLite cache backend at /data/semantic_cache.db"
```

### Rollback Validation Checklist

- [ ] LayerCache starts successfully
- [ ] Logs show SQLite backend initialization
- [ ] Health endpoint returns healthy status
- [ ] Test requests complete successfully
- [ ] Cache hits occur on repeated requests
- [ ] No errors in application logs
- [ ] Metrics dashboard shows SQLite backend

---

## 9. Post-Migration

### Monitoring

Set up monitoring for the following:

#### Redis Health

```bash
# Redis connectivity
redis-cli ping

# Memory usage (alert if > 80% of maxmemory)
redis-cli info memory | grep used_memory_ratio

# Connected clients
redis-cli info clients | grep connected_clients

# Keyspace statistics
redis-cli info keyspace
```

#### Cache Performance

```bash
# Cache hit rate (monitor over time)
curl -s http://localhost:8000/v1/cache/metrics | jq '.cache.hit_rate'

# Total entries
curl -s http://localhost:8000/v1/cache/metrics | jq '.cache.total_entries'

# Response latency
curl -s http://localhost:8000/metrics | grep layercache_request_duration
```

### Cleanup

After confirming stable operation (recommended: 7 days):

```bash
# Archive old SQLite database (optional)
mv /data/semantic_cache.db /data/semantic_cache.db.archived.$(date +%Y%m%d)

# Remove WAL files if present
rm -f /data/semantic_cache.db-wal /data/semantic_cache.db-shm

# Clean up old backups (keep at least 2)
ls -la /data/semantic_cache.backup.* | tail -n +3 | xargs rm -f
```

### Optimization

#### Redis Configuration Tuning

For production deployments, consider these Redis optimizations:

```bash
# Set maxmemory policy (recommended: allkeys-lru for cache)
redis-cli CONFIG SET maxmemory-policy allkeys-lru

# Set maxmemory limit (adjust based on available RAM)
redis-cli CONFIG SET maxmemory 2gb

# Enable AOF persistence
redis-cli CONFIG SET appendonly yes

# Make changes permanent (add to redis.conf)
echo "maxmemory-policy allkeys-lru" >> /etc/redis/redis.conf
echo "maxmemory 2gb" >> /etc/redis/redis.conf
```

#### LayerCache Configuration Tuning

Adjust these parameters based on observed performance:

```yaml
caching:
  semantic:
    # Increase pool size for high-concurrency deployments
    redis_pool_size: 20  # Default: 10

    # Adjust timeout based on network latency
    redis_timeout: 10.0  # Default: 5.0

    # Increase TTL if cache hit rate is low due to premature expiration
    default_ttl: 7200  # Default: 3600

    # Lower threshold for more aggressive caching (more hits, lower quality)
    similarity_threshold: 0.90  # Default: 0.95
```

### Performance Benchmarking

Compare before/after migration:

```bash
# Pre-migration baseline (if available)
cat /data/pre_migration_stats.json | jq .

# Post-migration stats
curl -s http://localhost:8000/v1/cache/metrics | jq .

# Key metrics to compare:
# - cache.hit_rate
# - cache.total_entries
# - Average response latency
```

---

## 10. FAQ

### Q: Will I lose all cached data during migration?

**A:** Yes, semantic cache entries cannot be migrated between SQLite and Redis due to different internal structures and embedding-dependent keys. Plan for a cache warm-up period of 1-4 hours depending on traffic volume.

### Q: Can I run both SQLite and Redis simultaneously?

**A:** LayerCache uses one backend at a time, configured via `backend: sqlite|redis`. However, the v1.5.0 factory includes automatic fallback to SQLite if Redis connection fails, providing resilience.

### Q: What happens if Redis becomes unavailable?

**A:** LayerCache automatically falls back to SQLite with a warning log message. The fallback is transparent to clients but may have different performance characteristics.

### Q: Do I need to change my application code?

**A:** No. The migration is transparent to client applications. The LayerCache API remains unchanged regardless of backend.

### Q: How much Redis memory do I need?

**A:** Memory requirements depend on cache volume. Estimate:
- Each cache entry: ~2-5 KB (depends on response size and embedding dimension)
- For 100,000 entries: ~200-500 MB
- Add 20% overhead for Redis internal structures

Monitor `used_memory_human` in Redis and adjust `maxmemory` accordingly.

### Q: Can I use Redis Cluster?

**A:** Yes, LayerCache supports Redis Cluster. Use a cluster connection string:
```yaml
redis_url: redis://host1:6379,host2:6379,host3:6379
```

### Q: What Redis version is required?

**A:** Redis 6.0+ is recommended. Redis 5.0 may work but lacks some features used for efficient key management.

### Q: How do I authenticate to Redis?

**A:** Include credentials in the connection URL:
```yaml
redis_url: redis://username:password@redis-host:6379/0
```

For sensitive deployments, use environment variable substitution in your deployment configuration.

### Q: Can I migrate back to SQLite later?

**A:** Yes, follow the rollback procedure in Section 8. Again, cache entries will not transfer, but the system will function normally with a warm-up period.

### Q: Does Redis provide better performance?

**A:** Under high concurrency (multiple simultaneous requests), Redis typically provides better performance due to:
- No file locking overhead
- Better concurrent read/write handling
- In-memory storage

For single-instance, low-traffic deployments, the difference may be negligible.

### Q: How do I monitor Redis health?

**A:** Use these commands:
```bash
# Basic health check
redis-cli ping

# Detailed stats
redis-cli info

# Slow log (debug slow queries)
redis-cli slowlog get 10

# Real-time monitoring
redis-cli --stat
```

### Q: What if migration fails mid-process?

**A:** The migration is non-destructive to the SQLite database. If Redis initialization fails, LayerCache automatically falls back to SQLite. Simply revert the configuration change and restart to return to pure SQLite operation.

---

## Support

For issues during migration:

1. Check LayerCache logs: `docker logs layercache` or `journalctl -u layercache`
2. Verify Redis connectivity: `redis-cli ping`
3. Review configuration syntax: `python -c "from layercache.config import LayerCacheSettings; LayerCacheSettings.from_yaml('layercache.yaml')"`
4. Consult GitHub issues: https://github.com/layercache/layercache/issues

---

**Document Version:** 1.0.0
**Last Updated:** 2026-05-27
**Applicable LayerCache Versions:** v1.5.0+
