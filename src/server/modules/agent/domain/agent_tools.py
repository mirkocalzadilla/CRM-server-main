"""Tool handlers for the MVP catalogue (§8, FLUJO_AGENTE §1).

Pure: each handler reads `ToolContext` and returns a result dict. Funnel changes
are *proposed and validated* here via `funnel_fsm` (the LLM proposes, the code
validates); the AgentService persists the side effect. `consultar_faq` is static
(RAG `consultar_kb` over `kb_chunk` is deferred).
"""

from __future__ import annotations

from collections.abc import Mapping

from server.modules.agent.domain.funnel_fsm import (
    FunnelStage,
    InvalidTransitionError,
    validate_transition,
)
from server.modules.agent.domain.tools import ToolContext

HANDOFF_REASONS = ("explicit_request", "payment_validation", "agent_error", "unknown_service")


async def get_service(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Return service data from the agent config.

    Published catalog snapshot (`config.services` as a list, SPEC_catalogo §5.1):
    return all active services so the model can present them (each entry carries its
    `categoria` + `categoria_slug` for `enviar_material`). Legacy dict shape
    (`{linea: servicio}`): return the line.
    """
    servicios = ctx.config.get("services")
    if isinstance(servicios, list):
        return {"encontrado": bool(servicios), "services": servicios}
    linea = str(tool_input.get("linea", "cursos"))
    if not isinstance(servicios, dict) or linea not in servicios:
        return {"encontrado": False, "linea": linea}
    return {"encontrado": True, "linea": linea, "servicio": servicios[linea]}


def _find_service(config: Mapping[str, object], slug: str) -> dict[str, object] | None:
    """Locate a service by `slug` in the published catalog snapshot (list shape)."""
    servicios = config.get("services")
    if isinstance(servicios, list):
        for entry in servicios:
            if isinstance(entry, dict) and entry.get("slug") == slug:
                return entry
    return None


def _find_category(config: Mapping[str, object], slug: str) -> dict[str, object] | None:
    """Locate a category by `slug` in the published catalog snapshot (`config.categories`)."""
    categorias = config.get("categories")
    if isinstance(categorias, list):
        for entry in categorias:
            if isinstance(entry, dict) and entry.get("slug") == slug:
                return entry
    return None


async def enviar_material(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Send the lead ALL the materials (PDF/image) of the CATEGORY they chose, by its
    catalog `categoria_slug` (#235). Pure: returns `enviar_documentos`; the AgentService
    dispatches them. The doc is sent once at category selection, not per service.
    """
    slug = str(tool_input.get("categoria_slug", "") or tool_input.get("slug", ""))
    categoria = _find_category(ctx.config, slug)
    if categoria is None:
        return {"ok": False, "error": f"categoría no encontrada: {slug!r}"}
    nombre = str(categoria.get("nombre", slug))
    documentos = _materials_to_docs(categoria, nombre)
    if not documentos:
        return {"ok": False, "error": "la categoría no tiene material asociado"}
    return {"ok": True, "enviar_documentos": documentos, "nombre": nombre}


def _materials_to_docs(categoria: dict[str, object], nombre: str) -> list[dict[str, str]]:
    """All materials of a category as document instructions (#235). The caption goes
    only on the first file so it isn't repeated on every attachment."""
    docs: list[dict[str, str]] = []
    materials = categoria.get("materials")
    if isinstance(materials, list):
        for material in materials:
            if isinstance(material, dict):
                url = material.get("url")
                filename = material.get("filename")
                if url and filename:
                    docs.append({"link": str(url), "filename": str(filename)})
    if docs:
        docs[0]["caption"] = f"Info de {nombre}"
    return docs


async def fijar_servicio(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Fija en la oportunidad el servicio que el lead ACEPTÓ avanzar (#133 v2).

    Pure: valida que el `slug` exista en el catálogo publicado y lo devuelve; el
    AgentService lo estampa en la card (`card_service`, source='captured') vía el
    ServiceCapturePort — reemplaza la captura implícita al enviar material. Independiente
    de `enviar_material`: sirve también para servicios sin material (cierre consultivo).
    Usar SOLO tras la aceptación del lead, nunca ante una simple consulta.

    Aceptar un servicio es señal de compra (igual que pedir el QR) → el lead es hot:
    propone avanzar el funnel hasta `qualified` sin re-abrir etapas terminales (#206).
    """
    slug = str(tool_input.get("slug", ""))
    if _find_service(ctx.config, slug) is None:
        return {"ok": False, "error": f"servicio no encontrado: {slug!r}"}
    result: dict[str, object] = {"ok": True, "slug": slug}
    stage = _advance_toward_qualified(ctx.funnel_stage)
    if stage is not ctx.funnel_stage:
        result["etapa"] = stage.value  # AgentService persiste la transición
    return result


async def enviar_qr_pago(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Signal to send the lead the payment QR image (closing flow `pago_qr`).

    Pure: it only flags intent; the AgentService attaches the configured QR URL
    (`payment_qr_url`) and sends it as an image. Use only for `pago_qr` services.

    Asking for the QR is a buying signal → the lead is hot: propose advancing the
    funnel up to `qualified` (rating 'hot') as far as the FSM allows, without
    re-opening terminal stages. The QR is sent even if the stage can't change (#184).
    """
    result: dict[str, object] = {"ok": True, "enviar_qr_pago": True}
    stage = _advance_toward_qualified(ctx.funnel_stage)
    if stage is not ctx.funnel_stage:
        result["etapa"] = stage.value  # AgentService persists the transition
    return result


def _advance_toward_qualified(current: FunnelStage) -> FunnelStage:
    """Best-effort walk from an active stage up to QUALIFIED, one FSM-validated hop at
    a time. Stops at the first disallowed hop, so terminal stages (handed_off,
    disqualified) and NEW stay put; QUALIFIED is idempotent."""
    stage = current
    for target in (FunnelStage.QUALIFYING, FunnelStage.QUALIFIED):
        try:
            stage = validate_transition(stage, target)
        except InvalidTransitionError:
            break
    return stage


async def guardar_nombre(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Capture the lead's full name (#91). Pure: returns it; the AgentService persists it
    to `conversation.full_name`, from which the 'won' hook creates the contact."""
    nombre = str(tool_input.get("nombre", "")).strip()
    if not nombre:
        return {"ok": False, "error": "nombre vacío"}
    return {"ok": True, "full_name": nombre}


async def consultar_faq(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Static FAQ lookup over the agent config (keyword match)."""
    pregunta = str(tool_input.get("pregunta", "")).lower()
    faq = ctx.config.get("faq")
    if not isinstance(faq, dict):
        return {"encontrado": False}
    for clave, respuesta in faq.items():
        if str(clave).lower() in pregunta:
            return {"encontrado": True, "respuesta": respuesta}
    return {"encontrado": False}


async def set_lead_stage(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Propose a funnel transition; the FSM accepts or rejects it."""
    target_raw = str(tool_input.get("etapa", ""))
    try:
        target = FunnelStage(target_raw)
    except ValueError:
        return {"ok": False, "error": f"unknown stage: {target_raw!r}"}
    return _try_transition(ctx.funnel_stage, target)


async def handoff_to_human(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Hand off to the human team; validates the transition to `handed_off`."""
    motivo = str(tool_input.get("motivo", ""))
    if motivo not in HANDOFF_REASONS:
        return {"ok": False, "error": f"unknown reason: {motivo!r}"}
    result = _try_transition(ctx.funnel_stage, FunnelStage.HANDED_OFF)
    if result["ok"]:
        result["motivo"] = motivo
    return result


async def out_of_scope(ctx: ToolContext, tool_input: dict[str, object]) -> dict[str, object]:
    """Guardrail: off-topic / out-of-region → disqualify (auditable, Meta policy)."""
    result = _try_transition(ctx.funnel_stage, FunnelStage.DISQUALIFIED)
    if result["ok"]:
        result["motivo"] = str(tool_input.get("motivo", "out_of_scope"))
    return result


def _try_transition(current: FunnelStage, target: FunnelStage) -> dict[str, object]:
    try:
        new_stage = validate_transition(current, target)
    except InvalidTransitionError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "etapa": new_stage.value}
