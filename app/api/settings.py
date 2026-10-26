from fastapi import APIRouter
from app.config import settings
from app.core.observability import langfuse_enabled

router = APIRouter(prefix="/api/settings", tags=["settings"])

@router.get("")
async def get_settings():
    return {"chat": {"configured": bool(settings.chat_base_url and settings.chat_api_key and settings.chat_model)},
            "database": {"configured": bool(settings.database_url)},
            "langfuse": {"configured": langfuse_enabled()},
            "repair_public_base_url": {"configured": bool(settings.repair_public_base_url)}}
