# Spec: Anthropic Auto `cache_control` Mode

## 1. Problem Statement

LayerCache injects explicit `cache_control: {"type": "ephemeral"}` markers at each stable layer boundary (L0, L1, L2) in `AnthropicAdapter.inject_markers()`. This gives fine-grained control: L0, L0+L1, and L0+L1+L2 are each independently cached regions.

However, Anthropic also supports a top-level `cache_control: {"type": "ephemeral"}` parameter on the request that enables **automatic breakpoint management**. Instead of manual per-layer markers, the API dynamically moves the single breakpoint to the last stable block. This is simpler and more robust for multi-turn conversations where the "growing tail" shifts where the ideal breakpoint is.

LayerCache currently has no way to use the auto mode. Users who want simplicity or have single-turn workflows must use the explicit marker approach, which is more code and harder to debug.

## 2. Scope

**Provider:** Anthropic only.
**Not affected:** OpenAI (implicit caching, no markers). Gemini (`CachedContent` API, fundamentally different mechanism).
**Not affected:** The `/v1/messages` shim (`anthropic_messages.py`) — it delegates to the same pipeline.

## 3. Proposed API Change

### Config (YAML)

```yaml
providers:
  anthropic:
    api_key_env: ANTHROPIC_API_KEY
    use_auto_cache_control: false   # NEW — default false
```

### Config (Pydantic)

Already added in a prior edit to `config.py`:

```python
class AnthropicProviderConfig(ProviderConfig):
    use_auto_cache_control: bool = False
```

And `ProvidersConfig.anthropic` typed as `AnthropicProviderConfig | None`.

### Behavioral contract

| `use_auto_cache_control` | Behavior |
|---|---|
| `false` (default) | Current per-block markers at L0/L1/L2 boundaries. No change. |
| `true` | Sets top-level `payload["cache_control"] = {"type": "ephemeral"}` instead. No per-block markers. System prompt markers are also removed (Anthropic handles breakpoints automatically). |

### How config reaches the adapter

The pipeline calls `detect_provider()` then `get_adapter()` per request. The config flag must reach `inject_markers()`.

**Option A (recommended):** Add an optional `config` dict parameter to `BaseAdapter.inject_markers()`. All existing adapters ignore it; `AnthropicAdapter` reads `config.get("use_auto_cache_control", False)`. The pipeline extracts the flag from its settings and passes it.

**Option B:** Thread `LayerCacheSettings` through to `BaseAdapter.__init__()`. Changes adapter instantiation pattern. More invasive.

**Option C:** Set the flag on the `payload` dict before calling `inject_markers()`, and let `AnthropicAdapter.inject_markers()` check for its presence. Clever but couples the payload format to config.

**Recommendation: Option A.** Minimal footprint. All three options are valid; A has the lowest blast radius.

## 4. Existing Code Review

### `AnthropicAdapter.inject_markers()` (anthropic.py:24-93)

- Takes `(self, prompt, payload)`. Returns modified payload.
- **System prompt handling** (lines 80-91): Wraps `payload["system"]` in a content block list with `cache_control`. In auto mode, this must be skipped — the system prompt is a separate field from `messages`, and Anthropic's auto mode doesn't require explicit markers on it.
- **Content formatting** (lines 110-129): `_format_content()` handles string → list conversion. In auto mode, this still runs (messages still need content blocks), but without `cache_control` on the last block.
- **Multi-modal content** (lines 62-64): Handles `list[dict]` content with per-block `cache_control`. In auto mode, this transform is skipped entirely.

### edge: `get_adapter()` creates fresh instances (init.py:62-68)

```python
def get_adapter(provider_name: str) -> BaseAdapter:
    return adapter_cls()
```

Each `inject_markers()` call gets a fresh adapter. This means:
- No state to worry about for P1
- But it's wasteful (harmless for P1, but notable for any future stateful adapter features)
- For GeminiAdapter, this is actually a **bug** — `_cache_map` and `_pending_creates` are re-initialized every request, meaning Gemini cached content is never actually reused (identified during code review, out of scope for this spec but flagged)

### Pipeline call sites (pipeline.py:155, 270)

```python
adapter = get_adapter(provider)
payload = adapter.inject_markers(prompt, payload)
```

Both `process_request` and `process_streaming_request` use the same pattern. Both need the config flag.

## 5. Security Review

| Threat | Analysis |
|---|---|
| **Config injection** | `use_auto_cache_control` is a boolean read from YAML → Pydantic. No arbitrary values. No injection vector. |
| **LiteLLM passthrough** | Top-level `cache_control` must survive LiteLLM's payload construction. If LiteLLM strips unknown fields, the feature silently degrades to no-op. Mitigation: log at `info` when auto mode is active. User can verify in logs. |
| **SSRF** | No new URLs, network calls, or user-controlled values. No risk. |
| **Data exposure** | Cache markers control caching behavior, not data access. No auth bypass. |

## 6. Test Requirements

| Test | Description |
|---|---|
| **Auto mode sets top-level key** | `inject_markers(..., config={"use_auto_cache_control": True})` → `payload["cache_control"] == {"type": "ephemeral"}` |
| **Auto mode skips per-block markers** | Auto mode payload must not contain `cache_control` on any individual message |
| **Auto mode skips system prompt markers** | `payload["system"]` must not be wrapped in `cache_control` blocks |
| **Explicit mode unchanged** | Default `config=None` produces same markers as current code |
| **Backward compat** | Existing tests for `test_injects_cache_control_markers` pass unchanged |
| **LiteLLM passthrough** | Integration test (or cassette) confirms LiteLLM passes top-level `cache_control` to the actual Anthropic API |

## 7. Interaction with Existing Features

| Feature | Interaction |
|---|---|
| **Canonicalizer** | Unaffected — runs before marker injection |
| **Semantic cache** | Unaffected — prefix hash is computed from L0+L1+L2, not from marker injection |
| **Enhancements** | Unaffected — L3 is between L2 and L4 in reassembly |
| **Metrics** | Unaffected — `cache_read_input_tokens` comes from response `usage`, not from sent markers |
| **`/v1/messages` shim** | Unaffected — delegates to same pipeline |
| **`reload_config()`** | If config changes at runtime, new requests use the new flag value. The pipeline reads `_settings` at request time. |

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Auto mode places one breakpoint instead of three, reducing cross-session cache granularity | Certain when enabled | Medium — L1 can't be separately cached across sessions | Default `false`. Users who need simplicity opt in knowingly. |
| LiteLLM strips `cache_control` from payload | Low | Medium — feature silently does nothing | Info log confirms; integration test |
| User sets `true` but has very short prefix (<1024 tokens) | Common | Low — no cache benefit either way | P2 threshold warning catches this |
