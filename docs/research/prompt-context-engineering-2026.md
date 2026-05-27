# Prompt & Context Engineering for LayerCache: 2026 State of the Art

**Last Updated:** May 2026  
**Target Version:** LayerCache v1.6+

---

## Executive Summary

Prompt caching and context engineering have emerged as the highest-ROI optimizations for production LLM systems in 2025-2026. Organizations implementing multi-tier caching architectures (semantic → prefix → inference) report **40-90% cost reductions** and **50-85% latency improvements** without degrading output quality. The key insight: token optimization is a **context-engineering problem, not a prompt-shortening problem**. Most teams waste effort making prompts shorter when the real cost drivers are bloated context, idle tool schemas, and stale conversation history.

For LayerCache specifically, the stratified L0-L4 architecture positions it uniquely to capitalize on these advances. By integrating provider-native prefix caching (Anthropic 90% discounts, OpenAI 50% automatic), semantic caching with adaptive thresholds, and systematic context compression at layer boundaries, LayerCache v1.6+ can deliver **60-80% token reduction** for typical workloads while maintaining or improving cache hit rates. The "1M token wall" research confirms that larger context windows don't solve the problem—thoughtful context curation does.

---

## 1. State of the Art (2025-2026)

### 1.1 Provider-Native Prompt Caching

All major LLM providers now offer production-ready prompt caching with significant cost advantages:

| Provider | Mechanism | Cache Read Cost | Cache Write Cost | Min Tokens | TTL |
|----------|-----------|-----------------|------------------|------------|-----|
| **Anthropic Claude** | Manual (`cache_control`) | $0.30/MTok (90% off) | $3.75/MTok (+25%) | 1,024 per checkpoint | 5 min (extends to 1hr) |
| **OpenAI GPT-5.x** | Automatic | 50-90% off base | No premium | 1,024 | 5-10 min (24hr GPT-5.1) |
| **Google Gemini** | Automatic/Explicit | ~50% off | Variable | 1,024 | ~10 min |
| **vLLM (self-hosted)** | APC (hash table) | ~90% off | GPU memory cost | Configurable | Session-based |

**Key Findings (2026):**
- Anthropic's December 2025 update delivers **90% cost reduction and 85% latency reduction** for long prompts with proper cache structure
- OpenAI's automatic caching requires **zero code changes** but achieves only 50% savings vs. Anthropic's 90%
- Research shows **31% of LLM queries exhibit semantic similarity**—massive inefficiency without caching
- Break-even for Anthropic: **1.4 cache hits per cached prefix** (remarkably low threshold)
- GPT-5.5 Pro ($30/$180/MTok) offers **no cached-input discount**—critical routing consideration

### 1.2 Context Engineering Fundamentals

Context engineering has supplanted prompt engineering as the primary discipline for production LLM systems:

> *"Context engineering is the skill that separates developers who get 10x value from AI coding agents from those who get 2x."* — MorphLLM, 2026

**Core Principles:**
1. **Context Rot**: Model performance degrades as context window fills. Research from Chroma (2025) shows GPT-4 accuracy drops from **98.1% to 64.1%** based solely on information structure within context.
2. **1M Token Wall**: SWE-rebench maintainer @Shevan05 identified a **clear performance ceiling around 1 million tokens**—performance degrades meaningfully past this point regardless of advertised context window.
3. **Attention Budget**: LLMs have finite attention capacity. Every token introduced depletes this budget, creating natural tension between context size and reasoning capability.
4. **Lost in the Middle**: Performance peaks when relevant content sits at beginning or end; drops when buried in middle (U-shaped attention curve).

### 1.3 Token Economics Reality

**Input-Output Ratio**: Production agents typically consume **100 tokens of context for every token generated**. This 100:1 ratio fundamentally shapes optimization strategy.

**Cost Structure Example** (customer support agent, 10K conversations/day):
- **Without optimization**: $700/day → $255,000/year
- **With 60% context compression**: $102,000/year
- **Savings**: $153,000/year through engineering optimization alone

