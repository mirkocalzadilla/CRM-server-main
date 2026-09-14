"""Decidir si un comprobante de pago se aprueba solo, o lo tiene que ver un humano.

El modelo de visión **extrae**; acá el código **decide**. Todos los checks son
determinísticos y puros: mismos datos ⇒ mismo veredicto, sin DB ni IO, y sin que el
LLM tenga voz en la aprobación.

El sesgo es explícito: **la duda va a un humano**. Un dato que no se pudo leer, una
moneda que no es la del precio, un precio que no es un número comparable, dos servicios
en la misma card — todo eso bloquea la aprobación automática en vez de intentar
adivinar. Aprobar un pago que no entró es mucho más costoso que hacer esperar a alguien
unos minutos.

La confianza que reporte el modelo **no es un check**: un modelo puede estar muy seguro
de algo falso. Lo que decide es la evidencia (monto, beneficiario, fecha, referencia).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from server.modules.crm.domain.beneficiary import matches_beneficiary

# Días de antigüedad tolerados. Un comprobante viejo puede ser de otra compra, o el
# reenvío de uno ya usado.
MAX_RECEIPT_AGE_DAYS = 7

# Códigos de check. Viajan a la DB y al CRM (que los traduce), así que son estables.
CHECK_AMOUNT = "amount"
CHECK_BENEFICIARY = "beneficiary"
CHECK_DATE = "date"
CHECK_REFERENCE = "reference"
CHECK_CURRENCY = "currency"
CHECK_PRICE = "price"

# Veredictos posibles.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ExtractedReceipt:
    """Lo que el modelo de visión leyó del comprobante. Todo opcional: un campo que no
    se pudo leer es `None`, y eso ya es información (bloquea la aprobación)."""

    amount: Decimal | None = None
    currency: str | None = None
    paid_at: date | None = None
    beneficiary: str | None = None
    reference: str | None = None
    bank: str | None = None


@dataclass(frozen=True, slots=True)
class ExpectedPayment:
    """Contra qué se compara: el precio del servicio aceptado y la config de la org."""

    amount: Decimal | None
    currency: str
    beneficiary: str | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Resultado de un check individual, con el detalle que ve el operador."""

    code: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ReceiptVerdict:
    """Veredicto completo: si se puede aprobar solo, y por qué."""

    verdict: str
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def failed_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


def evaluate(
    extracted: ExtractedReceipt, expected: ExpectedPayment, *, today: date
) -> ReceiptVerdict:
    """Corre todos los checks. `today` se inyecta para que el resultado sea reproducible.

    Se corren **todos** incluso si uno ya falló: el operador necesita ver el cuadro
    completo para decidir, no el primer problema que aparezca.
    """
    checks = (
        _check_price_is_comparable(expected),
        _check_currency(extracted, expected),
        _check_amount(extracted, expected),
        _check_beneficiary(extracted, expected),
        _check_date(extracted, today=today),
        _check_reference(extracted),
    )
    verdict = VERDICT_PASS if all(check.passed for check in checks) else VERDICT_FAIL
    return ReceiptVerdict(verdict=verdict, checks=checks)


def _check_price_is_comparable(expected: ExpectedPayment) -> CheckResult:
    """El servicio tiene un precio numérico contra el que comparar.

    Un precio en rango ("1.800 / 3.000") o abierto ("desde 3.800") no se puede validar
    solo: cualquiera de los dos montos sería "correcto".
    """
    if expected.amount is None:
        return CheckResult(
            CHECK_PRICE,
            False,
            "el servicio no tiene un precio numérico cargado (rango o texto): se valida a mano",
        )
    return CheckResult(CHECK_PRICE, True, f"precio esperado {expected.amount}")


