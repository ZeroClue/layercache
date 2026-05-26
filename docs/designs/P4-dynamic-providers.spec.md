# P4: Dynamic Providers + Dashboard

## Problem

1. `ProvidersConfig` hardcodes three fields (`anthropic`, `openai`, `gemini`). Users of other LiteLLM-supported providers (DeepSeek, Together, Mistral, Groq, etc.) have no config presence — they get the OpenAI adapter via fallback but can't see or control this.

2. The dashboard models page hardcodes the same three providers. LiteLLM knows about hundreds more, but the UI shows only the configured three.

3. The adapter selection (`detect_provider()`) is invisible and unconfigurable.

## Design

### Config: dict-based ProvidersConfig

Replace the fixed-field model with a dynamic dict:

```yaml
providers:
  anthropic:
    api_key_env: ANTHROPIC_API_KEY
  openai:
    api_key_env: OPENAI_API_KEY
  gemini:
    api_key_env: GOOGLE_API_KEY
  deepseek:
    api_key_env: DEEPSEEK_API_KEY
    adapter: openai                # Optional: override cache adapter
```

Model changes:

```python
class ProviderConfig(BaseModel):
    api_key_env: str
    base_url: str | None = None
    default_model: str | None = None
    max_retries: int = 3
    timeout: int = 120
    adapter: str | None = None       # NEW: explicit adapter name
```

```python
class ProvidersConfig(BaseModel):
    __root__: dict[str, ProviderConfig] = {}
    # No more fixed anthropic/openai/gemini fields
```

Or, less breaking (maintain backward compat):

```python
class ProvidersConfig(BaseModel):
    anthropic: AnthropicProviderConfig | None = None
    openai: ProviderConfig | None = None
    gemini: ProviderConfig | None = None
    extra: dict[str, ProviderConfig] = {}   # NEW: catch-all
```

**Recommendation**: Use the `__root__` approach. It's cleaner and the three specific fields were an implementation detail, not a stable API. The key names are just labels.

**DA objection — `AnthropicProviderConfig.use_auto_cache_control` disappears.**
The `__root__` approach collapses `AnthropicProviderConfig` into generic `ProviderConfig`, losing the dedicated field. Mitigation: keep `AnthropicProviderConfig` as a subclass but `ProvidersConfig.__root__` accepts `ProviderConfig` only; `use_auto_cache_control` moves into `ProviderConfig.adapter` selection logic instead. Users set `adapter: anthropic` and the auto-cache-control flag becomes a per-request extension, not a config field.

### Adapter selection order

`detect_provider()` gains a new first check:

1. If the model name prefix matches a key in `providers` that has `adapter` set → use that adapter
2. If the model name prefix matches a key in `providers` → use the default adapter for that provider (OpenAI)
3. Fall through to current logic (hardcoded prefix list → default OpenAI)

**DA — prefix matching needs rules.**
"Matches a key in providers" is ambiguous. Define: a provider key matches a model name if the model name starts with `{key}/` (LiteLLM format) or equals `{key}` (bare provider name). So `deepseek/deepseek-chat` matches key `deepseek`, but `some-other-model` does not match key `deepseek`. This prevents false positives.

### `adapter` field validation

`adapter` must be a key in `ADAPTER_REGISTRY`, or `None` (let the system decide). Reject unknown values at config load time with a clear error listing valid options.

### Dashboard models page

The models page stops hardcoding the provider list:

- **Default view**: rows for configured providers + providers with metrics data. This keeps the table manageable.
- **"Show all N providers" toggle**: reveals every provider from `litellm.models_by_provider` (live discovery, may be stale).
- Columns: Provider | Adapter (with caching strategy tooltip) | Key Set | Models | Requests (24h)
- Request counts: aggregate `_metrics.get_metrics()["by_model"]` by provider prefix using the **same** `detect_provider()` logic to stay consistent.

New table:

| Provider | Adapter | Key Set | Models | Requests (24h) |
|----------|---------|---------|--------|----------------|
| anthropic | anthropic (ephemeral cache_control) | ✅ | 15 | 1,240 |
| openai | openai (auto prefix) | ✅ | 48 | 3,500 |
| deepseek | openai (configured) | ✅ | 3 | 820 |
| mistral | openai (default) | — | 8 | 0 |
| together_ai | openai (default) | — | 120 | 0 |
+ show all (340 more...)

**DA — request aggregation is circular.**
To aggregate metrics by provider we need `detect_provider()` on the model name. This is the same function we're trying to make configurable. If metrics were recorded before the user configured an override, the aggregation uses the *old* provider mapping. Acceptable trade-off — stale metrics data is temporary and self-corrects.

### Backward compatibility

- Old configs with `providers.anthropic` etc. still work via the dict key `"anthropic"`
- `detect_provider()` still returns `"openai"` as default — no behavior change for unconfigured providers
- `AnthropicProviderConfig.use_auto_cache_control` moves into the generic `adapter` mechanism

## Implementation order

1. **P4a — Config model**: change `ProvidersConfig` to dict-backed, add `adapter` field to `ProviderConfig`
2. **P4b — Adapter resolution**: update `detect_provider()` and `get_adapter()` to check config overrides
3. **P4c — Dashboard**: rewrite models page route and template to show all LiteLLM providers

## Risks

- **Breaking change**: Configs referencing `providers.anthropic.use_auto_cache_control` need migration if the field moves. Mitigation: keep `AnthropicProviderConfig` as a deprecated alias or move `use_auto_cache_control` into `ProviderConfig.adapter` logic.
- **`litellm.models_by_provider` may be stale**: If LiteLLM's model list is out of date, the dashboard shows a stale provider list. Acceptable — it's a dashboard, not a discovery API.