**Cache Economics**:
- Static context (system instructions, tool descriptions): **95%+ cache hit rates**
- Semi-static context (user profiles, preferences): **60-80% hit rates**
- Dynamic context (real-time data, tool outputs): **0-20% hit rates**

---

## 2. Actionable Techniques for LayerCache

Categorized by effort/impact matrix for LayerCache integration:

### 2.1 High Impact, Low Effort (Quick Wins)

#### **Technique 1: Stable Prefix Architecture**
**Impact:** 50-90% input cost reduction  
**Effort:** Low (prompt restructuring)

**Implementation:**
```python
# LayerCache L0-L2 should form stable prefix
# L0: Model/system instructions (static)
# L1: Template structure (static)
# L2: Canonicalized context (semi-static)
# L3-L4: Dynamic user input (variable)

# Current LayerCache structure already aligns with this!
# Enhancement: Ensure L0+L1+L2 ≥ 1,024 tokens for cache eligibility
```

**Key Rules:**
- Static content FIRST (system prompts, tool definitions, schemas)
- Dynamic content LAST (user messages, session data, timestamps)
- Never inject dynamic values (timestamps, user IDs, request IDs) into L0-L2
- Use `metadata` field for debugging info, not prompt content

**Quantitative Impact:**
- Teams restructuring prompts report **70%+ cache hit rates** within 2 weeks
- 30,000-token document with 3 parallel questions: **59% cost reduction** from cache warming alone

#### **Technique 2: Provider Adapter Cache Integration**
**Impact:** 50-90% cost reduction on cached tokens  
**Effort:** Low (existing adapter pattern)

**Current LayerCache Architecture:**
LayerCache already has provider adapters in `layercache/adapters/` for Anthropic, OpenAI, and Gemini. Enhancement:

```python
# Anthropic adapter enhancement
class AnthropicAdapter:
    def create_cache_breakpoints(self, stratified_prompt: StratifiedPrompt):
        """Insert cache_control markers at L2/L3 boundary"""
        # L0+L1+L2 = stable prefix (cacheable)
        # L3+L4 = dynamic suffix (non-cacheable)
        return [
            {"type": "text", "text": l0_l1_l2_content, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": l3_l4_content}
        ]

# OpenAI adapter (automatic, but add monitoring)
class OpenAIAdapter:
    def extract_cache_metrics(self, response) -> dict:
        return {
            "cached_tokens": response.usage.prompt_tokens_details.cached_tokens,
            "total_tokens": response.usage.prompt_tokens,
            "cache_hit_rate": response.usage.prompt_tokens_details.cached_tokens / 
                             response.usage.prompt_tokens
        }
```

**Monitoring Requirements:**
- Track `cache_read_input_tokens` in all responses
- Target **70%+ cache hit rate** for stable-prompt workloads
- Alert if hit rate drops below 50% (indicates prompt instability)

#### **Technique 3: Tool Schema Deterministic Serialization**
**Impact:** 85% tool overhead reduction  
**Effort:** Low (sorting + caching)

**Problem:** Tool definitions included in every request. Real-world setups measure **55K-134K tokens** of tool-definition overhead before any work starts.

**Solution:**
```python
# In LayerCache enhancement plugin system
def serialize_tools_deterministic(tools: list) -> str:
    """Sort tools by name, sort schema keys, ensure byte-identical output"""
    sorted_tools = sorted(tools, key=lambda t: t["name"])
    for tool in sorted_tools:
        tool["parameters"] = sort_dict_keys(tool["parameters"])
    return json.dumps(sorted_tools, sort_keys=True)

# Cache the serialized tool definitions at L1
# Only reload when tools actually change
```

**Quantitative Impact:**
- One production setup reduced overhead from **134K to 8.7K tokens** (85% reduction)
- Use `allowed_tools` parameter (OpenAI) or tool masking (Anthropic) to restrict per-call without breaking cache

### 2.2 High Impact, Medium Effort (Strategic)

