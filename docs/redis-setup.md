# LayerCache v1.5.0 — Production Redis Setup Guide

**Version:** 1.5.0  
**Audience:** DevOps Engineers, SREs  
**Last Updated:** May 2026

---

## 1. Overview

### Why Redis?

LayerCache v1.5.0 introduces Redis as an alternative backend for the semantic cache layer. Redis provides significant advantages over SQLite in production environments:

| Feature | SQLite | Redis |
|---------|--------|-------|
| **Concurrency** | File-level locks, single writer | Connection pooling, multi-threaded |
| **Session Isolation** | Shared database file | Native key namespacing per session |
| **Scalability** | Single-node only | Cluster-ready, horizontal scaling |
| **Latency** | ~1-5ms (local disk) | ~0.5-2ms (in-memory)[^1] |
| **Persistence** | ACID transactions | RDB snapshots + AOF logging |
| **Memory Management** | Disk-based | In-memory with configurable eviction |
| **Multi-Agent Access** | Limited concurrent writes | High concurrent read/write throughput |

### When to Use Redis

**Use Redis when:**
- Multiple agents/users access LayerCache concurrently
- You need sub-millisecond cache lookup latency
- Horizontal scaling is required (multiple LayerCache instances)
- Session isolation is critical (prevent cross-session cache pollution)
- High write throughput expected (frequent cache invalidations)

**Use SQLite when:**
- Single-user or development environment
- Minimal resource footprint required
- Simpler deployment (no external dependencies)
- Lower query volume (<100 requests/minute)

---

## 2. Quick Start

### Docker Compose Setup

The following `docker-compose.yml` provides a production-ready Redis deployment with LayerCache:

```yaml
version: "3.8"

services:
  redis:
    image: redis:7-alpine
    container_name: layercache-redis
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
      - ./redis.conf:/usr/local/etc/redis/redis.conf:ro
    command: redis-server /usr/local/etc/redis/redis.conf
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3
    networks:
      - layercache-net
    deploy:
      resources:
        limits:
          memory: 2G

  layercache:
    build: .
    container_name: layercache
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
      - ./layercache.yaml:/app/layercache.yaml:ro
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - GOOGLE_API_KEY=${GOOGLE_API_KEY}
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      redis:
        condition: service_healthy
    restart: unless-stopped
    networks:
      - layercache-net

  prometheus:
    image: prom/prometheus:latest
    container_name: lc-prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    restart: unless-stopped
    networks:
      - layercache-net

volumes:
  redis-data:
  prometheus-data:

networks:
  layercache-net:
    driver: bridge
```

### Minimal Redis Configuration (`redis.conf`)

```conf
# Basic Redis Configuration for LayerCache
bind 0.0.0.0
port 6379

# Persistence
appendonly yes
appendfsync everysec
save 900 1
save 300 10
save 60 10000

# Memory
maxmemory 2gb
maxmemory-policy allkeys-lru

# Logging
loglevel notice
logfile /var/log/redis/redis-server.log

# Security (uncomment for production)
# requirepass your-secure-password-here
# protected-mode yes
```

### LayerCache Configuration (`layercache.yaml`)

```yaml
proxy:
  host: "0.0.0.0"
  port: 8000
  log_level: "info"

providers:
  anthropic:
    api_key_env: "ANTHROPIC_API_KEY"
    default_model: "claude-sonnet-4-20250514"
    timeout: 120
    max_retries: 3

caching:
  semantic:
    enabled: true
    backend: "redis"  # Switch from sqlite to redis
    redis_url: "redis://redis:6379/0"
    redis_pool_size: 20
    redis_timeout: 5.0
    default_ttl: 3600
    similarity_threshold: 0.95
    session_isolation: true
    session_id_header: "X-Session-ID"
    session_id_auto_generate: true
  max_session_tokens: 8192
  truncation_strategy: "recent"
```

### Start the Stack

```bash
docker-compose up -d
```

Verify Redis connectivity:

