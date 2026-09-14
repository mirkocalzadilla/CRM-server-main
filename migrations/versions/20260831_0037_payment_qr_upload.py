"""payment_settings.payment_qr_storage_ref — QR de pago subido, no linkeado

Revision ID: 0037_payment_qr_upload
Revises: 0036_card_attended
Create Date: 2026-08-31 00:00:00.000000+00:00

Hasta ahora el QR propio de una organización era una URL que el operador pegaba a
mano, apuntando a un hosting ajeno al sistema (un raw de GitHub). El archivo pasa a
vivir adentro (`media_root/payments/{org}/`), y para reemplazarlo o borrarlo hay que
saber **qué archivo del disco es de esa organización**.

Esa es la columna: el path relativo bajo `media_root`. Deducirlo desde
`payment_qr_url` no sirve — `media_base_url` cambia entre entornos y la URL también
puede ser externa (las que ya están configuradas en prod lo son). Con el ref, borrar
el anterior es exacto; sin él, sería adivinar.

NULL = el QR vigente no es un archivo nuestro (link externo previo, o el global).
Nada que backfillear: las filas existentes quedan como están y siguen funcionando.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037_payment_qr_upload"
down_revision: str | None = "0036_card_attended"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment_settings", sa.Column("payment_qr_storage_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_settings", "payment_qr_storage_ref")
