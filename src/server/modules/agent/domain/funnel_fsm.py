"""Funnel state machine: validated lead-stage transitions.

The LLM proposes a transition; this module accepts it or rejects it. Pure
domain logic — no DB, no LLM, fully testable in isolation.
See docs/DESIGN_AGENT_ARCHITECTURE.md §9.
"""

from __future__ import annotations

import enum


class FunnelStage(enum.StrEnum):
    """Lead progression stage, persisted on the conversation."""

    NEW = "new"
    ENGAGING = "engaging"
    QUALIFYING = "qualifying"
    QUALIFIED = "qualified"
    HANDED_OFF = "handed_off"
    DISQUALIFIED = "disqualified"


# Valid transitions. Any active stage may drop to `disqualified` or hand off to a
# human: handoff reasons explicit_request/agent_error can fire at any turn,
# payment_validation from `qualified` (§9 + FLUJO §1). `handed_off` and
# `disqualified` are terminal **for the LLM**: it can never propose a way out of
# them. Re-opening is a code-driven action only — see `REOPEN_TRANSITIONS`/`reopen`.
ALLOWED_TRANSITIONS: dict[FunnelStage, set[FunnelStage]] = {
    FunnelStage.NEW: {
        FunnelStage.ENGAGING,
        FunnelStage.HANDED_OFF,
        FunnelStage.DISQUALIFIED,
    },
    FunnelStage.ENGAGING: {
        FunnelStage.QUALIFYING,
        FunnelStage.HANDED_OFF,
        FunnelStage.DISQUALIFIED,
    },
    FunnelStage.QUALIFYING: {
        FunnelStage.QUALIFIED,
        FunnelStage.HANDED_OFF,
        FunnelStage.DISQUALIFIED,
    },
    FunnelStage.QUALIFIED: {FunnelStage.HANDED_OFF, FunnelStage.DISQUALIFIED},
    FunnelStage.HANDED_OFF: set(),
    FunnelStage.DISQUALIFIED: set(),
}


# Re-opening a terminal lead is a **code-driven** action (staff toggle / inbound on a
# closed lead), never an LLM proposal — kept out of ALLOWED_TRANSITIONS so the model
# can never escape a terminal stage on its own (FLUJO §1, SPEC_UAT_remediation R2).
# `fresh` (closed lead writes again) → NEW (start over); otherwise (manual toggle
# reactivation) → ENGAGING (continue with context, do not greet).
REOPEN_TRANSITIONS: dict[FunnelStage, set[FunnelStage]] = {
    FunnelStage.HANDED_OFF: {FunnelStage.NEW, FunnelStage.ENGAGING},
    FunnelStage.DISQUALIFIED: {FunnelStage.NEW, FunnelStage.ENGAGING},
}


class InvalidTransitionError(ValueError):
    """Raised when a proposed funnel transition is not allowed."""

    def __init__(self, current: FunnelStage, target: FunnelStage) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid funnel transition: {current} -> {target}")


def validate_transition(current: FunnelStage, target: FunnelStage) -> FunnelStage:
    """Return `target` if the transition is allowed, else raise.

    A no-op (`current == target`) is idempotent and always allowed: re-proposing
    the current stage must not fail.
    """
    if current == target:
        return target
    if target in ALLOWED_TRANSITIONS[current]:
        return target
    raise InvalidTransitionError(current, target)


def is_terminal(stage: FunnelStage) -> bool:
    """True if no LLM-proposable transitions leave this stage (re-opening aside)."""
    return not ALLOWED_TRANSITIONS[stage]


def reopen(current: FunnelStage, *, fresh: bool) -> FunnelStage:
    """Re-open a terminal lead by code decision. `fresh=True` (closed lead writes
    again) → NEW; `fresh=False` (manual toggle reactivation) → ENGAGING so the agent
    continues with context instead of greeting. Returns `current` unchanged if the
    stage is not re-openable (e.g. already active)."""
    target = FunnelStage.NEW if fresh else FunnelStage.ENGAGING
    if target in REOPEN_TRANSITIONS.get(current, set()):
        return target
    return current
