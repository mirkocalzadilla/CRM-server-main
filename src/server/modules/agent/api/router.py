from fastapi import APIRouter

from server.modules.agent.api.catalog_router import router as catalog_router
from server.modules.agent.api.category_router import router as category_router
from server.modules.agent.api.config_router import router as config_router
from server.modules.agent.api.service_link_router import router as service_link_router

agent_module_router = APIRouter()
agent_module_router.include_router(config_router)
agent_module_router.include_router(catalog_router)
agent_module_router.include_router(category_router)
agent_module_router.include_router(service_link_router)
