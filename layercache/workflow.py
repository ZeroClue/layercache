"""Workflow state management for orchestrated agent workflows.

This module provides persistence and state management for multi-agent
workflows, enabling resume after context resets and progress tracking.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class WorkflowStatus(StrEnum):
    """Overall workflow status."""

    IDLE = "IDLE"
    PLANNING = "PLANNING"
    DAA_REVIEW = "DAA_REVIEW"
    IMPLEMENTING = "IMPLEMENTING"
    CODE_REVIEW = "CODE_REVIEW"
    FIXING = "FIXING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"


class PhaseStatus(StrEnum):
    """Individual phase status."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


class Phase(BaseModel):
    """Single workflow phase."""

    name: str = Field(description="Human-readable phase name")
    status: PhaseStatus = Field(default=PhaseStatus.PENDING)
    completed_at: str | None = Field(default=None, description="ISO 8601 timestamp")
    tests_passing: int = Field(default=0, description="Number of passing tests")
    notes: str = Field(default="", description="Freeform notes")


class WorkflowState(BaseModel):
    """Complete workflow state."""

    workflow_id: str = Field(description="Kebab-case identifier")
    title: str = Field(description="Human-readable title")
    status: WorkflowStatus = Field(default=WorkflowStatus.IDLE)
    created_at: str = Field(description="ISO 8601 timestamp")
    updated_at: str = Field(description="ISO 8601 timestamp")
    phases: list[Phase] = Field(default_factory=list)
    active_agents: list[str] = Field(default_factory=list)
    pending_reviews: list[str] = Field(default_factory=list)
    notes: str = Field(default="")

    class Config:
        use_enum_values = True


