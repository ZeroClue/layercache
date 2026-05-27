# LayerCache Development Workflow

**Version:** 1.0  
**Last Updated:** 2026-05-27

This project uses an **orchestrated agent workflow** for feature development. Complex tasks are decomposed into parallelizable units, handled by specialized agents (DAA, Review, Fixer), with verification gates between each phase.

---

## Quick Start

### Start a New Feature

```bash
# Using opencode CLI
opencode task "/orchestrate Implement feature X"

# Or just tell me directly
"Orchestrate the implementation of tool schema serialization"
```

### Check Workflow Status

```bash
opencode task "/workflow status"
```

### Resume After Context Reset

```bash
opencode task "/workflow resume"
```

---

## Workflow Diagram

```
┌─────────────┐
│   Plan      │ User provides spec or requirements
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  DAA Review │ Devil's Advocate stress-tests the plan
└──────┬──────┘
       │ Conditions addressed?
       ▼
┌─────────────┐
│ Implement   │ Fixer Agent + TDD
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ Code Review │ Review Agent verifies implementation
└──────┬──────┘
       │ Blocking issues?
       ▼
┌─────────────┐
│    Fix      │ Fixer Agent addresses issues
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Verify     │ Tests, lint, typecheck
└──────┬──────┘
       │ All pass?
       ▼
┌─────────────┐
│    Merge    │ Ready for integration
└─────────────┘
```

---

## Available Agents

| Agent | Purpose | When to Invoke | Model |
|-------|---------|----------------|-------|
| **DAA** | Pre-implementation review | Before writing code | `deepseek-v4-flash` |
| **Review Agent** | Post-implementation review | After writing code | `deepseek-v4-flash` |
| **Fixer Agent** | Implement fixes | After review finds issues | `deepseek-v4-flash` |
| **Research Agent** | Deep research | Before planning | `deepseek-v4-flash` |

### Agent Definitions

- `.opencode/devils-advocate-agent.md` — DAA
- `.opencode/review-agent.md` — Review Agent
- `.opencode/fixer-agent.md` — Fixer Agent
- `.opencode/skills/orchestration.md` — Orchestration Skill

---

## Commands

### `/orchestrate <task>`

Start orchestration workflow for a complex task.

**Example:**
```
/orchestrate Implement v1.6 Phase 1.3: Tool schema deterministic serialization
```

**What happens:**
1. Task is decomposed into phases
2. DAA reviews the plan (if spec exists)
3. Fixer Agent implements
4. Review Agent verifies
5. Workflow state is persisted

---

### `/workflow status`

Show current workflow state.

**Output:**
```markdown
## Workflow: v1.6-prompt-context-engineering

**Overall Status:** 40% complete (2/5 phases)

| Phase | Status | Tests | Notes |
|-------|--------|-------|-------|
| 1.1 Stable Prefix | ✅ COMPLETE | 176 | All blocking issues fixed |
| 1.2 Anthropic Cache | ✅ COMPLETE | 187 | 90% cost reduction |
| 1.3 Tool Serialization | ⏳ PENDING | - | - |

**Next:** Phase 1.3 (Tool serialization)
```

---

### `/workflow resume`

Resume workflow after context reset.

**What happens:**
1. Loads `.opencode/workflow-state.json`
2. Summarizes completed work
3. Identifies next pending task
4. Asks how to proceed

---

### `/workflow abort`

Abort current workflow (preserves state for later resume).

---

## When to Use Orchestration

| Task Type | Use Orchestration? | Why |
|-----------|-------------------|-----|
| **New feature** (spec + implementation) | ✅ Yes | Requires DAA review |
| **Bug fix** (clear root cause) | ❌ No | Direct to Fixer Agent |
| **Code review** (existing PR) | ❌ No | Direct to Review Agent |
| **Multi-phase refactor** | ✅ Yes | Requires coordination |
| **Research task** | ✅ Yes | Structured workflow |
| **Simple task** (<10 min) | ❌ No | Overhead > value |

