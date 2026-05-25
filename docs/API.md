# API Reference

Complete reference for the LayerCache REST API. All endpoints are standard HTTP/REST.

---

## Base URL

```
http://your-host:8000
```

---

## Authentication

If `proxy_api_key` is configured in `layercache.yaml`, all requests must include the API key:

```
Authorization: Bearer your-proxy-api-key
```

Or via the `x-api-key` header:

```
x-api-key: your-proxy-api-key
```

**Note**: The provider API key (Anthropic, OpenAI, Gemini) is passed through the `Authorization` header and forwarded to the LLM provider.

---

## OpenAI-Compatible Endpoints

### POST /v1/chat/completions

Create a chat completion. Fully compatible with the [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat).

#### Request Headers

| Header | Required | Description |
|--------|----------|-------------|
| `Content-Type` | Yes | `application/json` |
| `Authorization` | Yes | `Bearer <provider-api-key>` (or `x-api-key`) |

#### Request Body

##### Standard OpenAI Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `model` | string | Yes | Model name (e.g., `anthropic/claude-3-5-sonnet-20241022`) |
| `messages` | array | Yes | Array of message objects |
| `temperature` | float | No | Sampling temperature (0-2) |
| `top_p` | float | No | Nucleus sampling parameter |
| `max_tokens` | integer | No | Maximum tokens to generate |
| `stream` | boolean | No | Enable streaming response (default: `false`) |
| `tools` | array | No | Tool/function definitions |
| `tool_choice` | string/object | No | Tool choice mode |
| `response_format` | object | No | Response format specification |

##### LayerCache Extension Fields

These can be included in the request body alongside standard fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `lc_template` | string | `null` | Name of a prompt template for L0/L1 |
| `lc_enhancements` | string[] | `[]` | Enhancement names to apply at L3 |
| `lc_cache_ttl` | integer | `300` | Semantic cache TTL in seconds (0 = skip) |
| `lc_layer_hints` | object | `null` | Explicit message index to layer mapping |
| `lc_skip_semantic_cache` | boolean | `false` | Skip semantic cache lookup |
| `lc_bypass_cache` | `false` | `false` | Skip all caching |

#### Example Request

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-ant-..." \
  -d '{
    "model": "anthropic/claude-3-5-sonnet-20241022",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is async/await in Python?"}
    ],
    "temperature": 0.7,
    "lc_enhancements": ["chain_of_thought"],
    "lc_cache_ttl": 600
  }'
```

#### Example Response (Non-Streaming)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "anthropic/claude-3-5-sonnet-20241022",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Async/await is a Python feature..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 150,
    "completion_tokens": 200,
    "total_tokens": 350,
    "cache_read_input_tokens": 100,
    "cache_creation_input_tokens": 50
  }
}
```

#### Example Response (Streaming)

When `stream: true`, the response is a Server-Sent Events (SSE) stream:

```
data: {"id":"chatcmpl-abc123","choices":[{"delta":{"content":"Async"}}]}

data: {"id":"chatcmpl-abc123","choices":[{"delta":{"content":"/await"}}]}

data: {"id":"chatcmpl-abc123","choices":[{"delta":{"content":" is..."}}]}

data: [DONE]
```

---

### GET /v1/models

List available models. Proxied to LiteLLM.

#### Response

```json
{
  "data": [
    {
      "id": "anthropic/claude-3-5-sonnet-20241022",
      "object": "model",
      "owned_by": "anthropic"
    },
    {
      "id": "openai/gpt-4o",
      "object": "model",
      "owned_by": "openai"
    }
  ]
}
```

---

## Cache Metrics Endpoints

### GET /v1/cache/metrics

Get cache performance metrics as JSON.

#### Response

