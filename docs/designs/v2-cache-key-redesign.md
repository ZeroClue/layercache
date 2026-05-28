# V2: Cache Key Redesign — Cross-Conversation Semantic Response Caching

## Status: Draft

## Executive Summary

LayerCache's current cache key (`prefix_hash = L0 + L1 + L2 + session_id`) fundamentally prevents cross-conversation cache hits because L2 changes **every turn** within a session. Provider KV caching already handles per-session token-level prefix reuse. **LayerCache's unique value proposition is cross-conversation response caching** — something no provider offers.

**Proposal**: Redesign the cache key to `prefix_hash = L0 + L1` only, removing L2 and `session_id` from the hash entirely. This enables semantic matching across any session history, dramatically increasing cache hit rates. L0/L1 standardization via prompt normalization and compression further broadens the cacheable surface.

## Problem Analysis

### The current design is self-defeating

| Component | Cache key? | Changes how often? | Effect |
|-----------|-----------|-------------------|--------|
| L0 (system prompt) | ✅ Yes | Rare (per-project/per-fork) | Stable — good for caching |
| L1 (context/docs) | ✅ Yes | Rare (per-project) | Stable — good for caching |
| L2 (session history) | ✅ Yes | **Every turn** | Destroys cache locality |
| session_id | ✅ Yes | Per-conversation/fork | Fragments across sessions |
| L4 (user query) | ❌ No (semantic) | Every turn | Used for embedding match |

**Result**: Every turn has a unique key. Cache hit rate approaches 0% for session-based clients, regardless of semantic similarity.

### Provider KV caching already handles the session case

- **Anthropic**: Caches KV vectors for identical token prefixes within a conversation (prompt caching).
- **OpenAI**: Automatic prefix caching — no explicit markers needed.
- **Gemini**: `CachedContent` API for explicit prefix caching.

All three handle the per-session, per-turn token-level reuse. They save prefill computation but **still bill for output tokens**.

### LayerCache's real value

LayerCache saves **100% of both input AND output cost** on cache hits. This is only valuable when the same response can be reused **across different sessions/users**. That requires removing session-specific and turn-specific data from the cache key.

## Proposed Architecture

### New prefix hash definition

```
prefix_hash = SHA-256(L0 + L1 [+ tools_hash])
```

- **L0 (SYSTEM)**: Core persona, safety rules, project configuration
- **L1 (CONTEXT)**: Documentation, reference material, tool definitions
- **tools_hash**: Deterministic hash of tool definitions (if provided)
- **Removed**: L2 (session history), session_id
- **Unchanged**: L4 (user query) → semantic embedding lookup within same prefix_hash bucket

### Impact on cache behavior

| Scenario | Old key | New key | Comment |
|----------|---------|---------|---------|
| Same user, same model, same turn | Hit | Hit | Unchanged |
| Same user, different turn | **Miss** (L2 changed) | **Hit** (L2 removed) | **Major improvement** |
| Different user, same prompt | **Miss** (session_id) | **Hit** (no session_id) | **Major improvement** |
| Different project (different L0) | Miss | Miss | Correct — different answers |
| Different tools | Miss | Miss | Correct — different answers |

### Prefix compression (L0 + L1 normalization)

To maximize the likelihood of prefix hash matches across users/projects with similar but not identical prompts, we introduce **prefix normalization** before hashing:

1. **Deterministic message ordering**: Already implemented — sorted by `content_hash()` within each layer.
2. **Whitespace normalization**: Collapse all whitespace sequences to single spaces.
3. **JSON canonicalization**: Sort keys, remove trailing commas, normalize numeric representations.
4. **Redact session-specific metadata**: Strip timestamps, random IDs, user names embedded in system messages.
5. **Optional template normalization**: Replace registry template content with template hash — users of the same template get the same prefix hash regardless of parameter expansion.

### Compression strategies for L0/L1

The system prompt in agentic tools (Claude Code, opencode) can be 10K-50K+ tokens. Normalizing and compressing L0/L1 serves two purposes:

1. **Increases match probability**: Different tool versions with non-semantic diffs produce the same normalized content.
2. **Reduces storage**: Smaller prefix content in the cache key indexes.

**Hard compression techniques** (deterministic, reversible):

- Token-level deduplication within system prompts
- Comment and whitespace removal
- Variable name normalization
- Instruction pruning (remove redundant instructions)

**Soft compression techniques** (lossy, needs empirical validation):

- Semantic hashing of L0/L1 sections — hash chunks by embedding and only keep unique chunks
- Instruction summarization via LLM (one-time, offline per template)
- Tool definition minification (strip descriptions, keep only name+parameters)

## Implementation Plan

### Phase 1: Model changes

Modify `StratifiedPrompt.prefix_hash()` in `layercache/models.py`. Remove L2 and session_id from the hash:

```python
def prefix_hash(self, tools: list[dict] | None = None) -> str:
    stable_layers = [LayerType.SYSTEM, LayerType.CONTEXT]

    prefix_content: list[str] = []

    for lt in stable_layers:
        for msg in sorted(self.layers[lt], key=lambda m: m.content_hash()):
            content = self._normalize_content(msg.content)
            prefix_content.append(f"{msg.role}:{content}")

    if tools:
        tool_hash = ToolSerializer.compute_tool_hash(tools)
        prefix_content.append(f"_tools:{tool_hash}")

    combined = "|".join(prefix_content)
    return hashlib.sha256(combined.encode()).hexdigest()
```

Note: `_normalize_content` performs whitespace normalization and metadata redaction before hashing:

```python
@staticmethod
def _normalize_content(content: str | dict | list) -> str:
    if isinstance(content, (dict, list)):
        return json.dumps(content, sort_keys=True, separators=(",", ":"))
    content = re.sub(r'\s+', ' ', str(content)).strip()
    content = re.sub(
        r'(?i)(timestamp|date|time|session[_-]?id|request[_-]?id):\s*\S+',
        r'\1:__REDACTED__',
        content,
    )
    return content
```

### Phase 2: Pipeline changes

In `layercache/pipeline.py`:

1. L2 continues to be classified normally (still needed for constructing the actual LLM request, enhancement context, and metrics).
2. The semantic cache `lookup()` and `store()` methods automatically use the new prefix hash.

## Validation Plan

### Controlled experiment

1. **Collect traces**: Capture real Claude Code / opencode conversation traces (5+ sessions, 10+ turns each).
2. **Replay with old key**: Compute prefix hash with `L0+L1+L2+session_id` — count cache hits.
3. **Replay with new key**: Compute prefix hash with `L0+L1` only — count cache hits.
4. **Compare hit rates**: Expected improvement: 0-5% → 30-60%.

### Correctness validation

1. **Semantic accuracy**: For every cache hit on the new key, verify the cached response is appropriate for the current query (semantic similarity check + manual review).
2. **False positive analysis**: Measure how often `L0+L1` is the same but the response should differ due to different L2 context. Expectation: at strict similarity thresholds (0.95+), the query embedding should prevent this.

### Risk: L2-dependent responses

Some responses depend on conversation history (e.g., "based on our previous discussion..."). The semantic similarity threshold is the guard: if the query is semantically identical to a previous query in a different context, the answer is likely similar enough to reuse. For context-dependent queries, the embedding distance will naturally be higher, causing a cache miss.

**Mitigation**: Log prefix hash collisions (same `L0+L1` but different `L2`) and measure response divergence. If divergence is significant, consider a hybrid approach where `L2` is a **weighted** factor (stored in the cache entry metadata) rather than part of the key.

## L0/L1 Optimization Program

### Goal

Standardize and shrink the cacheable prefix to maximize cache hit surface across tool versions and projects.

### Phase A: Audit (what's in L0/L1 today)

1. Capture L0/L1 content from 10+ real Claude Code / opencode sessions.
2. Tokenize and measure: how many tokens? What's the variance?
3. Identify common patterns: project-specific instructions, tool-specific parameters, documentation snippets.

### Phase B: Normalization

1. Implement deterministic normalization rules (Phase 3 above).
2. Measure token reduction and prefix hash match rate improvement.
3. Test against real traces.

### Phase C: Compression

1. Implement token deduplication within L0/L1.
2. Evaluate instruction pruning: which parts of the system prompt change between projects? Which are boilerplate?
3. Design a "signature" approach: hash known-bolierplate sections to `__TEMPLATE_HASH_xxx__` tokens, keeping only variable content in the actual prefix.

### Phase D: Model-specific optimization

Different providers have different KV cache architectures:

- **Anthropic**: Hierarchical KV cache with `cache_control` breakpoints. L0 should be structured to maximize cacheable breakpoints. Long context sections benefit most.
- **OpenAI**: Automatic prefix caching. L0/L1 size doesn't matter as much — hash hit/miss is binary.
- **Gemini**: Explicit `CachedContent` creation. L0/L1 size directly affects TTL and storage cost.

Design L0/L1 structure to maximize the best caching behavior for each provider adapter.

## Open Questions

1. **Should `tools_hash` stay in the hash or also be removed?** Tools change infrequently within a session but differ between projects. Removing tools from the hash would increase match rate but risks returning responses for different tool schemas. **Decision: keep tools_hash in the key** — the response format is tool-dependent.

2. **Should we keep L2 classification at all?** Yes — L2 is still needed for (a) constructing the actual LLM request, (b) semantic enhancement context (L3), (c) metrics and debugging. It's just removed from the cache key.

3. **Is prefix normalization worth it?** Needs validation. If the semantic cache key becomes too broad (same L0/L1 across very different projects), normalization could create false prefix matches. The semantic similarity threshold on L4 is the second guard.

## Related Papers

- **SmartCache** (NeurIPS 2025): Context-aware semantic + KV cache co-design. Uses Semantic Forest for hierarchical turn indexing; cross-session KV cache sharing reduces memory 59%, TTFT 78%.
- **ContextCache** (arxiv 2506.22791): Two-stage retrieval (vector search → self-attention) for context-aware matching. Directly addresses incorrect hits when similar queries appear in different conversational contexts.
- **LMCache** (arxiv 2510.09665): First efficient open-source KV caching layer. 15× throughput improvement via cache offloading and PD disaggregation. Key insight: context truncation cuts prefix hit ratio by 50%.
- **LLMs Get Lost In Multi-Turn Conversation** (ICLR 2026 Oral, arxiv 2505.06120): LLMs degrade 39% in multi-turn vs single-turn. Early turn assumptions are never corrected — relevant to why L2 should not be in the cache key.
- **Prompt Compression for LLMs: A Survey** (NAACL 2025, arxiv 2410.12388): Taxonomizes hard (filtering, paraphrasing) and soft (learned compression tokens) methods. Source for L0/L1 compression techniques.
- **Don't Break the Cache** (arxiv 2601.06007): System-prompt-only caching gives 80% of cost benefit with 28-31% TTFT improvement; full context caching can *increase* latency.

## References

- "Don't Break the Cache: Evaluation of Prompt Caching for Long-Horizon Agentic Tasks" (arxiv 2601.06007) — System-prompt-only caching gives 80% of benefit
- Research on prompt caching: [Anthropic prompt caching docs](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
- LayerCache AGENTS.md — Current architecture documentation
- `layercache/models.py:120` — Current `prefix_hash()` implementation
- `layercache/cache/semantic.py` — Current semantic cache implementation
- `layercache/stratifier.py` — Current stratification logic