#### **Technique 4: Multi-Tier Caching Architecture**
**Impact:** 70-80% of tokens routed through caching layers  
**Effort:** Medium (semantic cache integration)

**Architecture:**
```
Request → Semantic Cache (100% savings) 
        → Prefix Cache (50-90% savings) 
        → Full Inference (0% savings)
```

**LayerCache Integration:**
```python
# Current LayerCache semantic cache at entry point
# Enhancement: Add response-level caching as intermediate layer

class RequestPipeline:
    async def process(self, request: LayerCacheRequest):
        # Tier 1: Semantic cache (existing)
        semantic_hit = await self.semantic_cache.lookup(request)
        if semantic_hit:
            return semantic_hit.response
        
        # Tier 2: Prefix cache (provider-native, via adapters)
        # Handled by provider adapters automatically
        
        # Tier 3: Full inference
        response = await self.call_llm(request)
        
        # Store in both caches
        await self.semantic_cache.store(request, response)
        return response
```

**Semantic Cache Enhancements:**
- **Adaptive thresholds**: Static 0.8 cosine similarity performs poorly across diverse queries. Implement query-complexity-based threshold adjustment.
- **Multi-turn embedding**: Embed conversation state, not just single queries
- **Gated cross-encoder rerank**: Use fast embedding for retrieval, expensive cross-encoder for final validation

**Production Metrics** (GPTCache, 2025):
- **61-69% cache hit rates** on FAQ-style workloads
- **97%+ accuracy** on hits
- **40-50% latency reduction** on cache hits

#### **Technique 5: Context Compaction at Layer Boundaries**
**Impact:** 60% context reduction without information loss  
**Effort:** Medium (summarization logic)

**Problem:** Multi-turn conversations accumulate context exponentially. Turn 15 can consume 5,250+ tokens when 500-1,000 would suffice.

**LayerCache-Specific Solution:**
```python
# In stratifier.py, add compaction at L2→L3 boundary
class Stratifier:
    def compact_l2_history(self, l2_context: list, max_tokens: int) -> str:
        """Progressive summarization: recent=verbatim, older=summary, oldest=bullets"""
        # Keep last 5 turns verbatim
        recent = l2_context[-5:]
        
        # Summarize older turns
        older = l2_context[:-5]
        if older:
            summary = self._generate_summary(older, max_tokens=200)
            return [summary] + recent
        
        return recent
    
    def _generate_summary(self, turns: list, max_tokens: int) -> str:
        """Use cheap model (Haiku/gpt-4o-mini) for summarization"""
        # Preserve: user goals, decisions made, key facts
        # Discard: greetings, acknowledgments, repeated questions
```

**Trigger Strategy:**
- Don't summarize on every turn (adds LLM call costs)
- Summarize when L2 segment exceeds **80% of budget**
- Use anchored iterative summarization: maintain structured session-state document (intent, decisions, actions, next steps)

**Quantitative Impact:**
- Conversations extend from **15 turns to 50+ turns** within same budget
- **60% context reduction** without information loss
- Quality degrades gracefully rather than disappearing

#### **Technique 6: Selective Retrieval with Relevance Thresholds**
**Impact:** 40-60% RAG context reduction, 11% hallucination reduction  
**Effort:** Medium (threshold tuning)

**Problem:** RAG pipelines retrieve top-10 documents for every query. Low-relevance documents (scores 0.60-0.70) dilute context and increase hallucination risk.

**Implementation:**
```python
# In LayerCache cache/semantic.py
class SemanticCache:
    def retrieve_with_threshold(self, query: str, threshold: float = 0.82) -> list:
        """Only include documents above relevance threshold"""
        results = self.vector_store.similarity_search(query, k=10)
        
        # Filter by threshold
        relevant = [doc for doc in results if doc.similarity_score >= threshold]
        
        if not relevant:
            # Return empty with "I don't know" signal
            return [], "NO_RELEVANT_CONTEXT"
        
        return relevant[:5], None  # Max 5 highly relevant docs
```

