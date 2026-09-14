"""Nombres de los stages del pipeline, en un solo lugar.

`stage.name` es **dato por organización** (lo crea el seed, y un operador podría
renombrarlo), pero varios servicios necesitan localizar un stage concreto para
decidir qué hacer. Hasta ahora cada uno llevaba su propio string literal, así que
renombrar un stage rompía código en sitios que nadie miraba. Estas constantes son la
fuente única: si un nombre cambia, cambia acá y se migra el dato en la misma PR.

`HUMAN_STAGE_ORDER` fija el orden del pipeline humano; se usa para la regla de
no-regresión (dentro del pipeline humano el stage solo avanza, salvo move manual).
"""

from __future__ import annotations

PIPELINE_IA = "ia"
PIPELINE_HUMAN = "human"

# Nombre visible de cada pipeline. El `kind` es el identificador estable (nunca cambia);
# el nombre es lo que ve el operador y es dato por organización, igual que el de un stage.
# Los dos pipelines se llamaban por quién trabajaba ("Gestión IA" / "Gestión Humana"), y
# eso dejó de ser cierto cuando la validación del pago y la entrega pasaron a ser
# automáticas: hoy el sistema trabaja en los dos. Se nombran por la fase del negocio.
# Los nombres anteriores ("Gestión IA" / "Gestión Humana") viven solo en la migración
# 0035, acotando el UPDATE para que sea idempotente y no pise el rename manual de una
# organización: no hay código que resuelva un pipeline por nombre.
PIPELINE_IA_NAME = "Gestión Venta"
PIPELINE_HUMAN_NAME = "Gestión Postventa"

# Pipeline humano (post-handoff).
HUMAN_INTAKE = "Por atender"  # intake genérico: pidió humano / falló el bot
PAYMENT_VALIDATION = "Por validar pago"  # mandó comprobante → validar el pago
PAYMENT_VALIDATED = "Pago validado"  # pago OK → dispara la entrega
DELIVERED = "Entregado"  # se le entregó lo que compró (antes "Entrada enviada")
CLOSED = "Cerrado"  # terminal (won)
# Terminal perdido del pipeline humano. Hasta CR4 no existía: una oportunidad que se caía
# ya en manos de un humano no tenía dónde ir, y el pago cuya conciliación se rechaza
# necesita justamente eso — un cierre negativo, distinto del "Descalificado" del bot.
HUMAN_LOST = "Perdido"

# Nombre anterior de `DELIVERED`. La entrega dejó de ser siempre una entrada (un curso
# virtual recibe links, no un QR), así que el stage pasó a llamarse por el resultado y
# no por el medio. Se conserva para que la migración de datos y cualquier organización
# sin migrar sigan resolviendo el stage.
DELIVERED_LEGACY = "Entrada enviada"

HUMAN_STAGE_ORDER: tuple[str, ...] = (
    HUMAN_INTAKE,
    PAYMENT_VALIDATION,
    PAYMENT_VALIDATED,
    DELIVERED,
    CLOSED,
    HUMAN_LOST,
)

# Pipeline IA (espejo del funnel).
IA_NEW = "Nuevo"
IA_ENGAGING = "Enganchando"
IA_QUALIFYING = "Calificando"
IA_QUALIFIED = "Calificado"
IA_DISQUALIFIED = "Descalificado"
