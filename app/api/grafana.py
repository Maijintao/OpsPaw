"""Grafana 面板接口:按平台自动生成面板,给前端嵌 iframe 用。"""
import logging

from fastapi import APIRouter, HTTPException

from app.db import repository
from app.tools import grafana as grafana_mod

logger = logging.getLogger("app")
router = APIRouter()


@router.get("/api/grafana/dashboard")
async def grafana_dashboard(target_id: int | None = None):
    """确保该平台有一份面板,返回 uid / 地址 / 服务清单。

    不传 target_id 时取第一个启用的平台——前端只有一个 Grafana 入口,
    多平台的情况留给后续在页面上选(现在的靶子只有一个)。
    """
    if target_id is None:
        rows = [r for r in await repository.list_targets() if r.enabled]
        if not rows:
            raise HTTPException(status_code=404, detail="没有启用的接入平台")
        target_id = rows[0].id
    try:
        return await grafana_mod.ensure_dashboard(target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001  Grafana 不可达/拒绝,如实回
        logger.warning("生成 Grafana 面板失败 target=%s: %s", target_id, exc)
        raise HTTPException(status_code=502,
                            detail=f"Grafana 不可用:{type(exc).__name__}") from exc


@router.get("/api/grafana/series")
async def grafana_series(service: str = "", target_id: int | None = None, minutes: int = 60):
    """五张图的曲线(按服务分组)。service 传空 = 全部服务,不指定高亮。"""
    if target_id is None:
        rows = [r for r in await repository.list_targets() if r.enabled]
        if not rows:
            raise HTTPException(status_code=404, detail="没有启用的接入平台")
        target_id = rows[0].id
    try:
        return await grafana_mod.series(target_id, service, max(5, min(minutes, 360)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("取指标曲线失败 service=%s: %s", service, exc)
        raise HTTPException(status_code=502,
                            detail=f"取数失败:{type(exc).__name__}") from exc


@router.get("/api/grafana/targets")
async def grafana_targets():
    """可选的接入平台清单:以后客户接第二个平台,这里自动多一项。"""
    rows = await repository.list_targets()
    return {"items": [{"id": r.id, "name": r.name}
                      for r in rows if r.enabled]}


@router.get("/api/grafana/services")
async def grafana_services(target_id: int | None = None):
    """该平台的服务清单(轻量:只读服务目录,不生成面板)。"""
    if target_id is None:
        rows = [r for r in await repository.list_targets() if r.enabled]
        if not rows:
            raise HTTPException(status_code=404, detail="没有启用的接入平台")
        target_id = rows[0].id
    from app.db import repository as repo2
    from app.tools import service_catalog
    from app.tools import grafana as gmod
    hit = gmod._cache_get(f"svcs:{target_id}", 300)
    if hit is not None:
        return hit
    tgt = await repo2.get_target(target_id)
    if tgt is None:
        raise HTTPException(status_code=404, detail="接入平台不存在")
    try:
        services, _err = await service_catalog.discover_services()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"取服务清单失败:{type(exc).__name__}") from exc
    names = sorted({x["service_id"] for x in services if x.get("target") == tgt.name})
    out = {"services": names, "target": tgt.name}
    gmod._cache_set(f"svcs:{target_id}", out)
    return out
