# Deployment Guide

This guide covers deploying LayerCache to production environments, including Docker, Kubernetes, and bare-metal setups.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Deploy (Docker Compose)](#quick-deploy-docker-compose)
- [Docker Standalone](#docker-standalone)
- [Configuration](#configuration)
- [Environment Variables](#environment-variables)
- [Persistent Storage](#persistent-storage)
- [Health Checks & Monitoring](#health-checks--monitoring)
- [TLS / HTTPS](#tls--https)
- [Scaling Considerations](#scaling-considerations)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Troubleshooting](#troubleshooting)
- [Security Hardening](#security-hardening)

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Docker | 20.10+ | For containerized deployment |
| Docker Compose | 2.0+ | For multi-container setups |
| Python | 3.11+ | For bare-metal deployment |
| API Keys | — | At least one LLM provider key (Anthropic, OpenAI, or Google) |

---

## Quick Deploy (Docker Compose)

The fastest way to get LayerCache running in production.

### 1. Clone and Configure

```bash
git clone https://github.com/your-org/layercache.git
cd layercache
```

### 2. Set API Keys

Create a `.env` file in the project root (or export them directly):

```bash
# Required: At least one provider key
export ANTHROPIC_API_KEY=sk-ant-api03-...
export OPENAI_API_KEY=sk-...
export GOOGLE_API_KEY=AIza...

# Optional: Protect the proxy with an API key
export LAYERCACHE_PROXY_API_KEY=your-secret-key
```

### 3. Customize Configuration (Optional)

Edit `layercache.yaml` to adjust caching behavior, enhancement settings, and more. See the [Configuration](#configuration) section for details.

### 4. Start the Service

```bash
docker-compose up -d
```

### 5. Verify

```bash
curl http://localhost:8000/health
# {"status":"healthy","version":"1.4.0","semantic_cache":true}

curl http://localhost:8000/v1/cache/metrics
# {"llm_requests_total":0,"provider_token_cache_hit_rate":0,...}
```

### 6. Point Your Application

Update your LLM client's base URL:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-layercache-host:8000/v1",
    api_key="sk-ant-your-anthropic-key"  # Provider key
)
```

---

## Docker Standalone

### Build the Image

```bash
docker build -t layercache:1.4.0 .
```

### Run

```bash
docker run -d \
  --name layercache \
  -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e OPENAI_API_KEY=sk-... \
  -v ./data:/data \
  -v ./layercache.yaml:/app/layercache.yaml:ro \
  layercache:1.4.0
```

### Build Arguments

The Dockerfile pre-downloads the FastEmbed model during build to eliminate cold-start latency. This adds approximately 500MB to the image size but prevents a 10-15 second delay on the first request.

---

## Configuration

### Configuration File

LayerCache reads configuration from `layercache.yaml` (mounted at `/app/layercache.yaml` in the container).

#### Full Configuration Reference

```yaml
proxy:
  host: 0.0.0.0              # Bind address
  port: 8000                  # Listen port
  proxy_api_key: null         # Optional: Require API key to access the proxy
  log_level: info             # Logging level: debug, info, warning, error

providers:
  anthropic:
    api_key_env: ANTHROPIC_API_KEY    # Environment variable name for the API key
    base_url: null                     # Override Anthropic base URL (for proxies)
    default_model: null                # Default model if none specified
    max_retries: 3                     # Retry attempts on failure
    timeout: 120                       # Request timeout in seconds
  openai:
    api_key_env: OPENAI_API_KEY
    base_url: null
    default_model: null
    max_retries: 3
    timeout: 120
  gemini:
    api_key_env: GOOGLE_API_KEY
    base_url: null
    default_model: null
    max_retries: 3
    timeout: 120

caching:
  semantic:
    enabled: true                        # Enable/disable semantic cache
    backend: sqlite                      # Storage backend (sqlite)
    db_path: /data/semantic_cache.db     # Path to SQLite database
    default_ttl: 300                     # Default cache TTL in seconds
    similarity_threshold: 0.95           # Minimum cosine similarity for cache hit
    embedder: "BAAI/bge-small-en-v1.5"  # FastEmbed model name

  metrics:
    db_path: /data/metrics.db            # Path to metrics SQLite DB
    snapshot_interval: 60                 # Snapshot interval in seconds (min 30)
    retention_hours: 24                   # How long to retain snapshots

  max_session_tokens: 2000                # Optional: truncate L2 to keep within this token budget

enhancements:
  registered:
    - name: chain_of_thought
      class_path: "layercache.enhancements.chain_of_thought.ChainOfThoughtEnhancement"
    - name: structured_json
      class_path: "layercache.enhancements.structured_output.StructuredOutputEnhancement"
    - name: self_critique
      class_path: "layercache.enhancements.self_critique.SelfCritiqueEnhancement"
    - name: dynamic_few_shot
      class_path: "layercache.enhancements.dynamic_few_shot.DynamicFewShotEnhancement"
      config:
        vector_store: /data/few_shots/examples.json
        top_k: 3
```

### Environment Variable Overrides

Environment variables always take precedence over the YAML configuration file. This is useful for secret management in production (e.g., using Docker secrets, Kubernetes secrets, or cloud secret managers).

---

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `ANTHROPIC_API_KEY` | Anthropic Claude API key | If using Anthropic models |
| `OPENAI_API_KEY` | OpenAI GPT API key | If using OpenAI models |
| `GOOGLE_API_KEY` | Google Gemini API key | If using Gemini models |

---

## Persistent Storage

LayerCache uses the `/data` directory for persistent data. You **must** mount this as a volume to preserve state across container restarts.

### Directory Layout

```
/data/
├── semantic_cache.db         # SQLite database for semantic cache
├── metrics.db                # SQLite database for metric snapshots
├── prompts/                  # Prompt template files (YAML/JSON)
│   ├── code-assistant.yaml
│   └── writer.yaml
└── few_shots/                # Few-shot example files (JSON)
    └── examples.json
```

### Volume Mount

```bash
# Docker
-v /host/path/to/data:/data

# Docker Compose
volumes:
  - ./data:/data
```

### Backup Considerations

- **Semantic Cache DB**: This is a performance cache, not persistent storage. It can be safely deleted and will rebuild over time.
- **Prompt Templates**: These define your L0/L1 prompts. **Back these up** — they are critical for consistent behavior.
- **Few-Shot Examples**: Used for dynamic example retrieval. Back these up alongside templates.

---

## Health Checks & Monitoring

### Built-in Health Check

```bash
GET /health
```

Response:
```json
{
  "status": "healthy",
  "version": "1.4.0",
  "semantic_cache": true,
  "semantic_cache_stats": {
    "total_entries": 42,
    "valid_entries": 38
  }
}
```

### Web Dashboard

Access the management dashboard at `http://localhost:8000/dashboard`. Provides:

- **Overview**: Request rate, latency, cost savings charts
- **Models**: Per-model metrics breakdown
- **Cache**: Semantic cache browser
- **Templates**: Template CRUD management
- **Config**: In-browser config editor (hot-reload support)
- **Logs**: Live streaming log viewer (SSE)

If `proxy_api_key` is configured, the dashboard requires login.

### Prometheus Metrics

```bash
GET /metrics
```

Key metrics to monitor:
- `lc_llm_requests_total` — Total requests proxied
- `lc_semantic_cache_hits_total` — Requests served from semantic cache (zero LLM cost)
- `lc_cache_read_tokens_total` — Tokens read from provider prefix cache
- `lc_cost_saved_usd` — Estimated cost savings from caching
- `lc_request_duration_seconds_avg` — Average request duration

### Example Prometheus scrape config

```yaml
scrape_configs:
  - job_name: 'layercache'
    static_configs:
      - targets: ['layercache:8000']
    metrics_path: '/metrics'
    scrape_interval: 15s
```

### Recommended Alerting Rules

| Alert | Condition | Severity |
|-------|-----------|----------|
| High cache miss rate | `provider_token_cache_hit_rate < 0.3` for 5m | Warning |
| Semantic cache disabled | `semantic_cache` health check returns `false` | Warning |
| High error rate | HTTP 5xx rate > 5% for 2m | Critical |
| High latency | P95 duration > 5s for 5m | Warning |

---

## TLS / HTTPS

LayerCache itself does not handle TLS. In production, use a reverse proxy:

### Nginx Example

```nginx
server {
    listen 443 ssl;
    server_name llm-proxy.your-domain.com;

    ssl_certificate /etc/ssl/certs/layercache.pem;
    ssl_certificate_key /etc/ssl/private/layercache.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE streaming support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

### Caddy Example (Automatic HTTPS)

```
llm-proxy.your-domain.com {
    reverse_proxy localhost:8000
}
```

---

## Scaling Considerations

### Single Instance (Recommended for V1)

A single LayerCache instance can handle thousands of requests per minute. The SQLite semantic cache and in-memory enhancement registry are optimized for single-node operation.

### Horizontal Scaling (Future)

See the [ROADMAP.md](ROADMAP.md) for the full plan. Summary of what changes:

1. **Replace SQLite with Redis** — Point the semantic cache at a Redis instance for shared state
2. **Externalize the Prompt Registry** — Use a shared volume mount (NFS, S3, Git-synced)
3. **Load Balance** — Place LayerCache behind an L4 load balancer (round-robin is fine)
4. **Centralized Metrics** — Point all instances at the same Prometheus server

### Resource Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 1 core | 2 cores |
| RAM | 512 MB | 2 GB (1 GB for embedding model) |
| Disk | 100 MB | 1 GB (semantic cache DB growth) |
| Network | Low | Low (standard API traffic) |

---

## Kubernetes Deployment

### Example Deployment Manifest

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: layercache
spec:
  replicas: 1
  selector:
    matchLabels:
      app: layercache
  template:
    metadata:
      labels:
        app: layercache
    spec:
      containers:
        - name: layercache
          image: layercache:1.4.0
          ports:
            - containerPort: 8000
          env:
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: llm-api-keys
                  key: anthropic
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: llm-api-keys
                  key: openai
          volumeMounts:
            - name: data
              mountPath: /data
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: layercache-data
---
apiVersion: v1
kind: Service
metadata:
  name: layercache
spec:
  selector:
    app: layercache
  ports:
    - port: 8000
      targetPort: 8000
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: layercache
spec:
  rules:
    - host: llm-proxy.your-domain.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: layercache
                port: 8000
```

### Create the Secret

```bash
kubectl create secret generic llm-api-keys \
  --from-literal=anthropic=sk-ant-... \
  --from-literal=openai=sk-...
```

---

## Troubleshooting

### First Request is Slow

**Cause**: The FastEmbed model is downloading on first use.

**Solution**: The Dockerfile pre-downloads the model during build. If you are running outside Docker, run this once:

```python
python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
```

### Cache Hits are Low

**Checklist**:
1. Are your system prompts identical across requests? Even minor whitespace changes break prefix caching.
2. Is the `tools` array sorted consistently? LayerCache sorts them, but only if passed correctly.
3. For Anthropic: Is the prefix at least 1024 tokens? Anthropic requires a minimum prefix length.
4. Check the metrics: `GET /v1/cache/metrics` — look at `provider_token_cache_hit_rate`.

### Semantic Cache Returns Wrong Answers

**Actions**:
1. Increase `similarity_threshold` in config (try 0.97 or 0.99).
2. Reduce `default_ttl` to prevent stale responses.
3. Have clients pass `lc_cache_ttl: 0` for sensitive queries.
4. Review semantic cache hit logs to identify false positive patterns.

### Provider API Errors

**Actions**:
1. Verify API keys are set correctly.
2. Check the provider's status page.
3. Review LayerCache logs for specific error messages.
4. Ensure the model name format is correct (`anthropic/claude-3-5-sonnet-20241022` for Anthropic via LiteLLM).

### Memory Usage is High

**Cause**: The FastEmbed model loads into RAM (~400MB) and the semantic cache grows over time.

**Solutions**:
1. Run `cleanup_expired()` periodically or set a cron job to call `POST /v1/cache/cleanup`.
2. Disable semantic cache if not needed (`caching.semantic.enabled: false`).
3. Set a lower `default_ttl` to reduce cache size.

---

## Security Hardening

### Production Checklist

- [ ] Set `proxy_api_key` in configuration to protect the proxy endpoint
- [ ] Use TLS/HTTPS (reverse proxy or ingress)
- [ ] Store API keys in secrets (not environment variables or config files)
- [ ] Restrict network access to the proxy (firewall rules)
- [ ] Set up log aggregation and monitoring
- [ ] Configure Prometheus alerting rules
- [ ] Regularly update dependencies (`pip install --upgrade`)
- [ ] Pin Docker image versions (avoid `latest` tag)
- [ ] Review and test backup/restore for prompt templates
- [ ] Set up rate limiting if exposing to untrusted clients
