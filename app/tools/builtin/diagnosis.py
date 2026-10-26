"""从对话发起诊断，会话身份由系统注入。"""
from typing import Annotated
from langchain_core.tools import InjectedToolArg, tool
from app.db import repository
from app.tools import registry, service_catalog

@tool
async def start_diagnosis(
    service: str = "", symptom: str = "", platform: str = "",
    conversation_id: Annotated[int | None, InjectedToolArg] = None,
) -> dict:
    """调查服务异常的根因。服务未知时留空，系统先扫描平台；多平台同名服务必须指明 platform。
    任务在后台运行，结果由诊断卡展示。只告知任务已发起，不编造结论，不重复发起。
    """
    from app.diagnosis import runner
    service, platform = service.strip(), platform.strip()
    rows = [r for r in await repository.list_targets() if r.enabled]
    if service and any(r.name == service for r in rows):
        platform, service = service, ""
    if platform and not any(r.name == platform for r in rows):
        return {"error": "接入平台不存在或未启用", "code": "not_found"}
    if service:
        services, error = await service_catalog.discover_services()
        if error:
            return {"error": f"服务发现不可用:{error}", "code": "unavailable"}
        hit = service_catalog.find_service(services, service, platform or None)
        if hit is None:
            return {"error": "服务不存在或同名服务分属多个平台，请核对平台；未知服务可留空扫描。", "code": "not_found"}
        service, platform = hit["service_id"], hit["target"]
    diag_id = runner.start(service, symptom.strip(), conversation_id=conversation_id, platform=platform or None)
    return {"diagnosis_id": diag_id, "service": service, "status": "running",
            "note": "任务已发起，结果由诊断卡展示，不要重复发起或预告结论。"}

registry.register(registry.spec_from_langchain_tool(
    start_diagnosis, source="builtin", inject_conversation=True, max_retries=0))