```bash
docker-compose exec redis redis-cli ping
# Expected: PONG
```

---

## 3. Production Configuration

### Redis Server Tuning

#### Memory Configuration

```conf
# Set memory limit based on expected cache size
# Rule of thumb: 100K cache entries ≈ 500MB-1GB
maxmemory 4gb

# Eviction policy: remove least-recently-used keys when memory limit reached
maxmemory-policy allkeys-lru

# Enable active memory defragmentation
activedefrag yes
active-defrag-threshold-lower 10
active-defrag-threshold-upper 100
```

#### Persistence Settings

**RDB Snapshots (Point-in-time backups):**

```conf
save 900 1      # Save after 900 sec if at least 1 key changed
save 300 10     # Save after 300 sec if at least 10 keys changed
save 60 10000   # Save after 60 sec if at least 10000 keys changed

dbfilename dump.rdb
dir /data

# RDB compression (trade CPU for disk space)
rdbcompression yes
rdbchecksum yes
```

**AOF (Append-Only File) for durability:**

```conf
appendonly yes
appendfilename "appendonly.aof"

# Sync frequency: everysec = good balance of performance/durability
appendfsync everysec

# Rewrite AOF when it grows 100% larger than last rewrite
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb
```

#### Network Tuning

```conf
# TCP keepalive to detect dead connections
tcp-keepalive 300

# Connection backlog for high concurrency
tcp-backlog 511

# Timeout for idle client connections (0 = no timeout)
timeout 300
```

### Kernel-Level Optimizations

For bare-metal or VM deployments, optimize the host system:

```bash
# Disable Transparent Huge Pages (THP)
echo never > /sys/kernel/mm/transparent_hugepage/enabled

# Increase max connections
sysctl -w net.core.somaxconn=65535

# Increase memory overcommit
sysctl -w vm.overcommit_memory=1

# Disable swap (Redis performs poorly with swapping)
swapoff -a
```

Add to `/etc/sysctl.conf` for persistence:

```conf
net.core.somaxconn = 65535
vm.overcommit_memory = 1
```

---

## 4. LayerCache Configuration

### Redis Backend Configuration Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `backend` | string | `sqlite` | Cache backend (`sqlite` or `redis`) |
| `redis_url` | string | `redis://localhost:6379/0` | Redis connection URL |
| `redis_pool_size` | int | `10` | Connection pool size (1-100) |
| `redis_timeout` | float | `5.0` | Socket timeout in seconds |
| `default_ttl` | int | `3600` | Default cache TTL in seconds (0 = no expiry) |
| `similarity_threshold` | float | `0.95` | Minimum cosine similarity for cache hit |
| `session_isolation` | bool | `true` | Isolate cache entries by session ID |

### Redis URL Formats

**Basic connection:**
```yaml
redis_url: "redis://localhost:6379/0"
```

**With authentication:**
```yaml
redis_url: "redis://:password@localhost:6379/0"
```

**With username and password (Redis 6+ ACL):**
```yaml
redis_url: "redis://username:password@localhost:6379/0"
```

**TLS-encrypted connection:**
```yaml
redis_url: "rediss://:password@localhost:6379/0"
```

**Cluster connection (future support):**
```yaml
redis_url: "redis://node1:6379,node2:6379,node3:6379"
```

### Connection Pool Sizing

The `redis_pool_size` parameter controls concurrent connections from LayerCache to Redis:

| Deployment Size | Pool Size | Expected Concurrent Requests |
|-----------------|-----------|------------------------------|
| Small (dev) | 5-10 | <50 req/min |
| Medium | 10-25 | 50-500 req/min |
| Large | 25-50 | 500-2000 req/min |
| Enterprise | 50-100 | >2000 req/min |

**Formula:** `pool_size = (concurrent_requests × avg_request_time) / timeout`

Example: 100 concurrent requests, 2s avg time, 5s timeout → `pool_size = (100 × 2) / 5 = 40`

### TTL Strategy

Configure TTL based on content volatility:

