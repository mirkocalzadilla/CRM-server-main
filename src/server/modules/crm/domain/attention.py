"""Cuándo una conversación está realmente esperando a una persona.

La señal "sin responder" se derivaba solo del orden de los mensajes: IA apagada,
oportunidad abierta y el lead habló último. El orden dice quién habló al final; no
dice si alguien necesita algo. Y así termina casi toda conversación exitosa — el lead
recibe lo suyo, dice "gracias", nadie contesta porque no hay qué contestar — así que
la card quedaba marcada como si alguien esperara, para siempre.

Lo que faltaba no es adivinar la intención del último mensaje: es un estado explícito.
`attended_at` es una persona diciendo "miré esto, no hay nada pendiente", y cuenta como
respuesta. Si el lead vuelve a escribir después, la señal se reenciende sola — sin
deshacer nada a mano.

Funciones puras sobre timestamps: sin DB, testeables en aislamiento.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple


class Activity(NamedTuple):
    """Últimos timestamps de una conversación. `None` = eso nunca pasó."""

    last_lead: datetime | None
    last_agent: datetime | None
    last_human: datetime | None


def last_activity(activity: Activity | None) -> datetime | None:
    """Lo último que pasó en la conversación, venga de quien venga. `None` si no hubo
    ningún mensaje (p. ej. una oportunidad dada de alta a mano, #97)."""
    if activity is None:
        return None
    stamps = [t for t in activity if t is not None]
    return max(stamps) if stamps else None


def is_awaiting(
    activity: Activity | None,
    *,
    is_ai_active: bool,
    closed_at: datetime | None,
    attended_at: datetime | None,
) -> bool:
    """True si hay un mensaje del lead que nadie contestó ni marcó como atendido.

    Con la IA encendida el agente ya responde, y sobre una oportunidad cerrada (won/lost)
    no hay a quién responder: en los dos casos no hay nadie esperando.
    """
    if is_ai_active or closed_at is not None:
        return False
    if activity is None or activity.last_lead is None:
        return False
    # Marcar como atendida cuenta igual que responder: las dos son una persona haciéndose
    # cargo. La diferencia es que una le escribe al lead y la otra decide que no hace falta.
    answers = [t for t in (activity.last_agent, activity.last_human, attended_at) if t is not None]
    if not answers:
        return True
    return activity.last_lead > max(answers)