```json
{
  "llm_requests_total": 1250,
  "semantic_cache_hits_total": 180,
  "semantic_cache_misses_total": 1070,
  "provider_token_cache_hit_rate": 0.6521,
  "semantic_cache_hit_rate": 0.1440,
  "total_input_tokens": 1250000,
  "total_output_tokens": 625000,
  "total_cache_read_tokens": 815000,
  "total_cache_creation_tokens": 435000,
  "estimated_tokens_saved": 815000,
  "estimated_cost_saved_usd": 21.72,
  "estimated_total_cost_usd": 45.30,
  "avg_request_duration_seconds": 1.234,
  "p95_request_duration_seconds": 3.456,
  "by_model": {
    "anthropic/claude-3-5-sonnet-20241022": {
      "requests": 800,
      "provider_token_cache_hit_rate": 0.7200,
      "input_tokens": 800000,
      "cache_read_tokens": 576000
    },
    "openai/gpt-4o": {
      "requests": 450,
      "provider_token_cache_hit_rate": 0.5333,
      "input_tokens": 450000,
      "cache_read_tokens": 240000
    }
  }
}
```

#### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `llm_requests_total` | integer | Total requests proxied to LLM (excludes semantic cache hits) |
| `semantic_cache_hits_total` | integer | Requests served from semantic cache (zero LLM cost) |
| `semantic_cache_misses_total` | integer | Requests that missed the semantic cache |
| `provider_token_cache_hit_rate` | float | Fraction of input tokens read from provider prefix cache |
| `semantic_cache_hit_rate` | float | Fraction of requests served from semantic cache |
| `total_input_tokens` | integer | Total input tokens across all requests |
| `total_output_tokens` | integer | Total output tokens across all requests |
| `total_cache_read_tokens` | integer | Total tokens read from provider cache |
| `estimated_tokens_saved` | integer | Tokens that would have been billed at full input rate |
| `estimated_cost_saved_usd` | float | Estimated cost savings based on model pricing |
| `avg_request_duration_seconds` | float | Average LLM request duration |
| `p95_request_duration_seconds` | float | 95th percentile request duration |
| `by_model` | object | Per-model breakdown of metrics |

---

### GET /metrics

Get Prometheus-compatible metrics. Suitable for scraping by a Prometheus server.

#### Response (text/plain)

```
# HELP lc_llm_requests_total Total number of LLM requests proxied
# TYPE lc_llm_requests_total counter
lc_llm_requests_total 1250

# HELP lc_semantic_cache_hits_total Total semantic cache hits
# TYPE lc_semantic_cache_hits_total counter
lc_semantic_cache_hits_total 180

# HELP lc_tokens_saved_total Total tokens saved from caching
# TYPE lc_tokens_saved_total counter
lc_tokens_saved_total 815000

# HELP lc_cost_saved_usd Total cost saved from caching
# TYPE lc_cost_saved_usd counter
lc_cost_saved_usd 21.72

# TYPE lc_request_duration_seconds summary
lc_request_duration_seconds_avg 1.234
lc_request_duration_seconds_p95 3.456
```

---

## Prompt Template Endpoints

### GET /v1/prompts/templates

List all registered prompt templates.

#### Response

```json
[
  {
    "name": "code-assistant",
    "version": "1.0",
    "description": "Default coding assistant template"
  },
  {
    "name": "writer",
    "version": "1.0",
    "description": "Creative writing template"
  }
]
```

---

### POST /v1/prompts/templates

Create or update a prompt template.

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Template name (unique identifier) |
| `version` | string | No | Version string (default: `"1.0"`) |
| `description` | string | No | Human-readable description |
| `L0` | array | No | System layer messages |
| `L1` | array | No | Context layer messages |
| `metadata` | object | No | Additional metadata |

Each message in L0/L1 has:

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | Message role (typically `"system"`) |
| `content` | string | Message content |

#### Example Request

```bash
curl -X POST http://localhost:8000/v1/prompts/templates \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-proxy-key" \
  -d '{
    "name": "customer-support",
    "version": "1.0",
    "description": "Customer support agent",
    "L0": [
      {"role": "system", "content": "You are a friendly customer support agent."}
    ],
    "L1": [
      {"role": "system", "content": "Company: Acme Corp\nProducts: Widget Pro, Widget Mini"}
    ]
  }'
```

