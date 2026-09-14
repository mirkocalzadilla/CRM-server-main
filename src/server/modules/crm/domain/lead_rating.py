"""Calificación del lead (hot/medium/cold) derivada del funnel_stage (#96).

Regla determinística, sin LLM: la posición en el funnel ya codifica el avance del lead.
La card del CRM lo muestra como un badge de temperatura.
"""

from __future__ import annotations

from server.modules.agent.domain.funnel_fsm import FunnelStage

# Calificado = listo para pagar (caliente). Enganchando/Calificando/Derivado = en curso.
# Nuevo = primer contacto (frío). Descalificado = fuera de scope (frío).
_RATING_BY_STAGE: dict[FunnelStage, str] = {
    FunnelStage.QUALIFIED: "hot",
    FunnelStage.ENGAGING: "medium",
    FunnelStage.QUALIFYING: "medium",
    FunnelStage.HANDED_OFF: "medium",
    FunnelStage.NEW: "cold",
    FunnelStage.DISQUALIFIED: "cold",
}


def rating_for_stage(funnel_stage: FunnelStage | None, *, accepted_service: bool = False) -> str:
    """hot | medium | cold. `None` (card sin conversación) → cold.

    `accepted_service`: la card tiene un servicio aceptado (el bot lo fijó con
    fijar_servicio, o el operador lo asignó). Un lead que ya aceptó un servicio y está
    calificándose o derivado a cierre es una compra en curso → hot, aunque `handed_off`
    mapee a medium por defecto (#206)."""
    if funnel_stage is None:
        return "cold"
    if funnel_stage is FunnelStage.QUALIFIED:
        return "hot"
    if accepted_service and funnel_stage in (FunnelStage.QUALIFYING, FunnelStage.HANDED_OFF):
        return "hot"
    return _RATING_BY_STAGE.get(funnel_stage, "cold")
