"""app_chat_histories: media saliente del takeover (#251)

Revision ID: 0024_app_history_media
Revises: 0023_answered_watermark
Create Date: 2026-07-23 00:00:00.000000+00:00

El staff en takeover solo podía enviar texto. Para adjuntos (imagen/PDF) y el QR
de pago manual, el mensaje humano espejado necesita saber qué media llevó:
`media_type` ('image' | 'document') + `media_url` (URL absoluta enviada por
WhatsApp). NULL = mensaje de texto (todas las filas existentes). Reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_app_history_media"  # <=32 chars: alembic_version.version_num es VARCHAR(32)
down_revision: str | None = "0023_answered_watermark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_chat_histories", sa.Column("media_type", sa.Text(), nullable=True))
    op.add_column("app_chat_histories", sa.Column("media_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_chat_histories", "media_url")
    op.drop_column("app_chat_histories", "media_type")
