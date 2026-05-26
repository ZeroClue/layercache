# Roadmap

Consolidated direction for LayerCache — drawn from the PRD, TDD, architecture docs, and post-launch polish items that were previously scattered across the repository.

---

## Theme

LayerCache's evolution follows three phases:

1. **Single-instance proxy** (V1) — current; drop-in caching proxy
2. **Distributed infrastructure** (V2) — shared state, observability, horizontal scaling
3. **Platform ecosystem** (V3) — plugins, A/B testing, custom models

---

## Near-term (V2)

### P0 — Core distributed infrastructure

| Item | Dependencies | Notes |
|------|-------------|-------|
| **Redis semantic cache** | Production Redis (sentinel/cluster, auth, connection pooling) | Replace SQLite with Redis for shared state across instances. Not a straight swap — serialization, TTL, and eviction models differ from aiosqlite. |
| **Git-synced prompt registry** | SSH key management in container, Git hosting webhook | Watch a Git repo for prompt template changes instead of mounting YAML files. Adds network dependency and credential rotation burden. Enables GitOps workflows. |

### P1 — Observability & operations

| Item | Dependencies | Notes |
|------|-------------|-------|
| **Kubernetes Helm chart** | Ongoing maintenance (every config change needs chart update) | Production-grade chart with PVC, HPA, PodDisruptionBudget, service mesh support. Verify demand before starting — non-trivial upkeep. |
| **Prometheus + Grafana dashboard** | Centralized Prometheus | Export curated Grafana dashboard JSON with the cache-specific metrics. Well-scoped, one-time effort. |
| **Request/response logging** | Retention policy, S3/object-store sink | Configurable retention, structured JSON logs. Data volume and PII considerations need design before implementation. |

### P2 — Protocol enhancements

| Item | Dependencies | Notes |
|------|-------------|-------|
| **WebSocket support** | None | Persistent reverse-proxied connections for streaming use cases that need low-latency bidirectional transport. |
| **Client-level rate limiting** | Persistent key-value store (tied to Redis cache item) | Per-API-key or per-IP quotas enforced at the proxy layer. Counters must survive restarts. |
| **Anthropic auto cache_control** | Adapter interface, LiteLLM passthrough test | Automatically inject `cache_control` on system and historical messages instead of relying on layer-based manual placement. Design spec at `docs/designs/P1-anthropic-auto-cache-control.spec.md`. |
| **Cache invalidation API** | None | Endpoints to invalidate semantic cache entries by prefix hash or model. Currently no way to expire entries without deleting the database. |
| **Config JSON Schema** | None | Generate JSON Schema from `LayerCacheSettings` for IDE autocompletion on `layercache.yaml`. Small effort, high developer experience value. |

---

## Long-term (V3)

| Item | Dependencies | Notes |
|------|-------------|-------|
| **Multi-modal caching (CLIP)** | Redis cache | Hash image inputs with CLIP embeddings for vision requests (GPT-4V, Claude 3.5 Sonnet). Needs ~600MB model in memory. |
| **A/B testing framework** | Metrics DB, dashboard | Route a fraction of requests through different enhancement configs; compare quality/cost in the dashboard. |
| **Custom embedding models** | Plugin system | Allow swappable embedders (Ada-002, Cohere, local ONNX, etc.) instead of hard-coded bge-small-en. |
| **Multi-region deployment** | Redis cache, Git-synced registry | Active-active proxy instances in multiple regions with global cache replication. |

---

## Deprioritised / Icebox

Items that have been proposed but lack a clear trigger to schedule.

| Item | Reason |
|------|--------|
| Advanced dashboard auth (RBAC, OIDC) | SessionMiddleware + proxy API key is sufficient for a local tool |
| Semantic cache streaming | Low demand — semantic cache already returns responses as pseudo-streams |
| Multi-provider cache transparency | Providers converge on automatic prefix caching; explicit per-provider control diminishing in value |
| Plugin marketplace | Too vague to scope. Needs packaging format, sandboxing model, and pipeline integration design before scheduling |