#### Response

```json
{
  "status": "ok",
  "name": "customer-support",
  "version": "1.0"
}
```

---

### DELETE /v1/prompts/templates/{template_name}

Delete a prompt template by name.

#### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `template_name` | string | Name of the template to delete |

#### Response (Success)

```json
{
  "status": "ok",
  "deleted": "customer-support"
}
```

#### Response (Not Found)

```json
{
  "detail": "Template 'customer-support' not found"
}
```

---

### POST /v1/prompts/reload

Reload all prompt templates from disk. Useful after editing YAML template files without restarting the service.

#### Response

```json
{
  "status": "ok",
  "templates": [
    {"name": "code-assistant", "version": "1.0", "description": "..."},
    {"name": "writer", "version": "1.0", "description": "..."}
  ]
}
```

---

## Health Check

### GET /health

Check the health status of the LayerCache service.

#### Response

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "semantic_cache": true,
  "semantic_cache_stats": {
    "total_entries": 42,
    "valid_entries": 38
  }
}
```

#### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"healthy"` or `"unhealthy"` |
| `version` | string | LayerCache version |
| `semantic_cache` | boolean | Whether the semantic cache is operational |
| `semantic_cache_stats` | object | Cache entry counts (only present if cache is enabled) |
| `semantic_cache_stats.total_entries` | integer | Total entries in the cache |
| `semantic_cache_stats.valid_entries` | integer | Non-expired entries |

---

## Error Responses

All errors follow a consistent format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

### HTTP Status Codes

| Code | Description |
|------|-------------|
| `200` | Success |
| `400` | Bad request (invalid JSON, missing required fields) |
| `401` | Unauthorized (proxy API key required but not provided) |
| `403` | Forbidden (invalid proxy API key) |
| `404` | Not found (template not found, etc.) |
| `500` | Internal server error |

### Common Errors

**401 Unauthorized**:
```json
{
  "detail": "Proxy API key required"
}
```

**400 Bad Request**:
```json
{
  "detail": "Invalid JSON body: Expecting property name enclosed in double quotes"
}
```

**404 Not Found**:
```json
{
  "detail": "Template 'nonexistent' not found"
}
```

**500 Internal Server Error**:
```json
{
  "error": {
    "message": "Internal server error",
    "type": "server_error"
  }
}
```

---

## SDK Usage Examples

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-ant-your-key"
)

# Basic request with enhancements
response = client.chat.completions.create(
    model="anthropic/claude-3-5-sonnet-20241022",
    messages=[
        {"role": "user", "content": "Explain quicksort."}
    ],
    extra_body={
        "lc_enhancements": ["chain_of_thought"],
        "lc_cache_ttl": 600
    }
)

# With a template
response = client.chat.completions.create(
    model="claude-3-5-sonnet-20241022",
    messages=[{"role": "user", "content": "Review this code."}],
    extra_body={"lc_template": "code-assistant"}
)

# Streaming
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Tell me a story."}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

### JavaScript/TypeScript (OpenAI SDK)

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
  baseURL: 'http://localhost:8000/v1',
  apiKey: 'sk-ant-your-key',
});

const response = await client.chat.completions.create({
  model: 'anthropic/claude-3-5-sonnet-20241022',
  messages: [
    { role: 'user', content: 'What is caching?' },
  ],
  // LayerCache extensions via body
  lc_enhancements: ['chain_of_thought'],
  lc_cache_ttl: 600,
});
```

### cURL

```bash
# Simple request
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-ant-..." \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'

# With enhancements and template
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-ant-..." \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "messages": [
      {"role": "user", "content": "Review this function."}
    ],
    "lc_template": "code-assistant",
    "lc_enhancements": ["self_critique"]
  }'
```