**Threshold Calibration:**
- Start at **0.80-0.82** for factual queries
- Lower if agent says "I don't know" too often
- Raise if agent hallucinates from irrelevant context
- Implement adaptive thresholds based on query complexity

**Quantitative Impact:**
- Before: 10 documents/query, 3-4 relevant, 6-7 noise, **15% hallucination rate**
- After: 3-5 highly relevant documents, **4% hallucination rate**

### 2.3 High Impact, High Effort (Architecture Changes)

#### **Technique 7: Model Routing by Task Complexity**
**Impact:** 40-60% cost reduction via right-sizing  
**Effort:** High (routing layer + quality monitoring)

**Architecture:**
```python
# New LayerCache module: layercache/router.py
class ModelRouter:
    def route(self, request: LayerCacheRequest) -> str:
        """Classify query, route to cost-effective model"""
        # Use cheap model for classification
        category = self.classify_query(request, model="haiku-4.5")
        
        route_map = {
            "simple": "haiku-4.5",      # $1/MTok input
            "moderate": "sonnet-4.6",   # $3/MTok input
            "complex": "opus-4.7"       # $15/MTok input
        }
        
        return route_map.get(category, "sonnet-4.6")
    
    def classify_query(self, request: LayerCacheRequest, model: str) -> str:
        """
        Categories:
        - simple: grammar, formatting, basic Q&A, classification
        - moderate: multi-step reasoning, brief analysis, extraction
        - complex: creative generation, advanced code, nuanced reasoning
        """
```

**Routing Heuristics:**
- **Simple** (Haiku/gpt-4o-mini): Classification, extraction, formatting, grammar
- **Moderate** (Sonnet/gpt-4o): Multi-step reasoning, analysis, structured generation
- **Complex** (Opus/gpt-4o-pro): Creative work, architecture decisions, edge cases

**Advisor Pattern** (Anthropic, April 2026):
- Pair cheap executor (Sonnet/Haiku) with expensive advisor (Opus)
- Advisor consulted only when confidence < threshold
- **73-87% cost reduction** vs. Opus-only agents
- Advisor generates 400-700 tokens per consultation

**Caveats:**
- Claude Opus 4.7 uses **new tokenizer (+35% tokens for code)**—factor into budget
- GPT-5.5 Pro offers **no cached-input discount**—avoid for cacheable workloads
- o3 dropped **80% in price** (April 2026) to $2/$8/MTok—reassess for reasoning tasks

#### **Technique 8: LLMLingua-Style Prompt Compression**
**Impact:** 3-20x compression, 1.7-5.7x latency speedup  
**Effort:** High (ML model integration)

**Approaches:**

| Method | Compression | Quality Loss | Speedup | Best For |
|--------|-------------|--------------|---------|----------|
| **LLMLingua-2** (token-level) | 3-6x | ~1.5 pts GSM8K | 3-6x | Prose, general QA |
| **LLMLingua** (original) | Up to 20x | 1.5-3 pts | 1.7-5.7x | RAG, long docs |
| **SWE-Pruner** (code-specific) | 2-4x | Minimal | 2-3x | Code generation |
| **EHPC** (attention-based) | 2-5x | <1 pt | 2-4x | No aux model needed |

**LayerCache Integration:**
```python
# New enhancement plugin: layercache/enhancements/compression.py
class PromptCompressor:
    def __init__(self, method: str = "llmlingua-2"):
        self.model = self._load_compressor(model)
    
    def compress(self, prompt: str, target_ratio: float = 0.3) -> str:
        """Compress prompt to target ratio (e.g., 0.3 = 70% reduction)"""
        # Token-level pruning for prose
        # Chunk-level for code (preserve syntax)
        # Query-aware: preserve tokens relevant to specific question
```

**Critical Considerations:**
- **Code requires chunk-level pruning** (token-level breaks syntax)
- SWE-Bench: SWE-Pruner **64% success** vs. LLMLingua-2 **54%**
- Moderate compression can **enhance** performance by removing noise
- No single method dominates—benchmark per domain