def _check_currency(extracted: ExtractedReceipt, expected: ExpectedPayment) -> CheckResult:
    """El precio está en la moneda del comprobante.

    Un servicio en USD nunca se auto-valida: no hay tasa de cambio en el sistema y el
    negocio confirma el monto en Bs a mano.
    """
    if expected.currency != "BOB":
        return CheckResult(
            CHECK_CURRENCY,
            False,
            f"el servicio está en {expected.currency}: el monto se confirma a mano",
        )
    if extracted.currency is not None and extracted.currency != "BOB":
        return CheckResult(
            CHECK_CURRENCY, False, f"el comprobante está en {extracted.currency}, no en Bs"
        )
    return CheckResult(CHECK_CURRENCY, True, "pago en Bs")


def _check_amount(extracted: ExtractedReceipt, expected: ExpectedPayment) -> CheckResult:
    """El monto pagado es exactamente el precio del servicio.

    Sin tolerancia: las transferencias bolivianas son por el monto exacto, y un
    sobrepago o un pago parcial es justamente lo que un humano tiene que mirar (para
    eso existe el override con nota).
    """
    if extracted.amount is None:
        return CheckResult(CHECK_AMOUNT, False, "no se pudo leer el monto del comprobante")
    if expected.amount is None:
        return CheckResult(CHECK_AMOUNT, False, "no hay precio numérico con el que comparar")
    if extracted.amount != expected.amount:
        return CheckResult(
            CHECK_AMOUNT,
            False,
            f"el comprobante dice {extracted.amount} y el servicio cuesta {expected.amount}",
        )
    return CheckResult(CHECK_AMOUNT, True, f"monto correcto ({extracted.amount})")


def _check_beneficiary(extracted: ExtractedReceipt, expected: ExpectedPayment) -> CheckResult:
    """El pago fue a la cuenta esperada."""
    if expected.beneficiary is None:
        return CheckResult(
            CHECK_BENEFICIARY,
            False,
            "no hay beneficiario esperado configurado: cargalo en la config de pagos",
        )
    if not extracted.beneficiary:
        return CheckResult(
            CHECK_BENEFICIARY, False, "no se pudo leer el destinatario del comprobante"
        )
    if not matches_beneficiary(expected.beneficiary, extracted.beneficiary):
        return CheckResult(
            CHECK_BENEFICIARY,
            False,
            f"el pago figura a nombre de {extracted.beneficiary}, no de {expected.beneficiary}",
        )
    return CheckResult(CHECK_BENEFICIARY, True, f"destinatario correcto ({extracted.beneficiary})")


def _check_date(extracted: ExtractedReceipt, *, today: date) -> CheckResult:
    """El comprobante es reciente y no está fechado en el futuro."""
    if extracted.paid_at is None:
        return CheckResult(CHECK_DATE, False, "no se pudo leer la fecha del comprobante")
    age = (today - extracted.paid_at).days
    if age < 0:
        return CheckResult(
            CHECK_DATE, False, f"el comprobante está fechado en el futuro ({extracted.paid_at})"
        )
    if age > MAX_RECEIPT_AGE_DAYS:
        return CheckResult(
            CHECK_DATE,
            False,
            f"el comprobante es de hace {age} días (máximo {MAX_RECEIPT_AGE_DAYS})",
        )
    return CheckResult(CHECK_DATE, True, f"comprobante del {extracted.paid_at}")


def _check_reference(extracted: ExtractedReceipt) -> CheckResult:
    """El comprobante tiene su identificador único de transacción.

    Sin identificador no se puede detectar que el mismo comprobante se reuse, así que
    nunca se aprueba solo — es la defensa contra reenviar la misma captura dos veces.
    """
    if not (extracted.reference or "").strip():
        return CheckResult(
            CHECK_REFERENCE,
            False,
            "no se pudo leer el número de transacción: sin él no se puede evitar el reuso",
        )
    return CheckResult(CHECK_REFERENCE, True, f"transacción {extracted.reference}")


def age_limit_start(today: date) -> date:
    """Fecha más antigua aceptable (útil para mostrar el criterio en la UI)."""
    return today - timedelta(days=MAX_RECEIPT_AGE_DAYS)
