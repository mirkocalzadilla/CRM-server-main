"""Tests de las tool handlers (puro, sin DB ni LLM). Ver §8 + FLUJO_AGENTE §1."""

from __future__ import annotations

from server.modules.agent.domain import agent_tools
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.tools import ToolContext

CONFIG: dict[str, object] = {
    "services": {"cursos": {"precio": "480 Bs"}},
    "faq": {"ubicacion": "Av. Siempre Viva 123"},
}


def _ctx(stage: FunnelStage = FunnelStage.NEW) -> ToolContext:
    return ToolContext(funnel_stage=stage, config=CONFIG)


async def test_get_service_found() -> None:
    result = await agent_tools.get_service(_ctx(), {"linea": "cursos"})
    assert result["encontrado"] is True
    assert result["servicio"] == {"precio": "480 Bs"}


async def test_get_service_not_found() -> None:
    result = await agent_tools.get_service(_ctx(), {"linea": "servicios"})
    assert result == {"encontrado": False, "linea": "servicios"}


_CATALOG: dict[str, object] = {
    "services": [
        {
            "slug": "curso-edicion",
            "nombre": "Curso de edición",
            "precio": "480 Bs",
            "categoria": "Cursos",
            "categoria_slug": "cursos",
        },
        {"slug": "asesoria", "nombre": "Asesoría", "precio": "200 Bs", "categoria_slug": "sin-doc"},
    ],
    "categories": [
        {
            "slug": "cursos",
            "nombre": "Cursos",
            "materials": [{"url": "https://media.test/curso.pdf", "filename": "curso.pdf"}],
        },
        {"slug": "sin-doc", "nombre": "Sin doc", "materials": []},  # categoría sin material
    ],
}


def _ctx_catalog(stage: FunnelStage = FunnelStage.ENGAGING) -> ToolContext:
    return ToolContext(funnel_stage=stage, config=_CATALOG)


async def test_get_service_returns_published_list() -> None:
    # #87: con catálogo publicado (forma lista) get_service devuelve los servicios con slug.
    result = await agent_tools.get_service(_ctx_catalog(), {"linea": "cursos"})
    assert result["encontrado"] is True
    services = result["services"]
    assert isinstance(services, list)
    assert {o["slug"] for o in services} == {"curso-edicion", "asesoria"}


async def test_enviar_material_returns_document_for_category() -> None:
    # #235: el material se manda por categoría (categoria_slug), no por servicio.
    result = await agent_tools.enviar_material(_ctx_catalog(), {"categoria_slug": "cursos"})
    assert result["ok"] is True
    docs = result["enviar_documentos"]
    assert len(docs) == 1
    assert docs[0]["link"] == "https://media.test/curso.pdf"
    assert docs[0]["filename"] == "curso.pdf"
    assert docs[0]["caption"] == "Info de Cursos"


_CATALOG_MULTI: dict[str, object] = {
    "categories": [
        {
            "slug": "cursos",
            "nombre": "Cursos",
            "materials": [
                {"url": "https://media.test/curso.pdf", "filename": "curso.pdf"},
                {"url": "https://media.test/promo.jpg", "filename": "promo.jpg"},
            ],
        },
    ]
}


async def test_enviar_material_sends_all_materials() -> None:
    # #235: una categoría con varios archivos manda TODOS (pdf + imagen); caption solo en el 1ro.
    ctx = ToolContext(funnel_stage=FunnelStage.ENGAGING, config=_CATALOG_MULTI)
    result = await agent_tools.enviar_material(ctx, {"categoria_slug": "cursos"})
    assert result["ok"] is True
    docs = result["enviar_documentos"]
    assert [d["filename"] for d in docs] == ["curso.pdf", "promo.jpg"]
    assert docs[0]["caption"] == "Info de Cursos"
    assert "caption" not in docs[1]


async def test_enviar_material_unknown_category() -> None:
    result = await agent_tools.enviar_material(_ctx_catalog(), {"categoria_slug": "no-existe"})
    assert result["ok"] is False


async def test_enviar_material_category_without_material() -> None:
    result = await agent_tools.enviar_material(_ctx_catalog(), {"categoria_slug": "sin-doc"})
    assert result["ok"] is False


async def test_fijar_servicio_returns_slug_and_qualifies_from_engaging() -> None:
    # #133 v2 / #206: fija el servicio aceptado (devuelve el slug validado para que el
    # AgentService lo estampe) y, como aceptar es señal de compra, avanza a calificado.
    result = await agent_tools.fijar_servicio(
        _ctx_catalog(FunnelStage.ENGAGING), {"slug": "curso-edicion"}
    )
    assert result == {"ok": True, "slug": "curso-edicion", "etapa": "qualified"}


async def test_fijar_servicio_works_for_service_without_material() -> None:
    # A diferencia de enviar_material, fijar_servicio no exige material: sirve para
    # el cierre consultivo (servicios que se coordinan con un humano).
    result = await agent_tools.fijar_servicio(
        _ctx_catalog(FunnelStage.QUALIFYING), {"slug": "asesoria"}
    )
    assert result == {"ok": True, "slug": "asesoria", "etapa": "qualified"}


