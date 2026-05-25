# Architecture

Deep dive into the LayerCache system architecture, components, data flow, and design decisions.

---

## Table of Contents

- [System Overview](#system-overview)
- [Request Pipeline](#request-pipeline)
- [Component Architecture](#component-architecture)
- [Data Models](#data-models)
- [Provider Adapters](#provider-adapters)
- [Enhancement Engine](#enhancement-engine)
- [Semantic Cache](#semantic-cache)
- [Prompt Registry](#prompt-registry)
- [Metrics & Observability](#metrics--observability)
- [Configuration System](#configuration-system)
- [Design Decisions](#design-decisions)
- [Performance Characteristics](#performance-characteristics)
- [Future Architecture Evolution](#future-architecture-evolution)

---

## System Overview

LayerCache is an asynchronous Python proxy built on FastAPI. It sits between client applications and LLM providers, transparently optimizing prompts for caching, applying enhancements, and routing requests.

```
┌─────────────────┐     ┌──────────────────────────────────────┐     ┌─────────────┐
│                 │     │           LayerCache Proxy            │     │             │
│  Client         │────▶│                                      │────▶│ Anthropic   │
│  Applications   │     │  ┌────────────────────────────────┐  │     │             │
│                 │     │  │       Request Pipeline          │  │     ├─────────────┤
│  (OpenAI SDK,   │     │  │                                │  │     │             │
│   HTTP clients, │     │  │  1. Semantic Cache Lookup      │  │     │  OpenAI     │
│   LangChain,    │     │  │  2. Stratify (L0→L4)           │  │     │             │
│   any REST)     │     │  │  3. Canonicalize               │  │     ├─────────────┤
│                 │     │  │  4. Enhance (L3 injection)      │  │     │             │
│                 │◀────│  │  5. Inject Cache Markers       │  │     │  Gemini     │
│                 │     │  │  6. Route via LiteLLM           │  │     │             │
│                 │     │  │  7. Handle Response             │  │     │             │
│                 │     │  │  8. Store & Record Metrics      │  │     │             │
│                 │     │  └────────────────────────────────┘  │     │             │
│                 │     │                                      │     │             │
│                 │     │  ┌──────────┐ ┌────────┐ ┌────────┐│     │             │
│                 │     │  │ Semantic │ │ Prompt │ │Metrics ││     │             │
│                 │     │  │  Cache   │ │Registry│ │Collector│     │             │
│                 │     │  └──────────┘ └────────┘ └────────┘│     │             │
└─────────────────┘     └──────────────────────────────────────┘     └─────────────┘
```

---

## Request Pipeline

Every request flows through an 8-stage pipeline implemented in `pipeline.py`:

### Stage 1: Semantic Cache Lookup

**Purpose**: Bypass the LLM entirely for semantically similar queries seen before.

- Compute a temporary `StratifiedPrompt` from the raw messages
- Generate a prefix hash (SHA-256 of L0+L1+L2)
- Generate a query embedding (L4 content via FastEmbed)
- Look up the SQLite cache for matching entries (prefix hash + cosine similarity > 0.95)
- If hit: return cached response immediately (zero LLM tokens, < 20ms latency)
- If miss: proceed to Stage 2

**Skip conditions**: `lc_skip_semantic_cache`, `lc_bypass_cache`, or semantic cache disabled.

### Stage 2: Stratification

**Purpose**: Classify each message into the L0-L4 layer architecture.

The Stratifier supports three classification modes:
1. **Template mode**: L0/L1 loaded from the Prompt Registry, remaining messages auto-classified
2. **Explicit hints**: Client provides `lc_layer_hints` mapping message indices to layer names
3. **Heuristic mode** (default): Automatic classification based on role, position, and content patterns

### Stage 3: Canonicalization

**Purpose**: Normalize all content for deterministic, byte-for-byte identical output.

- Strip and normalize whitespace in all message content
- Collapse multiple newlines and spaces
- Minify JSON schemas in tool definitions
- Sort tools alphabetically by `function.name`
- Sort messages within the same layer by content hash

### Stage 4: Enhancement Injection

**Purpose**: Apply cache-safe prompt engineering techniques at L3.

- Read `lc_enhancements` from the request
- Look up each named enhancement in the `EnhancementRegistry`
- Apply enhancements in order (they append messages to L3 only)
- Dynamic Few-Shot uses async embedding for query similarity search

**Critical invariant**: Enhancements NEVER modify L0, L1, L2, or L4. The prefix hash remains unchanged.

### Stage 5: Cache Marker Injection

**Purpose**: Add provider-specific cache control markers at stable layer boundaries.

- Detect the provider from the model name
- Instantiate the appropriate adapter (Anthropic, OpenAI, or Gemini)
- The adapter translates L0-L4 boundaries into provider-specific API parameters

### Stage 6: Provider Routing

**Purpose**: Send the processed request to the LLM provider via LiteLLM.

- Build the final LiteLLM payload from the stratified prompt
- Pass the provider API key from the Authorization header
- LiteLLM handles HTTP connection pooling, retries, and failover

### Stage 7: Response Handling

**Purpose**: Process the LLM response and extract cache metrics.

- Parse the provider response format
- Extract cache usage data (tokens read from cache, tokens written to cache)
- Record metrics in the MetricsCollector

### Stage 8: Cache Storage

**Purpose**: Store the response in the semantic cache for future lookups.

- Generate the prefix hash and query embedding
- Store the response payload with TTL in the SQLite cache
- Handle errors gracefully (cache store failure does not affect the response)

---

## Component Architecture

### Stratifier (`stratifier.py`)

**Single Responsibility**: Convert raw message arrays into `StratifiedPrompt` objects.

```
Input: list[dict] (OpenAI format) + optional template/hints
Output: StratifiedPrompt (messages organized by LayerType)
```

**Classification rules**:
- `role=system` at index 0 → L0 (core persona)
- `role=system` with tool keywords or >500 chars → L1 (context)
- `role=assistant` or `role=tool` → L2 (session history)
- `role=user` at the final index → L4 (user query)
- `role=user` at non-final indices → L2 (session history)

### Canonicalizer (`canonicalizer.py`)

**Single Responsibility**: Normalize content for deterministic output.

**Non-negotiable rule**: Never alter the semantic meaning of any message. Only formatting changes are permitted.

### Adapters (`adapters/`)

**Single Responsibility**: Translate L0-L4 boundaries into provider-specific API parameters.

Each adapter implements the `BaseAdapter` interface:
- `inject_markers(prompt, payload) -> payload` — Add cache markers
- `extract_cache_metrics(response) -> dict` — Parse cache usage from response

### Enhancement Engine (`enhancements/`)

**Single Responsibility**: Apply composable prompt engineering at L3 only.

**Plugin architecture**: Each enhancement is a class implementing `BaseEnhancement`:
- `name` — Unique string identifier
- `apply(prompt, **kwargs) -> prompt` — Modify the prompt

### Semantic Cache (`cache/semantic.py`)

**Single Responsibility**: Store and retrieve LLM responses based on query similarity.

**Dual-key design**:
1. **Exact key**: SHA-256 hash of L0+L1+L2 content
2. **Semantic key**: Embedding of L4 (user query)

A cache hit requires BOTH keys to match (prefix hash exact, query embedding similarity > threshold).

### Prompt Registry (`registry/prompt_registry.py`)

**Single Responsibility**: Manage named, versioned prompt templates.

Stores L0 and L1 content on the server, ensuring all requests using the same template have byte-for-byte identical prefixes.

### Metrics Collector (`metrics/collector.py`)

**Single Responsibility**: Track, aggregate, and expose cache performance metrics.

Tracks per-request and aggregate statistics with both JSON and Prometheus output formats.

---

## Data Models

### StratifiedPrompt

The central data structure. All components operate on this representation:

```python
class StratifiedPrompt(BaseModel):
    layers: dict[LayerType, list[StratifiedMessage]]
    
    def reassemble(self) -> list[dict]           # L0→L4 flattened messages
    def prefix_hash(self) -> str                  # SHA-256 of L0+L1+L2
    def get_user_query(self) -> str               # Extract L4 content
    def clone(self) -> StratifiedPrompt           # Deep copy
```

### LayerType Enum

```python
class LayerType(str, Enum):
    SYSTEM = "L0_SYSTEM"        # Cacheable (immutable)
    CONTEXT = "L1_CONTEXT"       # Cacheable (rarely changes)
    SESSION = "L2_SESSION"      # Cacheable (per-conversation)
    ENHANCEMENT = "L3_ENHANCEMENT"  # Not cached
    USER = "L4_USER"            # Not cached
```

### Data Flow

```
HTTP Request (JSON)
    │
    ▼
LayerCacheRequest (Pydantic validation)
    │
    ▼
StratifiedPrompt (layers dict)
    │
    ├── Semantic Cache: prefix_hash() + embed(L4)
    ├── Canonicalizer: normalize each layer's content
    ├── Enhancements: append to L3 layer
    ├── Adapter: reassemble() + inject provider markers
    │
    ▼
LiteLLM Payload (dict)
    │
    ▼
LLM Provider Response (dict)
    │
    ▼
HTTP Response (JSON/SSE)
```

---

## Provider Adapters

### Adapter Selection

The provider is auto-detected from the model name:

| Model Name Pattern | Detected Provider | Adapter |
|-------------------|-------------------|---------|
| `anthropic/...`, `claude-*` | Anthropic | `AnthropicAdapter` |
| `openai/...`, `gpt-*` | OpenAI | `OpenAIAdapter` |
| `gemini/...`, `google/...` | Gemini | `GeminiAdapter` |

### Anthropic Adapter Detail

```python
# For each stable layer boundary (end of L0, L1, L2):
msg_dict["content"] = [
    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
]
```

Anthropic caches from the start of the prompt up to each `cache_control` marker. By placing markers at each layer boundary, we get:
- L0 cached (used by all requests with the same system prompt)
- L0+L1 cached (used by all requests with the same context)
- L0+L1+L2 cached (full conversation prefix)

### Gemini Adapter Detail

The Gemini adapter maintains an in-memory mapping:

```python
_cache_map: dict[str, str]  # prefix_hash -> CachedContentName
_pending_creates: set[str]  # hashes awaiting background creation
```

On the first request with a new prefix hash:
1. Send the full prompt to the LLM (no cache benefit)
2. Trigger background creation of a `CachedContent` resource

On subsequent requests:
1. Use the cached content reference
2. Only send L2+ content (L0+L1 is in the cache)

---

## Enhancement Engine

### Enhancement Lifecycle

```
Request arrives with lc_enhancements: ["chain_of_thought", "dynamic_few_shot"]
    │
    ▼
EnhancementRegistry.apply_enhancements(prompt, names)
    │
    ├── ChainOfThoughtEnhancement.apply(prompt)
    │   └── Appends user/assistant pair at L3
    │
    ├── StructuredOutputEnhancement.apply(prompt)
    │   └── Appends JSON format instruction at L3
    │
    └── DynamicFewShotEnhancement.apply_async(prompt)
        ├── Embed L4 query
        ├── Search vector store for top-K examples
        └── Append examples at L3
    │
    ▼
L0-L2 unchanged → prefix_hash() identical → cache preserved!
```

### Cache Safety Proof

```
Before enhancements:
  prefix_hash("You are helpful." + "You have tools.") = "abc123..."

After enhancements:
  prefix_hash("You are helpful." + "You have tools.") = "abc123..."  # IDENTICAL!

The enhancement messages live in L3, which is excluded from the prefix hash.
```

---

## Semantic Cache

### Storage Schema

```sql
CREATE TABLE semantic_cache (
    id TEXT PRIMARY KEY,
    prefix_hash TEXT NOT NULL,        -- SHA-256 of L0+L1+L2
    query_text TEXT NOT NULL,          -- Original user query
    query_embedding BLOB NOT NULL,     -- FastEmbed vector (JSON)
    response_payload TEXT NOT NULL,     -- Full LLM response (JSON)
    model TEXT NOT NULL,               -- Model name
    ttl_expires_at REAL NOT NULL,      -- Unix timestamp
    created_at REAL NOT NULL
);

-- Indexes for fast lookup
CREATE INDEX idx_prefix_hash ON semantic_cache(prefix_hash);
CREATE INDEX idx_ttl_expires ON semantic_cache(ttl_expires_at);
```

### Lookup Algorithm

```python
async def lookup(prompt):
    prefix_hash = sha256(L0 + L1 + L2)
    query_embedding = embed(L4)
    
    # Find entries with matching prefix AND similar query
    entries = db.query(
        prefix_hash = prefix_hash,
        ttl_expires_at > now()
    )
    
    for entry in entries:
        similarity = cosine_similarity(query_embedding, entry.embedding)
        if similarity > 0.95:
            return entry.response  # Cache hit!
    
    return None  # Cache miss
```

### Embedding Pipeline

```
User Query (L4)
    │
    ▼
ProcessPoolExecutor  ←─── Avoids blocking the async event loop
    │
    ▼
FastEmbed (BAAI/bge-small-en-v1.5)
    │
    ▼
384-dimensional float vector
    │
    ▼
JSON-serialized → stored in SQLite BLOB
```

---

## Metrics & Observability

### Metrics Hierarchy

```
MetricsCollector
├── Counters
│   ├── llm_requests_total
│   ├── semantic_cache_hits_total
│   ├── semantic_cache_misses_total
│   ├── total_input_tokens
│   ├── total_output_tokens
│   ├── total_cache_read_tokens
│   └── total_cache_creation_tokens
├── Gauges
│   ├── estimated_tokens_saved
│   ├── estimated_cost_saved_usd
│   └── estimated_total_cost_usd
├── Histograms
│   ├── request_latencies (avg, p95)
│   └── per-request duration tracking
└── Per-Model Breakdown
    ├── model_requests
    ├── model_input_tokens
    ├── model_cache_read_tokens
    └── model_provider_cache_hit_rate
```

### Cost Calculation

For each request, LayerCache calculates:

```
cost_saved = cache_read_tokens * (input_price - cache_read_price) / 1,000,000
```

Example for Claude 3.5 Sonnet:
- Input price: $3.00 per 1M tokens
- Cache read price: $0.30 per 1M tokens
- Savings: $2.70 per 1M cached tokens

---

## Configuration System

### Loading Priority

```
1. layercache.yaml (file-based configuration)
2. Environment variables (override YAML values)
3. Request-level parameters (lc_cache_ttl, lc_template, etc.)
```

### Pydantic Settings

All configuration is validated via Pydantic models:

```python
LayerCacheSettings
├── ProxyConfig (host, port, api_key)
├── ProvidersConfig (anthropic, openai, gemini)
├── CachingConfig (semantic: enabled, ttl, threshold, embedder)
└── EnhancementsConfig (registered plugin list)
```

---

## Design Decisions

### Why Python + FastAPI?

- **Ecosystem**: LiteLLM, Pydantic, and async libraries are Python-first
- **Performance**: FastAPI with uvicorn provides async I/O sufficient for proxy workloads
- **Developer Experience**: Pydantic v2 provides type-safe configuration and request validation
- **Embedding ecosystem**: FastEmbed, sentence-transformers, and ONNX Runtime are Python-native

### Why SQLite for Semantic Cache (V1)?

- **Zero dependencies**: No Redis/Postgres infrastructure required
- **Sufficient performance**: < 20ms lookup for typical cache sizes (< 100K entries)
- **Simple deployment**: Single file, easy to backup, easy to delete and rebuild
- **Async support**: aiosqlite provides full async operation

### Why ProcessPoolExecutor for Embeddings?

- **CPU-bound**: Embedding generation is CPU-intensive and would block the async event loop
- **Isolation**: Separate process prevents GIL contention
- **Crash safety**: If the embedding process crashes, it does not affect the main server

### Why Not Modify the Prompt Text?

The canonicalizer only changes formatting, never semantics. This is a deliberate design choice:
- Eliminates the risk of altering the LLM's interpretation
- Makes the system predictable and auditable
- Tools and structured data (JSON schemas) benefit from canonicalization without content risk

---

## Performance Characteristics

### Latency Budget (Per Request)

| Component | Target | Notes |
|-----------|--------|-------|
| Semantic cache lookup | < 20ms | SQLite + cosine similarity |
| Canonicalization | < 5ms | Pure CPU, in-memory |
| Enhancement injection | < 2ms | String concatenation |
| Cache marker injection | < 1ms | Dict manipulation |
| **Total proxy overhead** | **< 50ms** | P95 on cache miss |
| Semantic cache hit | < 20ms | No LLM call needed |

### Memory Usage

| Component | Typical Usage |
|-----------|--------------|
| FastEmbed model | ~400 MB |
| Semantic cache (10K entries) | ~50 MB |
| Prompt registry | < 1 MB |
| Runtime overhead | ~50 MB |
| **Total** | **~500 MB** |

### Throughput

A single LayerCache instance can handle:
- ~500-1000 requests/second (non-streaming, cache miss)
- ~2000+ requests/second (semantic cache hit)
- Limited by LLM provider API rate limits

---

## Future Architecture Evolution

### V1 (Current)

```
Single Instance
├── SQLite semantic cache
├── File-based prompt registry
├── In-memory metrics
└── Synchronous provider calls
```

### V2 (Planned)

```
Distributed
├── Redis semantic cache (shared state)
├── Git-synced prompt registry
├── Prometheus + Grafana (centralized metrics)
├── WebSocket support
└── Multi-modal caching (CLIP embeddings for images)
```

### V3 (Future)

```
Platform
├── Web dashboard for cache management
├── A/B testing framework for enhancements
├── Plugin marketplace for custom enhancements
├── Multi-region deployment
└── Custom embedding model support
```
