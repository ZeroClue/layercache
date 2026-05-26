# DA Proposal: L2 Session Truncation (`max_session_turns`)

## Motivation

Long multi-turn conversations grow L2 unboundedly. The "growing tail" pushes the stable prefix further from the end, making it harder for provider caching to stay effective. Best-practice guidance recommends sending only the last 3-5 turns.

LayerCache currently passes all conversation history through L2 with no truncation.

## Provider scope

**All providers.** Truncation is pipeline-level, applied before adapter-specific marker injection. The reduced prefix benefits Anthropic, OpenAI, and Gemini equally.

## Proposed Change

### 1. Config

```yaml
caching:
  # Max user/assistant turns to keep in L2. null = no limit (current default).
  # Token-based alternative (more precise): max_session_tokens.
  max_session_tokens: 2000   # keep at most ~2000 tokens of history
```

Use **token count** instead of turn count, per DA feedback. A turn can be 50 tokens or 5000. Token count is more precise and less surprising. Users who know their average turn size can set it intuitively.

### 2. Implementation

The `_truncate_session()` method uses token counting via LiteLLM (same as P2):

```python
def _truncate_session(
    self,
    prompt: StratifiedPrompt,
    max_tokens: int | None,
    model: str,
) -> StratifiedPrompt:
    if max_tokens is None or max_tokens <= 0:
        return prompt

    session_messages = prompt.layers.get(LayerType.SESSION, [])
    if not session_messages:
        return prompt

    # Work backwards, removing the oldest turns until under budget
    # Handle tool_call/tool_result interleaves by keeping complete logical turns
    kept: list[StratifiedMessage] = []
    total = 0
    for msg in reversed(session_messages):
        estimated = _estimate_tokens(msg.content, model)
        if total + estimated > max_tokens:
            break
        kept.insert(0, msg)
        total += estimated

    prompt.layers[LayerType.SESSION] = kept
    return prompt
```

Token estimation uses `litellm.token_counter` with fallback (same as P2).

### 3. Position in pipeline

Applied after canonicalization (Stage 3) and before enhancement injection (Stage 4). This means:
- We truncate clean, deterministic content
- L3 enhancements are not affected (they sit between L2 and L4)
- The adapter sees the final truncated message count

### 4. Semantic cache interaction

Truncation changes L2 content, which changes the prefix hash. This means:
- In-flight conversational semantic cache misses are guaranteed after truncation
- Provider prefix caching benefits from the smaller, denser prefix
- This is a trade-off: semantic cache misses vs. provider cache hits

Log at `info` when active so users are aware.

## Files Changed

- `layercache/config.py` — add `max_session_tokens` to `CachingConfig`
- `layercache/pipeline.py` — add `_truncate_session()` method; call in both paths
- `layercache/models.py` — no changes needed
- `layercache.yaml` — document default

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Losing context breaks LLM responses | Default `null` — opt-in only. Users who need full history (legal, medical) don't enable it. |
| Semantic cache guaranteed miss after truncation changes prefix hash | Logged at info. Acceptable trade-off: provider prefix caching benefits more than semantic caching in long conversations. |
| Tool-call turns with 4+ messages counted imprecisely | Token-based counting handles variable-length messages correctly; the reversed scan keeps complete trailing turns |
| Users don't know about the feature | Document in deployment guide; mention in the threshold warning log (P2) |
| LiteLLM tokenizer may not handle composite content | try/except chars/4 fallback per P2 pattern |

## Alternatives Considered

1. **Turn count (`max_session_turns`)** — Simpler but opaque per DA feedback. User has one verbose turn and one terse turn — which budget wins?
2. **Summarization-based truncation** — High complexity and cost. Overkill for V1.
3. **Turn count default of 5, no opt-out** — Breaking change; dangerous for users who rely on full history.
