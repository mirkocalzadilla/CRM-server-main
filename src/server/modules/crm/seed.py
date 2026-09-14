"""Seed del tablero CRM por organización (M-CRM-data). Idempotente.

Estados globales (`open`/`won`/`lost`) + los 2 pipelines (venta = espejo del funnel;
postventa = post-handoff) con sus stages ordenadas. Reusable por cualquier
org; lo invoca `scripts/seed_mirko.py`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.crm.domain import stages as stage_names
from server.modules.crm.domain.models import Pipeline, Stage, StageStatus

_STAGE_STATUSES: list[tuple[str, str, str | None]] = [
    ("open", "Abierto", None),
    ("won", "Ganado", "#16a34a"),
    ("lost", "Perdido", "#dc2626"),
]

# (kind, name, position, [(stage_name, status_code), ...]); F2 (Asistió/No asistió/Lost) diferido.
_PIPELINES: list[tuple[str, str, int, list[tuple[str, str]]]] = [
    (
        stage_names.PIPELINE_IA,
        stage_names.PIPELINE_IA_NAME,
        1,
        [
            (stage_names.IA_NEW, "open"),
            (stage_names.IA_ENGAGING, "open"),
            (stage_names.IA_QUALIFYING, "open"),
            (stage_names.IA_QUALIFIED, "open"),
            (stage_names.IA_DISQUALIFIED, "lost"),
        ],
    ),
    (
        stage_names.PIPELINE_HUMAN,
        stage_names.PIPELINE_HUMAN_NAME,
        2,
        [
            (stage_names.HUMAN_INTAKE, "open"),
            (stage_names.PAYMENT_VALIDATION, "open"),
            (stage_names.PAYMENT_VALIDATED, "open"),
            (stage_names.DELIVERED, "open"),
            (stage_names.CLOSED, "won"),
            (stage_names.HUMAN_LOST, "lost"),
        ],
    ),
]


async def seed_crm(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Estados globales + 2 pipelines con sus stages para la organización. Idempotente."""
    for code, name, color in _STAGE_STATUSES:
        if await session.get(StageStatus, code) is None:
            session.add(StageStatus(code=code, name=name, color=color))
    await session.flush()

    for kind, name, position, stages in _PIPELINES:
        existing = await session.scalar(
            select(Pipeline).where(
                Pipeline.organization_id == organization_id, Pipeline.kind == kind
            )
        )
        if existing is not None:
            continue
        pipeline = Pipeline(
            organization_id=organization_id, kind=kind, name=name, position=position
        )
        session.add(pipeline)
        await session.flush()
        for pos, (stage_name, status_code) in enumerate(stages, start=1):
            session.add(
                Stage(
                    pipeline_id=pipeline.id, name=stage_name, position=pos, status_code=status_code
                )
            )
    await session.flush()
