# Orchestration Workflow Template

**Purpose:** Copy this template to new projects to enable orchestrated agent workflows.

---

## Installation

### Step 1: Copy Agent Definitions

```bash
# From LayerCache project
cp -r .opencode/ my-new-project/.opencode/
```

### Step 2: Copy Workflow Module

```bash
cp layercache/workflow.py my-new-project/
```

### Step 3: Update Agent Configs

Edit `.opencode/*.md` files to:
- Update project name references
- Adjust file paths
- Customize verification commands

### Step 4: Create ORCHESTRATION.md

```bash
cp ORCHESTRATION.md my-new-project/
```

Then edit to reflect:
- Project-specific phases
- Project-specific agents
- Custom verification steps

---

## Template Structure

```
.opencode/
├── devils-advocate-agent.md    # DAA (pre-implementation review)
├── review-agent.md             # Review Agent (post-implementation review)
├── fixer-agent.md              # Fixer Agent (implements fixes)
├── skills/
│   └── orchestration.md        # Orchestration skill
└── workflow-state.json         # Auto-generated, do not copy

my-project/
├── workflow.py                 # Workflow state management
└── ORCHESTRATION.md            # User-facing documentation
```

---

## Customization Guide

### Add Project-Specific Agents

Create `.opencode/my-custom-agent.md`:

```markdown
# My Custom Agent

**Model:** `deepseek-v4-flash`
**Purpose:** [What this agent does]

## When to Invoke

- [Trigger condition 1]
- [Trigger condition 2]

## Output Format

[What the agent returns]

## Handoff Protocol

### To Orchestrator

[What to report after completion]

### Escalation Triggers

[When to escalate instead of proceeding]
```

Then update `.opencode/skills/orchestration.md`:

```markdown
## Agent Portfolio

| Agent | Purpose | Model |
|-------|---------|-------|
| My Custom Agent | [Purpose] | deepseek-v4-flash |
```

---

### Add Custom Verification Steps

Edit `ORCHESTRATION.md`, add to "Verification Gates":

```markdown
### Project-Specific Verification

```bash
# Run project-specific checks
npm run test       # For TypeScript projects
cargo test         # For Rust projects
go test ./...      # For Go projects
```

---

### Customize Workflow States

Edit `.opencode/skills/orchestration.md`, modify `WorkflowStatus`:

```python
class WorkflowStatus(StrEnum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    DAA_REVIEW = "DAA_REVIEW"
    IMPLEMENTING = "IMPLEMENTING"
    CODE_REVIEW = "CODE_REVIEW"
    FIXING = "FIXING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    # Add custom states:
    WAITING_ON_USER = "WAITING_ON_USER"
    SECURITY_REVIEW = "SECURITY_REVIEW"
```

---

### Add Workflow Configuration

Create `.opencode/workflow-config.json`:

```json
{
  "project_name": "My Project",
  "agents": {
    "security-auditor": {
      "description": "Security-focused code review",
      "model": "deepseek-v4-flash",
      "trigger": "security-sensitive changes"
    }
  },
  "verification": {
    "test_command": "pytest tests/ -v",
    "lint_command": "ruff check .",
    "format_command": "ruff format .",
    "typecheck_command": "mypy ."
  },
  "phases": {
    "default": ["Planning", "Implementation", "Review", "Fix", "Verify"]
  }
}
```

---

## Example: TypeScript Project

### Modified Agent: Review Agent

Edit `.opencode/review-agent.md`:

```markdown
## Code Review Mode

### TypeScript-Specific Checklist

- [ ] Type safety (no `any` types)
- [ ] Proper use of generics
- [ ] Async/await error handling
- [ ] React hooks rules (if applicable)
- [ ] ESLint compliance

### Verification Commands

```bash
npm run test
npm run lint
npm run typecheck
```

---

## Example: Rust Project

### Modified Agent: Fixer Agent

Edit `.opencode/fixer-agent.md`:

```markdown
## Fix Workflow

### Rust-Specific Patterns

**Ownership Fixes:**
```rust
// Before (borrow checker error)
fn process(data: &Vec<String>) { ... }

// After (proper borrowing)
fn process(data: &[String]) { ... }
```

**Verification Commands:**
```bash
cargo test
cargo clippy
cargo fmt --check
```

---

## Testing the Template

### Smoke Test

1. **Create test project:**
   ```bash
   mkdir test-workflow
   cd test-workflow
   # Copy template files
   ```

2. **Start workflow:**
   ```
   /orchestrate Add a new feature
   ```

3. **Verify:**
   - Workflow state file created
   - Agents spawn correctly
   - Handoffs work
   - State persists across restarts

4. **Clean up:**
   ```bash
   cd ..
   rm -rf test-workflow
   ```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Agents not spawning | Check model availability, verify `.opencode/*.md` syntax |
| State not persisting | Check file permissions on `.opencode/workflow-state.json` |
| Handoffs failing | Verify handoff protocol in agent definitions |
| Verification failing | Update commands in `workflow-config.json` |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-05-27 | Initial template from LayerCache |

---

## References

- **LayerCache Original:** `/home/arminm/projects/layercache/.opencode/`
- **Orchestration Skill:** `.opencode/skills/orchestration.md`
- **Workflow Module:** `workflow.py`
