"""Superficie de config del agente (M-Config). Todo exige `platform_operator`.

`GET /agents` lista los agentes del tenant; `GET /agents/{id}` devuelve la config
viva + versión activa; `PUT /agents/{id}` aplica cambios y crea una `AgentVersion`
nueva (auditable). El cliente (client_admin/staff) recibe 403 (RBAC 3 niveles,
core/api/deps.py).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from server.modules.agent.api.config_schemas import (
    AgentRead,
    AgentUpdate,
    AgentVersionRead,
    ModelRead,
)
from server.modules.agent.domain.model_catalog import MODEL_CATALOG
from server.modules.agent.domain.models import Agent, AgentVersion
from server.modules.agent.services.agent_config_service import AgentConfigService
from server.modules.core.api.deps import DbSession, require_platform_operator
from server.modules.core.domain.schemas import AuthenticatedUser

router = APIRouter(prefix="/agents", tags=["agent-config"])

OperatorUser = Annotated[AuthenticatedUser, Depends(require_platform_operator)]


def _to_read(agent: Agent, version: AgentVersion | None) -> AgentRead:
    read = AgentRead.model_validate(agent)
    if version is not None:
        read = read.model_copy(update={"current_version": AgentVersionRead.model_validate(version)})
    return read


@router.get("", response_model=list[AgentRead])
async def list_agents(session: DbSession, ctx: OperatorUser) -> list[AgentRead]:
    agents = await AgentConfigService(session).list_agents(ctx.tenant.id)
    return [_to_read(agent, version) for agent, version in agents]


@router.get("/models", response_model=list[ModelRead])
async def list_models(ctx: OperatorUser) -> list[ModelRead]:
    """Catálogo de modelos permitidos para el dropdown del agente (#103, FE #60).
    Declarado antes de `/{agent_id}` para que "models" no se parsee como UUID."""
    return [ModelRead.model_validate(model) for model in MODEL_CATALOG]


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent_config(agent_id: uuid.UUID, session: DbSession, ctx: OperatorUser) -> AgentRead:
    agent, version = await AgentConfigService(session).get_agent(agent_id, ctx.tenant.id)
    return _to_read(agent, version)


@router.put("/{agent_id}", response_model=AgentVersionRead)
async def update_agent_config(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    session: DbSession,
    ctx: OperatorUser,
) -> AgentVersionRead:
    version = await AgentConfigService(session).update_agent(
        agent_id, ctx.tenant.id, payload, ctx.user.id
    )
    return AgentVersionRead.model_validate(version)