```yaml
caching:
  semantic:
    # Short TTL for rapidly changing data
    default_ttl: 300  # 5 minutes

    # Or use request-level override via lc_cache_ttl
    # in the request extra_body
```

**TTL Recommendations:**

| Content Type | Recommended TTL |
|--------------|-----------------|
| System prompts | 86400 (24h) |
| User conversations | 3600 (1h) |
| Dynamic data lookups | 300 (5m) |
| Static knowledge | 604800 (7d) |
| Code generation | 1800 (30m) |

---

## 5. Session Isolation

### How It Works

LayerCache v1.5.0 implements session isolation to prevent cache pollution between different users or conversation contexts:

1. **Session ID Extraction:** LayerCache reads the `X-Session-ID` header from incoming requests
2. **Auto-Generation:** If missing and `session_id_auto_generate: true`, a UUID is generated
3. **Key Namespacing:** Cache entries are stored with session-aware keys in Redis
4. **Lookup Scoping:** Cache lookups only search within the current session's entries

### Redis Key Structure

```
layercache:cache:{prefix_hash}:{entry_id}     # Cache entry data
layercache:index:{prefix_hash}                # Sorted set index (session-scoped)
layercache:ttl:{entry_id}                     # TTL expiration tracking
```

### Configuration Options

```yaml
caching:
  semantic:
    session_isolation: true           # Enable session isolation
    session_id_header: "X-Session-ID" # Header name to read
    session_id_auto_generate: true    # Generate UUID if header missing
```

### Disabling Session Isolation

For use cases where cache sharing is desired (e.g., common knowledge base):

```yaml
caching:
  semantic:
    session_isolation: false
```

**Warning:** Disabling isolation may cause cross-session cache pollution. Only disable for stateless queries.

### Session ID Best Practices

| Practice | Recommendation |
|----------|----------------|
| **Format** | UUID v4 or opaque random string |
| **Length** | 32-64 characters |
| **Generation** | Server-side, cryptographically secure |
| **Rotation** | Per conversation thread or user session |
| **Logging** | Never log full session IDs (truncate for debugging) |

Example client request:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "X-Session-ID: usr_abc123-def456-ghi789" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

---

## 6. Monitoring

### Key Redis Metrics

Monitor these metrics via Prometheus (`/metrics` endpoint) or direct Redis CLI:

| Metric | Command | Alert Threshold | Description |
|--------|---------|-----------------|-------------|
| **Memory Usage** | `INFO memory` | >80% of maxmemory | Cache memory consumption |
| **Connected Clients** | `INFO clients` | >pool_size × instances | Active connections |
| **Hit Rate** | `INFO stats` (keyspace_hits/total) | <70% | Cache effectiveness |
| **Evicted Keys** | `INFO stats` (evicted_keys) | >100/min | Memory pressure |
| **Latency** | `LATENCY DOCTOR` | >10ms p99 | Performance degradation |
| **Connected Replicas** | `INFO replication` | <expected | Replication health |
| **Last Save Time** | `INFO persistence` (last_save_time) | >3600s ago | Backup freshness |
| **AOF Delay** | `INFO persistence` (aof_delayed_fsync) | >0 | Disk I/O bottleneck |

### Prometheus Metrics Endpoint

LayerCache exposes cache metrics at `/metrics`:

```prometheus
# Cache backend type
layercache_cache_backend{backend="redis"} 1

# Cache operations
layercache_cache_hits_total 1250
layercache_cache_misses_total 340
layercache_cache_stores_total 1590

# Cache size
layercache_cache_entries_total 847

# Redis connection pool
layercache_redis_pool_size 20
layercache_redis_pool_active 12
layercache_redis_pool_idle 8

# Latency histogram
layercache_cache_lookup_duration_seconds_bucket{le="0.01"} 1100
layercache_cache_lookup_duration_seconds_bucket{le="0.1"} 1240
layercache_cache_lookup_duration_seconds_bucket{le="1.0"} 1250
```

### Redis CLI Monitoring Commands

