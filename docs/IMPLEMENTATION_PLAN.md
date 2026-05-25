# Implementation Plan: LayerCache
### Intelligent Prompt Enhancement & Token Caching Proxy

**Timeline:** 8 Sprints (approx. 8-10 weeks for a small team/solo dev)
**Methodology:** Agile/Iterative. Every sprint results in a runnable (though perhaps incomplete) proxy.

---

## 0. Project Setup & Architecture Bootstrapping (Sprint 0)

**Goal:** Establish the codebase, CI/CD, and basic passthrough proxy.

*   **Task 0.1: Repository & Tooling Setup**
    *   Initialize Python project with `pyproject.toml` (Poetry/Poetry-core or Hatch).
    *   Setup `ruff` (linting/formatting) and `mypy` (type checking).
    *   Create GitHub Actions pipeline (lint, test, build Docker image).
*   **Task 0.2: Core Dependencies**
    *   `fastapi`, `uvicorn` (HTTP server)
    *   `litellm` (Provider routing/SDK abstraction)
    *   `pydantic` v2 (Data validation)
    *   `redis` / `sqlite-vss` (Caching backends)
    *   `fastembed` (Local embedding generation)
    *   `prometheus-client` (Metrics)
*   **Task 0.3: Basic Passthrough Proxy**
    *   Implement FastAPI app with `POST /v1/chat/completions`.
    *   Extract provider API key from headers, route to LiteLLM.
    *   Return provider response transparently.
    *   *Milestone: A drop-in localhost proxy that successfully proxies OpenAI/Anthropic calls.*

---

## 1. Phase 1: Cache Optimizer & Stratification (Sprints 1-2)

**Goal:** Parse, normalize, and inject cache markers to guarantee prefix cache hits without altering prompt semantics.

### Sprint 1: The Stratifier & Canonicalizer

*   **Task 1.1: Define Pydantic Models**
    *   Implement `StratifiedMessage` and `StratifiedPrompt` models.
*   **Task 1.2: Stratification Logic**
    *   Implement heuristic engine to classify incoming messages into L0-L4.
    *   Handle `lc_layer_hints` (explicit client mapping).
*   **Task 1.3: Canonicalizer Engine**
    *   Implement whitespace normalization (`.strip()`, regex for multiple newlines).
    *   Implement JSON schema minification for tool definitions.
    *   Implement alphabetical sorting of `tools` array.
*   **Task 1.4: Integration & Testing**
    *   Write extensive Pytest suite with mock messages to ensure canonicalization is deterministic.
    *   Integrate Stratifier + Canonicalizer into the FastAPI request pipeline.

### Sprint 2: Provider Cache Marker Injection

*   **Task 2.1: Adapter Pattern Implementation**
    *   Create `BaseAdapter` interface with `inject_markers(prompt, payload)`.
*   **Task 2.2: Anthropic Adapter**
    *   Implement logic to append `"cache_control": {"type": "ephemeral"}` to the last block of L0, L1, and L2.
*   **Task 2.3: OpenAI Adapter**
    *   Ensure payload structure strictly places L0-L2 at the beginning (OpenAI caches automatically).
*   **Task 2.4: Gemini Adapter (Basic)**
    *   Implement synchronous `CachedContent` creation/checking for L0/L1.
*   **Task 2.5: End-to-End Testing**
    *   Wire up Anthropic/OpenAI APIs (use recorded VCR cassettes or sandbox keys).
    *   *Milestone: Proxy successfully injects Anthropic markers. Verify via Anthropic response `usage.cache_read_input_tokens` > 0 on subsequent requests.*

---

## 2. Phase 2: Cache-Safe Enhancements & Registry (Sprints 3-4)

**Goal:** Add dynamic prompt modifications without breaking the L0-L2 cache prefix.

### Sprint 3: The Enhancement Engine

*   **Task 3.1: BaseEnhancement Interface**
    *   Define the `apply(prompt: StratifiedPrompt) -> StratifiedPrompt` interface.
*   **Task 3.2: Implement Core Enhancements**
    *   `ChainOfThoughtEnhancement`
    *   `StructuredOutputEnhancement` (JSON schema enforcement)
    *   `SelfCritiqueEnhancement`
*   **Task 3.3: L3 Injection Logic**
    *   Update pipeline to accept `lc_enhancements` array from request.
    *   Ensure all enhancement messages are strictly appended to `LayerType.ENHANCEMENT` (L3).
*   **Task 3.4: Integration Testing**
    *   Verify that applying enhancements does *not* change the token count/structure of L0-L2 compared to a non-enhanced request.

### Sprint 4: Prompt Registry & Dynamic Few-Shots

*   **Task 4.1: YAML Prompt Registry**
    *   Implement file watcher/loader for YAML directory.
    *   Allow requests to specify `lc_template: "code-review-v2"`, which overrides client L0/L1.
*   **Task 4.2: Dynamic Few-Shot Engine**
    *   Setup local vector store (SQLite-vss or FAISS).
    *   Implement `DynamicFewShotEnhancement`.
    *   On request, embed L4 (user query), retrieve top-K examples, inject into L3.
*   **Task 4.3: Management API**
    *   `POST /v1/prompts/templates` (CRUD for registry).
    *   `POST /v1/prompts/examples` (CRUD for few-shot vector DB).
    *   *Milestone: Proxy can dynamically add CoT/Few-shots while maintaining >50% prefix cache hit rates.*

