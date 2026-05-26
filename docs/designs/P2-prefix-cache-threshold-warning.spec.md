# Spec: Prefix Cache Threshold Warning

## 1. Problem Statement

Provider prefix caching requires a minimum prefix length to trigger:
- **Anthropic:** ~1024 tokens
- **OpenAI:** ~1024 tokens (128-token billing increments)
- **Gemini:** Varies by model, generally 1024+ tokens

If L0+L1+L2 falls below this threshold, injected `cache_control` markers, auto breakpoints, or `CachedContent` references are all **ineffective** — the provider ignores them. Users get low cache hit rates with no diagnostic feedback.

Currently LayerCache silently injects markers regardless of prefix length. The only way to diagnose is to check `cache_read_input_tokens` in the response, which requires running a real request and parsing the output.

## 2. Scope

**All providers.** The threshold concept applies equally to Anthropic, OpenAI, and Gemini. The token estimate uses the target model's tokenizer via LiteLLM.

**Pipeline stage:** After canonicalization (Stage 3), before marker injection (Stage 6). Both `process_request` and `process_streaming_request`.

## 3. Proposed Change

### Behavioral contract

After the prompt is stratified and canonicalized, estimate the token count of L0+L1+L2. If below ~1024 tokens, emit a warning once per unique prefix hash per hour.

The warning is **read-only diagnostic** — no behavior change, no request modification, no performance impact.

### Token estimation

Use `litellm.token_counter(model=model_name, text=prefix_text)` for accurate per-model counting.

```python
def _estimate_prefix_tokens(prompt: StratifiedPrompt, model: str) -> int:
    """Estimate tokens in the stable prefix (L0+L1+L2) using LiteLLM tokenizer.

    Falls back to chars/4 heuristic if tokenizer unavailable.
    """
    import litellm

    # Concatenate L0+L1+L2 content
    parts: list[str] = []
    for layer_type in (LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION):
        for msg in sorted(prompt.layers[layer_type], key=lambda m: m.content_hash()):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            parts.append(content)
    prefix_text = "\n".join(parts)

    if not prefix_text.strip():
        return 0

    try:
        return litellm.token_counter(model=model, text=prefix_text)
    except Exception:
        # Fallback: ~4 chars per token (rough heuristic)
        return len(prefix_text) // 4
```

### Rate limiting

```python
# Module-level throttle: prefix_hash -> timestamp of last warning
_prefix_warning_throttle: dict[str, float] = {}
_THROTTLE_SECONDS = 3600  # once per hour per prefix
```

Warning logic:

```python
estimated = _estimate_prefix_tokens(prompt, request.model)
if estimated < 1024:
    prefix_hash = prompt.prefix_hash()
    now = time.time()
    last_warn = _prefix_warning_throttle.get(prefix_hash, 0)
    if now - last_warn > _THROTTLE_SECONDS:
        _prefix_warning_throttle[prefix_hash] = now
        logger.warning(
            "Stable prefix (L0+L1+L2) ~%d tokens (model=%s) — below ~1024 token "
            "caching threshold. Add more content to L0/L1 or expect low cache hit rates.",
            estimated,
            request.model,
        )
```

### Log message

Level: `WARNING` (may trigger monitoring). Fires at most once per prefix hash per hour.

## 4. Existing Code Review

### Pipeline flow (pipeline.py)

- `process_request` (line 87): Stages 1-9. The natural insertion point is between line 143 (canonicalization ends) and line 145 (enhancement starts). Both paths need the warning.
- `process_streaming_request` (line 212): Same stages. Insertion at line 261 (after canonicalize) and line 263 (before enhancements).
- `_estimate_prefix_tokens` needs the model name, which is available as `request.model` in both paths.
- `prompt.prefix_hash()` is already a method on `StratifiedPrompt` — no change needed.

### LiteLLM availability

LiteLLM is imported inside `_call_llm` and `_stream_llm` (lines 375, 396). Adding a top-level `import litellm` is safe — it's already in the project dependencies and imported at call time. However, eager import at module level may slow startup. **Recommendation:** import inside `_estimate_prefix_tokens` (lazy) to avoid affecting startup time.

### `litellm.token_counter` API

Existing LiteLLM method. Signature: `token_counter(model="gpt-4", text="hello world")` → `int`. Known to work with Anthropic model names like `claude-3-5-sonnet-20241022` and OpenAI model names like `gpt-4o`. Returns tokens for the given text using the model's tokenizer.

### Rate-limiting data structure

`_prefix_warning_throttle: dict[str, float]` grows unboundedly in theory. In practice, it grows by one entry per unique prefix hash seen, which is bounded by the number of distinct prompts seen. Even at 10K unique prefixes, this is ~500KB of memory. Acceptable. Can be wrapped in a `lru_cache` or `weakref` if memory becomes a concern.

## 5. Security Review

| Threat | Analysis |
|---|---|
| **Token counting denial** | `litellm.token_counter` could theoretically be slow on very long text. Mitigation: try/except with fallback. Token counting is O(n) on text length and runs in-process. |
| **Prefix hash exposure in logs** | Warning includes prefix hash in the throttle dict, not in the log message. The log message includes `prefix_hash[:12]` for traceability. No API keys or user content exposed. |
| **Module-level mutable state** | `_prefix_warning_throttle` is a shared dict. In the async context, concurrent requests could race on it. Mitigation: dict writes are atomic in CPython; eventual inconsistency is harmless (missed warning or double-warning, both acceptable). |
| **Log injection** | `model` field is validated by `validate_model_name()` before reaching the pipeline. User-controlled but restricted to alphanumeric + `/_-`. |

## 6. Test Requirements

| Test | Description |
|---|---|
| **Short prefix triggers warning** | Prompt with < 1024 tokens of L0+L1+L2 → warning logged |
| **Long prefix no warning** | Prompt with > 1024 tokens → no warning |
| **Rate limiting** | Same prefix hash within 1 hour → second warning suppressed |
| **Fallback on tokenizer failure** | Mock `litellm.token_counter` to raise → falls back to chars/4 heuristic |
| **Empty prefix** | L0+L1+L2 all empty → warning with 0 tokens |
| **No side effects** | Warning fires on correct line; payload/messages/response unchanged |

## 7. Interaction with Existing Features

| Feature | Interaction |
|---|---|
| **P1 auto `cache_control`** | Warning fires regardless of cache_control mode — helpful for both |
| **P3 session truncation** | Warning fires **after** truncation, so it measures the truncated prefix length |
| **Semantic cache** | Unaffected — semantic cache has its own hit/miss tracking unrelated to provider prefix caching |
| **Metrics** | Unaffected — purely diagnostic |
| **`/v1/messages` shim** | Unaffected — delegates to same pipeline, gets the same warning |

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `litellm.token_counter` raises for unknown model | Medium | Low — falls back to chars/4 heuristic | try/except with debug log |
| Log spam in high-traffic deployments | Low | Low — rate-limited to once/prefix-hash/hour | Verified in test |
| WARNING level triggers PagerDuty for short prompts | Medium | Medium — false alert | Users should adjust monitoring; documented in release notes |
