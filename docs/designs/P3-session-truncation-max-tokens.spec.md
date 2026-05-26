# Spec: L2 Session Truncation (`max_session_tokens`)

## 1. Problem Statement

Multi-turn conversations grow L2 (session history) unboundedly. As L2 grows:
1. The stable prefix (L0+L1+L2) becomes longer, consuming more input tokens per request
2. Provider prefix caching becomes less effective — the "growing tail" means the cached prefix is a smaller fraction of the total prompt
3. The TTL on the cached prefix resets less often because the prefix changes more frequently

Best practice guidance recommends sending only the last 3-5 turns (or ~2000 tokens) of conversation history.

LayerCache currently passes **all** conversation history through L2 with no truncation. Users who want truncation must do it client-side before sending messages.

## 2. Scope

**All providers.** Truncation is pipeline-level, applied before adapter-specific marker injection. Affects Anthropic, OpenAI, and Gemini equally.

**Pipeline stage:** After canonicalization (Stage 3), before enhancement injection (Stage 4) and marker injection (Stage 6).

## 3. Proposed API Change

### Config

```yaml
caching:
  max_session_tokens: 2000    # NEW — null or 0 = no limit (default)
```

Use **token count** (not turn count). Rationale per DA feedback: a turn can be 50 tokens (terse) or 5000 tokens (verbose). Token count is more precise and less surprising.

### Config model

Already added in a prior edit:

```python
class CachingConfig(BaseModel):
    semantic: SemanticCacheConfig = ...
    metrics: MetricsConfig = ...
    max_session_tokens: int | None = None  # NEW
```

### Behavioral contract

| `max_session_tokens` | Behavior |
|---|---|
| `null` or `0` | No truncation (current default, backward compatible) |
| `> 0` | Keep as many trailing session messages as fit within the budget. Oldest messages discarded first. Tool-call interleaves (user→tool_call→tool_result→assistant) kept as complete logical clusters. |

### Truncation algorithm

```python
def _truncate_session(self, prompt: StratifiedPrompt, model: str) -> None:
    max_tokens = self._max_session_tokens
    if max_tokens is None or max_tokens <= 0:
        return

    session_msgs = prompt.layers.get(LayerType.SESSION, [])
    if not session_msgs:
        return

    import litellm

    # Work backwards: keep complete trailing clusters of messages
    # A "cluster" is everything between user/assistant boundaries
    # that forms a logical turn (including tool_call/tool_result interleaves)
    kept: list = []
    total = 0
    budget = max_tokens

    for msg in reversed(session_msgs):
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        try:
            tokens = litellm.token_counter(model=model, text=content)
        except Exception:
            tokens = len(content) // 4  # fallback

        if total + tokens > budget:
            break
        kept.insert(0, msg)
        total += tokens

    if len(kept) != len(session_msgs):
        prompt.layers[LayerType.SESSION] = kept
        logger.info(
            "Truncated L2 from %d to %d messages (~%d tokens) for model=%s",
            len(session_msgs),
            len(kept),
            total,
            model,
        )
```

Note on tool-call interleaves: The reversed-scan approach naturally keeps complete trailing turns because a tool call sequence always ends with `assistant` content. If the first kept message is a `tool_call` or `tool_result`, its preceding context is already gone — acceptable degradation. A later refinement could track turn boundaries by pattern-matching role sequences.

## 4. Existing Code Review

### Message roles in L2 (stratifier.py)

The stratifier classifies these roles into L2 (SESSION):
- `role=assistant` → L2
- `role=tool` → L2
- `role=user` at non-final index → L2

So L2 can contain user, assistant, and tool messages. The truncation algorithm treats all roles equally by token cost — no role-specific logic needed.

### prefix_hash() dependency (models.py)

`StratifiedPrompt.prefix_hash()` computes SHA-256 of L0+L1+L2 content. After truncation, L2 is different, so `prefix_hash()` returns a different value. This means:
- **Semantic cache:** Guaranteed miss for any truncated conversation (old hash ≠ new hash)
- **Provider prefix cache:** Benefits from smaller, denser prefix (more likely to hit Anthropic/OpenAI cache)

### Enhancement injection (pipeline.py, Stage 4)

Enhancements inject messages at L3, which is between L2 and L4 in reassembled output. Truncation of L2 does not affect L3 or L4. The prefix hash changes only because L2 changed, but the actual user query (L4) and enhancements (L3) are unaffected.