#### **Technique 9: File System as External Context**
**Impact:** Effectively unlimited context, 95% reduction in token transmission  
**Effort:** High (agent behavior change)

**Pattern** (from Manus AI):
- Treat file system as **structured, externalized memory**
- Agent writes intermediate results to files, reads on demand
- Avoids irreversible compression risk
- Unlimited size, persistent by nature, directly operable

**LayerCache Application:**
```yaml
# layercache.yaml enhancement
context_management:
  external_storage:
    enabled: true
    base_path: /data/layercache/context
    strategy: "reference_only"  # Store refs in context, full data on disk
    ttl_hours: 24
```

**Implementation:**
```python
# In pipeline.py
class RequestPipeline:
    async def store_context_reference(self, context_id: str, content: str):
        """Write large context to disk, return reference"""
        path = f"{self.config.context_storage}/{context_id}.json"
        async with aiofiles.open(path, 'w') as f:
            await f.write(content)
        return f"CONTEXT_REF:{context_id}"
    
    async def load_context_reference(self, ref: str) -> str:
        """Load context from disk when needed"""
        context_id = ref.replace("CONTEXT_REF:", "")
        path = f"{self.config.context_storage}/{context_id}.json"
        async with aiofiles.open(path, 'r') as f:
            return await f.read()
```

---

## 3. Integration Recommendations for LayerCache v1.6+

### 3.1 Immediate (v1.6.0 - Q2 2026)

**Priority 1: Stable Prefix Enforcement**
- [ ] Audit L0-L2 token count (ensure ≥1,024 for cache eligibility)
- [ ] Add validation: reject requests with dynamic content in L0-L1
- [ ] Documentation: prompt structure best practices for users
- [ ] Metrics: track cache hit rate per provider

**Priority 2: Provider Adapter Cache Integration**
- [ ] Anthropic: Add `cache_control` markers at L2/L3 boundary
- [ ] OpenAI: Extract and log `cached_tokens` from responses
- [ ] Gemini: Implement explicit cache creation for long contexts
- [ ] Dashboard: Add cache utilization visualization

**Priority 3: Tool Schema Optimization**
- [ ] Deterministic serialization (sorted keys, stable ordering)
- [ ] Cache tool definitions separately from request context
- [ ] Implement `allowed_tools` pattern for per-call restriction

### 3.2 Short-Term (v1.6.1 - Q3 2026)

**Priority 4: Multi-Tier Caching**
- [ ] Enhance semantic cache with adaptive thresholds
- [ ] Add response-level caching layer
- [ ] Implement cross-encoder reranking for high-value queries
- [ ] Metrics: cache hit rate by tier (semantic vs. prefix)

**Priority 5: Context Compaction**
- [ ] Progressive summarization at L2→L3 boundary
- [ ] Configurable compaction triggers (token budget %, turn count)
- [ ] Use cheap model for summarization (Haiku/gpt-4o-mini)
- [ ] Preserve structured session state (intent, decisions, actions)

**Priority 6: Selective Retrieval**
- [ ] Relevance threshold filtering (default 0.82)
- [ ] "I don't know" signal for no-relevant-context cases
- [ ] Hybrid retrieval (semantic + keyword/metadata)
- [ ] Agent-controlled vs. automatic retrieval toggle

### 3.3 Medium-Term (v1.7+ - Q4 2026)

**Priority 7: Model Routing**
- [ ] Task classification layer (simple/moderate/complex)
- [ ] Router configuration in `layercache.yaml`
- [ ] Quality monitoring per model tier
- [ ] Advisor pattern for high-stakes tasks

**Priority 8: Prompt Compression**
- [ ] LLMLingua-2 integration as optional enhancement
- [ ] Code-specific pruner (SWE-Pruner style)
- [ ] Benchmark suite per domain (prose vs. code vs. structured)
- [ ] Compression ratio configuration per layer

**Priority 9: External Context Storage**
- [ ] File system backend for large context
- [ ] Reference-only injection in prompt
- [ ] TTL-based cleanup
- [ ] Agent training for file-based memory patterns

---