**Quick health check:**
```bash
redis-cli ping
# PONG
```

**Memory statistics:**
```bash
redis-cli INFO memory | grep used_memory_human
# used_memory_human:1.23G
```

**Cache hit rate:**
```bash
redis-cli INFO stats | grep -E "keyspace_hits|keyspace_misses"
# keyspace_hits:1250
# keyspace_misses:340
# Hit rate = 1250 / (1250 + 340) = 78.6%
```

**Slow log (commands >10ms):**
```bash
redis-cli SLOWLOG GET 10
```

**Real-time latency monitoring:**
```bash
redis-cli --latency-history -i 1
# min: 0, max: 3, avg: 0.5 (100 samples)
```

**Client connections:**
```bash
redis-cli CLIENT LIST | wc -l
# 24
```

### Alerting Thresholds

Configure alerts in your monitoring system:

| Alert | Condition | Severity |
|-------|-----------|----------|
| **RedisDown** | `redis_up == 0` for 1m | Critical |
| **HighMemory** | `used_memory / maxmemory > 0.85` for 5m | Warning |
| **LowHitRate** | `cache_hits / (cache_hits + cache_misses) < 0.6` for 10m | Warning |
| **HighLatency** | `p99_latency > 50ms` for 5m | Warning |
| **ConnectionExhaustion** | `connected_clients > pool_size × 0.9` for 5m | Critical |
| **NoRecentBackup** | `time() - last_save_time > 7200` | Warning |

---

## 7. Troubleshooting

### Common Issues and Solutions

#### Issue: Connection Refused

**Symptoms:**
```
Failed to initialize Redis cache: Connection refused
```

**Causes:**
- Redis service not running
- Wrong host/port in `redis_url`
- Network firewall blocking connection

**Solutions:**
```bash
# Check Redis status
docker-compose ps redis

# Verify Redis is listening
docker-compose exec redis redis-cli ping

# Check network connectivity
docker-compose exec layercache nc -zv redis 6379

# Review LayerCache logs
docker-compose logs layercache | grep -i redis
```

#### Issue: Authentication Failed

**Symptoms:**
```
Failed to initialize Redis cache: NOAUTH Authentication required
```

**Solutions:**
```yaml
# Update redis_url with password
caching:
  semantic:
    redis_url: "redis://:your-password@redis:6379/0"
```

#### Issue: Memory Limit Exceeded

**Symptoms:**
```
OOM command not allowed when used memory > 'maxmemory'
```

**Solutions:**
```conf
# Increase memory limit in redis.conf
maxmemory 8gb

# Or change eviction policy
maxmemory-policy allkeys-lru
```

```bash
# Runtime adjustment
redis-cli CONFIG SET maxmemory 8gb
redis-cli CONFIG SET maxmemory-policy allkeys-lru
```

#### Issue: High Latency

**Symptoms:**
- Cache lookups taking >50ms
- Request timeouts

**Solutions:**
```bash
# Check for slow commands
redis-cli SLOWLOG GET 100

# Check memory fragmentation
redis-cli INFO memory | grep fragmentation

# Defragment if ratio >1.5
redis-cli CONFIG SET activedefrag yes

# Check for large keys
redis-cli --bigkeys
```

#### Issue: Cache Not Working (Always Miss)

**Symptoms:**
- `layercache_cache_hits_total` stays at 0
- All requests go to LLM provider

**Solutions:**
```bash
# Verify entries exist
redis-cli KEYS "layercache:index:*"

# Check TTL expiration
redis-cli TTL layercache:ttl:{entry_id}

# Verify similarity threshold (may be too high)
# Lower threshold in layercache.yaml
caching:
  semantic:
    similarity_threshold: 0.90  # Default is 0.95
```

### Troubleshooting Decision Tree

