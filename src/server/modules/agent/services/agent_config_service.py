"""Edición versionada del agente (SPEC_M-Config §3.A).

Cada `update_agent` aplica los cambios a los campos vivos de `Agent`, crea una
`AgentVersion` nueva (snapshot completo post-cambio, `version_number = max+1`,
`created_by = operador`) y apunta `current_version_id` a ella — todo en una
transacción. Sin draft ni rollback en MVP (Fase 2).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.api.config_schemas import AgentUpdate
from server.modules.agent.domain.models import Agent, AgentVersion
from server.modules.agent.repositories.agent_repository import AgentRepository
from server.shared.exceptions import NotFoundException

# Nodos que ya no se cargan a mano en el form: ofertas/resumen vienen del Catálogo
# de Servicios (`config.services`, vía CatalogService.publish) y `faq` queda
# deprecado como nodo manual (#105). Se descartan del config entrante (no se
# rechaza, para no acoplar el deploy del server al del front #62).
_MANUAL_CATALOG_NODES = frozenset({"ofertas", "faq", "resumen"})
# Keys server-owned: las escribe la proyección del catálogo (`CatalogSnapshotService`),
# el form no las maneja y un guardado nunca debe pisar el snapshot publicado.
#
# **Son tres, no una.** Cuando se agregaron `categories` (#235, materiales por categoría)
# y `events` (#276, las fechas de cada curso), esta lista se quedó con `services`: guardar
# el form del agente las borraba del contexto del bot hasta que alguien volviera a tocar
# el catálogo o la agenda. El síntoma no es un error, es el bot diciendo que no tiene
# fechas o no encontrando el material de una categoría.
_SERVER_OWNED = frozenset({"services", "categories", "events"})


class AgentConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AgentRepository(session)

    async def get_agent(
        self, agent_id: uuid.UUID, organization_id: uuid.UUID
    ) -> tuple[Agent, AgentVersion | None]:
        """Agente org-scoped + su versión activa (None si nunca fue editado)."""
        agent = await self.repo.get(agent_id, organization_id)
        if agent is None:
            raise NotFoundException(f"Agente {agent_id} no encontrado")
        version = await self._active_version(agent)
        return agent, version

    async def list_agents(
        self, organization_id: uuid.UUID
    ) -> list[tuple[Agent, AgentVersion | None]]:
        """Agentes del tenant + versión activa de cada uno (uno por tenant en el MVP)."""
        agents = await self.repo.list_for_organization(organization_id)
        return [(agent, await self._active_version(agent)) for agent in agents]

    @staticmethod
    def _merge_config(current: dict[str, object], incoming: dict[str, object]) -> dict[str, object]:
        """Config entrante del form, sin los nodos manuales del catálogo y sin pisar las
        keys server-owned (la proyección del catálogo se preserva) (#105).

        Las server-owned se toman **siempre de `current`** y nunca de lo que llegue: si el
        form manda una copia, es la que leyó cuando se abrió la pantalla, y guardarla
        revertiría cualquier cambio del catálogo hecho en el medio.
        """
        merged = {
            key: value
            for key, value in incoming.items()
            if key not in _MANUAL_CATALOG_NODES and key not in _SERVER_OWNED
        }
        for key in _SERVER_OWNED:
            if key in current:
                merged[key] = current[key]
        return merged

    async def _active_version(self, agent: Agent) -> AgentVersion | None:
        if agent.current_version_id is None:
            return None
        return await self.repo.get_version(agent.current_version_id)

    async def update_agent(
        self,
        agent_id: uuid.UUID,
        organization_id: uuid.UUID,
        payload: AgentUpdate,
        operator_id: uuid.UUID,
    ) -> AgentVersion:
        agent = await self.repo.get(agent_id, organization_id)
        if agent is None:
            raise NotFoundException(f"Agente {agent_id} no encontrado")

        if payload.display_name is not None:
            # Identidad del agente (no versionada: no está en el snapshot agent_version).
            agent.display_name = payload.display_name
        if payload.system_prompt is not None:
            agent.system_prompt = payload.system_prompt
        if payload.model is not None:
            agent.model = payload.model
        if payload.config is not None:
            agent.config = self._merge_config(agent.config, payload.config)

        version = await self.repo.add_version(
            AgentVersion(
                agent_id=agent.id,
                version_number=await self.repo.next_version_number(agent.id),
                system_prompt=agent.system_prompt,
                model=agent.model,
                tools=agent.tools,
                config=agent.config,
                change_summary=payload.change_summary,
                created_by=operator_id,
            )
        )
        agent.current_version_id = version.id
        await self.session.commit()
        return version
