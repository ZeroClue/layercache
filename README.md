<p align="center">
  <img src="https://img.shields.io/badge/version-1.1.0-blue" alt="Version 1.1.0">
  <img src="https://img.shields.io/badge/python-3.11+-green" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/tests-73%20passing-brightgreen" alt="73 Tests Passing">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="MIT License">
</p>

<h1 align="center">LayerCache</h1>

<p align="center">
  <strong>Intelligent Prompt Enhancement & Token Caching Proxy</strong><br>
  A self-hosted, provider-agnostic LLM proxy that cuts costs by 30-60% and latency by 40%+ through aggressive token caching and cache-safe prompt engineering.
</p>

---

## Table of Contents

- [Overview](#overview)
- [Why LayerCache?](#why-layercache)
- [Core Concept: The Layered Prompt Architecture](#core-concept-the-layered-prompt-architecture)
- [Features](#features)
- [Quick Start](#quick-start)
- [Usage Examples](#usage-examples)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Docker Deployment](#docker-deployment)
- [Architecture](#architecture)
- [Development](#development)
- [Documentation](#documentation)
- [License](#license)

---

## Overview

LayerCache sits between your application and LLM providers (Anthropic, OpenAI, Google Gemini). It is a **drop-in replacement** for your LLM provider's base URL — just point your OpenAI SDK at LayerCache.

In the background, LayerCache:

1. **Canonicalizes** your prompts for byte-for-byte deterministic output (maximizing prefix cache hits)
2. **Injects provider-specific cache markers** at stable layer boundaries
3. **Applies prompt enhancements** (Chain of Thought, few-shot examples, etc.) *without breaking the cache*
4. **Caches semantically similar queries** to bypass the LLM entirely on repeat requests
5. **Tracks metrics** — token savings, cost reduction, cache hit rates — via Prometheus

## Why LayerCache?

| Problem | LayerCache Solution |
|---------|---------------------|
| Prompt prefix cache misses due to whitespace/ordering differences | Automatic canonicalization ensures identical prompts produce byte-for-byte identical output |
| Adding prompt enhancements (CoT, few-shots) breaks provider caching | Layered architecture (L0-L4) ensures enhancements are injected *after* the cached prefix |
| No visibility into cache performance or cost savings | Built-in Prometheus metrics and JSON dashboard showing hit rates, tokens saved, and $ saved |
| Different providers have different caching mechanisms | Provider adapters handle Anthropic (ephemeral markers), OpenAI (auto-caching), and Gemini (CachedContent) |
| Repeated similar queries waste tokens and money | Semantic cache with embedding similarity matching bypasses the LLM for near-duplicate queries |

## Core Concept: The Layered Prompt Architecture

The key insight behind LayerCache is that prompts have **naturally occurring layers** with different stability profiles. By enforcing strict separation between these layers, we can optimize caching and enhance prompts without invalidating provider prefix caches.

| Layer | Content | Mutability | Cache Status |
|-------|---------|------------|--------------|
| **L0: System** | Core persona, safety rules, output format | Immutable | **Cached** |
| **L1: Context** | Domain knowledge, tool definitions, static few-shots | Updated rarely | **Cached** |
| **L2: Session** | Conversation history, user preferences | Per session/turn | **Cached** (short TTL) |
| **L3: Enhancement** | Dynamic instructions (CoT, RAG, dynamic few-shots) | Per request | **Uncached** |
| **L4: User Input** | The actual user query | Dynamic | **Uncached** |

Cache breakpoints are placed at L0/L1/L2 boundaries. Enhancements are injected at L3, ensuring they never invalidate the stable prefix.

## Features

### Phase 1: Cache Optimizer & Routing (MVP)
- **Prompt Canonicalizer** — Automatic whitespace normalization, JSON minification, tool sorting
- **Provider Cache Marker Injection** — Anthropic ephemeral markers, OpenAI prefix alignment, Gemini CachedContent
- **Universal Routing** — LiteLLM-based multi-provider routing with automatic failover
- **Cache Observability** — Prometheus metrics and JSON dashboard

### Phase 2: Cache-Safe Enhancements
- **Enhancement API** — Composable prompt engineering via request metadata
- **Suffix Injection** — Enhancements injected at L3, never breaking L0-L2 cache
- **Dynamic Few-Shot Selector** — Embedding-based retrieval of relevant examples
- **Prompt Registry** — Named, versioned prompt templates (YAML/JSON)

### Phase 3: Semantic Cache
- **Local Embeddings** — FastEmbed (BAAI/bge-small-en-v1.5) in ProcessPoolExecutor
- **Dual-Key Strategy** — Prefix hash (exact) + query embedding (semantic similarity)
- **Configurable TTLs** — Per-request and default TTLs with automatic cleanup

## Quick Start

### Option 1: Docker (Recommended)

```bash
# Clone the repository
git clone https://github.com/your-org/layercache.git
cd layercache

# Set your API keys
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# Start the proxy
docker-compose up -d
```

### Option 2: pip install

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export ANTHROPIC_API_KEY=your-key
export OPENAI_API_KEY=your-key

# Run the proxy
uvicorn layercache.main:app --host 0.0.0.0 --port 8000
```

### Verify it works

```bash
curl http://localhost:8000/health
# {"status":"healthy","version":"1.1.0","semantic_cache":true}
```

## Usage Examples

### Basic Proxy (Zero Configuration)

Just point your existing OpenAI client at LayerCache. No code changes needed — caching works automatically.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-ant-your-anthropic-key"  # Provider key passed through
)

response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain async/await in Python."}
    ]
)
```

### With Cache-Safe Enhancements

Add Chain of Thought reasoning without breaking the cache prefix:

```python
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the time complexity of quicksort?"}
    ],
    extra_body={
        "lc_enhancements": ["chain_of_thought"]
    }
)
```

### Using a Prompt Template

Reference a named template from the registry instead of sending L0/L1 with every request:

```python
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[
        {"role": "user", "content": "Review this code for bugs."}
    ],
    extra_body={
        "lc_template": "code-assistant"
    }
)
```

### Controlling Semantic Cache

```python
# Skip semantic cache for this request
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    extra_body={
        "lc_cache_ttl": 0,           # No semantic caching
        "lc_enhancements": ["self_critique"]
    }
)

# Custom TTL (10 minutes)
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    extra_body={
        "lc_cache_ttl": 600
    }
)
```

### Checking Cache Performance

```bash
# JSON dashboard
curl http://localhost:8000/v1/cache/metrics

# Prometheus metrics
curl http://localhost:8000/metrics
```

## API Reference

### OpenAI-Compatible Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/chat/completions` | Chat completions (drop-in OpenAI replacement) |
| `GET` | `/v1/models` | List available models |

### Management Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/v1/cache/metrics` | Cache performance metrics (JSON) |
| `GET` | `/metrics` | Prometheus metrics (text/plain) |
| `GET` | `/v1/prompts/templates` | List prompt templates |
| `POST` | `/v1/prompts/templates` | Create/update a template |
| `DELETE` | `/v1/prompts/templates/{name}` | Delete a template |
| `POST` | `/v1/prompts/reload` | Reload templates from disk |

### LayerCache Request Extensions

These fields can be added to any `POST /v1/chat/completions` request:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `lc_template` | `string` | `null` | Name of a prompt template to use for L0/L1 |
| `lc_enhancements` | `string[]` | `[]` | Enhancement names to apply at L3 |
| `lc_cache_ttl` | `int` | `300` | Semantic cache TTL in seconds (0 = skip) |
| `lc_layer_hints` | `object` | `null` | Explicit `index -> layer` mapping |
| `lc_skip_semantic_cache` | `bool` | `false` | Skip semantic cache lookup entirely |
| `lc_bypass_cache` | `bool` | `false` | Skip all caching (semantic + provider) |

### Built-in Enhancements

| Name | Description |
|------|-------------|
| `chain_of_thought` | Instructs the LLM to reason step-by-step |
| `structured_json` | Enforces JSON output format (optional schema) |
| `self_critique` | Instructs the LLM to review and refine its own response |
| `dynamic_few_shot` | Retrieves relevant few-shot examples from a local vector store |

## Configuration

All configuration is done via `layercache.yaml`:

```yaml
proxy:
  host: 0.0.0.0
  port: 8000
  proxy_api_key: "your-optional-proxy-secret"  # Protect the proxy itself

caching:
  semantic:
    enabled: true
    db_path: /data/semantic_cache.db
    default_ttl: 300              # 5 minutes
    similarity_threshold: 0.95    # Cosine similarity for semantic cache
    embedder: "BAAI/bge-small-en-v1.5"

enhancements:
  registered:
    - name: chain_of_thought
    - name: structured_json
    - name: self_critique
    - name: dynamic_few_shot
      config:
        vector_store: /data/few_shots/examples.json
        top_k: 3
```

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `ANTHROPIC_API_KEY` | Anthropic API key | If using Anthropic |
| `OPENAI_API_KEY` | OpenAI API key | If using OpenAI |
| `GOOGLE_API_KEY` | Google Gemini API key | If using Gemini |

## Docker Deployment

```bash
# Build and start
docker-compose up -d

# View logs
docker-compose logs -f layercache

# Stop
docker-compose down
```

### Docker Volumes

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| `./data` | `/data` | Persistent storage (cache DB, templates, examples) |
| `./layercache.yaml` | `/app/layercache.yaml` | Configuration file (read-only) |

## Architecture

```
Client Application
        │
        ▼
┌──────────────────────────────────────┐
│          LayerCache Proxy            │
│  ┌────────────────────────────────┐  │
│  │     Request Pipeline           │  │
│  │  1. Semantic Cache Lookup     │  │
│  │  2. Stratify (L0→L4)          │  │
│  │  3. Canonicalize              │  │
│  │  4. Enhance (L3 injection)    │  │
│  │  5. Inject Cache Markers      │  │
│  │  6. Route via LiteLLM         │  │
│  │  7. Handle Response           │  │
│  │  8. Store & Record Metrics    │  │
│  └────────────────────────────────┘  │
│                                      │
│  ┌──────────┐ ┌────────┐ ┌────────┐ │
│  │ Semantic │ │ Prompt │ │Metrics │ │
│  │  Cache   │ │Registry│ │Collector│ │
│  └──────────┘ └────────┘ └────────┘ │
└──────────────────────────────────────┘
        │         │         │
        ▼         ▼         ▼
   Anthropic   OpenAI    Gemini
```

## Development

### Prerequisites

- Python 3.11+
- pip

### Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=layercache --cov-report=term-missing
```

### Running Tests

```bash
# All tests
pytest tests/ -v

# Specific test file
pytest tests/test_stratifier.py -v

# With verbose output
pytest tests/ -v --tb=short
```

### Code Quality

```bash
# Lint and format
ruff check layercache/
ruff format layercache/

# Type checking
mypy layercache/
```

### Project Structure

```
layercache/
├── layercache/                   # Core package
│   ├── main.py                   # FastAPI application
│   ├── pipeline.py               # Request processing pipeline
│   ├── models.py                 # Pydantic data models
│   ├── stratifier.py             # L0-L4 message classification
│   ├── canonicalizer.py          # Prompt normalization
│   ├── config.py                 # YAML configuration
│   ├── adapters/                 # Provider cache marker injection
│   │   ├── anthropropic.py       # Anthropic cache_control
│   │   ├── openai.py             # OpenAI auto-caching
│   │   └── gemini.py             # Gemini CachedContent
│   ├── enhancements/             # Cache-safe prompt enhancements
│   │   ├── base.py               # BaseEnhancement ABC
│   │   ├── chain_of_thought.py   # Step-by-step reasoning
│   │   ├── structured_output.py  # JSON format enforcement
│   │   ├── self_critique.py      # Self-review injection
│   │   └── dynamic_few_shot.py   # Vector-based example retrieval
│   ├── cache/                    # Semantic caching
│   │   ├── semantic.py           # SQLite-backed cache
│   │   └── embedder.py           # FastEmbed wrapper
│   ├── metrics/                  # Observability
│   │   └── collector.py          # Prometheus + ROI tracking
│   └── registry/                 # Prompt template management
│       └── prompt_registry.py    # YAML/JSON template loader
├── tests/                        # Test suite (73 tests)
├── data/                         # Sample data
│   ├── prompts/                  # Prompt templates
│   └── few_shots/                # Few-shot examples
├── docs/                         # Documentation
│   ├── PRD.md                    # Product Requirements
│   ├── TDD.md                    # Technical Design
│   ├── IMPLEMENTATION_PLAN.md    # Sprint plan
│   ├── ARCHITECTURE.md           # Architecture deep-dive
│   ├── DEPLOYMENT.md             # Deployment guide
│   ├── USER_GUIDE.md             # User guide
│   └── API.md                    # API reference
├── Dockerfile                    # Production image
├── docker-compose.yml            # Docker Compose config
├── layercache.yaml               # Default configuration
├── pyproject.toml                # Python project config
└── requirements.txt              # Dependencies
```

## Documentation

| Document | Description |
|----------|-------------|
| [PRD](docs/PRD.md) | Product Requirements Document |
| [TDD](docs/TDD.md) | Technical Design Document |
| [Implementation Plan](docs/IMPLEMENTATION_PLAN.md) | 8-sprint development roadmap |
| [Architecture](docs/ARCHITECTURE.md) | System architecture deep-dive |
| [Deployment Guide](docs/DEPLOYMENT.md) | Production deployment instructions |
| [User Guide](docs/USER_GUIDE.md) | Comprehensive usage guide |
| [API Reference](docs/API.md) | Full API documentation |
| [CHANGELOG](CHANGELOG.md) | Version history and changes |

## License

This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for the full text.

**What this means:**
- ✅ You can freely use, copy, modify, and distribute this software
- ✅ You can use it for commercial and private purposes
- ✅ You must include a copy of the license and copyright notice
- ⚠️  The software is provided "as-is" without warranty

For more information, visit https://opensource.org/licenses/MIT