### Both pipeline paths

`process_request` (line 87) and `process_streaming_request` (line 212) both need this. The insertion point is the same: after canonicalization (line 143/261) and before enhancement (line 145/263).

### Config threading

The pipeline constructor currently takes `timeout` and `max_retries` from config. `max_session_tokens` follows the same pattern: pass it to `RequestPipeline.__init__()`.

**Change to `main.py`:** Extract `max_session_tokens` from `_settings.caching.max_session_tokens` and pass it to the pipeline.

## 5. Security Review

| Threat | Analysis |
|---|---|
| **Data loss** | Truncation discards messages. If a user needs full history for context (legal, medical, long-running analysis), enabling this config could cause incorrect responses. Mitigation: default `null` means opt-in only. |
| **Tool call breaks** | If a tool_call message is dropped but the corresponding tool_result is kept (or vice versa), the LLM receives an orphaned tool message. Mitigation: the reversed scan keeps complete trailing clusters. A tool_call without its result would only appear at the truncation boundary — the `break` condition ensures the entire tool sequence is either fully kept or fully dropped. |
| **Token counting accuracy** | LiteLLM's token_counter returns the correct token count per model. The fallback (chars/4) is only used for unrecognized models, which means the truncation budget may be misestimated — but this is bounded (chars/4 is a reasonable estimate) and the worst case is "kept slightly more or fewer tokens than configured." |
| **Log exposure** | The info log includes the pre/truncated message counts and model name. No user message content is logged. |

## 6. Test Requirements

| Test | Description |
|---|---|
| **No truncation at default** | `max_session_tokens=None` → all L2 messages preserved |
| **Truncation under budget** | 10 messages, budget 3 messages worth of tokens → 3 kept |
| **Entire L2 fits in budget** | 2 messages, budget 10K tokens → both kept |
| **Tool call interleaves** | user→tool_call→tool_result→assistant sequence → entire cluster kept or dropped |
| **Zero budget** | `max_session_tokens=0` → no truncation (same as null) |
| **Empty L2** | No session messages → no error |
| **prefix_hash changes** | After truncation, `prompt.prefix_hash()` returns a different value |
| **L3/L4 unaffected** | Enhancement messages and user query unchanged after truncation |
| **Model-specific token counting** | Different models (gpt-4o vs claude-3-sonnet) produce different truncation boundaries |
| **Integration: config threading** | `main.py` creates pipeline with correct `max_session_tokens` value |

## 7. Interaction with Existing Features

| Feature | Interaction |
|---|---|
| **P2 threshold warning** | Warning fires after truncation — measures truncated prefix, which is accurate |
| **P1 auto cache_control** | Truncation + auto mode: smaller prefix, one auto breakpoint. Both improvements compound. |
| **Semantic cache** | Guaranteed miss after truncation (prefix hash changes). Mitigation: logged at info. Trade-off: provider cache hit rate improves more than semantic cache miss rate worsens in long conversations. |
| **Canonicalizer** | Truncation runs after canonicalization, so we truncate clean, deterministic content. Canonicalizer doesn't need to know about truncation. |
| **Enhancements** | Truncation runs before enhancement injection. L3 unaffected. |
| **Metrics** | `total_input_tokens` reflects the truncated content (fewer tokens). `cache_read_input_tokens` should increase (better provider cache hits). |
| **`/v1/messages` shim** | Unaffected — delegates to same pipeline |
| **`reload_config()`** | Hot-reload of `max_session_tokens` takes effect on next request. The pipeline stores the value at init time, so hot-reload requires updating it on the pipeline instance. |

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Losing context breaks LLM responses | Medium (for users who enable it) | High | Default `null` — opt-in. Users who need full history don't enable it. |
| Orphaned tool messages at truncation boundary | Low | Medium | Reversed scan keeps complete clusters. Edge case: boundary splits a multi-message tool sequence. |
| Semantic cache guaranteed miss after truncation | Certain | Low | Trade-off documented. In long conversations, semantic cache was unlikely to hit anyway (similarity threshold is strict). |
| Users unaware of feature | Medium | Low | Logged at info when active. Documented in deployment guide. |
| Performance: token counting per request | Low | Low | `litellm.token_counter` is O(n) on text length. Truncation runs once per request. Acceptable overhead. |
