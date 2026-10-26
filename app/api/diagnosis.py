"""诊断任务发起、轮询和结论确认。"""
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import repository
from app.diagnosis import runner
router = APIRouter(prefix="/api/diagnosis")

class DiagnosisIn(BaseModel):
    service: str = Field(default="", max_length=128)
    platform: str | None = Field(default=None, max_length=128)
    symptom: str = Field(default="", max_length=512)
    conversation_id: int | None = None

class ConfirmIn(BaseModel):
    confirmed: bool

@router.post("")
async def create_diagnosis(body: DiagnosisIn) -> dict:
    """发起诊断任务:立即返回 id,管线后台跑,前端拿 id 轮询事件流。"""
    service = body.service.strip()
    diag_id = runner.start(service, body.symptom.strip(), conversation_id=body.conversation_id, platform=body.platform)
    await asyncio.sleep(0)   # 让 start 里 create_task 的任务先挂上事件循环
    return {"diagnosis_id": diag_id, "service": service, "status": "running"}

@router.get("/{diag_id}")
async def get_diagnosis(diag_id: str) -> dict:
    # 内存优先(进行中的任务只有内存有);没有再查存档——进程重启后卡片靠这条拿回结论
    run = runner.get(diag_id)
    if run is not None:
        return run
    archived = await repository.get_diagnosis_run(diag_id)
    if archived is not None:
        return archived
    raise HTTPException(404, "诊断任务不存在")

async def _load_run(diag_id: str) -> dict | None:
    return runner.get(diag_id) or await repository.get_diagnosis_run(diag_id)

@router.post("/{diag_id}/confirm")
async def confirm_diagnosis(diag_id: str, body: ConfirmIn) -> dict:
    """用户确认/否认根因。确认前结论只是系统分析,不能拿它启动修复。"""
    live = runner.get(diag_id)
    run = live or await repository.get_diagnosis_run(diag_id)
    if run is None:
        raise HTTPException(404, "诊断任务不存在或还没落库")
    if run.get("status") != "done":
        raise HTTPException(409, "诊断还没结束,不能确认")
    verdict = (run.get("result") or {}).get("verdict")
    if verdict not in ("root_cause", "critical_failure"):
        raise HTTPException(409, "诊断结论不是根因,不需要确认")
    saved = await repository.confirm_diagnosis(diag_id, body.confirmed)
    if saved is None:
        raise HTTPException(404, "诊断任务不存在或还没落库")
    if live is not None:
        live["confirmation"] = saved["confirmation"]
    return {"id": diag_id, "confirmation": saved["confirmation"]}
