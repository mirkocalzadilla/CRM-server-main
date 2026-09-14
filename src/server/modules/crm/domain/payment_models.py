"""Configuración de pagos por organización.

Hasta ahora la única config de pago era global (`settings.payment_qr_url`, una env
var): servía con un solo cliente, pero el QR de pago y —sobre todo— el **beneficiario
esperado** de un comprobante son propios de cada organización. Esta tabla los mueve
al tenant, con fallback a la env var mientras no haya fila (nada se rompe en prod).

`expected_beneficiary` es el nombre contra el que se compara el destinatario que
figura en el comprobante. El matching es tolerante (mayúsculas, tildes, orden,
truncamiento, máscaras tipo "M*** C***") porque cada banco lo escribe distinto; si
no coincide, el comprobante va a revisión humana en vez de aprobarse.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Numeric, Text, UniqueConstraint
from sqlalchemy import Date as SADate
from sqlalchemy import DateTime as SADateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PaymentSettings(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Config de pagos de una organización. Una fila por tenant (UNIQUE).

    `organization_id` va sin FK, como en el resto del modelo multi-tenant del repo
    (`card`, `service`): el scoping se garantiza en cada query, no con una constraint.
    """

    __tablename__ = "payment_settings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,
        index=True,
    )
    # Nombre esperado del destinatario del comprobante. NULL = sin configurar ⇒ el
    # check de beneficiario no puede pasar y el comprobante va a humano.
    expected_beneficiary: Mapped[str | None] = mapped_column(Text)
    # QR de pago de la organización. NULL = usar el global (`settings.payment_qr_url`).
    payment_qr_url: Mapped[str | None] = mapped_column(Text)
    # Path relativo bajo `media_root` cuando el QR es un archivo subido acá. NULL = la
    # URL es externa (o no hay ninguna). Es lo que permite borrar el archivo anterior
    # al reemplazarlo: la URL no alcanza porque `media_base_url` cambia entre entornos.
    payment_qr_storage_ref: Mapped[str | None] = mapped_column(Text)


class PaymentReceipt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Un comprobante que mandó un lead, con lo que se leyó y lo que se decidió.

    Es el registro auditable de una validación: qué decía el comprobante, qué check
    pasó y cuál no, y quién terminó aprobándolo. Sirve para tres cosas distintas: que
    el operador vea el detalle en la card, que un pago aprobado solo se pueda revisar
    después, y que el mismo comprobante no se pueda usar dos veces.

    **Las dos defensas anti-reuso** son constraints, no lógica:

    - `(organization_id, reference)` — el número de transacción del banco es único, así
      que la misma transferencia no puede validar dos compras. Un `reference` NULL (no
      se pudo leer) no colisiona en Postgres, y por eso un comprobante sin identificador
      legible **nunca** se aprueba solo: sin él esta defensa no existe.
    - `(organization_id, image_sha256)` — la misma captura reenviada, aunque el
      identificador no se haya podido leer.
    """

    __tablename__ = "payment_receipt"
    __table_args__ = (
        UniqueConstraint("organization_id", "reference", name="uq_payment_receipt_org_reference"),
        UniqueConstraint("organization_id", "image_sha256", name="uq_payment_receipt_org_sha"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Mensaje de WhatsApp del que salió: hace el job idempotente.
    wamid: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    media_path: Mapped[str | None] = mapped_column(Text)
    image_sha256: Mapped[str] = mapped_column(Text, nullable=False)

    # Lo que se leyó del comprobante. NULL = no se pudo leer (y eso bloquea el auto-pass).
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[date | None] = mapped_column(SADate)
    beneficiary: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    bank: Mapped[str | None] = mapped_column(Text)

    # Veredicto (`pass`/`fail`) y el resultado de cada check, para mostrar el semáforo.
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    checks: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False, default=list)
    # Lo extraído tal como se leyó, en forma serializable. Redundante con las columnas
    # de arriba a propósito: esas se normalizan para comparar, y esto es lo que el
    # operador tiene que poder ver aunque una normalización falle.
    extracted: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)

    # Quién aprobó el pago y cuándo: `'system'` (checks en verde) o el uuid del operador
    # (1 click u override). NULL = todavía nadie lo aprobó. Es lo que le permite al CRM
    # distinguir "validalo" de "ya está validado y entregado" (server#292).
    approved_at: Mapped[datetime | None] = mapped_column(SADateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(Text)

    # Conciliación humana posterior. Un pago aprobado solo queda "por confirmar" hasta
    # que alguien lo mira contra el banco; el rechazo revoca lo entregado.
    human_confirmed_at: Mapped[datetime | None] = mapped_column(SADateTime(timezone=True))
    human_rejected_at: Mapped[datetime | None] = mapped_column(SADateTime(timezone=True))
    human_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Nota del operador: obligatoria cuando aprueba a mano un comprobante con checks en
    # rojo (un sobrepago legítimo, un pago desde otra cuenta) o cuando lo rechaza.
    human_note: Mapped[str | None] = mapped_column(Text)