```
┌─────────────────────────────────────────┐
│  LayerCache Redis Cache Not Working    │
└─────────────────┬───────────────────────┘
                  │
         ┌────────▼────────┐
         │  Can you ping   │
         │  Redis?         │
         └────────┬────────┘
           Yes    │    No
                  │
         ┌────────▼────────┐
         │  Check network  │
         │  - DNS resolve  │
         │  - Firewall     │
         │  - Service up   │
         └────────┬────────┘
                  │
         ┌────────▼────────┐
         │  Auth working?  │
         └────────┬────────┘
           Yes    │    No
                  │
         ┌────────▼────────┐
         │  Verify redis_  │
         │  url credentials│
         └────────┬────────┘
                  │
         ┌────────▼────────┐
         │  Cache entries  │
         │  exist?         │
         └────────┬────────┘
           Yes    │    No
                  │
         ┌────────▼────────┐
         │  Check TTL &    │
         │  similarity_    │
         │  threshold      │
         └────────┬────────┘
                  │
         ┌────────▼────────┐
         │  Review hit/    │
         │  miss ratio     │
         └─────────────────┘
```

---

## 8. Performance Tuning

### Connection Pool Optimization

**LayerCache Configuration:**

```yaml
caching:
  semantic:
    redis_pool_size: 30      # Increase for high concurrency
    redis_timeout: 3.0       # Reduce timeout for faster failover
```

**Trade-offs:**
- Larger pool = better concurrency but more Redis memory
- Smaller timeout = faster failure detection but more false positives

### Timeout Settings

| Setting | Recommended | Description |
|---------|-------------|-------------|
| `redis_timeout` | 3-5s | Socket read/write timeout |
| `timeout` (Redis) | 300s | Idle client disconnect |
| `tcp-keepalive` | 300s | TCP keepalive interval |

### Batch Operations

LayerCache uses Redis pipelines for atomic multi-key operations:

```python
async with self._redis.pipeline(transaction=True) as pipe:
    await pipe.set(cache_key, json.dumps(cache_entry))
    await pipe.set(ttl_key, str(now + effective_ttl))
    await pipe.zadd(index_key, {entry_id: now})
    await pipe.expire(cache_key, effective_ttl)
    await pipe.expire(ttl_key, effective_ttl)
    await pipe.expire(index_key, effective_ttl)
    await pipe.execute()
```

This reduces round-trips from 6 to 1 per cache store operation.

### Index Optimization

The sorted set index enables efficient time-based queries:

```bash
# Get entries by recency (newest first)
ZRANGE layercache:index:{prefix_hash} 0 -1 WITHSCORES

# Get entries in time range
ZRANGEBYSCORE layercache:index:{prefix_hash} {start} {end}
```

### Embedding Performance

Embeddings are the bottleneck in semantic cache lookups. Optimize with:

```yaml
caching:
  semantic:
    embedder: "BAAI/bge-small-en-v1.5"  # Fast, 384 dimensions
```

For higher throughput, consider:
- Larger embedding models (better quality, slower)
- Caching embeddings separately
- Async embedding generation in background

### Benchmarking

Test cache performance:

```bash
# Install redis-benchmark
apt-get install redis-tools

# Run benchmarks
redis-benchmark -h redis -p 6379 -n 10000 -c 50

# Expected results for LayerCache workload:
# SET: ~50000 ops/sec
# GET: ~80000 ops/sec
# ZADD: ~30000 ops/sec
# ZRANGE: ~25000 ops/sec
```

---

## 9. Security

### Authentication

**Enable password authentication:**

```conf
# redis.conf
requirepass SuperSecretPassword123!
```

**Update LayerCache config:**

```yaml
caching:
  semantic:
    redis_url: "redis://:SuperSecretPassword123!@redis:6379/0"
```

**Use environment variables (recommended):**

```yaml
caching:
  semantic:
    redis_url: "redis://:${REDIS_PASSWORD}@redis:6379/0"
```

```bash
# docker-compose.yml
environment:
  - REDIS_PASSWORD=${REDIS_PASSWORD}
```

### Redis 6+ ACL

For fine-grained access control:

```conf
# Create dedicated user for LayerCache
ACL SETUSER layercache on >LayerCachePass123 ~layercache:* +@read +@write +@keyspace
```