class WorkflowManager:
    """Manages workflow state persistence and operations."""

    def __init__(self, state_path: str | Path = ".opencode/workflow-state.json") -> None:
        """Initialize workflow manager.

        Args:
            state_path: Path to workflow state JSON file.
        """
        self.state_path = Path(state_path)
        self._state: WorkflowState | None = None

    def create_workflow(
        self,
        workflow_id: str,
        title: str,
        phases: list[str] | None = None,
    ) -> WorkflowState:
        """Create a new workflow.

        Args:
            workflow_id: Kebab-case identifier (e.g., "v1.6-phase-1.3").
            title: Human-readable title.
            phases: Optional list of phase names.

        Returns:
            Created workflow state.
        """
        now = datetime.utcnow().isoformat() + "Z"

        phase_objects = (
            [Phase(name=name, status=PhaseStatus.PENDING) for name in phases] if phases else []
        )

        self._state = WorkflowState(
            workflow_id=workflow_id,
            title=title,
            status=WorkflowStatus.PLANNING,
            created_at=now,
            updated_at=now,
            phases=phase_objects,
            active_agents=[],
            pending_reviews=[],
        )

        self.save()
        return self._state

    def load(self) -> WorkflowState | None:
        """Load workflow state from disk.

        Returns:
            Workflow state if exists, None otherwise.
        """
        if not self.state_path.exists():
            return None

        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            self._state = WorkflowState(**data)
            return self._state
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Corrupted state file
            print(f"Warning: Workflow state corrupted: {e}")
            return None

    def save(self) -> None:
        """Save current workflow state to disk."""
        if self._state is None:
            return

        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self._state.model_dump(), f, indent=2)

    def update_status(self, status: WorkflowStatus) -> None:
        """Update workflow status.

        Args:
            status: New workflow status.
        """
        if self._state is None:
            raise RuntimeError("No workflow loaded")

        self._state.status = status
        self._state.updated_at = datetime.utcnow().isoformat() + "Z"
        self.save()

    def complete_phase(self, phase_name: str, tests_passing: int = 0, notes: str = "") -> None:
        """Mark a phase as complete.

        Args:
            phase_name: Name of the phase to complete.
            tests_passing: Number of passing tests.
            notes: Optional notes.
        """
        if self._state is None:
            raise RuntimeError("No workflow loaded")

        for phase in self._state.phases:
            if phase.name == phase_name:
                phase.status = PhaseStatus.COMPLETE
                phase.completed_at = datetime.utcnow().isoformat() + "Z"
                phase.tests_passing = tests_passing
                phase.notes = notes
                break

        self._state.updated_at = datetime.utcnow().isoformat() + "Z"
        self.save()

    def start_phase(self, phase_name: str) -> None:
        """Mark a phase as in progress.

        Args:
            phase_name: Name of the phase to start.
        """
        if self._state is None:
            raise RuntimeError("No workflow loaded")

        for phase in self._state.phases:
            if phase.name == phase_name:
                phase.status = PhaseStatus.IN_PROGRESS
                break

        self._state.updated_at = datetime.utcnow().isoformat() + "Z"
        self.save()

    def add_active_agent(self, agent_name: str) -> None:
        """Add an agent to the active agents list.

        Args:
            agent_name: Name of the agent.
        """
        if self._state is None:
            raise RuntimeError("No workflow loaded")

        if agent_name not in self._state.active_agents:
            self._state.active_agents.append(agent_name)
            self._state.updated_at = datetime.utcnow().isoformat() + "Z"
            self.save()

    def remove_active_agent(self, agent_name: str) -> None:
        """Remove an agent from the active agents list.

        Args:
            agent_name: Name of the agent.
        """
        if self._state is None:
            raise RuntimeError("No workflow loaded")

        if agent_name in self._state.active_agents:
            self._state.active_agents.remove(agent_name)
            self._state.updated_at = datetime.utcnow().isoformat() + "Z"
            self.save()

    def add_pending_review(self, review_path: str) -> None:
        """Add a pending review document.

        Args:
            review_path: Path to review document.
        """
        if self._state is None:
            raise RuntimeError("No workflow loaded")

        if review_path not in self._state.pending_reviews:
            self._state.pending_reviews.append(review_path)
            self._state.updated_at = datetime.utcnow().isoformat() + "Z"
            self.save()

    def get_summary(self) -> str:
        """Get human-readable workflow summary.

        Returns:
            Markdown-formatted summary.
        """
        if self._state is None:
            return "No active workflow"

        completed = sum(1 for p in self._state.phases if p.status == PhaseStatus.COMPLETE)
        total = len(self._state.phases)
        percent = (completed / total * 100) if total > 0 else 0

        lines = [
            f"## Workflow: {self._state.title}",
            "",
            f"**Overall Status:** {percent:.0f}% complete ({completed}/{total} phases)",
            "",
            "| Phase | Status | Tests | Notes |",
            "|-------|--------|-------|-------|",
        ]

        for phase in self._state.phases:
            status_icon = {
                PhaseStatus.COMPLETE: "✅",
                PhaseStatus.IN_PROGRESS: "⏳",
                PhaseStatus.PENDING: "⏳",
                PhaseStatus.BLOCKED: "🚫",
            }.get(phase.status, "⏳")

            test_str = str(phase.tests_passing) if phase.tests_passing > 0 else "-"
            lines.append(
                f"| {phase.name} | {status_icon} {phase.status.value} |"
                f" {test_str} | {phase.notes} |"
            )

        # Find next pending phase
        next_phase = next((p for p in self._state.phases if p.status == PhaseStatus.PENDING), None)
        if next_phase:
            lines.append("")
            lines.append(f"**Next:** {next_phase.name}")

        if self._state.active_agents:
            lines.append("")
            lines.append(f"**Active Agents:** {', '.join(self._state.active_agents)}")

        return "\n".join(lines)

    def reset(self) -> None:
        """Reset workflow state to IDLE."""
        if self._state is None:
            return

        self._state.status = WorkflowStatus.IDLE
        self._state.active_agents = []
        self._state.updated_at = datetime.utcnow().isoformat() + "Z"
        self.save()

    def delete(self) -> None:
        """Delete workflow state file."""
        if self.state_path.exists():
            self.state_path.unlink()
        self._state = None


# Convenience functions for skill usage


