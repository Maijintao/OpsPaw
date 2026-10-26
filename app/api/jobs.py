from fastapi import APIRouter, HTTPException
from app.core import jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

@router.get("")
async def list_jobs():
    return {"jobs": jobs.status_all()}

@router.post("/{name}")
async def start_job(name: str):
    if name not in jobs.JOBS:
        raise HTTPException(404, "作业不存在")
    try:
        await jobs.start(name)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return jobs.status(name)

@router.get("/{name}")
async def get_job(name: str):
    if name not in jobs.JOBS:
        raise HTTPException(404, "作业不存在")
    return jobs.status(name)

@router.post("/{name}/stop")
async def stop_job(name: str):
    if name not in jobs.JOBS:
        raise HTTPException(404, "作业不存在")
    try:
        await jobs.stop(name)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return jobs.status(name)
