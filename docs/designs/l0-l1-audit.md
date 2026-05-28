# L0/L1 System Prompt Audit

**Date:** 2026-05-28
**Scope:** opencode + Claude Code system prompt structure analysis

## 1. OpenCode System Prompts

OpenCode ships **4 distinct system prompt templates** embedded in the compiled
binary (`~/.opencode/bin/opencode`). Only the first two are relevant for
initial session L0/L1 analysis; the latter two are continuation/injection
prompts.

### 1.1 "OpenCode" Agent (primary coding assistant)

Used for most interactive sessions. Structure:

```
You are OpenCode, the best coding agent on the planet.
You are an interactive CLI tool that helps users with software engineering tasks...
IMPORTANT: You must NEVER generate or guess URLs for the user...
...
# Tone and style
- Only use emojis if the user explicitly requests it...
- Your output will be displayed on a command line interface...
- NEVER create files unless they're absolutely necessary...
# Professional objectivity
Prioritize technical accuracy and truthfulness...
# Task Management
You have access to the TodoWrite tools to help you manage and plan tasks...
... (continues with detailed tool usage patterns, examples) ...
```

### 1.2 "opencode" Agent (concise assistant)

Used for this session. Structure:

```
You are opencode, an interactive CLI tool that helps users with software engineering tasks...
IMPORTANT: You must NEVER generate or guess URLs for the user...
When the user directly asks about opencode, first use the WebFetch tool...
# Tone and style
You should be concise, direct, and to the point...
IMPORTANT: Keep your responses short, since they will be displayed on a CLI...
```

### 1.3 Agent Continuation

```
You are opencode, an agent - please keep going until the user's query is completely resolved...
# Workflow
1. Fetch any URL's provided by the user...
```

### 1.4 Software Engineering Agent

```
You are opencode, an interactive CLI agent specializing in software engineering tasks...
# Core Mandates
- Conventions: Rigorously adhere to existing project conventions...
# Primary Workflows
## Software Engineering Tasks
1. Understand...
2. Plan...
3. Implement...
4. Verify (Tests)...
5. Verify (Standards)...
```

---

## 2. Claude Code System Prompts

Based on public analysis (Piebald-AI, claudecodecamp.com, understandingai.net):

### Architecture

Claude Code splits its system prompt at a **static/dynamic boundary**:

```
┌──────────────────────────────────┐
│ STATIC (globally cached)         │ ← same for all users
│  • Model identity                │
│  • Tool definitions (44+ tools)  │
│  • Behavioral rules              │
│  • Safety guidelines             │
├──────────────────────────────────┤
│ DYNAMIC (per-request)            │ ← varies per session
│  • User context                  │
│  • Session state                 │
│  • Feature flags                 │
│  • system-reminder blocks        │
└──────────────────────────────────┘
```

### Custom Instruction Layers (CLAUDE.md)

Loaded at session start, injected into the dynamic section:

1. **Managed policy** — `/etc/claude-code/CLAUDE.md` (org-wide, mandatory)
2. **User global** — `~/.claude/CLAUDE.md`
3. **Project root** — `./CLAUDE.md`
4. **Local project** — `./CLAUDE.local.md`
5. **Parent directory** — walk up from cwd

### Tool Injection

- 44+ typed tools with safety properties
- Sorted alphabetically for cache stability
- Skills loaded on-demand via `Skill` tool call/response pair
- Tool definitions are in the **static** section (globally cached)

---

## 3. L0/L1 Classification Analysis

### Current Classification (from stratifier.py)

| Layer | Rule | Content Type |
|-------|------|--------------|
| **L0** | First `system` message | Core persona, safety rules |
| **L1** | Subsequent `system` + contextual hints (>500 chars, tools, schema, etc.) | Context, tool definitions, project instructions |
| **L2** | assistant/tool roles, non-final user messages | Conversation history |
| **L3** | Enhancement plugins | CoT, structured output, etc. |
| **L4** | Last user message | Current query |

### What Both Tools Actually Send

**opencode** sends a single `role: "system"` message containing the entire
system prompt (identity + rules + tone + workflow + tool instructions), then
the user messages. The binary constructs:

```
system = [joined system prompt text]       # single system message
messages = [system, ...user messages...]
tools = sorted(tool_definitions)
```

**Claude Code** likely mirrors this — a single system message with the
combined built-in + custom instructions, plus tool definitions in the
`tools` parameter.

### Problem: Everything is L0

Because both tools send **a single `system` message**, the stratifier classifies
the entire prompt as **L0**. This means:

- L0 contains a mix of: stable boilerplate (identity, rules, safety) + variable
  content (CLAUDE.md instructions, project context)
- L1 is empty (no second system message)
- The prefix hash (L0+L1) includes both stable and variable content
- A project-specific CLAUDE.md change breaks the cache for all queries

---

## 4. Content Composition Breakdown

### opencode Prompt Content Types

| Section | Stability | Tokens (est.) | Cache Impact | Layer |
|---------|-----------|---------------|-------------|-------|
| Identity ("You are opencode...") | **Always identical** | ~15 | Harmless | L0 |
| URL safety rule | **Always identical** | ~20 | Harmless | L0 |
| WebFetch docs rule | **Always identical** | ~40 | Harmless | L0 |
| Tone & style section | **Always identical** | ~80 | Harmless | L0 |
| Professional objectivity | **Always identical** | ~60 | Harmless | L0 |
| Task management guidance | **Always identical** | ~80 | Harmless | L0 |
| Tool descriptions | **Varies by tool set** | ~500+ | **Fragments cache** | L0 |
| Project context (AGENTS.md) | **Per-project** | ~30-200 | **Fragments cache** | L0 |
| Skill instructions | **Per-skill** | ~100-500 | **Fragments cache** | L0 |