---

## 3. Phase 3: Semantic Cache & Observability (Sprints 5-6)

**Goal:** Bypass the LLM entirely for semantically similar queries, and expose cost savings metrics.

### Sprint 5: Semantic Cache

*   **Task 5.1: FastEmbed Integration**
    *   Setup `TextEmbedding` using `BAAI/bge-small-en-v1.5`.
    *   Run embedding generation in a `ProcessPoolExecutor` to avoid blocking FastAPI's event loop.
*   **Task 5.2: Cache Key Generation**
    *   Implement exact-match hashing for L0-L2 (`hashlib.sha256`).
    *   Implement semantic embedding for L4.
*   **Task 5.3: SQLite-vss Cache Store**
    *   Create tables: `id`, `prefix_hash`, `query_embedding`, `response_payload`, `ttl_expires_at`.
    *   Implement `lookup` (cosine similarity > 0.95 + prefix hash match + valid TTL).
    *   Implement `store` (upsert query/response).
*   **Task 5.4: Pipeline Integration**
    *   Intercept request before Canonicalizer. If semantic cache hit, return immediately.
    *   If miss, proceed to LLM, then store result.
    *   *Milestone: Repeated similar questions return instantly with 0 LLM tokens consumed.*

### Sprint 6: Observability & Metrics

*   **Task 6.1: Prometheus Metrics**
    *   Counters: `lc_llm_requests_total`, `lc_llm_tokens_saved_total`, `lc_semantic_cache_hits_total`.
    *   Histograms: `lc_request_duration_seconds`.
*   **Task 6.2: Cache ROI Calculator**
    *   Parse standard provider responses to extract `cache_read_input_tokens` (Anthropic) / `cached_tokens` (OpenAI).
    *   Calculate estimated $ saved based on model pricing.
*   **Task 6.3: Metrics Endpoint & Dashboard**
    *   Expose `GET /metrics` for Prometheus.
    *   Expose `GET /v1/cache/metrics` for JSON dashboard consumption.
    *   *Milestone: Clear visibility into cache hit rates and cost savings.*

---

## 4. Phase 4: Polish, Streaming, and Production Readiness (Sprints 7-8)

**Goal:** Make it robust enough for production workloads.

### Sprint 7: Streaming & Configuration

*   **Task 7.1: Streaming Support**
    *   Implement `StreamingResponse` in FastAPI.
    *   Ensure stream chunks pass through untouched.
    *   Parse the final chunk in Anthropic/OpenAI streams to extract `usage` data for Prometheus/ROI tracking.
*   **Task 7.2: Semantic Cache & Streams**
    *   If semantic cache hit, stream the cached response back with artificial delay (to mimic standard streaming behavior and prevent client UI glitches).
*   **Task 7.3: Configuration Layer**
    *   Implement `layercache.yaml` parsing (Pydantic settings).
    *   Allow enabling/disabling semantic cache, setting TTLs, configuring embedder models.

### Sprint 8: Hardening & Deployment

*   **Task 8.1: Gemini Async Context Caching**
    *   Refactor Gemini Adapter to create `CachedContent` asynchronously in the background if a new prefix hash is detected. Store mapping in SQLite. Next request uses the ready cache.
*   **Task 8.2: Error Handling & Fallbacks**
    *   If Semantic Cache DB fails, fail open (proxy request normally).
    *   If embedding model fails, skip semantic caching.
    *   LiteLLM fallback configurations.
*   **Task 8.3: Docker Optimization**
    *   Multi-stage build.
    *   Pre-download FastEmbed model in Dockerfile to prevent 10s cold-start latency on first request.
*   **Task 8.4: Documentation & Release**
    *   README with Quickstart, Configuration docs.
    *   Publish Docker image to GHCR.

---

## Testing Strategy Specifics

To test LayerCache effectively without spending a fortune on API calls:

1.  **Unit Tests (No API Calls):**
    *   Test the Canonicalizer: `assert canonicalize(messy_prompt) == clean_prompt`
    *   Test the Stratifier: `assert stratify(messages) == {L0: [...], L4: [...]}`
    *   Test Marker Injection: `assert anthropic_inject(stratified) has cache_control at index X`

2.  **Integration Tests (VCR.py or similar):**
    *   Record real API interactions (request + response, including cache headers) as YAML/JSON cassettes.
    *   Replay them in CI. This allows testing the full pipeline against "real" data without API keys or costs.

3.  **End-to-End Smoke Tests (Manual/Nightly):**
    *   A small script that runs against a real Anthropic/OpenAI key, sends the same prompt twice, and asserts `cache_read_input_tokens > 0` on the second call.

## Key Risks & Mitigations during Implementation

| Risk | Mitigation Strategy |
| :--- | :--- |
| **Embedding model blocks async loop** | Strict use of `asyncio.get_running_loop().run_in_executor(ProcessPoolExecutor(), embed_fn)` |
| **Semantic Cache returns bad/wrong answers** | Default high similarity threshold (0.95). Allow model-specific thresholds. Log all cache hits for auditing. |
| **Canonicalizer breaks prompt semantics** | Limit string manipulation to whitespace only. Never rephrase. Sort only stable arrays (tools), never conversational messages. |
| **Provider SDK updates break marker injection** | Pin LiteLLM versions. Write integration tests against VCR cassettes. Update cassettes manually on version bumps. |