```yaml
caching:
  semantic:
    redis_url: "redis://layercache:LayerCachePass123@redis:6379/0"
```

### Network Isolation

**Docker network segmentation:**

```yaml
networks:
  layercache-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.28.0.0/16

services:
  redis:
    networks:
      - layercache-net
    # Do NOT expose to public network
    # ports:  # Remove public exposure
    #   - "6379:6379"
```

**Firewall rules (bare-metal):**

```bash
# Allow only LayerCache host
iptables -A INPUT -p tcp -s 10.0.1.50 --dport 6379 -j ACCEPT
iptables -A INPUT -p tcp --dport 6379 -j DROP
```

### TLS Encryption

**Enable TLS in Redis:**

```conf
# redis.conf
tls-port 6379
port 0  # Disable non-TLS

tls-cert-file /etc/redis/tls/redis.crt
tls-key-file /etc/redis/tls/redis.key
tls-ca-cert-file /etc/redis/tls/ca.crt

tls-auth-clients yes
```

**Update LayerCache connection URL:**

```yaml
caching:
  semantic:
    redis_url: "rediss://:password@redis:6379/0"
```

**Generate self-signed certificates (testing):**

```bash
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout redis.key -out redis.crt \
  -days 365 -subj '/CN=redis'
```

### Protected Mode

Enable Redis protected mode for default installations:

```conf
protected-mode yes
```

This blocks external connections unless authentication or binding is explicitly configured.

---

## 10. Backup & Recovery

### RDB Backup Strategy

**Scheduled RDB snapshots:**

```conf
save 900 1
save 300 10
save 60 10000

dbfilename dump.rdb
dir /data
```

**Manual backup:**

```bash
# Trigger background save
redis-cli BGSAVE

# Check save status
redis-cli LASTSAVE

# Copy RDB file
cp /data/dump.rdb /backup/dump-$(date +%Y%m%d-%H%M%S).rdb
```

**Automated backup script:**

```bash
#!/bin/bash
# /usr/local/bin/redis-backup.sh

BACKUP_DIR="/backup/redis"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RETENTION_DAYS=7

# Trigger save
redis-cli BGSAVE
sleep 5  # Wait for BGSAVE to complete

# Copy RDB
cp /data/dump.rdb "$BACKUP_DIR/dump-$TIMESTAMP.rdb"

# Compress
gzip "$BACKUP_DIR/dump-$TIMESTAMP.rdb"

# Remove old backups
find "$BACKUP_DIR" -name "dump-*.rdb.gz" -mtime +$RETENTION_DAYS -delete
```

**Cron job:**

```bash
# /etc/cron.d/redis-backup
0 2 * * * root /usr/local/bin/redis-backup.sh
```

### AOF Backup Strategy

**AOF configuration:**

```conf
appendonly yes
appendfsync everysec
appendfilename "appendonly.aof"

auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb
```

**Manual AOF backup:**

```bash
# Trigger AOF rewrite (compacts AOF file)
redis-cli BGREWRITEAOF

# Backup AOF file
cp /data/appendonly.aof /backup/appendonly-$(date +%Y%m%d-%H%M%S).aof
```

### Combined Backup Strategy (Recommended)

```yaml
# docker-compose.yml backup volume
volumes:
  - redis-data:/data
  - ./backups:/backups

# Backup both RDB and AOF
services:
  backup:
    image: redis:7-alpine
    volumes:
      - redis-data:/data:ro
      - ./backups:/backups
    command: >
      sh -c "cp /data/dump.rdb /backups/ &&
             cp /data/appendonly.aof /backups/"
    schedule: "0 3 * * *"  # Daily at 3 AM
```

### Recovery Procedures

**Full recovery from RDB:**

```bash
# Stop Redis
docker-compose stop redis

# Restore RDB file
cp /backup/dump-20250101-020000.rdb /data/dump.rdb

# Start Redis
docker-compose start redis
```

**Point-in-time recovery with AOF:**

