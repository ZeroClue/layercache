# LayerCache Review Process

## Overview

LayerCache uses a formal review workflow to ensure quality and alignment:

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Spec      │ →  │   Devil's   │ →  │   Spec      │
│   Draft     │    │  Advocate   │    │   Update    │
└─────────────┘    └─────────────┘    └─────────────┘
                                              ↓
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│    Merge    │ ←  │   Code      │ ←  │   Phase     │
│             │    │   Review    │    │   Impl      │
└─────────────┘    └─────────────┘    └─────────────┘
```

## Using the Review Agent

### Spec Review

**When:** Before any implementation begins

**How:**

```bash
# Invoke review agent
opencode task "Review the spec at docs/specs/v1.5.0-scale-context.md"
```

**What happens:**
1. Agent reads the spec document
2. Checks against review checklist
3. Produces review in `docs/reviews/`
4. Updates spec status header

**Expected output:**
- Review file: `docs/reviews/YYYY-MM-DD-spec-[name].md`
- Spec status updated to "DA Approved" or "DA Approved with Conditions"

### Code Review

**When:** After each implementation phase (P1, P2, P3...)

**How:**

```bash
# Invoke review agent for code
opencode task "Review the P1 implementation (Redis backend + session isolation)"
```

**What happens:**
1. Agent reads changed files
2. Checks against spec requirements
3. Verifies test coverage
4. Produces review in `docs/reviews/`

**Expected output:**
- Review file: `docs/reviews/YYYY-MM-DD-code-[phase].md`
- PR approval or change requests

## Review Archive Structure

```
docs/reviews/
├── README.md              # This file - archive index
├── 2026-05-26-spec-v1.5.0-scale-context.md
├── 2026-05-26-code-p1-redis-session.md
└── ...
```

## Review Templates

### Spec Review Template

```markdown
## Spec Review: [SPEC NAME]

### Verdict
[✅ Approve | ⚠️ Approve with conditions | ❌ Reject]

### Conditions (if applicable)
1. [Specific change required]

### Strengths
- [What the spec does well]

### Concerns
| Section | Issue | Severity |
|---------|-------|----------|
| [Location] | [Issue] | [High/Medium/Low] |

### Recommendations
1. [Concrete suggestion]

### Missing Edge Cases
- [Edge case not addressed]

### Security Notes
- [Security consideration]
```

### Code Review Template

```markdown
## Code Review: [CHANGE DESCRIPTION]

### Verdict
[✅ Approve | ⚠️ Approve with nitpicks | ❌ Request changes]

### Required Changes
| File:Line | Issue | Fix |
|-----------|-------|-----|
| [Location] | [Issue] | [Fix] |

### Nitpicks (optional)
- [Non-blocking suggestion]

### Strengths
- [What the code does well]

### Test Coverage
| Area | Status |
|------|--------|
| Unit tests | [✅/⚠️/❌] |
| Edge cases | [✅/⚠️/❌] |
| Error paths | [✅/⚠️/❌] |

### Security Notes
- [Security observation]
```

## Status Definitions

### Spec Status

| Status | Meaning |
|--------|---------|
| Draft | Initial spec, not yet reviewed |
| DA Review | Under Devil's Advocate review |
| DA Approved | Approved, ready for implementation |
| DA Approved with Conditions | Approved pending specific changes |
| Implementing | Implementation in progress |
| Complete | All phases implemented and reviewed |

### Code Review Status

| Status | Meaning |
|--------|---------|
| ✅ Approve | Ready to merge |
| ⚠️ Approve with nitpicks | Mergeable, minor improvements suggested |
| ❌ Request changes | Must fix before merge |

## Checklist for Authors

### Before Requesting Spec Review

- [ ] Problem statement is clear and measurable
- [ ] Requirements have testable acceptance criteria
- [ ] Implementation plan is realistic
- [ ] Edge cases are considered
- [ ] Security implications addressed
- [ ] Architecture alignment verified

### Before Requesting Code Review

- [ ] All acceptance criteria met
- [ ] Tests written and passing
- [ ] Lint/typecheck pass (`ruff check`, `mypy`)
- [ ] Config schema updated if needed
- [ ] Documentation updated
- [ ] No debug code or comments

## Review Agent Location

The review agent is defined at `.opencode/review-agent.md` and can be invoked via:

```bash
opencode task "Review [spec/code] at [path]"
```

Or by referencing this document and the agent definition.

## Examples

See the `docs/reviews/` directory for example reviews:
- `2026-05-26-spec-v1.5.0-scale-context.md` — Spec review example
- `2026-05-26-code-p1-redis-session.md` — Code review example