## 4. Cost-Benefit Analysis

### 4.1 Estimated Token Savings

| Technique | Input Token Reduction | Output Token Reduction | Total Cost Savings | Implementation Effort |
|-----------|----------------------|------------------------|-------------------|----------------------|
| Stable Prefix + Cache | 50-90% (cached portion) | 0% | 40-70% | Low (1-2 days) |
| Tool Schema Optimization | 85% (tool overhead) | 0% | 10-20% | Low (1 day) |
| Context Compaction | 60% (history) | 0% | 20-30% | Medium (3-5 days) |
| Selective Retrieval | 40-60% (RAG context) | 0% | 15-25% | Medium (3-5 days) |
| Model Routing | 0% | 0% | 40-60% (model cost) | High (1-2 weeks) |
| Prompt Compression | 50-80% (compressed portion) | 0% | 20-40% | High (1-2 weeks) |
| **Combined (typical workload)** | **70-85%** | **10-20%** | **60-80%** | - |

### 4.2 ROI Calculation Example

**Baseline:** LayerCache processing 1M requests/month
- Average input: 5,000 tokens/request @ $3/MTok = $15,000/month
- Average output: 500 tokens/request @ $15/MTok = $7,500/month
- **Total: $22,500/month**

**After Optimization (conservative 60% savings):**
- Input tokens: 2,000/request (60% reduction via caching + compaction)
- Output tokens: 450/request (10% reduction via output constraints)
- New input cost: $6,000/month
- New output cost: $6,750/month
- **Total: $12,750/month**
- **Savings: $9,750/month ($117,000/year)**

**Implementation Cost:**
- Engineering time: 4-6 weeks (2 engineers)
- Estimated cost: $40,000-60,000
- **Payback period: 4-6 months**

### 4.3 Performance Impact

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| Time-to-First-Token (TTFT) | 2.5s | 0.8s | 68% faster |
| Cache Hit Rate | 0% (no cache) | 70%+ | - |
| Context Window Utilization | 95% (bloated) | 60-80% (optimized) | Better quality |
| Hallucination Rate | 15% | 4% | 73% reduction |
| Max Conversation Turns | 15 | 50+ | 233% increase |

---

## 5. Gotchas & Failure Modes

### 5.1 When Caching Hurts

**One-Shot Workflows:**
- If each session is completely unique with no shared context
- Paying 25% write premium (Anthropic) with zero reads
- **Solution:** Run numbers before enabling `cache_control` everywhere

**Dynamic System Prompts:**
- Heavy personalization (user preferences, current date, dynamic instructions)
- Undermines prefix caching entirely
- **Solution:** Move personalization to separate, later prompt section

**Short Prompts:**
- Below 1,024-token threshold, caching doesn't engage
- **Solution:** Focus on other optimizations (model routing, output constraints)

**Parallel Execution Trap:**
- Fire 10 parallel requests before first cache written
- Result: 10 cache writes, 0 reads, bill 5-10x expected
- **Solution:** Dedicated warmup call before parallel processing
  ```python
  # Warm cache first
  await client.messages.create(
      system=[{"text": doc, "cache_control": {"type": "ephemeral"}}],
      messages=[{"role": "user", "content": "Ready."}],
      max_tokens=1
  )
  # Now parallel requests hit cache
  ```

### 5.2 Context Engineering Pitfalls

**Context Poisoning:**
- Old tool outputs, resolved errors, outdated decisions remain in prompt
- **Solution:** Incremental summarization + reference-based storage

**Context Fragmentation:**
- Information scatters across history, retrieval difficult
- **Solution:** Anchored iterative summarization (structured session state)

**Context Staleness:**
- Historical information outdated but remains in context
- **Solution:** Relevance-based pruning, TTL on retrieved data

**Tool Definition Bloat:**
- 55K-134K tokens of tool overhead before any work
- **Solution:** Disable unused MCP servers, on-demand tool loading

### 5.3 Provider-Specific Caveats