```bash
# Stop Redis
docker-compose stop redis

# Restore base RDB
cp /backup/dump-20250101-020000.rdb /data/dump.rdb

# Apply AOF up to specific point
cp /backup/appendonly-20250101-140000.aof /data/appendonly.aof

# Start Redis (will replay AOF)
docker-compose start redis
```

**Verify recovery:**

```bash
# Check data integrity
redis-cli DBSIZE

# Sample keys
redis-cli KEYS "layercache:*" | head -20

# Check memory
redis-cli INFO memory | grep used_memory_human
```

### Disaster Recovery Plan

| Scenario | Recovery Time Objective (RTO) | Recovery Procedure |
|----------|-------------------------------|-------------------|
| Single Redis instance failure | <5 minutes | Restart container, auto-reload RDB |
| Data corruption | <30 minutes | Restore from last RDB + AOF |
| Complete data loss | <1 hour | Restore from off-site backup |
| Redis cluster failure | <15 minutes | Failover to replica, rebuild master |

### Backup Verification

Regularly test backup restoration:

```bash
# Monthly restore test
redis-cli --rdb /backup/dump-latest.rdb --csv > /tmp/verify.csv
wc -l /tmp/verify.csv  # Should match expected key count
```

---

## Appendix A: Complete Production Configuration

### `layercache.yaml` (Production)

```yaml
proxy:
  host: "0.0.0.0"
  port: 8000
  proxy_api_key: "${LAYERCACHE_API_KEY}"
  log_level: "info"

providers:
  anthropic:
    api_key_env: "ANTHROPIC_API_KEY"
    default_model: "claude-sonnet-4-20250514"
    timeout: 120
    max_retries: 3
    adapter: "anthropic"

  openai:
    api_key_env: "OPENAI_API_KEY"
    default_model: "gpt-4o"
    timeout: 120
    max_retries: 3

caching:
  semantic:
    enabled: true
    backend: "redis"
    redis_url: "redis://${REDIS_PASSWORD}@redis:6379/0"
    redis_pool_size: 30
    redis_timeout: 5.0
    default_ttl: 3600
    similarity_threshold: 0.95
    session_isolation: true
    session_id_header: "X-Session-ID"
    session_id_auto_generate: true
  max_session_tokens: 8192
  truncation_strategy: "recent"
  token_counter: "tiktoken"

metrics:
  db_path: "/data/metrics.db"
  snapshot_interval_seconds: 60
  snapshot_retention_days: 7
```

### `redis.conf` (Production)

```conf
# Network
bind 0.0.0.0
port 6379
protected-mode yes
tcp-backlog 511
timeout 300
tcp-keepalive 300

# Security
requirepass ${REDIS_PASSWORD}

# Memory
maxmemory 4gb
maxmemory-policy allkeys-lru
activedefrag yes

# Persistence
appendonly yes
appendfsync everysec
save 900 1
save 300 10
save 60 10000
dbfilename dump.rdb
dir /data

# Logging
loglevel notice
logfile /var/log/redis/redis-server.log

# Slow log (commands >10ms)
slowlog-log-slower-than 10000
slowlog-max-len 128
```

---

## Appendix B: Quick Reference Commands

```bash
# Health check
redis-cli ping

# Memory usage
redis-cli INFO memory | grep used_memory_human

# Cache entry count
redis-cli KEYS "layercache:index:*" | wc -l

# Clear all cache entries
redis-cli KEYS "layercache:*" | xargs redis-cli DEL

# Watch real-time operations
redis-cli MONITOR | grep layercache

# Export RDB backup
redis-cli BGSAVE && cp /data/dump.rdb ./backup.rdb

# Check connected clients
redis-cli CLIENT LIST

# Get latency stats
redis-cli LATENCY DOCTOR
```

---

[^1]: Redis latency "~0.5-2ms" refers to network round-trip time only; excludes embedding computation (~50-150ms).

**Support:** For issues or questions, refer to the LayerCache documentation at `/docs` or open an issue on the repository.
