# Orchestration Skill

**Trigger:** `/orchestrate`, `/workflow status`, `/workflow resume`, or natural language like "orchestrate this", "use the workflow", "resume the workflow"

**Purpose:** Coordinate multi-agent workflows for complex development tasks. Decomposes work into parallelizable units, spawns specialized agents (DAA, Review, Fixer), manages handoffs, and maintains workflow state across sessions.

---

## Command Parsing

The skill recognizes these command patterns:

| Pattern | Action |
|---------|--------|
| `/orchestrate <task>` | Start new workflow |
| "orchestrate this" | Start workflow for current context |
| "use the workflow" | Start workflow for current context |
| `/workflow status` | Show current workflow state |
| "what's the workflow status" | Show current workflow state |
| `/workflow resume` | Resume after context reset |
| "resume the workflow" | Resume after context reset |
| "/workflow abort" | Abort current workflow |

### Auto-Detect Heuristic

**Automatically use orchestration when:**
- User mentions "implement" + "feature" or "phase"
- Task involves >3 files (detected from file paths in prompt)
- User references a spec document (`docs/specs/*.md`, `docs/plans/*.md`)
- Task description mentions "DAA", "review", or "verification"

**Skip orchestration when:**
- User says "just do it" or "skip the workflow"
- Task is clearly a single-file change
- Task is labeled "bug fix" with clear root cause

---

## When to Use

| Task Type | Use Orchestration? | Why |
|-----------|-------------------|-----|
| **New feature** (spec + implementation) | ✅ Yes | Requires DAA review before implementation |
| **Bug fix** (clear root cause) | ❌ No | Direct to Fixer Agent |
| **Code review** (existing PR) | ❌ No | Direct to Review Agent |
| **Multi-phase refactor** | ✅ Yes | Requires coordination, verification gates |
| **Research task** | ✅ Yes | Benefits from structured workflow |
| **Simple task** (<10 min) | ❌ No | Overhead exceeds value |

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
- User explicitly says "just do it"

---

## Workflow States

```
┌─────────────┐
│   IDLE      │ ← Initial state
└──────┬──────┘
       │ /orchestrate <task>
       ▼
┌─────────────┐
│  PLANNING   │ ← Decomposing task, identifying agents
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  DAA REVIEW │ ← Pre-implementation stress test
└──────┬──────┘
       │ Conditions addressed?
       ▼
┌─────────────┐
│IMPLEMENTING │ ← Fixer Agent + TDD
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ CODE REVIEW │ ← Review Agent verification
└──────┬──────┘
       │ Blocking issues?
       ▼
┌─────────────┐
│   FIXING    │ ← Fixer Agent addresses issues
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  VERIFYING  │ ← Tests, lint, typecheck
└──────┬──────┘
       │ All pass?
       ▼
┌─────────────┐
│   COMPLETE  │ ← Ready for merge
└─────────────┘
```

---

## Commands

### `/orchestrate <task>`

Start orchestration workflow for a task.

**Example:**
```
/orchestrate Implement v1.6 Phase 1.3: Tool schema deterministic serialization
```

**What happens:**
1. Parse task description
2. Identify required agents (DAA, Review, Fixer)
3. Create workflow state file
4. Start with DAA review (if spec exists) or planning (if no spec)
5. Report back with workflow plan

---

### `/workflow status`

Show current workflow state.

**Output:**
```markdown
## Workflow Status

**Workflow:** v1.6-prompt-context-engineering
**Phase:** Phase 1.2 (Anthropic cache markers)
**Status:** COMPLETE ✅

**Completed:**
- Phase 1.1: Stable Prefix Architecture (176 tests pass)
- Phase 1.2: Anthropic cache markers (187 tests pass)

**Pending:**
- Phase 1.3: Tool schema deterministic serialization
- Phase 2.1: Multi-tier caching hierarchy

**Next Step:** Continue with Phase 1.3?
```

---

### `/workflow resume`

Resume workflow after context reset.

**Trigger patterns:**
- `/workflow resume`
- "resume the workflow"
- "continue where we left off"
- "what was I working on?"

**Implementation:**
```python
from layercache.workflow import handle_workflow_resume

def handle_resume_command():
    """Handle /workflow resume command."""
    return handle_workflow_resume()
```

**What happens:**
1. Loads workflow state from `.opencode/workflow-state.json`
2. Calculates completion percentage
3. Finds last active or next pending phase
4. Returns formatted summary with next steps

**Example output:**
```markdown
## Workflow Resumed: v1.6-prompt-context-engineering

**Overall Status:** 60% complete (3/5 phases)

**Current Status:** IMPLEMENTING

**Last Activity:** Phase 1.3: Tool Schema Serialization
- Tests passing: 198
- Notes: Review approved, minor issues fixed

Continue with this phase, or switch tasks?
```

---

### `/workflow abort`

Abort current workflow.

**What happens:**
1. Save current state (for potential resume)
2. Clean up agent tasks
3. Return to IDLE state

