"""Tests del mapeo puro `target_stage` (funnel + is_ai_active + motivo → pipeline/stage).

El reconcile ligado a DB (`CardService.sync`, que lee el motivo del último
`handoff_event`) se valida en el e2e contra Postgres.
"""

from __future__ import annotations

import pytest

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.crm.services.card_service import (
    HUMAN_INTAKE_STAGE,
    PAYMENT_VALIDATION_STAGE,
    target_stage,
)


@pytest.mark.parametrize(
    ("funnel", "expected_stage"),
    [
        (FunnelStage.NEW, "Nuevo"),
        (FunnelStage.ENGAGING, "Enganchando"),
        (FunnelStage.QUALIFYING, "Calificando"),
        (FunnelStage.QUALIFIED, "Calificado"),
        (FunnelStage.DISQUALIFIED, "Descalificado"),
    ],
)
def test_active_maps_to_ia_mirror_stage(funnel: FunnelStage, expected_stage: str) -> None:
    assert target_stage(funnel, is_ai_active=True) == ("ia", expected_stage)


def test_payment_validation_goes_to_payment_stage() -> None:
    assert target_stage(
        FunnelStage.HANDED_OFF, is_ai_active=False, handoff_reason="payment_validation"
    ) == ("human", PAYMENT_VALIDATION_STAGE)


def test_explicit_request_goes_to_generic_intake() -> None:
    # Pedir humano ≠ validar pago → intake genérico, no "Por validar pago".
    assert target_stage(
        FunnelStage.HANDED_OFF, is_ai_active=False, handoff_reason="explicit_request"
    ) == ("human", HUMAN_INTAKE_STAGE)


def test_agent_error_goes_to_generic_intake() -> None:
    assert target_stage(
        FunnelStage.HANDED_OFF, is_ai_active=False, handoff_reason="agent_error"
    ) == ("human", HUMAN_INTAKE_STAGE)


def test_unknown_service_goes_to_generic_intake() -> None:
    # #94: servicio fuera del catálogo → "Por atender" (intake), no validar pago.
    assert target_stage(
        FunnelStage.HANDED_OFF, is_ai_active=False, handoff_reason="unknown_service"
    ) == ("human", HUMAN_INTAKE_STAGE)


def test_unknown_or_missing_reason_defaults_to_generic_intake() -> None:
    # Sin evento (takeover manual) o motivo desconocido → intake genérico.
    assert target_stage(FunnelStage.HANDED_OFF, is_ai_active=False) == (
        "human",
        HUMAN_INTAKE_STAGE,
    )
    assert target_stage(FunnelStage.QUALIFIED, is_ai_active=False, handoff_reason="???") == (
        "human",
        HUMAN_INTAKE_STAGE,
    )


def test_intake_and_payment_stages_are_distinct() -> None:
    assert HUMAN_INTAKE_STAGE != PAYMENT_VALIDATION_STAGE
