# User Guide

A comprehensive guide to using LayerCache — from basic proxy setup to advanced prompt engineering and cache optimization.

---

## Table of Contents

- [Introduction](#introduction)
- [Getting Started](#getting-started)
- [Basic Proxy Usage](#basic-proxy-usage)
- [Understanding the Layered Architecture](#understanding-the-layered-architecture)
- [Prompt Canonicalization](#prompt-canonicalization)
- [Provider Cache Markers](#provider-cache-markers)
- [Using Enhancements](#using-enhancements)
- [Prompt Templates](#prompt-templates)
- [Semantic Cache](#semantic-cache)
- [Monitoring & Metrics](#monitoring--metrics)
- [Advanced Patterns](#advanced-patterns)
- [Best Practices](#best-practices)
- [FAQ](#faq)

---

## Introduction

LayerCache is a drop-in proxy for LLM providers that adds intelligent caching and prompt optimization. You configure it once by changing your LLM client's base URL, and it works transparently — reducing costs and latency automatically.

### What LayerCache Does For You

Without LayerCache:
```
Your App → Anthropic/OpenAI/Gemini API
(Every request billed at full token rate)
```

With LayerCache:
```
Your App → LayerCache → Anthropic/OpenAI/Gemini API
(Repeated prefixes cached, similar queries bypassed entirely)
```

### Who Should Use LayerCache

- **Teams spending $100+/month on LLM APIs** — Typical savings of 30-60%
- **Applications with repetitive prompts** — Same system instructions, similar user queries
- **Multi-provider setups** — Unified caching across Anthropic, OpenAI, and Gemini
- **Teams wanting better prompt engineering** — Cache-safe enhancements without extra code

---

## Getting Started

### Step 1: Install and Run

```bash
# Clone the repo
git clone https://github.com/your-org/layercache.git
cd layercache

# Set your API key(s)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# Start with Docker
docker-compose up -d

# Or start with Python
pip install -r requirements.txt
uvicorn layercache.main:app --port 8000
```

### Step 2: Update Your Application

The only change needed in your application is the `base_url`:

**Before:**
```python
from openai import OpenAI
client = OpenAI(api_key="sk-ant-...")  # Direct to Anthropic
```

**After:**
```python
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:8000/v1",  # Point to LayerCache
    api_key="sk-ant-..."                  # Provider key (passed through)
)
```

That is it. LayerCache now automatically:
- Canonicalizes your prompts for cache-friendliness
- Injects provider-specific cache markers
- Routes to the correct provider based on model name
- Tracks metrics on your caching performance

### Step 3: Verify It Works

```bash
# Health check
curl http://localhost:8000/health

# Send a test request (twice — second should use cache)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-ant-..." \
  -d '{
    "model": "anthropic/claude-3-5-sonnet-20241022",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Hello!"}
    ]
  }'

# Check cache metrics
curl http://localhost:8000/v1/cache/metrics
```

---

## Basic Proxy Usage

### Supported Model Formats

LayerCache auto-detects the provider from the model name:

| Model Name Format | Provider |
|-------------------|----------|
| `anthropic/claude-3-5-sonnet-20241022` | Anthropic |
| `claude-3-5-sonnet-20241022` | Anthropic |
| `openai/gpt-4o` | OpenAI |
| `gpt-4o` | OpenAI |
| `gpt-4o-mini` | OpenAI |
| `gemini/gemini-1.5-pro` | Gemini |
| `gemini-1.5-flash` | Gemini |

### Standard OpenAI SDK Features

All standard features work through the proxy:

```python
# Temperature, max_tokens, etc.
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[...],
    temperature=0.7,
    max_tokens=1024,
    top_p=0.9,
)

# Tool / function calling
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"}
                },
                "required": ["location"]
            }
        }
    }]
)

# Streaming
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[...],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content, end="")
```

---

## Understanding the Layered Architecture

LayerCache classifies every message in your prompt into one of five layers. Understanding this is key to getting the most out of the system.

### The Five Layers

**L0: System (Immutable, Cached)**
- Your core persona: "You are a helpful coding assistant."
- Safety rules and constraints
- Output format instructions
- This should rarely or never change

**L1: Context (Rarely Changes, Cached)**
- Tool definitions
- Domain knowledge / reference documents
- Static few-shot examples
- Changes weekly at most

**L2: Session (Per-Conversation, Cached)**
- Conversation history (previous user/assistant turns)
- User preferences from earlier in the session
- Changes with each turn but builds on previous context

**L3: Enhancement (Per-Request, Uncached)**
- Dynamic instructions injected by LayerCache
- Chain of Thought, few-shot examples, self-critique prompts
- Placed between session history and the user query

**L4: User Input (Per-Request, Uncached)**
- The actual user query
- The most dynamic part of the prompt

### How Layers Affect Caching

```
[L0 System     ]──── cache breakpoint ────┐
[L1 Context    ]──── cache breakpoint ────┤ CACHED PREFIX
[L2 Session    ]──── cache breakpoint ────┘
[L3 Enhancement]                         │ UNCACHED
[L4 User Input ]                         │ UNCACHED
```

The provider caches everything up to the last breakpoint (L2). As long as L0-L2 remain identical, the cache hits — regardless of what L3 and L4 contain. This means you can freely add Chain of Thought, dynamic few-shots, or any other enhancement without invalidating the cache.

---

## Prompt Canonicalization

LayerCache automatically normalizes your prompts for maximum cache-friendliness. This happens transparently — you do not need to change anything.

### What Gets Canonicalized

| Transformation | Example |
|---------------|---------|
| Whitespace stripping | `"  Hello world  "` → `"Hello world"` |
| Newline collapse | `"Line 1\n\n\n\nLine 2"` → `"Line 1\n\nLine 2"` |
| Space collapse | `"Hello   world"` → `"Hello world"` |
| Trailing whitespace per line | `"Line 1   \nLine 2  "` → `"Line 1\nLine 2"` |
| Tool sorting | `[zebra, apple]` → `[apple, zebra]` |
| JSON key sorting | `{"name": ..., "age": ...}` → `{"age": ..., "name": ...}` |
| JSON minification | `{"a": 1, "b": 2}` → `{"a":1,"b":2}` |

### What Does NOT Get Canonicalized

- Message text content (only whitespace/formatting changes)
- Conversation ordering within the same layer (sorted by content hash for determinism)
- Tool definitions themselves (only their order and JSON formatting)

---

## Provider Cache Markers

LayerCache automatically injects provider-specific cache markers at the boundaries of your stable layers (L0, L1, L2).

### Anthropic

Anthropic uses `cache_control` markers. LayerCache injects them at the end of L0, L1, and L2:

```json
{
  "role": "system",
  "content": [
    {
      "type": "text",
      "text": "You are a helpful assistant.",
      "cache_control": {"type": "ephemeral"}
    }
  ]
}
```

**Requirements**: Minimum 1024 tokens in the prefix. Cache TTL is 5 minutes and extends on each hit.

### OpenAI

OpenAI caches the prompt prefix automatically. LayerCache ensures L0-L2 appears as a deterministic, unbroken prefix. No explicit markers are needed.

**Requirements**: Minimum 1024 tokens. Cache TTL is managed automatically by OpenAI.

### Gemini

Gemini requires explicit `CachedContent` resources. LayerCache creates these in the background and references them in subsequent requests.

**Requirements**: Cache TTL is configurable (1 minute to hours). First request with a new prefix has no cache benefit, but subsequent requests do.

---

## Using Enhancements

Enhancements are prompt engineering techniques injected at L3. They improve response quality without breaking the cache.

### Available Enhancements

#### chain_of_thought

Instructs the LLM to think step-by-step before answering. Ideal for complex reasoning, math, and logic problems.

```python
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[
        {"role": "system", "content": "You are a math tutor."},
        {"role": "user", "content": "What is 15% of 240?"}
    ],
    extra_body={
        "lc_enhancements": ["chain_of_thought"]
    }
)
```

#### structured_json

Forces the LLM to respond with valid JSON. Optionally includes a JSON schema.

```python
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "system", "content": "You are a data extraction assistant."},
        {"role": "user", "content": "Extract the name and age from: John is 30 years old."}
    ],
    extra_body={
        "lc_enhancements": ["structured_json"]
    }
)
```

#### self_critique

Asks the LLM to review and refine its own response. Useful for high-stakes outputs where accuracy matters.

```python
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[
        {"role": "system", "content": "You are a medical research assistant."},
        {"role": "user", "content": "Summarize the key findings of this paper: ..."}
    ],
    extra_body={
        "lc_enhancements": ["self_critique"]
    }
)
```

#### dynamic_few_shot

Retrieves relevant examples from a local vector store based on the user query and injects them as few-shot examples.

```python
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {"role": "user", "content": "How do I sort a dictionary by value in Python?"}
    ],
    extra_body={
        "lc_enhancements": ["dynamic_few_shot"]
    }
)
```

### Combining Enhancements

You can stack multiple enhancements in a single request:

```python
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[...],
    extra_body={
        "lc_enhancements": ["chain_of_thought", "structured_json"]
    }
)
```

All enhancements are injected at L3, so the cache prefix (L0-L2) remains unchanged.

### Creating Custom Enhancements

Implement the `BaseEnhancement` interface:

```python
from layercache.enhancements.base import BaseEnhancement
from layercache.models import StratifiedPrompt, LayerType

class CustomEnhancement(BaseEnhancement):
    name = "my_custom_enhancement"

    def apply(self, prompt: StratifiedPrompt, **kwargs) -> StratifiedPrompt:
        self._add_enhancement_message(
            prompt,
            role="user",
            content="Custom instruction here.",
            insert_at_start=True,
        )
        self._add_enhancement_message(
            prompt,
            role="assistant",
            content="Understood.",
            insert_at_start=True,
        )
        return prompt

# Register it
registry.register(CustomEnhancement())
```

---

## Prompt Templates

Templates store your L0 (System) and L1 (Context) layers on the server. This guarantees 100% prefix match across all requests using the same template.

### Using a Template

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

The proxy loads `code-assistant.yaml` and assembles L0/L1 from the template. Your client only sends the user query.

### Managing Templates via API

#### List Templates

```bash
curl http://localhost:8000/v1/prompts/templates
```

#### Create a Template

```bash
curl -X POST http://localhost:8000/v1/prompts/templates \
  -H "Content-Type: application/json" \
  -d '{
    "name": "customer-support",
    "version": "1.0",
    "description": "Customer support agent template",
    "L0": [
      {
        "role": "system",
        "content": "You are a friendly customer support agent. Be empathetic and solution-oriented."
      }
    ],
    "L1": [
      {
        "role": "system",
        "content": "Company: Acme Corp. Products: Widget Pro, Widget Mini. Return policy: 30 days."
      }
    ]
  }'
```

#### Delete a Template

```bash
curl -X DELETE http://localhost:8000/v1/prompts/templates/customer-support
```

#### Reload from Disk

```bash
curl -X POST http://localhost:8000/v1/prompts/reload
```

### Template File Format

Create YAML files in the `data/prompts/` directory:

```yaml
name: "my-template"
version: "1.0"
description: "Description of my template"

L0:
  - role: "system"
    content: |
      You are a helpful assistant with specific expertise.

L1:
  - role: "system"
    content: |
      Additional context, tool definitions, or reference material.
```

---

## Semantic Cache

The semantic cache bypasses the LLM entirely when a sufficiently similar query has been seen before (with the same system instructions).

### How It Works

1. When a request arrives, LayerCache computes a **prefix hash** from L0+L1+L2 and an **embedding** of the user query (L4).
2. It checks the cache for entries with the same prefix hash AND a query embedding with cosine similarity > 0.95.
3. If found, the cached response is returned immediately — zero tokens consumed, near-zero latency.
4. If not found, the request goes to the LLM, and the response is stored for future use.

### Controlling the Semantic Cache

```python
# Default behavior (300s TTL)
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
)

# Custom TTL
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    extra_body={"lc_cache_ttl": 600}  # 10 minutes
)

# Skip semantic cache for this request
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    extra_body={"lc_skip_semantic_cache": True}
)

# Skip all caching (both semantic and provider)
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    extra_body={"lc_bypass_cache": True}
)
```

### Tuning for Your Use Case

| Setting | Higher Value | Lower Value |
|---------|-------------|-------------|
| `similarity_threshold` | Fewer false positives, more LLM calls | More cache hits, risk of stale/wrong answers |
| `default_ttl` | Longer cache freshness | More cache hits, risk of stale data |
| `lc_cache_ttl` (per-request) | Override for sensitive queries | Override for safe, repetitive queries |

---

## Managing Long Conversations

### L2 Session Truncation

In long multi-turn conversations, the session history (L2) grows unboundedly. This pushes the stable prefix further from the end, making provider prefix caching less effective and consuming more input tokens per request.

LayerCache can automatically truncate L2 to fit within a token budget, keeping only the most recent turns:

```yaml
caching:
  max_session_tokens: 2000   # Keep at most ~2000 tokens of conversation history
```

When enabled:
- The pipeline splits L2 into **turn groups** (user → assistant/tool sequences)
- Oldest complete groups are dropped until the remaining fit within budget
- At least one turn is always preserved
- Tool-call interleaves (user → tool_call → tool_result → assistant) are kept as complete clusters

**Trade-off**: Truncation changes the prefix hash, which means the semantic cache will miss for the conversation. However, provider prefix caching benefits from the smaller, denser prefix — net positive for long conversations with stable L0/L1.

**Default**: `null` (no truncation). Opt-in only.

### Prefix Threshold Diagnostics

Provider prefix caching (Anthropic, OpenAI, Gemini) requires a minimum prefix of ~1024 tokens. If L0+L1+L2 is below this threshold, cache markers are ineffective.

LayerCache logs an INFO message (once per hour per prefix) when the stable prefix is too short:

```
Stable prefix (L0+L1+L2) ~500 tokens (model=claude-3-5-sonnet-20241022) —
below ~1024 token caching threshold. Add more content to L0/L1 or expect
low cache hit rates.
```

This diagnostic fires after any truncation, so it measures the final prefix length.

---

## Monitoring & Metrics

### Web Dashboard

LayerCache includes a built-in web dashboard at `http://localhost:8000/dashboard`. It provides:

- **Overview**: Request rate, latency distribution, cost savings charts (live-updating via Chart.js)
- **Models**: Per-model breakdown of requests, tokens, cache hit rate
- **Cache**: Semantic cache browser (entries, expiry, similarity)
- **Templates**: Template list, create, edit, and delete
- **Config**: In-browser config editor with save support (requires `HX-Request` header, rate-limited)
- **Logs**: Live streaming log viewer (via SSE)

If `proxy_api_key` is configured, the dashboard requires login. Otherwise, it's open.

### JSON Metrics

```bash
curl http://localhost:8000/v1/cache/metrics
```

Key fields to watch:

| Field | What It Means | Target |
|-------|--------------|--------|
| `provider_token_cache_hit_rate` | % of input tokens served from provider cache | > 60% |
| `semantic_cache_hit_rate` | % of requests served from semantic cache | > 10% |
| `estimated_tokens_saved` | Total tokens not billed | — |
| `estimated_cost_saved_usd` | Estimated $ saved | — |
| `by_model.{model}.provider_token_cache_hit_rate` | Per-model cache hit rate | > 50% |

### Prometheus

```bash
curl http://localhost:8000/metrics
```

Integrate with your existing Prometheus + Grafana setup for historical dashboards and alerting.

---

## Advanced Patterns

### Pattern 1: Multi-Tenant with Per-Tenant Templates

Use different prompt templates for different clients:

```python
# Client A: Coding assistant
response = client.chat.completions.create(
    model="claude-3-5-sonnet-20241022",
    messages=[{"role": "user", "content": query}],
    extra_body={"lc_template": "coding-assistant-v2"}
)

# Client B: Legal advisor
response = client.chat.completions.create(
    model="claude-3-5-sonnet-20241022",
    messages=[{"role": "user", "content": query}],
    extra_body={"lc_template": "legal-advisor-v1"}
)
```

### Pattern 2: Progressive Enhancement

Start with no enhancements, then add them based on query complexity:

```python
def get_enhancements(query: str) -> list[str]:
    if query_requires_reasoning(query):
        return ["chain_of_thought"]
    if query_requires_structure(query):
        return ["structured_json"]
    return []

response = client.chat.completions.create(
    model="claude-3-5-sonnet-20241022",
    messages=[...],
    extra_body={"lc_enhancements": get_enhancements(query)}
)
```

### Pattern 3: Explicit Layer Hints

For complex prompts where the heuristic might misclassify, use explicit hints:

```python
response = client.chat.completions.create(
    model="claude-3-5-sonnet-20241022",
    messages=[
        {"role": "system", "content": "Core persona"},
        {"role": "system", "content": "Tool definitions"},
        {"role": "user", "content": "Previous question"},
        {"role": "assistant", "content": "Previous answer"},
        {"role": "user", "content": "Current question"},
    ],
    extra_body={
        "lc_layer_hints": {
            "0": "L0",
            "1": "L1",
            "2": "L2",
            "3": "L2",
            "4": "L4"
        }
    }
)
```

---

## Best Practices

### For Maximum Cache Hit Rate

1. **Keep system prompts stable** — Avoid dynamic timestamps, user IDs, or request-specific data in L0.
2. **Use templates** — Store L0/L1 in the Prompt Registry for guaranteed consistency.
3. **Sort tools alphabetically** — LayerCache does this automatically, but be aware if constructing prompts manually.
4. **Avoid random elements in L0-L2** — UUIDs, timestamps, or random selections break prefix matching.
5. **Consistent conversation format** — Send conversation history in the same format every time.

### For Maximum Cost Savings

1. **Enable semantic cache** — Set up few-shot examples for common query patterns.
2. **Use appropriate TTLs** — Longer TTLs for stable domains (FAQs, documentation), shorter for dynamic data.
3. **Leverage enhancements** — Chain of Thought and few-shots improve response quality without extra LLM calls.
4. **Monitor metrics** — Watch `estimated_cost_saved_usd` to validate ROI.

### For Maximum Response Quality

1. **Use templates** — Well-crafted L0/L1 prompts are the foundation of quality.
2. **Add few-shot examples** — The `dynamic_few_shot` enhancement retrieves relevant examples automatically.
3. **Use self-critique sparingly** — It adds latency but improves accuracy for critical responses.
4. **Test enhancement combinations** — Some work better together; measure quality delta.

---

## FAQ

**Q: Does LayerCache modify my prompts?**

A: The canonicalizer makes only non-semantic changes: whitespace normalization, JSON key sorting, and tool ordering. The actual text content of your messages is never altered. Enhancements are *additive* — they add new messages at L3 but never change your existing L0-L4 messages.

**Q: Will LayerCache break my existing application?**

A: No. LayerCache is a transparent proxy. It conforms to the OpenAI API specification and passes through all standard parameters. Your existing code works without modification.

**Q: How much faster are cached requests?**

A: Provider prefix cache hits typically reduce Time to First Token (TTFT) by 50%+ for Anthropic and Gemini. Semantic cache hits return near-instantly (< 20ms) since no LLM call is made.

**Q: Can I use LayerCache with streaming?**

A: Yes. Streaming works transparently. Semantic cache hits are also streamed back (with artificial chunking) to maintain compatibility with streaming client libraries.

**Q: What happens if the semantic cache DB is corrupted?**

A: LayerCache fails open — if the cache DB is unavailable, requests proceed to the LLM normally. The cache simply provides no benefit until the issue is resolved.

**Q: Does LayerCache work with tool/function calling?**

A: Yes. Tool definitions are automatically sorted and canonicalized for cache-friendliness. The tool calling response format is passed through transparently.

**Q: How do I clear the semantic cache?**

A: You can invalidate by prefix hash via the API, or simply delete the SQLite database file at `/data/semantic_cache.db` and restart the service.

**Q: Can I run multiple LayerCache instances?**

A: In V1, each instance has its own local SQLite cache. For multi-instance deployment, use Redis as the cache backend (planned for V2 — see [ROADMAP.md](ROADMAP.md)). The provider prefix caching (Anthropic/OpenAI) works across all instances independently.