**Anthropic:**
- Opus 4.7 new tokenizer: **+35% tokens for code** (negligible for plain English)
- Factor into budget when migrating from Opus 4.6
- Cache TTL: 5 minutes default, extends to 1 hour with regular hits

**OpenAI:**
- GPT-5.5 Pro: **No cached-input discount** (exception to 90% rule)
- GPT-5.4 family: 90% cached-input discounts apply
- Automatic caching: cannot manually clear cache

**Google:**
- Variable pricing based on cached context size and duration
- Storage fees for cached content
- Best for massive context windows (up to 2M tokens)

---

## 6. References

### 6.1 Research Papers

1. **"Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks"** (arXiv:2601.06007v2, Jan 2026)
   - Comprehensive evaluation across OpenAI, Anthropic, Google
   - 41-80% API cost reduction, 13-31% TTFT improvement
   - Strategic cache boundary control outperforms naive full-context caching

2. **"GPT Semantic Cache: Reducing LLM Costs and Latency via Semantic Caching"** (arXiv:2411.05276v3, 2025)
   - Embedding-based caching with in-memory storage
   - 68.8% API call reduction, 97%+ accuracy

3. **"Context Rot in Long-Context Language Models"** (Chroma Research, 2025)
   - GPT-4 accuracy drops from 98.1% to 64.1% with poor context structure
   - U-shaped attention curve (lost in the middle)

4. **"LLMLingua-2: Token-Level Prompt Compression"** (arXiv:2403.12968, 2024)
   - 3-6x speedup over earlier methods
   - 3-6x compression with ~1.5 point GSM8K loss

5. **"SWE-Pruner: Domain-Specific Code Compression"** (arXiv:2601.16746v3, 2026)
   - 64% task success on SWE-Bench vs. 54% for general methods
   - Chunk-level pruning preserves syntactic validity

### 6.2 Documentation & Guides