def get_workflow_manager() -> WorkflowManager:
    """Get workflow manager instance."""
    return WorkflowManager()


def load_or_create_workflow(workflow_id: str, title: str, phases: list[str]) -> WorkflowState:
    """Load existing workflow or create new one.

    Args:
        workflow_id: Kebab-case identifier.
        title: Human-readable title.
        phases: List of phase names.

    Returns:
        Workflow state (loaded or created).
    """
    manager = WorkflowManager()
    state = manager.load()

    if state and state.workflow_id == workflow_id:
        return state

    return manager.create_workflow(workflow_id, title, phases)


def on_agent_complete(
    workflow_id: str,
    agent_name: str,
    phase_name: str | None = None,
    tests_passing: int = 0,
    notes: str = "",
) -> None:
    """Called by agents when they complete their task.

    Automatically saves workflow state and removes agent from active list.

    Args:
        workflow_id: ID of the workflow (e.g., "v1.6-prompt-context-engineering").
        agent_name: Name of the completing agent (e.g., "Fixer Agent").
        phase_name: Optional phase name to update.
        tests_passing: Number of passing tests.
        notes: Completion notes (e.g., "Ready for review", "APPROVE WITH FIXES").
    """
    manager = WorkflowManager()
    state = manager.load()

    if not state or state.workflow_id != workflow_id:
        return

    # Remove agent from active list
    manager.remove_active_agent(agent_name)

    # Update workflow-level notes (for auto-spawn logic)
    state.notes = notes

    # Update phase if specified
    if phase_name:
        for phase in state.phases:
            if phase.name == phase_name:
                phase.notes = notes
                if tests_passing > 0:
                    phase.tests_passing = tests_passing
                break

    manager.save()


def auto_spawn_next_agent() -> str | None:
    """Automatically spawn the next agent based on workflow state.

    Returns:
        Name of next agent to spawn, or None if orchestrator should handle transition.
    """
    manager = WorkflowManager()
    state = manager.load()

    if not state:
        return None

    if state.status == "DAA_REVIEW":
        if "PROCEED" in state.notes:
            manager.update_status(WorkflowStatus.IMPLEMENTING)
            manager.add_active_agent("Fixer Agent")
            return "Fixer Agent"

    elif state.status == "IMPLEMENTING":
        manager.update_status(WorkflowStatus.CODE_REVIEW)
        manager.add_active_agent("Review Agent")
        return "Review Agent"

    elif state.status == "CODE_REVIEW":
        if "APPROVE" in state.notes:
            return None
        else:
            manager.update_status(WorkflowStatus.FIXING)
            manager.add_active_agent("Fixer Agent")
            return "Fixer Agent"

    elif state.status == "FIXING":
        manager.update_status(WorkflowStatus.CODE_REVIEW)
        manager.add_active_agent("Review Agent")
        return "Review Agent"

    return None


def handle_workflow_resume() -> str:
    """Handle /workflow resume command.

    Returns:
        Markdown-formatted summary for resume command.
    """
    manager = WorkflowManager()
    state = manager.load()

    if not state:
        return "No workflow state found. Start with /orchestrate <task>"

    completed = sum(1 for p in state.phases if p.status == "COMPLETE")
    total = len(state.phases)
    percent = (completed / total * 100) if total > 0 else 0

    summary = f"""## Workflow Resumed: {state.title}

**Overall Status:** {percent:.0f}% complete ({completed}/{total} phases)

**Current Status:** {state.status}
"""

    in_progress = [p for p in state.phases if p.status == "IN_PROGRESS"]
    pending = [p for p in state.phases if p.status == "PENDING"]

    if in_progress:
        summary += f"""
**Last Activity:** {in_progress[0].name}
- Tests passing: {in_progress[0].tests_passing}
- Notes: {in_progress[0].notes}

Continue with this phase, or switch tasks?"""
    elif pending:
        summary += f"""
**Next Phase:** {pending[0].name}

Start this phase?"""
    else:
        summary += """
✅ All phases complete!

Start a new workflow with /orchestrate <task>?"""

    return summary
