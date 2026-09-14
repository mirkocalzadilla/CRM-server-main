"""Captura automática del servicio que el bot le envió al lead (#133).

Implementa `ServiceCapturePort` (agent/domain/ports): tras un turno donde el agente
mandó el material de uno o más servicios (`enviar_material`), estampa esos servicios
en la card de la conversación como `card_service` con `source='captured'`. Best-effort
e idempotente: un servicio ya enlazado (asignado o capturado) no se duplica. Es el
puente agente→crm, cableado en `dispatch_handler` (el módulo del agente no toca crm).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.agent_state import State
from server.modules.agent.repositories.service_repository import ServiceRepository
from server.modules.crm.repositories.card_repository import CardRepository
from server.modules.crm.repositories.card_service_repository import CardServiceRepository


class ServiceCaptureService:
    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def on_captured(self, state: State, slugs: tuple[str, ...]) -> None:
        card = await CardRepository(self._session).get_by_conversation(state.conversation_id)
        if card is None or card.organization_id != state.tenant_id:
            return
        services = ServiceRepository(self._session)
        card_services = CardServiceRepository(self._session)
        linked = {
            row.service_id for row in await card_services.list_for_card(card.id, state.tenant_id)
        }
        captured = False
        for slug in dict.fromkeys(slugs):  # dedup, preserva orden
            service = await services.get_by_slug(state.agent_id, slug)
            if service is None or service.organization_id != state.tenant_id:
                continue
            if service.id in linked:
                continue  # ya asignado o capturado: no duplicar
            await card_services.capture(card.id, state.tenant_id, service.id)
            linked.add(service.id)
            captured = True
        if captured:
            await self._session.commit()
