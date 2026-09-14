"""Avisos operativos de una card: por qué algo automático no se completó.

Cuando el sistema entrega solo, hay situaciones que no son un error del sistema pero
tampoco un final feliz: el lead no dio su nombre, el servicio no tiene modalidad
cargada, la ventana de 24h de WhatsApp se cerró, llegó un segundo comprobante. En
todos esos casos el flujo hace lo máximo que puede sin arriesgar y deja un aviso para
que un humano decida.

Los avisos son **códigos**, no texto: el CRM los traduce al español. Así el backend no
carga copy de UI y agregar un idioma o cambiar una frase no toca la DB.

Funciones puras sobre listas de strings (testeables sin DB): idempotentes, preservan el
orden de aparición y nunca duplican.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# El lead llegó a la entrega sin nombre: se le entregó igual y la oportunidad quedó sin
# cerrar. Nunca se bloquea una entrega ya pagada por un dato administrativo (#241).
NEEDS_NAME = "needs_name"
# Fuera de la ventana de 24h de WhatsApp: Meta rechaza el envío libre (131047). La
# entrega queda pendiente y se reintenta al próximo mensaje del lead.
DELIVERY_PENDING = "delivery_pending"
# Llegó otro comprobante con la card ya entregada (posible pago doble). No se re-entrega
# ni se mueve nada: lo revisa un humano.
EXTRA_RECEIPT = "extra_receipt"
# El servicio aceptado no tiene modalidad cargada, así que no se sabe qué entregar.
NO_MODALITY = "no_modality"
# Falta un link de entrega que la modalidad necesita (p. ej. un curso virtual sin grupo
# ni reunión). Antes de mandarle al lead un mensaje roto, va a un humano.
MISSING_LINK = "missing_link"
# La card tiene más de un servicio aceptado: no se puede decidir sola qué entregar.
AMBIGUOUS_SERVICE = "ambiguous_service"
# El comprobante que mandó el lead no pasó los checks automáticos: hay que mirarlo.
RECEIPT_REVIEW = "receipt_review"
# El pago se aprobó solo y espera la confirmación humana contra el banco.
PAYMENT_UNCONFIRMED = "payment_unconfirmed"
# El servicio es presencial pero no tiene ningún evento próximo cargado: una entrada sin
# fecha ni lugar no le sirve al lead ni se puede validar en la puerta.
NO_EVENT = "no_event"
# El evento llegó a su cupo. No se emite una entrada de más en silencio: en la puerta
# habría alguien con su QR válido y sin lugar.
CAPACITY_FULL = "capacity_full"

ALL_FLAGS: frozenset[str] = frozenset(
    {
        NEEDS_NAME,
        DELIVERY_PENDING,
        EXTRA_RECEIPT,
        NO_MODALITY,
        MISSING_LINK,
        AMBIGUOUS_SERVICE,
        RECEIPT_REVIEW,
        PAYMENT_UNCONFIRMED,
        NO_EVENT,
        CAPACITY_FULL,
    }
)


def normalize(raw: object) -> list[str]:
    """Lista de avisos conocidos a partir de lo que haya en la columna JSON.

    Tolerante por diseño: la columna es JSON, así que puede traer `None`, un valor de
    otro tipo, o códigos de una versión anterior. Un aviso desconocido se descarta en
    vez de romper la lectura del tablero.
    """
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in ALL_FLAGS and item not in seen:
            seen.append(item)
    return seen


def add(flags: Sequence[str] | None, *new: str) -> list[str]:
    """Avisos con `new` agregados al final. Idempotente."""
    result = normalize(list(flags or []))
    for flag in new:
        if flag in ALL_FLAGS and flag not in result:
            result.append(flag)
    return result


def remove(flags: Sequence[str] | None, *old: str) -> list[str]:
    """Avisos sin los de `old`. Idempotente."""
    drop = set(old)
    return [flag for flag in normalize(list(flags or [])) if flag not in drop]


def has(flags: Sequence[str] | None, flag: str) -> bool:
    return flag in normalize(list(flags or []))


def replace_delivery_flags(flags: Sequence[str] | None, *new: str) -> list[str]:
    """Reemplaza los avisos que dependen del intento de entrega, conservando el resto.

    Un reintento tiene que poder limpiar el "quedó pendiente" o el "falta un link" del
    intento anterior sin borrar avisos de otra naturaleza (p. ej. que llegó un
    comprobante extra, que sigue necesitando revisión humana igual).
    """
    keep = remove(flags, *_DELIVERY_FLAGS)
    return add(keep, *new)


_DELIVERY_FLAGS: tuple[str, ...] = (
    DELIVERY_PENDING,
    NO_MODALITY,
    MISSING_LINK,
    AMBIGUOUS_SERVICE,
    NEEDS_NAME,
    NO_EVENT,
    CAPACITY_FULL,
)


def merge(*groups: Iterable[str]) -> list[str]:
    """Une varios conjuntos de avisos preservando el orden y sin duplicar."""
    result: list[str] = []
    for group in groups:
        result = add(result, *group)
    return result
