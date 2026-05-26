# DA Proposal: Anthropic Auto `cache_control` Mode

## Motivation

Anthropic supports a top-level `cache_control: {"type": "ephemeral"}` parameter that enables **automatic breakpoint management**. Instead of manually placing markers at each layer boundary, the API dynamically moves the breakpoint to the last stable block as the conversation grows.

LayerCache currently uses explicit per-block markers in `adapters/anthropic.py`.

## Provider scope

**Anthropic only.** OpenAI caching is fully implicit — no markers. Gemini uses the `CachedContent` API which is fundamentally different from `cache_control`. If this proves useful, the concept of an "auto" mode could apply to Gemini's `CachedContent` TTL management in a separate change, but the Anthropic `cache_control` API is unique.

## Proposed Changes

### 1. New config option: `use_auto_cache_control` (boolean, default `false`)

In the provider config block:

```yaml
providers:
  anthropic:
    use_auto_cache_control: false  # default: explicit markers (current behavior)
    # when true: use top-level cache_control instead of per-block markers
```

Default is **`false`** (keep explicit markers). Rationale:
- Auto mode places only **one** breakpoint at the end of the stable prefix, losing cross-session cache granularity
- Explicit markers let L0, L1, and L2 each be cached independently — important when L1 is shared across many sessions but L0 is per-session
- Users who want simplicity can opt in; existing behavior is preserved

### 2. Adapter needs config access

Currently `AnthropicAdapter` is instantiated by `get_adapter()` in `adapters/__init__.py` with no config reference. Three options:

**Option A (recommended):** Pass the full `LayerCacheSettings` to `BaseAdapter.__init__()` at pipeline startup. Adapters are stateless singletons; this adds a settings reference.

**Option B:** Pass only the relevant provider config as a dict. Lighter coupling but more code.

**Option C:** Use an environment variable at import time. Simplest but can't be hot-reloaded.

### 3. LiteLLM compatibility

If LiteLLM strips the top-level `cache_control` field, this feature silently does nothing. Mitigations:
- Before shipping: test via integration test against real Anthropic API (or cassette) that LiteLLM passes it through
- At runtime: log at `info` level when auto mode is active so users can verify in logs
- Acceptance criteria for this change: a test that confirms the payload reaching the real Anthropic API includes `cache_control`

## Files Changed

- `layercache/config.py` — add `use_auto_cache_control` to `ProviderConfig` (or a new `AnthropicConfig`)
- `layercache/adapters/__init__.py` — accept `settings` in `get_adapter()`
- `layercache/adapters/anthropic.py` — branch on config flag
- `layercache/adapters/base.py` — store `settings` on `BaseAdapter`
- `layercache/pipeline.py` — pass `settings` to `get_adapter()`
- Test: integration test confirming top-level `cache_control` in LiteLLM payload; unit tests for both modes

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Auto mode places one breakpoint, reducing cross-session cache efficiency | Default `false` — users opt in intentionally |
| LiteLLM strips field, feature silently fails | Integration test + log confirmation |
| Threading config through adders increases coupling | Option A: settings ref on base adapter, already needed for future provider-specific configs |
| Auto mode doesn't support Anthropic's 4-breakpoint limit | Explicit markers remain available via opt-out |

## Alternatives Considered

1. **Always auto, remove explicit markers** — DA rejected: loses cross-session caching granularity.
2. **Per-request override via `extra_body`** — Users can already set `cache_control` manually through LiteLLM passthrough if needed.
3. **Auto mode for simple deployments, explicit for complex** — Config toggle is the right knob.
