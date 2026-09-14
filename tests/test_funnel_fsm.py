"""Tests del funnel FSM (puro, sin DB ni LLM). Ver DESIGN_AGENT_ARCHITECTURE §9."""

from __future__ import annotations

import pytest

from server.modules.agent.domain.funnel_fsm import (
    ALLOWED_TRANSITIONS,
    FunnelStage,
    InvalidTransitionError,
    is_terminal,
    reopen,
    validate_transition,
)

ACTIVE_STAGES: list[FunnelStage] = [
    FunnelStage.NEW,
    FunnelStage.ENGAGING,
    FunnelStage.QUALIFYING,
    FunnelStage.QUALIFIED,
]

TERMINAL_STAGES: list[FunnelStage] = [FunnelStage.HANDED_OFF, FunnelStage.DISQUALIFIED]

# Every transition the graph allows (§9).
VALID_TRANSITIONS: list[tuple[FunnelStage, FunnelStage]] = [
    # forward progression
    (FunnelStage.NEW, FunnelStage.ENGAGING),
    (FunnelStage.ENGAGING, FunnelStage.QUALIFYING),
    (FunnelStage.QUALIFYING, FunnelStage.QUALIFIED),
    # disqualify from any active stage
    (FunnelStage.NEW, FunnelStage.DISQUALIFIED),
    (FunnelStage.ENGAGING, FunnelStage.DISQUALIFIED),
    (FunnelStage.QUALIFYING, FunnelStage.DISQUALIFIED),
    (FunnelStage.QUALIFIED, FunnelStage.DISQUALIFIED),
    # handoff from any active stage (explicit_request / agent_error fire anytime)
    (FunnelStage.NEW, FunnelStage.HANDED_OFF),
    (FunnelStage.ENGAGING, FunnelStage.HANDED_OFF),
    (FunnelStage.QUALIFYING, FunnelStage.HANDED_OFF),
    (FunnelStage.QUALIFIED, FunnelStage.HANDED_OFF),
]

# Cross product minus valid pairs minus self-loops = everything that must raise.
INVALID_TRANSITIONS: list[tuple[FunnelStage, FunnelStage]] = [
    (current, target)
    for current in FunnelStage
    for target in FunnelStage
    if current != target and (current, target) not in VALID_TRANSITIONS
]


@pytest.mark.parametrize(("current", "target"), VALID_TRANSITIONS)
def test_valid_transitions_return_target(current: FunnelStage, target: FunnelStage) -> None:
    assert validate_transition(current, target) is target


@pytest.mark.parametrize(("current", "target"), INVALID_TRANSITIONS)
def test_invalid_transitions_raise(current: FunnelStage, target: FunnelStage) -> None:
    with pytest.raises(InvalidTransitionError):
        validate_transition(current, target)


@pytest.mark.parametrize("stage", list(FunnelStage))
def test_self_transition_is_idempotent(stage: FunnelStage) -> None:
    """Re-proposing the current stage is a no-op, never an error."""
    assert validate_transition(stage, stage) is stage


@pytest.mark.parametrize("stage", TERMINAL_STAGES)
def test_terminal_stages_have_no_exits(stage: FunnelStage) -> None:
    assert ALLOWED_TRANSITIONS[stage] == set()
    assert is_terminal(stage)


@pytest.mark.parametrize("stage", ACTIVE_STAGES)
def test_active_stages_are_not_terminal(stage: FunnelStage) -> None:
    assert not is_terminal(stage)
    assert ALLOWED_TRANSITIONS[stage]


def test_handoff_reachable_from_every_active_stage() -> None:
    """§9: handed_off reachable from any active stage — explicit_request / agent_error
    fire anytime, payment_validation from qualified."""
    sources = {
        current
        for current, targets in ALLOWED_TRANSITIONS.items()
        if FunnelStage.HANDED_OFF in targets
    }
    assert sources == set(ACTIVE_STAGES)


def test_exception_carries_context() -> None:
    """A transition out of a terminal stage is invalid and reports its endpoints."""
    with pytest.raises(InvalidTransitionError) as exc_info:
        validate_transition(FunnelStage.HANDED_OFF, FunnelStage.ENGAGING)
    assert exc_info.value.current is FunnelStage.HANDED_OFF
    assert exc_info.value.target is FunnelStage.ENGAGING


def test_every_stage_has_a_transition_entry() -> None:
    """No stage may be missing from the graph (else validate_transition KeyErrors)."""
    assert set(ALLOWED_TRANSITIONS) == set(FunnelStage)


# --- Re-apertura (code-driven, never LLM-proposable) — SPEC_UAT_remediation R2 ---


@pytest.mark.parametrize("stage", TERMINAL_STAGES)
def test_reopen_fresh_resets_to_new(stage: FunnelStage) -> None:
    """A closed lead who writes again restarts in NEW (decisión de negocio #1)."""
    assert reopen(stage, fresh=True) is FunnelStage.NEW


@pytest.mark.parametrize("stage", TERMINAL_STAGES)
def test_reopen_continue_goes_to_engaging(stage: FunnelStage) -> None:
    """Manual toggle reactivation continues (no greeting) → engaging (New#1c)."""
    assert reopen(stage, fresh=False) is FunnelStage.ENGAGING


@pytest.mark.parametrize("stage", ACTIVE_STAGES)
def test_reopen_is_noop_on_active_stages(stage: FunnelStage) -> None:
    """Re-opening a non-terminal stage leaves it unchanged."""
    assert reopen(stage, fresh=True) is stage
    assert reopen(stage, fresh=False) is stage


def test_reopen_is_not_an_llm_transition() -> None:
    """Re-apertura stays out of the LLM-validated graph: the model can never escape a
    terminal stage via validate_transition; terminals remain terminal."""
    for stage in TERMINAL_STAGES:
        assert is_terminal(stage)
        with pytest.raises(InvalidTransitionError):
            validate_transition(stage, FunnelStage.NEW)
