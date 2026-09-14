"""rbac 3 niveles: tenant_user_role owner/admin/member -> client_admin/staff

Recrea el tipo enum ``tenant_user_role`` (Postgres no permite quitar valores
in-place). Migra los datos existentes: ``owner``/``admin`` -> ``client_admin``,
``member`` -> ``staff``. El ``platform_operator`` NO vive en este enum: es la
dimensión global ``users.is_superuser``, que se marca aparte (seed/ABM), no acá.

Revision ID: 0007_rbac_3_niveles
Revises: c4f1a2b3d5e6
Create Date: 2026-06-06 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_rbac_3_niveles"
down_revision: str | None = "c4f1a2b3d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE tenant_user_role_new AS ENUM ('client_admin', 'staff')")
    op.execute("ALTER TABLE tenant_users ALTER COLUMN role DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE tenant_users
        ALTER COLUMN role TYPE tenant_user_role_new
        USING (
            CASE role::text
                WHEN 'owner' THEN 'client_admin'
                WHEN 'admin' THEN 'client_admin'
                WHEN 'member' THEN 'staff'
            END
        )::tenant_user_role_new
        """
    )
    op.execute("DROP TYPE tenant_user_role")
    op.execute("ALTER TYPE tenant_user_role_new RENAME TO tenant_user_role")
    op.execute("ALTER TABLE tenant_users ALTER COLUMN role SET DEFAULT 'staff'")


def downgrade() -> None:
    # Best-effort: client_admin -> admin, staff -> member (la distinción owner se pierde).
    op.execute("CREATE TYPE tenant_user_role_old AS ENUM ('owner', 'admin', 'member')")
    op.execute("ALTER TABLE tenant_users ALTER COLUMN role DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE tenant_users
        ALTER COLUMN role TYPE tenant_user_role_old
        USING (
            CASE role::text
                WHEN 'client_admin' THEN 'admin'
                WHEN 'staff' THEN 'member'
            END
        )::tenant_user_role_old
        """
    )
    op.execute("DROP TYPE tenant_user_role")
    op.execute("ALTER TYPE tenant_user_role_old RENAME TO tenant_user_role")
    op.execute("ALTER TABLE tenant_users ALTER COLUMN role SET DEFAULT 'member'")