1. **Anthropic Prompt Caching Docs** (https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
   - Cache control API, pricing, best practices
   - 90% cost reduction, 5-minute to 1-hour TTL

2. **OpenAI Prompt Caching Cookbook** (https://developers.openai.com/cookbook/examples/prompt_caching_201)
   - Automatic caching, monitoring, optimization strategies
   - 50-90% discounts depending on model

3. **Anthropic Context Engineering Guide** (https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
   - Offloading, reduction, retrieval, isolation techniques
   - Token budget allocation framework

4. **LangChain Context Engineering** (https://www.langchain.com/blog/context-engineering-for-agents)
   - Summarization at agent boundaries
   - Production patterns for multi-turn agents

### 6.3 Blog Posts & Analysis

1. **"Prompt Caching Infrastructure: Reducing LLM Costs and Latency"** (Introl, Dec 2025)
   - Multi-tier caching architecture
   - 90% cost reduction, 85% latency reduction

2. **"Context Engineering for AI Agents: Token Optimization"** (FlowHunt, 2026)
   - Four core techniques with production examples
   - 40-60% token cost reduction typical

3. **"Prompt Caching: The Optimization That Cuts LLM Costs by 90%"** (Tian Pan, Oct 2025)
   - Parallel execution trap, cache warming
   - 59% reduction from warmup alone

4. **"LLM Token Optimization Strategies: Complete Guide"** (TokenOptimize.dev, 2026)
   - Advisor tool pattern, model routing
   - 73-87% cost reduction with advisor pattern

5. **"Context Engineering: Why More Tokens Makes Agents Worse"** (MorphLLM, 2026)
   - 1M token wall, CLAUDE.md playbook
   - 5.5x fewer tokens with proper engineering

6. **"Context Engineering for AI Agents: Lessons from Building Manus"** (Manus.im, July 2025)
   - KV-cache hit rate as primary metric
   - File system as external context

### 6.4 Tools & Libraries

1. **GPTCache** (https://github.com/zilliztech/GPTCache)
   - Open-source semantic caching
   - 61-69% hit rates, 97%+ accuracy

2. **LLMLingua** (https://github.com/microsoft/LLMLingua)
   - Prompt compression library
   - Up to 20x compression

3. **LiteLLM** (https://github.com/BerriAI/litellm)
   - Model routing, cost tracking
   - Unified API across providers

4. **Redis LangCache** (https://redis.io/langcache/)
   - Semantic caching with Redis
   - Up to 73% cost reduction

---

## Appendix A: LayerCache-Specific Implementation Checklist

### v1.6.0 Sprint (2 weeks)

**Week 1: Cache Foundation**
- [ ] Audit current L0-L2 token counts across sample workloads
- [ ] Add validation middleware (reject dynamic content in L0-L1)
- [ ] Anthropic adapter: `cache_control` insertion at L2/L3 boundary
- [ ] OpenAI adapter: cache metrics extraction
- [ ] Update dashboard: cache hit rate visualization

**Week 2: Tool Optimization**
- [ ] Deterministic tool serialization utility
- [ ] Tool definition cache (separate from request context)
- [ ] `allowed_tools` pattern implementation
- [ ] Documentation: user guide for prompt structure
- [ ] Metrics dashboard: cache utilization per provider

### v1.6.1 Sprint (3 weeks)

**Week 1-2: Multi-Tier Caching**
- [ ] Semantic cache adaptive thresholds
- [ ] Response-level caching layer
- [ ] Cross-encoder reranking integration
- [ ] Metrics: hit rate by tier

**Week 3: Context Compaction**
- [ ] Progressive summarization logic
- [ ] Configurable triggers (budget %, turn count)
- [ ] Cheap model integration for summarization
- [ ] Session state preservation

### v1.7 Sprint (4 weeks)

**Week 1-2: Model Routing**
- [ ] Task classification layer
- [ ] Router configuration schema
- [ ] Quality monitoring per tier
- [ ] Advisor pattern implementation

**Week 3-4: Advanced Compression**
- [ ] LLMLingua-2 integration
- [ ] Code-specific pruner
- [ ] Benchmark suite
- [ ] Compression ratio configuration

---

## Appendix B: Configuration Examples

### layercache.yaml (v1.6+)

```yaml
caching:
  semantic:
    enabled: true
    db_path: /data/semantic_cache.db
    embedder: BAAI/bge-small-en-v1.5
    similarity_threshold: 0.82  # Adaptive in v1.6.1
    max_results: 5
    
  prefix:
    enabled: true
    min_tokens: 1024  # Provider minimum
    monitor_hit_rate: true
    alert_threshold: 0.50  # Alert if hit rate < 50%

context_management:
  compaction:
    enabled: true
    trigger_budget_percent: 80  # Compact when L2 > 80% of budget
    max_recent_turns: 5  # Keep last N turns verbatim
    summary_model: haiku-4.5  # Cheap model for summarization
    
  retrieval:
    strategy: selective  # selective vs. automatic
    relevance_threshold: 0.82
    max_documents: 5
    hybrid_search: true  # semantic + keyword
    
  external_storage:
    enabled: false  # v1.7+
    base_path: /data/layercache/context
    ttl_hours: 24

routing:
  enabled: false  # v1.7+
  classifier_model: haiku-4.5
  routes:
    simple: haiku-4.5
    moderate: sonnet-4.6
    complex: opus-4.7
    
  advisor:
    enabled: false  # v1.7+
    model: opus-4.7
    confidence_threshold: 0.7
    max_tokens: 700

compression:
  enabled: false  # v1.7+
  method: llmlingua-2  # llmlingua-2, swe-pruner, ehpc
  target_ratio: 0.3  # 70% compression
  domain: auto  # auto, prose, code, structured
  
providers:
  anthropic:
    adapter: cache_control  # Explicit breakpoints
    cache_ttl_minutes: 60
    
  openai:
    adapter: automatic  # No code changes
    monitor_cached_tokens: true
    
  gemini:
    adapter: explicit  # Manual cache creation
    cache_ttl_hours: 1
```

---

**Document Version:** 1.0  
**Research Period:** January-May 2026  
**Next Review:** Q3 2026 (post-v1.6.1 release)
