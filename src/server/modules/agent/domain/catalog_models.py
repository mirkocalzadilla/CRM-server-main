"""Modelos del catálogo de servicios + categorías + materiales (SPEC_admin_catalogo_kb §4).

`service` = catálogo curado del agente (lo que el operador carga desde la UI);
`service_category` = categoría del catálogo (ABM por organización, #106: solo
nombre + orden; el material/PDF y el slug son del servicio, no de la categoría);
`asset` = material subido (PDF/imagen) hosteado en `media_root` y enviado por URL
pública. Identificadores estructurales en inglés; los campos de negocio siguen el
vocabulario del catálogo curado (§2.2) para que el snapshot publicado (§5.1) sea
una proyección directa. Multi-tenant: todo scoped por `organization_id`.

`deleted_at` = baja lógica (soft delete): la fila nunca se borra para que el
historial del contacto (#99) y `card_service` (#132) sigan resolviendo el nombre
del servicio aunque el operador lo elimine del catálogo.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin

SERVICE_CLOSINGS = ("pago_qr", "handoff_consultivo")
SERVICE_CURRENCIES = ("BOB", "USD")
ASSET_KINDS = ("pdf", "image")
# Modalidad de entrega post-pago. `NULL` (ausente) = el servicio no entrega nada al
# pagar: es el default seguro y cubre todo lo que no es un curso.
SERVICE_MODALITIES = ("presencial", "virtual")
# Tipos de link que se le pueden mandar al lead como parte de la entrega.
SERVICE_LINK_KINDS = ("whatsapp_group", "meeting", "maps", "other")


class Asset(Base, UUIDPrimaryKeyMixin):
    """Material subido (PDF/imagen) hosteado en `media_root`; el bot lo manda por URL.

    `category_id` lo enlaza a una categoría (≤5 materiales por categoría, #235); queda
    `NULL` mientras el material se sube pero la categoría aún no se guarda. `service_id`
    es el enlace legacy a un servicio (#108, ya no se usa para nuevos materiales) que se
    conserva por el histórico.
    """

    __tablename__ = "asset"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service.id", ondelete="SET NULL"), index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_category.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # pdf | image
    filename: Mapped[str] = mapped_column(Text, nullable=False)  # ASCII, visible al lead
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)  # path en media_root
    public_url: Mapped[str] = mapped_column(Text, nullable=False)  # URL HTTPS para Meta
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ServiceCategory(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Categoría del catálogo (ABM por organización, #106).

    `nombre` + `orden` + `slug` (clave estable por org que el agente usa en
    `enviar_material`, #235) + sus materiales (PDF/imagen). Borrar una categoría deja
    los servicios en `category_id = NULL` (sin categoría, FK `ON DELETE SET NULL`) y
    borra sus materiales (FK `asset.category_id ON DELETE CASCADE`).
    """

    __tablename__ = "service_category"
    __table_args__ = (
        UniqueConstraint("organization_id", "nombre"),
        UniqueConstraint("organization_id", "slug", name="uq_service_category_org_slug"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ≤5 materiales por categoría (#235), ordenados por subida.
    materials: Mapped[list[Asset]] = relationship(
        order_by=Asset.created_at, lazy="selectin", foreign_keys=Asset.category_id
    )


class Service(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Servicio del catálogo de un agente. `slug` único por agente."""

    __tablename__ = "service"
    __table_args__ = (UniqueConstraint("agent_id", "slug"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_category.id", ondelete="SET NULL")
    )
    resumen: Mapped[str] = mapped_column(Text, nullable=False)
    detalle: Mapped[str | None] = mapped_column(Text)
    precio: Mapped[str] = mapped_column(Text, nullable=False)
    moneda: Mapped[str] = mapped_column(Text, nullable=False)
    flujo_cierre: Mapped[str] = mapped_column(Text, nullable=False, default="pago_qr")
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Qué se entrega al validarse el pago: presencial (entrada QR + ubicación),
    # virtual (links) o NULL = nada. NULL es el default seguro para todo servicio
    # que no sea un curso; sin modalidad no hay entrega automática.
    modality: Mapped[str | None] = mapped_column(Text)
    # Precio como número, para comparar contra el monto de un comprobante. `precio`
    # queda como el texto que ve el lead (admite rangos y otras monedas y por eso no
    # sirve para validar). NULL = precio no comparable ⇒ validación humana.
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # El material vive en la categoría (#235), no en el servicio.
    category: Mapped[ServiceCategory | None] = relationship(lazy="joined")
    # Links de entrega (grupo, llamada, ubicación). NO se proyectan al snapshot del
    # agente a propósito: el LLM no debe poder entregarlos antes de que el pago esté
    # validado. Los lee el fulfillment desde la DB.
    links: Mapped[list["ServiceLink"]] = relationship(
        back_populates="service",
        cascade="all, delete-orphan",
        order_by="ServiceLink.orden",
        lazy="selectin",
    )


class ServiceLink(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Link tipado que se le manda al lead como entrega de un servicio.

    `kind` decide para qué sirve (grupo de WhatsApp, reunión Meet/Zoom, ubicación en
    Maps, otro) y con eso el fulfillment arma el mensaje: un curso virtual entrega
    grupo y/o llamada; uno presencial, la ubicación junto con la entrada. Un link
    requerido que falta nunca se manda a medias: cae a revisión humana.
    """

    __tablename__ = "service_link"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('whatsapp_group','meeting','maps','other')",
            name="service_link_kind_check",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    service: Mapped[Service] = relationship(back_populates="links")
