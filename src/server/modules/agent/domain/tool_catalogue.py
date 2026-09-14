"""MVP tool catalogue: the 5 `ToolDefinition`s + the default registry (§8).

Descriptions state when NOT to use each tool (anti-misrouting). Input schemas are
JSON Schema; structured output is enforced by the model via `tool_choice`.
"""

from __future__ import annotations

from server.modules.agent.domain import agent_tools
from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.tools import ToolDefinition, ToolRegistry

_STAGE_VALUES = [stage.value for stage in FunnelStage]

GET_SERVICE = ToolDefinition(
    name="get_service",
    description=(
        "Devuelve los servicios del catálogo: nombre, resumen, detalle, precio y "
        "material. El 'cuándo' (fechas/edición de un curso) sale del campo `detalle` "
        "del servicio. Usá para 'de qué trata', 'cuánto cuesta', 'cuándo es'. NO usar "
        "para logística general (usá consultar_faq) ni para cambiar la etapa del lead."
    ),
    # `linea` es la forma legacy (config dict); el catálogo publicado devuelve la
    # lista completa y lo ignora → opcional.
    input_schema={
        "type": "object",
        "properties": {
            "linea": {"type": "string", "enum": ["cursos", "servicios"]},
        },
    },
    handler=agent_tools.get_service,
)

ENVIAR_MATERIAL = ToolDefinition(
    name="enviar_material",
    description=(
        "Envía al lead el PDF/material de la CATEGORÍA que eligió, identificada por su "
        "'categoria_slug' del catálogo (mirá el campo `categoria_slug` en lo que devuelve "
        "get_service). Usá una sola vez, cuando el lead elige una categoría, para "
        "mandarle su documento. Si la categoría no tiene material la herramienta falla: "
        "no prometas ni inventes archivos. NO reenvíes el material al elegir un servicio "
        "puntual dentro de la categoría, ni lo uses para precio (get_service) o derivar."
    ),
    input_schema={
        "type": "object",
        "properties": {"categoria_slug": {"type": "string"}},
        "required": ["categoria_slug"],
    },
    handler=agent_tools.enviar_material,
)

FIJAR_SERVICIO = ToolDefinition(
    name="fijar_servicio",
    description=(
        "Fija en la oportunidad el servicio que el lead ACEPTÓ, por su 'slug' del "
        "catálogo (mirá los slugs en get_service). Usá SOLO cuando el lead confirmó que "
        "quiere avanzar con ese servicio puntual (dijo que lo quiere, que le sirve, que "
        "sigan), no cuando solo pregunta o pide info. Sirve también para servicios sin "
        "material y para cierre 'handoff_consultivo'. NO usar para mandar el material "
        "(usá enviar_material) ni para cambiar la etapa del lead (usá set_lead_stage)."
    ),
    input_schema={
        "type": "object",
        "properties": {"slug": {"type": "string"}},
        "required": ["slug"],
    },
    handler=agent_tools.fijar_servicio,
)

ENVIAR_QR_PAGO = ToolDefinition(
    name="enviar_qr_pago",
    description=(
        "Envía al lead la imagen del QR de pago y marca la oportunidad como calificada "
        "(lead caliente), no hace falta un set_lead_stage aparte. Usá cuando el lead "
        "quiere pagar/inscribirse en un servicio de cierre 'pago_qr' (mirá `flujo_cierre` "
        "en lo que devuelve get_service). NO usar para servicios 'handoff_consultivo' "
        "(esos se derivan con handoff_to_human, sin cobro) ni para mandar el material del "
        "servicio (usá enviar_material)."
    ),
    input_schema={"type": "object", "properties": {}},
    handler=agent_tools.enviar_qr_pago,
)

GUARDAR_NOMBRE = ToolDefinition(
    name="guardar_nombre",
    description=(
        "Guarda el nombre completo del lead (se usa para emitir la entrada y registrar "
        "su contacto). Usá cuando el lead te dice su nombre, al pasar a calificado y "
        "antes de mandar el QR de pago. NO usar para el nombre de un servicio ni para "
        "cambiar la etapa del lead (usá set_lead_stage)."
    ),
    input_schema={
        "type": "object",
        "properties": {"nombre": {"type": "string"}},
        "required": ["nombre"],
    },
    handler=agent_tools.guardar_nombre,
)

CONSULTAR_FAQ = ToolDefinition(
    name="consultar_faq",
    description=(
        "FAQ estático (ubicación, cupo, qué llevar) desde la config. NO usar para "
        "precio/servicio (usá get_service) ni para preguntas que requieran datos en vivo."
    ),
    input_schema={
        "type": "object",
        "properties": {"pregunta": {"type": "string"}},
        "required": ["pregunta"],
    },
    handler=agent_tools.consultar_faq,
)

SET_LEAD_STAGE = ToolDefinition(
    name="set_lead_stage",
    description=(
        "Propone avanzar la etapa del lead en el funnel (ej. a 'qualified' cuando "
        "quiere inscribirse). El código valida la transición. NO usar para derivar "
        "a un humano (usá handoff_to_human)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "etapa": {"type": "string", "enum": _STAGE_VALUES},
            "razon": {"type": "string"},
        },
        "required": ["etapa", "razon"],
    },
    handler=agent_tools.set_lead_stage,
)

HANDOFF_TO_HUMAN = ToolDefinition(
    name="handoff_to_human",
    description=(
        "Deriva al equipo humano de Mirko (terminal). Motivos: 'explicit_request' "
        "(pide hablar con alguien), 'payment_validation' (mandó comprobante), "
        "'agent_error' (no podés ayudar), 'unknown_service' (pide un servicio de Mirko "
        "que NO figura en get_service: no respondas ni inventes, solo derivá). Tras "
        "esto el agente queda en silencio."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "motivo": {"type": "string", "enum": list(agent_tools.HANDOFF_REASONS)},
            "resumen": {"type": "string"},
        },
        "required": ["motivo"],
    },
    handler=agent_tools.handoff_to_human,
)

OUT_OF_SCOPE = ToolDefinition(
    name="out_of_scope",
    description=(
        "Guardrail: el lead está fuera de alcance (fuera de Bolivia, otra moneda, "
        "tema no relacionado) → descalifica con respuesta amable. NO usar si solo "
        "falta información que sí podés dar."
    ),
    input_schema={
        "type": "object",
        "properties": {"motivo": {"type": "string"}},
        "required": ["motivo"],
    },
    handler=agent_tools.out_of_scope,
)

DEFAULT_TOOLS = [
    GET_SERVICE,
    ENVIAR_MATERIAL,
    FIJAR_SERVICIO,
    ENVIAR_QR_PAGO,
    GUARDAR_NOMBRE,
    CONSULTAR_FAQ,
    SET_LEAD_STAGE,
    HANDOFF_TO_HUMAN,
    OUT_OF_SCOPE,
]


def build_default_registry() -> ToolRegistry:
    """The MVP registry, injected into the AgentService."""
    return ToolRegistry(DEFAULT_TOOLS)