---

## Auto-Agent Spawning

After each agent completes, the skill should automatically spawn the next agent based on workflow state.

### Auto-Spawn Logic

```python
from layercache.workflow import auto_spawn_next_agent, on_agent_complete

# After agent completes
on_agent_complete(
    workflow_id="v1.6-prompt-context-engineering",
    agent_name="Fixer Agent",
    phase_name="Phase 1.4: OpenAI Cache Metrics",
    tests_passing=202,
    notes="Implementation complete, ready for review"
)

# Auto-spawn next agent
next_agent = auto_spawn_next_agent()
if next_agent:
    # Spawn next_agent automatically
    pass  # Orchestrator spawns {next_agent}
```

### State Transitions

| Current Status | Condition | Next Status | Auto-Spawned Agent |
|----------------|-----------|-------------|-------------------|
| DAA_REVIEW | "PROCEED" in notes | IMPLEMENTING | Fixer Agent |
| IMPLEMENTING | Always | CODE_REVIEW | Review Agent |
| CODE_REVIEW | "APPROVE" in notes | (Orchestrator handles) | None |
| CODE_REVIEW | No "APPROVE" | FIXING | Fixer Agent |
| FIXING | Always | CODE_REVIEW | Review Agent |

### Agent Completion Protocol

After completing a task, every agent should:

1. Call `on_agent_complete()` with results
2. This auto-saves workflow state
3. Orchestrator calls `auto_spawn_next_agent()`
4. Next agent is spawned automatically (if applicable)

**Example agent handoff:**
```python
from layercache.workflow import on_agent_complete

# At end of agent task
on_agent_complete(
    workflow_id="v1.6-prompt-context-engineering",
    agent_name="Fixer Agent",
    phase_name="Phase 1.4: OpenAI Cache Metrics",
    tests_passing=202,
    notes="Implementation complete, ready for review"
)
```

---

## Agent Portfolio

| Agent | Purpose | Model | Invocation |
|-------|---------|-------|------------|
| **DAA** | Pre-implementation review | `deepseek-v4-flash` | `task "DAA Review: [plan]"` |
| **Review Agent** | Post-implementation review | `deepseek-v4-flash` | `task "Review: [files]"` |
| **Fixer Agent** | Implement fixes | `deepseek-v4-flash` | `task "Fix: [issues]"` |
| **Research Agent** | Deep research | `deepseek-v4-flash` | `task "Research: [topic]"` |

---

## Handoff Protocols

### DAA → Orchestrator

After DAA review, agent provides:
```markdown
## DAA Summary

**Recommendation:** [PROCEED | PROCEED WITH CONDITIONS | REVISE & RESUBMIT | BLOCK]

**Critical Conditions:** [N conditions]
1. [Condition 1]
2. [Condition 2]

**Next Step:** [Recommended action]
```

**Orchestrator Action:**
- If PROCEED → Spawn Fixer Agent for implementation
- If CONDITIONS → Update plan, then spawn Fixer Agent
- If REVISE → Return to user for plan revision
- If BLOCK → Escalate to user

---

### Fixer → Orchestrator

After fix implementation, agent provides:
```markdown
## Fix Summary

**Issues Fixed:** [N issues]
1. [Issue 1 — file:line]

**Tests Added:** [N tests]
- [test_file]::[test_name]

**Verification:** [PASS/FAIL]

**Ready for:** Review Agent
```

**Orchestrator Action:**
- Spawn Review Agent to verify fixes
- Update workflow state

---

### Review → Orchestrator

After code review, agent provides:
```markdown
## Review Summary

**Verdict:** [APPROVE | APPROVE WITH MINOR FIXES | REQUEST CHANGES]

**Blocking Issues:** [N issues]
1. [Issue 1]

**Next Step:** [Recommended action]
```

**Orchestrator Action:**
- If APPROVE → Mark phase complete, move to next phase
- If FIXES → Spawn Fixer Agent
- If CHANGES → Escalate to user

---

## Workflow State Schema

```json
{
  "workflow_id": "string (kebab-case identifier)",
  "title": "string (human-readable title)",
  "status": "IDLE | PLANNING | DAA_REVIEW | IMPLEMENTING | CODE_REVIEW | FIXING | VERIFYING | COMPLETE",
  "created_at": "ISO 8601 timestamp",
  "updated_at": "ISO 8601 timestamp",
  "phases": [
    {
      "name": "string",
      "status": "PENDING | IN_PROGRESS | COMPLETE | BLOCKED",
      "completed_at": "ISO 8601 timestamp or null",
      "tests_passing": "number",
      "notes": "string"
    }
  ],
  "active_agents": ["agent names currently working"],
  "pending_reviews": ["review document paths"],
  "notes": "string (freeform notes)"
}
```

---

## State Persistence

**File:** `.opencode/workflow-state.json`

