"""merge T1 T2

Revision ID: 33feacab726f
Revises: 0002_magic_link, 0003_agent_initial
Create Date: 2026-05-29 01:40:48.502146+00:00

"""
from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '33feacab726f'
down_revision: str | None = ('0002_magic_link', '0003_agent_initial')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