async def test_fijar_servicio_does_not_advance_from_new() -> None:
    # #206: desde NEW el FSM no permite saltar a calificado — solo fija el servicio.
    result = await agent_tools.fijar_servicio(
        _ctx_catalog(FunnelStage.NEW), {"slug": "curso-edicion"}
    )
    assert result == {"ok": True, "slug": "curso-edicion"}


async def test_fijar_servicio_unknown_slug() -> None:
    result = await agent_tools.fijar_servicio(_ctx_catalog(), {"slug": "no-existe"})
    assert result["ok"] is False


async def test_enviar_qr_pago_signals_intent() -> None:
    # Desde NEW el FSM no permite avanzar a calificado: solo señaliza el envío del QR.
    result = await agent_tools.enviar_qr_pago(_ctx(FunnelStage.NEW), {})
    assert result == {"ok": True, "enviar_qr_pago": True}


async def test_enviar_qr_pago_qualifies_from_qualifying() -> None:
    # #184: pedir el QR es señal de compra → el lead se marca como calificado (hot).
    result = await agent_tools.enviar_qr_pago(_ctx(FunnelStage.QUALIFYING), {})
    assert result == {"ok": True, "enviar_qr_pago": True, "etapa": "qualified"}


async def test_enviar_qr_pago_qualifies_from_engaging() -> None:
    # #184: aun si el modelo saltó el paso de qualifying, el QR avanza el funnel hasta
    # calificado (dos hops válidos del FSM: engaging → qualifying → qualified).
    result = await agent_tools.enviar_qr_pago(_ctx(FunnelStage.ENGAGING), {})
    assert result == {"ok": True, "enviar_qr_pago": True, "etapa": "qualified"}


async def test_enviar_qr_pago_idempotent_when_already_qualified() -> None:
    # Ya calificado: no re-propone etapa (idempotente), igual manda el QR.
    result = await agent_tools.enviar_qr_pago(_ctx(FunnelStage.QUALIFIED), {})
    assert result == {"ok": True, "enviar_qr_pago": True}


async def test_guardar_nombre_returns_name() -> None:
    # #91: el tool devuelve el nombre limpio; el AgentService lo persiste en full_name.
    result = await agent_tools.guardar_nombre(_ctx(), {"nombre": "  Juan Pérez  "})
    assert result == {"ok": True, "full_name": "Juan Pérez"}


async def test_guardar_nombre_empty_rejected() -> None:
    result = await agent_tools.guardar_nombre(_ctx(), {"nombre": "   "})
    assert result["ok"] is False


async def test_consultar_faq_match() -> None:
    result = await agent_tools.consultar_faq(_ctx(), {"pregunta": "cual es la ubicacion?"})
    assert result == {"encontrado": True, "respuesta": "Av. Siempre Viva 123"}


async def test_consultar_faq_no_match() -> None:
    result = await agent_tools.consultar_faq(_ctx(), {"pregunta": "hay estacionamiento?"})
    assert result == {"encontrado": False}


async def test_set_lead_stage_valid() -> None:
    result = await agent_tools.set_lead_stage(
        _ctx(FunnelStage.NEW), {"etapa": "engaging", "razon": "x"}
    )
    assert result == {"ok": True, "etapa": "engaging"}


async def test_set_lead_stage_invalid_transition() -> None:
    result = await agent_tools.set_lead_stage(
        _ctx(FunnelStage.NEW), {"etapa": "qualified", "razon": "x"}
    )
    assert result["ok"] is False
    assert "error" in result


async def test_set_lead_stage_unknown_stage() -> None:
    result = await agent_tools.set_lead_stage(_ctx(), {"etapa": "bogus", "razon": "x"})
    assert result["ok"] is False


async def test_handoff_valid_from_active_stage() -> None:
    result = await agent_tools.handoff_to_human(
        _ctx(FunnelStage.ENGAGING), {"motivo": "explicit_request"}
    )
    assert result == {"ok": True, "etapa": "handed_off", "motivo": "explicit_request"}


async def test_handoff_unknown_service_reason_valid() -> None:
    # #94: pedir un servicio fuera del catálogo es un motivo válido de derivación.
    result = await agent_tools.handoff_to_human(
        _ctx(FunnelStage.ENGAGING), {"motivo": "unknown_service"}
    )
    assert result == {"ok": True, "etapa": "handed_off", "motivo": "unknown_service"}


async def test_handoff_unknown_reason() -> None:
    result = await agent_tools.handoff_to_human(_ctx(FunnelStage.QUALIFIED), {"motivo": "bogus"})
    assert result["ok"] is False


async def test_handoff_from_terminal_stage_rejected() -> None:
    result = await agent_tools.handoff_to_human(
        _ctx(FunnelStage.DISQUALIFIED), {"motivo": "agent_error"}
    )
    assert result["ok"] is False


async def test_out_of_scope_disqualifies() -> None:
    result = await agent_tools.out_of_scope(
        _ctx(FunnelStage.ENGAGING), {"motivo": "fuera de Bolivia"}
    )
    assert result == {"ok": True, "etapa": "disqualified", "motivo": "fuera de Bolivia"}
