# DA Proposal: Prefix Cache Threshold Warning

## Motivation

Provider prefix caching requires a minimum prefix to trigger:
- Anthropic: ~1024 tokens
- OpenAI: ~1024 tokens (128-token billing increments)
- Gemini: varies by model, generally 1024+ tokens

If L0+L1+L2 falls below this threshold, `cache_control` markers are wasted. Users get low cache hit rates with no diagnostic.

Currently LayerCache silently injects markers regardless of prefix length.

## Proposed Change

Add a warning when the stable prefix is below caching threshold. Provider-agnostic since all major providers have similar minimums.

### Implementation

Use LiteLLM's built-in token counting instead of a heuristic. LiteLLM already imports `tiktoken` for OpenAI and `anthropic` tokenizer for Anthropic. Since we already call LiteLLM for routing (`litellm.acompletion`), LiteLLM's `token_counter` or `encoding` helpers are available:

```python
import litellm

estimated = litellm.token_counter(model=model_name, text=prefix_text)
```

This gives a per-model accurate count with no new dependency. Falls back to ~4 chars/token heuristic if the model isn't recognized (logs at `debug`).

**Rate-limit** the warning to once per hour per distinct prefix-hash to avoid log spam on production traffic:

```python
_prefix_warning_throttle: set[str] = set()  # prefix hashes warned this hour

if prefix_hash not in _prefix_warning_throttle and estimated < 1024:
    _prefix_warning_throttle.add(prefix_hash)
    logger.warning(
        "Stable prefix ~%d tokens (model=%s) — below ~1024 token caching threshold. "
        "Cache markers will be ineffective. Add more content to L0/L1 or expect low hit rates.",
        estimated, model_name,
    )
```

Reset the throttle set on a schema-level periodic timer, or just let it grow bounded by distinct prefixes.

## Provider scope

**All providers.** The threshold applies to Anthropic, OpenAI, and Gemini. The token estimate uses the target model's tokenizer via LiteLLM.

## Files Changed

- `layercache/pipeline.py` — add warning after canonicalization, before marker injection

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| `litellm.token_counter` may be slow or unavailable | try/except falls back to chars/4 heuristic; logged at debug |
| 10K WARNING lines/day for short prefixes | Rate-limited: once per prefix hash per hour |
| False positives from tokenizer mismatch | Tokenizer is model-specific via LiteLLM; more accurate than chars/4 |
| False negatives (warning when cache actually works) | Threshold is advisory — 1024 is the documented minimum but some providers cache smaller prefixes; no behavior change, just a hint |

## Alternatives Considered

1. **Remove the feature** — Users get no diagnostic; they just wonder why hit rates are zero.
2. **Skip marker injection when below threshold** — Too aggressive; threshold is advisory, not a hard cutoff.
3. **Dashboard-only notice** — Too late; users need feedback when developing, not after deploying.
