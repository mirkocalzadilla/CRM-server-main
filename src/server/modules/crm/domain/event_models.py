"""Eventos: la fecha y el lugar concretos a los que da acceso una entrada.

Tabla dedicada y no campos dentro de `Service` porque son dos ciclos de vida distintos:
un servicio es **lo que se vende** y dura mientras esté en el catálogo; un evento es una
**fecha que ocurre y pasa**. Un mismo curso presencial tiene varias ediciones al año, y
cada una con su lugar y su cupo.

Es lo que hace posible el control en la puerta: sin evento, un QR no se puede rechazar
por "es de otra fecha" — y una entrada que no se puede rechazar no controla nada.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy import DateTime as SADateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.modules.agent.domain.catalog_models import Service
from server.shared.base_model import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Estados del evento. `scheduled` = agendado a futuro; `active` = en curso o inminente,
# es el que recibe las entradas nuevas; `closed` = terminado, ya no se le emiten.
EVENT_STATUSES = ("scheduled", "active", "closed")
EVENT_SCHEDULED = "scheduled"
EVENT_ACTIVE = "active"
EVENT_CLOSED = "closed"


class Event(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Una edición concreta de un servicio presencial: cuándo, dónde y para cuántos."""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint("status IN ('scheduled','active','closed')", name="event_status_check"),
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
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(SADateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    # Ubicación propia del evento. Cuando está cargada **pisa** el link de maps del
    # servicio: el curso puede ser el mismo y la sede cambiar de una edición a otra.
    maps_url: Mapped[str | None] = mapped_column(Text)
    # Cupo. `NULL` = sin límite; con límite, al llenarse no se emiten más entradas solas.
    capacity: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=EVENT_SCHEDULED)

    # El servicio del que este evento es una edición. Lo usa la proyección al snapshot
    # del agente, que necesita el slug para relacionar la fecha con el curso.
    service: Mapped[Service] = relationship(lazy="joined")
