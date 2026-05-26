# Roadmap

Consolidated direction for LayerCache — drawn from the PRD, TDD, architecture docs, and post-launch polish items that were previously scattered across the repository.

---

## Theme

LayerCache's evolution follows three phases:

1. **Single-instance proxy** (V1) — current; drop-in caching proxy
2. **Distributed infrastructure** (V2) — shared state, observability, horizontal scaling
3. **Platform ecosystem** (V3) — plugins, A/B testing, custom models

---

## Completed

These were originally future items that have been shipped.

| Item | Shipped In |
|------|-----------|
| Web-based metrics dashboard (Jinja2 + HTMX + Chart.js) | 1.2.0 |
| Config editor with atomic save + hot-reload | 1.3.0 |
| Persistent metrics DB with time-series snapshots | 1.2.0 |
| CSRF protection + rate limiting on config save | 1.3.0 |
| MetricsCollector thread-safety | 1.3.0 |
| L2 session truncation (`max_session_tokens`) | 1.4.0 |
| Prefix threshold diagnostic warning | 1.4.0 |

---

## Near-term (V2)

### P0 — Core distributed infrastructure

| Item | Dependencies | Notes |
|------|-------------|-------|
| **Redis semantic cache** | None | Replace SQLite with Redis for shared state across instances. Required for horizontal scaling. Cache protocol stays the same — swap backend. |
| **Git-synced prompt registry** | None | Watch a Git repo for prompt template changes instead of mounting YAML files. Enables GitOps workflows. |

### P1 — Observability & operations

| Item | Dependencies | Notes |
|------|-------------|-------|
| **Kubernetes Helm chart** | None | Production-grade chart with PVC, HPA, PodDisruptionBudget, service mesh support. |
| **Prometheus + Grafana dashboard** | Centralized Prometheus | Export curated Grafana dashboard JSON with the cache-specific metrics. |
| **Request/response logging** | None | Configurable retention, structured JSON logs, optional S3/object-store sink. |

### P2 — Protocol enhancements

| Item | Dependencies | Notes |
|------|-------------|-------|
| **WebSocket support** | None | Persistent reverse-proxied connections for streaming use cases that need low-latencey bidirectional transport. |
| **Client-level rate limiting** | None | Per-API-key or per-IP quotas enforced at the proxy layer. |

---

## Long-term (V3)

| Item | Dependencies | Notes |
|------|-------------|-------|
| **Multi-modal caching (CLIP)** | Redis cache | Hash image inputs with CLIP embeddings for vision requests (GPT-4V, Claude 3.5 Sonnet). |
| **A/B testing framework** | Metrics DB, dashboard | Route a fraction of requests through different enhancement configs; compare quality/cost in the dashboard. |
| **Custom embedding models** | Plugin system | Allow swapable embedders (Ada-002, Cohere, local ONNX, etc.) instead of hard-coded bge-small-en. |
| **Plugin marketplace** | A/B testing, custom embeddings | Third-party enhancement packages loaded at startup or via API. |
| **Multi-region deployment** | Redis cache, Git-synced registry | Active-active proxy instances in multiple regions with global cache replication. |

---

## Deprioritised / Icebox

Items that have been proposed but lack a clear trigger to schedule.

| Item | Reason |
|------|--------|
| Advanced dashboard auth (RBAC, OIDC) | SessionMiddleware + proxy API key is sufficient for a local tool |
| Semantic cache streaming | Low demand — semantic cache already returns responses as pseudo-streams |
| Multi-provider cache transparency | Providers converge on automatic prefix caching; explicit per-provider control diminishing in value |
