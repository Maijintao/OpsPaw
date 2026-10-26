from pydantic import BaseModel
from fastapi import APIRouter
from app.core import jobs
from app.db import retention

router = APIRouter(prefix="/api/admin", tags=["admin"])

@router.get("/overview")
async def overview():
    return {"jobs": jobs.status_all(), "retention": await retention.get_policy()}

class RetentionBody(BaseModel):
    enabled: bool = False
    clear_days: int
    slim_days: int
    intake_days: int

@router.get("/retention")
async def retention_get():
    return await retention.get_policy()

@router.post("/retention")
async def retention_save(body: RetentionBody):
    return await retention.save_policy(body.enabled, body.clear_days,
                                       body.slim_days, body.intake_days)