**On Workflow Start:**
```python
state = {
    "workflow_id": generate_id(task),
    "title": task,
    "status": "PLANNING",
    "created_at": now(),
    "updated_at": now(),
    "phases": [],
    "active_agents": [],
    "notes": ""
}
save_state(state)
```

**On Phase Complete:**
```python
state = load_state()
state["phases"].append({
    "name": phase_name,
    "status": "COMPLETE",
    "completed_at": now(),
    "tests_passing": test_count,
    "notes": ""
})
state["status"] = next_phase_status()
state["updated_at"] = now()
save_state(state)
```

**On Resume:**
```python
state = load_state()
if state:
    print(f"Workflow detected: {state['title']}")
    print(f"Status: {state['status']}")
    print(f"Completed phases: {count_complete(state['phases'])}")
    print(f"Next: {get_next_pending(state['phases'])}")
```

---

## Error Handling

### Agent Fails Mid-Task

**Detection:** Task returns error or times out

**Recovery:**
1. Log failure in workflow state
2. Retry once with same agent
3. If retry fails, escalate to user
4. Preserve state for resume

### Context Reset

**Detection:** No conversation history, but workflow state exists

**Recovery:**
1. Auto-load workflow state
2. Summarize last known state
3. Ask user: "Resume workflow or start fresh?"

### Conflicting Workflows

**Detection:** User starts `/orchestrate` while another workflow is active

**Recovery:**
1. Show current workflow status
2. Ask: "Abort current workflow and start new one, or continue existing?"

---

## Examples

### Example 1: New Feature

```
User: /orchestrate Implement v1.6 Phase 1.3: Tool schema deterministic serialization

Orchestrator: 
  Workflow started: v1.6-phase-1.3-tool-serialization
  Status: PLANNING
  
  Step 1: Checking for spec...
  Found: docs/plans/v1.6-prompt-context-engineering.md
  
  Step 2: Spawning DAA for plan review...
  [DAA review in progress]
  
  Estimated time: 15-20 minutes
```

### Example 2: Resume After Reset

```
User: /workflow resume

Orchestrator:
  Workflow detected: v1.6-prompt-context-engineering
  Status: IMPLEMENTING
  Current Phase: Phase 1.2 (Anthropic cache markers)
  
  Completed:
  - Phase 1.1: Stable Prefix Architecture (176 tests pass) ✅
  
  In Progress:
  - Phase 1.2: Anthropic cache markers (agent working)
  
  Pending:
  - Phase 1.3: Tool schema deterministic serialization
  
  Last activity: Fixer Agent implementing cache markers
  
  Continue waiting for Fixer Agent, or switch to Phase 1.3?
```

### Example 3: Status Check

```
User: /workflow status

Orchestrator:
  ## Workflow: v1.6-prompt-context-engineering
  
  **Overall Status:** 40% complete (2/5 phases)
  
  | Phase | Status | Tests | Notes |
  |-------|--------|-------|-------|
  | 1.1 Stable Prefix | ✅ COMPLETE | 176 | All blocking issues fixed |
  | 1.2 Anthropic Cache | ✅ COMPLETE | 187 | 90% cost reduction on cached tokens |
  | 1.3 Tool Serialization | ⏳ PENDING | - | - |
  | 1.4 OpenAI Metrics | ⏳ PENDING | - | - |
  | 2.1 Multi-tier Cache | ⏳ PENDING | - | DAA review pending |
  
  **Next:** Phase 1.3 (Tool serialization)
  **ETA:** 30-45 minutes
```

---

## Integration with Skills

This skill coordinates with:
- **`test-driven-development`**: Fixer Agent uses TDD loop
- **`verification-before-completion`**: Final verification gate
- **`requesting-code-review`**: Review Agent workflow
- **`using-superpowers`**: Skill discovery on workflow start

---

## Customization

### Project-Specific Agents

Add to `.opencode/workflow-config.json`:
```json
{
  "agents": {
    "security-auditor": {
      "description": "Security-focused code review",
      "model": "deepseek-v4-flash",
      "trigger": "security-sensitive changes"
    }
  }
}
```

### Custom Verification Steps

Add to workflow config:
```json
{
  "verification": {
    "load_test": "pytest tests/load_test.py -v",
    "benchmark": "python benchmarks/run.py",
    "docs_check": "scripts/validate-docs.py"
  }
}
```

---

## Anti-Patterns

❌ **Don't:**
- Use orchestration for simple tasks (<10 min)
- Spawn agents without clear handoff criteria
- Skip verification gates
- Ignore workflow state (always check before starting)

✅ **Do:**
- Use orchestration for multi-phase work
- Document decisions in workflow state
- Provide clear summaries after each agent handoff
- Verify before claiming phase complete

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Workflow stuck in one state | Check agent task status, retry or escalate |
| State file corrupted | Delete `.opencode/workflow-state.json`, restart |
| Agent not responding | Check model availability, fallback to alternative |
| Too many parallel agents | Reduce concurrency, batch related tasks |
