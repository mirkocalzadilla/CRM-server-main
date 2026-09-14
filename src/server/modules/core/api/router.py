from fastapi import APIRouter

from server.modules.core.api.auth_router import router as auth_router
from server.modules.core.api.tenant_router import router as tenant_router
from server.modules.core.api.user_router import router as user_router

core_router = APIRouter()
core_router.include_router(auth_router)
core_router.include_router(user_router)
core_router.include_router(tenant_router)