### Heuristic

**Use orchestration when:**
- Task involves >3 files or >100 lines of code
- Task requires design decisions before implementation
- Task has multiple phases with verification gates
- Task will take >1 hour total effort

**Skip orchestration when:**
- Task is a simple bug fix
- Task is a single-file change
- Task is well-understood and low-risk

---

## Workflow State

**File:** `.opencode/workflow-state.json`

This file persists workflow progress across sessions. It contains:
- Current phase
- Completed phases (with test counts)
- Active agents
- Pending reviews

**Do not edit manually** — use workflow commands.

---

## Example: v1.6 Implementation

### Current Workflow

```
Workflow: v1.6-prompt-context-engineering
Status: IMPLEMENTING
Progress: 2/5 phases (40%)
```

### Completed Phases

1. **Phase 1.1: Stable Prefix Architecture**
   - Added `stable_prefix_tokens()` method
   - Added prefix threshold validation
   - Added response metadata (`lc_prefix_hash`, `lc_prefix_tokens`)
   - **Tests:** 176 pass
   - **Review:** 3 blocking issues fixed

2. **Phase 1.2: Anthropic Cache Markers**
   - Added `cache_control` injection at L2/L3 boundary
   - Added cache metrics extraction
   - **Tests:** 187 pass
   - **Review:** Approved

### Pending Phases

3. **Phase 1.3: Tool Schema Serialization**
   - Deterministic JSON serialization
   - Cache tool definitions at L1
   - **ETA:** 30-45 minutes

4. **Phase 1.4: OpenAI Cache Metrics**
   - Extract cache metrics from OpenAI responses
   - Dashboard integration

5. **Phase 2.1: Multi-tier Caching**
   - Semantic → Prefix → Inference hierarchy
   - Cache validation mechanism
   - **DAA Review:** Pending

---

## Verification Gates

Before claiming a phase complete, verify:

```bash
# All tests pass
pytest tests/ -x --tb=short

# Lint clean
ruff check layercache/ tests/

# Format clean
ruff format layercache/ tests/

# Typecheck clean
mypy layercache/
```

**Evidence required:**
- Test output (showing pass count)
- Lint output (showing "All checks passed")
- Typecheck output (showing "Success: no issues")

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Workflow stuck | Run `/workflow status`, check agent task |
| State file corrupted | Delete `.opencode/workflow-state.json`, restart |
| Agent not responding | Check model availability, retry |
| Context reset | Run `/workflow resume` |

---

## For New Projects

To use this workflow in a new project:

1. **Copy agent definitions:**
   ```bash
   cp -r .opencode/ my-new-project/.opencode/
   ```

2. **Copy workflow module:**
   ```bash
   cp layercache/workflow.py my-new-project/
   ```

3. **Update agent configs** for project-specific needs

4. **Initialize workflow:**
   ```
   /orchestrate Implement feature X
   ```

See `docs/workflows/orchestration-template.md` for a customizable template.

---

## References

- **Orchestration Skill:** `.opencode/skills/orchestration.md`
- **DAA Agent:** `.opencode/devils-advocate-agent.md`
- **Review Agent:** `.opencode/review-agent.md`
- **Fixer Agent:** `.opencode/fixer-agent.md`
- **Workflow Module:** `layercache/workflow.py`

---

## FAQ

**Q: Can I skip orchestration for simple tasks?**  
A: Yes! Just ask me to do it directly. Orchestration is for complex, multi-phase work.

**Q: What if I close opencode mid-workflow?**  
A: Run `/workflow resume` — state is persisted in `.opencode/workflow-state.json`.

**Q: Can I have multiple workflows?**  
A: One active workflow at a time. Starting a new one will prompt to abort the current.

**Q: How do I know which agent is working?**  
A: Run `/workflow status` — shows active agents.

**Q: Can I customize the workflow?**  
A: Yes! Edit `.opencode/workflow-config.json` for project-specific agents and verification steps.
