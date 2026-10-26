import json
from pathlib import Path
from fastapi import APIRouter
from app.core.observability import langfuse_enabled

router = APIRouter(prefix="/api/observability", tags=["observability"])
_REPORT = Path(__file__).resolve().parents[2] / "data/ch09/reports/cost_by_intent.json"

@router.get("/overview")
async def overview():
    report = None
    try:
        report = json.loads(_REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {"configured": langfuse_enabled(), "report": report,
            "status": "ready" if report else ("not_configured" if not langfuse_enabled() else "not_generated")}
