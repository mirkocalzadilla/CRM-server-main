"""Seed local de M0: producto, template, agente e instancia de WhatsApp para Mirko.

Idempotente (lookup por slug / whatsapp_number antes de crear). Reemplazar
WA_NUMBER por el número real antes de probar contra WhatsApp de verdad.

Uso: uv run python scripts/seed_mirko.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.agent.domain.models import Agent, AgentInstance, AgentTemplate, Product
from server.modules.agent.domain.persona import DEFAULT_PERSONA_PROMPT
from server.modules.core.domain.models import Tenant, TenantUser, TenantUserRole, User
from server.modules.core.repositories.tenant_repository import TenantRepository
from server.modules.crm.seed import seed_crm
from server.shared.database import async_session_maker, dispose_engine
from server.shared.logger import configure_logging, get_logger
from server.shared.security import get_password_hash

logger = get_logger("seed_mirko")

TENANT_SLUG = "mirko"
TENANT_NAME = "Mirko"
PRODUCT_SLUG = "cursos-mirko"
TEMPLATE_SLUG = "ventas-cursos"
AGENT_MODEL = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT = DEFAULT_PERSONA_PROMPT  # persona base (FLUJO_AGENTE §3); editable en /crm
WA_NUMBER = "+59100000000"  # placeholder — reemplazar por el número real de Mirko
HANDOFF_WHATSAPP = "+59100000001"  # placeholder — WhatsApp del equipo de Gestión Postventa
OPERATOR_EMAIL = "operador@mirko.com"  # usuario de dev para probar la API CRM (login)
OPERATOR_PASSWORD = "mirko1234"  # placeholder de desarrollo

# Config que leen las tools (get_service → config["services"][linea]; consultar_faq →
# config["faq"]). Valores placeholder — reemplazar por los datos reales de Mirko.
AGENT_CONFIG: dict[str, object] = {
    "services": {
        "cursos": {
            "descripcion": "Curso de edición y producción audiovisual con Mirko.",
            "precio": "480 Bs",
            "ciudades": ["La Paz"],
            "fechas": ["2026-07-12"],
        },
    },
    "faq": {
        "ubicacion": "La Paz (dirección exacta al confirmar la inscripción).",
        "cupo": "Cupos limitados.",
        "que llevar": "Tu laptop; el resto del equipo lo ponemos nosotros.",
    },
}


async def _get_or_create_tenant(session: AsyncSession) -> Tenant:
    repo = TenantRepository(session)
    tenant = await repo.get_by_slug(TENANT_SLUG)
    if tenant is not None:
        return tenant
    tenant = await repo.add(Tenant(name=TENANT_NAME, slug=TENANT_SLUG))
    await session.flush()
    logger.info("seed.tenant_created", tenant_id=str(tenant.id))
    return tenant


async def _get_or_create_product(session: AsyncSession) -> Product:
    product = await session.scalar(select(Product).where(Product.slug == PRODUCT_SLUG))
    if product is not None:
        return product
    product = Product(slug=PRODUCT_SLUG, display_name="Cursos Mirko", is_active=True)
    session.add(product)
    await session.flush()
    return product


async def _get_or_create_template(session: AsyncSession) -> AgentTemplate:
    template = await session.scalar(
        select(AgentTemplate).where(AgentTemplate.slug == TEMPLATE_SLUG)
    )
    if template is not None:
        return template
    template = AgentTemplate(
        slug=TEMPLATE_SLUG,
        display_name="Ventas de cursos",
        product_slug=PRODUCT_SLUG,
        system_prompt=SYSTEM_PROMPT,
        default_model=AGENT_MODEL,
    )
    session.add(template)
    await session.flush()
    return template


async def _get_or_create_agent(session: AsyncSession, tenant: Tenant) -> Agent:
    agent = await session.scalar(
        select(Agent).where(Agent.organization_id == tenant.id, Agent.product_slug == PRODUCT_SLUG)
    )
    if agent is not None:
        return agent
    agent = Agent(
        organization_id=tenant.id,
        product_slug=PRODUCT_SLUG,
        display_name="Agente Mirko",
        system_prompt=SYSTEM_PROMPT,
        model=AGENT_MODEL,
        config=AGENT_CONFIG,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _get_or_create_operator(session: AsyncSession, tenant: Tenant) -> User:
    """Usuario de dev para probar la API: client_admin + platform_operator. Idempotente.

    Marca ``is_superuser`` (platform_operator) para poder probar también
    config/M-Config, y crea la membresía como ``client_admin`` en el tenant.
    """
    user = await session.scalar(select(User).where(User.email == OPERATOR_EMAIL))
    if user is None:
        user = User(
            email=OPERATOR_EMAIL,
            full_name="Operador Mirko",
            hashed_password=get_password_hash(OPERATOR_PASSWORD),
            is_active=True,
        )
        session.add(user)
        await session.flush()
    user.is_superuser = True
    membership = await session.scalar(
        select(TenantUser).where(TenantUser.tenant_id == tenant.id, TenantUser.user_id == user.id)
    )
    if membership is None:
        session.add(
            TenantUser(tenant_id=tenant.id, user_id=user.id, role=TenantUserRole.CLIENT_ADMIN)
        )
        await session.flush()
    return user


async def _get_or_create_instance(session: AsyncSession, agent: Agent) -> AgentInstance:
    instance = await session.scalar(
        select(AgentInstance).where(AgentInstance.whatsapp_number == WA_NUMBER)
    )
    if instance is not None:
        return instance
    instance = AgentInstance(
        agent_id=agent.id,
        display_name="WhatsApp Mirko",
        whatsapp_number=WA_NUMBER,
        handoff_whatsapp=HANDOFF_WHATSAPP,
    )
    session.add(instance)
    await session.flush()
    return instance


async def seed() -> None:
    async with async_session_maker() as session:
        tenant = await _get_or_create_tenant(session)
        await _get_or_create_product(session)
        await _get_or_create_template(session)
        agent = await _get_or_create_agent(session, tenant)
        instance = await _get_or_create_instance(session, agent)
        await _get_or_create_operator(session, tenant)
        await seed_crm(session, tenant.id)
        await session.commit()

        logger.info(
            "seed.done",
            tenant_id=str(tenant.id),
            product_slug=PRODUCT_SLUG,
            agent_id=str(agent.id),
            instance_id=str(instance.id),
            whatsapp_number=instance.whatsapp_number,
        )


async def main() -> None:
    configure_logging()
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