### Claude Code Prompt Content Types

| Section | Stability | Tokens (est.) | Cache Impact | Layer |
|---------|-----------|---------------|-------------|-------|
| Model identity | **Always identical** | ~10 | Harmless | L0 |
| Built-in rules & safety | **Always identical** | ~500 | Harmless | L0 |
| Tool definitions (44+) | **Static across releases** | ~2000+ | Harmless (sorted) | L0 |
| CLAUDE.md custom instructions | **Per-project** | ~100-1000+ | **Fragments cache** | L1 |
| Skills loaded on-demand | **Per-skill** | ~100-500 | **Injected mid-session** | L2 |

---

## 5. Normalization / Compression Opportunities

### 5.1 Template-Hash Compression (Highest ROI)

The opencode system prompt is identical across all sessions of the same agent
type. Instead of including the full text in the hash, compute a **template
hash** from a canonical copy of the known system prompt.

**How it works:**
1. At startup, detect which agent template is being used (by fingerprinting
   the first N characters of the system message)
2. Store the mapping: `agent_type → template_hash`
3. In `prefix_hash()`, use the template hash instead of the full system text
4. Only the **variable parts** (tool definitions, project context) affect
   the hash

**Benefit:** All sessions of the same agent type share the same L0 hash,
regardless of which project they're in.

### 5.2 Tool Definition Canonicalization (Medium ROI)

Both tools sort tool definitions alphabetically, but the tool set itself varies
between projects (different MCP servers, different skills).

**Approach:**
1. Strip tool definitions from the prefix hash entirely (already done for
   tools_hash — secondary filter handles this)
2. OR: Hash only the tool *names* and *types*, exclude descriptions/schemas

### 5.3 CLAUDE.md / AGENTS.md Stripping (Medium ROI)

Project-specific context files are injected into the system prompt. These
vary per project and are the primary cache fragmenter.

**Approach:**
1. Detect and redact project context sections from the prefix hash
2. Use a boundary marker if the tool provides one (Claude Code's
   static/dynamic split)
3. Heuristic: content below known boilerplate markers is project-specific

### 5.4 Whitespace & Metadata Normalization (Already Done)

`_normalize_content()` in `models.py` handles:
- Whitespace collapsing ✓
- Timestamp/ID redaction ✓

### 5.5 Static System Prompt Library (Highest Impact)

Maintain a registry of known system prompt templates with pre-computed
hashes:

```python
KNOWN_TEMPLATES = {
    "opencode-agent-v2": "a1b2c3d4...",
    "opencode-agent-concise": "e5f6g7h8...",
    "claude-code-default": "i9j0k1l2...",
}
```

On startup, fingerprint the system prompt against known templates. If it
matches, use the pre-computed hash. Only the delta (tools + project context)
goes into the cache key.

---

## 6. Recommendations

### Immediate (Low Effort, High Impact)

1. **Add template fingerprinting** to detect known system prompt templates
   and hash them canonically instead of byte-by-byte. This alone would make
   all opencode sessions share L0 regardless of project.

2. **Add a `template_hash` field** to `CacheEntry` alongside `tool_hash`
   for observability (already partially done — `tool_hash` exists).

### Short-term (Medium Effort)

3. **Build a template registry** with pre-computed hashes for known
   agent types. Load from config or auto-detect.

4. **Add CLAUDE.md / AGENTS.md section detection** — use boundary markers
   or heuristics to identify project-specific context and exclude it from
   the hash.

### Long-term (Higher Effort)

5. **Implement section-level hashing** — hash each logical section of
   the system prompt independently. Cache key = combination of section
   hashes. Sections that change break only their own cache bucket.

6. **Auto-detect template structure** — if no template match, analyze
   the system prompt to find stable vs variable sections statistically.

---

## 7. Appendix: Real-World Data Points

### opencode Session Characteristics (50 entries)

| Metric | Value |
|--------|-------|
| Total queries | 50 |
| Avg query length | 124 chars |
| Shortest query | 1 char |
| Longest query | 626 chars |
| Session lifecycle | Multi-turn, session_id persists across turns |

### OpenCode System Prompt Template (Concise variant, ~300 tokens est.)

```text
You are opencode, an interactive CLI tool that helps users with software
engineering tasks. Use the instructions below and the tools available to
you to assist the user.

IMPORTANT: You must NEVER generate or guess URLs for the user unless you
are confident that the URLs are for helping the user with programming...

When the user directly asks about opencode, first use the WebFetch tool to
gather information to answer the question from opencode docs at
https://opencode.ai

# Tone and style
You should be concise, direct, and to the point...
IMPORTANT: You should minimize output tokens as much as possible...
IMPORTANT: Keep your responses short...

# Professional objectivity
Prioritize technical accuracy and truthfulness over validating the user's
beliefs...

# Task Management
You have access to the TodoWrite tools...
```

### Claude Code System Prompt (from Piebald-AI, ~2500 tokens est.)

```text
You are Claude Code, Anthropic's official CLI for Claude.
You are an interactive CLI tool that helps users with software engineering
tasks...
... (45+ tool definitions, behavioral rules, safety guidelines) ...
```

---

## 8. Next Steps

1. [**P0**] Implement template fingerprinting for known system prompt
   patterns (opencode, Claude Code). ~1 day.
2. [**P1**] Add template fingerprinting to the pipeline — compute hash
   from template + delta instead of full content. ~2 days.
3. [**P2**] Collect real prompts from Claude Code sessions to validate
   the distinction between static and dynamic sections. ~ongoing.
4. [**P2**] Investigate whether tool definitions should be section-hashed
   (names only, not descriptions/schemas). ~1 day.
