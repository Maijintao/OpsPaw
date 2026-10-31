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


# 调用图缓存在进程内:诊断存档里每条的 result 都是几十上百 KB,
# 每次请求都翻十几条去解析,慢到"读取中…"能挂好几秒。
# 图是按平台算的、和具体服务无关,缓存一份就够,按服务过滤在内存里做。
_CTX_CACHE: dict = {"at": 0.0, "target": None, "edges": [], "abnormal": [],
                    "nodes": 0, "from": None}
# 错误日志聚类缓存:键 (平台名, 服务, 分钟窗),60 秒——看板切换服务/时间窗时
# 不至于每次都打一遍 Loki,又不至于陈旧太久
_LOGS_CACHE: dict = {}


async def _metric_fallback_edges(target_id, target_name: str | None) -> list:
    """诊断存档里还没有调用图时的兜底:从 Prometheus 的 span 指标(CLIENT span)
    推一份全平台调用边。只要接了 Prometheus 且有 span 指标就有,不依赖跑过诊断;
    服务清单缓存没热或推不出来就返回空,如实空着不硬画。"""
    from app.tools import grafana as gmod
    svcs = gmod._cache_get(f"svcs:{target_id}", 300)
    if not svcs or not (svcs.get("services") or []):
        return []
    from app.tools.builtin.observability import _resolve
    from app.diagnosis import detect
    row, src = await _resolve("prometheus", target_name)
    if row is None:
        return []
    pairs = await detect.metric_edges(
        {"url": src.get("url"), "headers": src.get("headers")},
        set(svcs["services"]))
    return [{"from": a, "to": b, "calls": 1} for (a, b) in sorted(pairs)]


async def _edge_rates(target_id, target_name: str | None) -> dict:
    """每条调用边的真实流量速率(次/秒,近 10 分钟 rate):给流量图的涌动速度做刻度。

    来自 Prometheus spanmetrics 的 CLIENT span(与诊断层边级错误归属同一数据源),
    操作名经段级匹配归到被调服务。查不到(Loki 没接 span 指标/平台未接 Prometheus)
    返回空,前端退回用诊断存档的调用量做相对刻度——如实缺,不编数。
    """
    from app.tools import grafana as gmod
    hit = gmod._cache_get(f"edgerate:{target_id}", 300)
    if hit is not None:
        return hit
    from app.tools.builtin.observability import _resolve
    from app.diagnosis import detect
    row, src = await _resolve("prometheus", target_name)
    if row is None:
        gmod._cache_set(f"edgerate:{target_id}", {})
        return {}
    prom = {"url": src.get("url"), "headers": src.get("headers")}
    try:
        info = await detect._discover(prom)
        if info is None:
            gmod._cache_set(f"edgerate:{target_id}", {})
            return {}
        kind = info.get("kind_label") or "span_kind"
        status = info.get("status_label") or "status_code"
        svc_label = info["service_label"]
        expr = (f'sum by ({svc_label}, span_name) (rate({info["family"]}'
                f'{{{kind}=~"SPAN_KIND_CLIENT|client"}}[600s]))')
        rows = await detect._query(prom, expr)
    except Exception:  # noqa: BLE001
        gmod._cache_set(f"edgerate:{target_id}", {})
        return {}
    # 已知服务集合:用平台服务清单(缓存热的),集合为空就归不出被调方,如实返回空
    svcs = gmod._cache_get(f"svcs:{target_id}", 300) or {}
    known = sorted(svcs.get("services") or [])
    if not known:
        gmod._cache_set(f"edgerate:{target_id}", {})
        return {}
    import re as _re
    canon = {_re.sub(r"[^a-z0-9]", "", k.lower()): k for k in known}
    out: dict = {}
    for r in rows or []:
        m = r.get("metric") or {}
        caller = m.get(svc_label) or ""
        op = m.get("span_name") or ""
        v = float((r.get("value") or [0, 0])[1] or 0)
        if not caller or v <= 0:
            continue
        callee = None
        for seg in _re.split(r"[^a-z0-9]+", op.lower()):
            key = _re.sub(r"[^a-z0-9]", "", seg)
            if not key:
                continue
            if key in canon:                       # 段==服务名(flagd → flagd)
                callee = canon[key]
                break
            if key.endswith("service") and key[:-7] in canon:
                callee = canon[key[:-7]]           # 段==服务名+service(recommendationservice → recommendation)
                break
        if callee and callee != caller:
            k = (caller, callee)
            out[k] = out.get(k, 0.0) + v
    gmod._cache_set(f"edgerate:{target_id}", out)
    return out


async def _error_log_clusters(service: str, target_name: str | None,
                              minutes: int) -> dict:
    """Loki 错误日志按规则聚类(连接被拒 12 条、超时 3 条…):规则是通用正则,
    不挑环境;Loki 没接/查不到就如实带一句说明,不编。"""
    import time as _t
    key = (target_name, service, minutes)
    hit = _LOGS_CACHE.get(key)
    if hit and _t.time() - hit["at"] < 60:
        return hit["data"]
    from app.tools.builtin.observability import query_platform_logs
    from app.diagnosis import logsig
    data: dict
    try:
        # query_platform_logs 是 LangChain 工具对象(StructuredTool),不是裸函数,
        # 直接 call 会 TypeError——用 ainvoke 走它的异步实现
        out = await query_platform_logs.ainvoke({
            "service": service, "minutes": minutes,
            "filter_text": "error", "target_name": target_name})
        if out.get("error"):
            data = {"note": out["error"], "clusters": [], "total": 0, "minutes": minutes}
        else:
            cl = logsig.classify(out.get("lines") or [])
            labels = sorted((cl.get("labels") or {}).items(),
                            key=lambda kv: (-kv[1], kv[0]))
            data = {"total": out.get("total") or 0, "minutes": minutes,
                    "clusters": [{"label": k, "count": v,
                                  "sample": (cl.get("samples") or {}).get(k, "")}
                                 for k, v in labels[:6]]}
    except Exception as exc:  # noqa: BLE001
        data = {"note": f"日志查询失败:{type(exc).__name__}",
                "clusters": [], "total": 0, "minutes": minutes}
    _LOGS_CACHE[key] = {"at": _t.time(), "data": data}
    return data


def _parse_result(raw) -> dict:
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return raw if isinstance(raw, dict) else {}


def _scan_graph(rows, target_id) -> None:
    """从最近的诊断存档里找一份带调用图的,缓存下来。"""
    import time as _t
    for r in rows:
        graph = _parse_result(r.result).get("full_graph") or {}
        edges = graph.get("edges") or []
        if not edges:
            continue
        _CTX_CACHE.update({
            "at": _t.time(), "target": target_id, "edges": edges,
            "abnormal": [n["service"] for n in (graph.get("nodes") or []) if n.get("abnormal")],
            "nodes": len(graph.get("nodes") or []), "from": r.id,
        })
        return
    # 没找到也记一次时间,免得每次请求都翻一遍库
    _CTX_CACHE.update({"at": _t.time(), "target": target_id, "edges": [],
                       "abnormal": [], "nodes": 0, "from": None})


@router.get("/api/grafana/context")
async def grafana_context(service: str = "", target_id: int | None = None,
                          minutes: int = 60):
    """服务监控页下方两块:上下游调用图(大图) + 最近事件·错误日志。

    调用图优先取**最近一次诊断存下的调用图**(采集时真跑出来的),没有存档时
    从 Prometheus 的 span 指标现推一份兜底——接了 Prometheus 就有,不依赖跑过诊断。
    错误日志聚类走 Loki(带服务标签即可,规则是通用正则)。
    """
    import time as _t
    from sqlalchemy import select
    from app.db.base import async_session
    from app.db.models import DiagnosisRun, IntakeEvent, Sentinel

    # 缓存过期(5 分钟)或换了平台才翻库
    if not _CTX_CACHE["at"] or _t.time() - _CTX_CACHE["at"] > 300 \
            or _CTX_CACHE["target"] != target_id:
        async with async_session() as sess:
            rows = (await sess.execute(
                select(DiagnosisRun).order_by(DiagnosisRun.created_at.desc())
                .limit(12))).scalars().all()
        _scan_graph(rows, target_id)

    # 没给平台就用第一个启用的(前端监控页默认不传),后续缓存键统一用解析后的 id
    if target_id is None:
        _rows = [r for r in await repository.list_targets() if r.enabled]
        target_id = _rows[0].id if _rows else None

    edges = _CTX_CACHE["edges"]
    # 诊断存档还没有图 → 指标侧兜底,结果也进 5 分钟缓存(图是全平台的,按服务过滤在前端做)
    graph_from = "diagnosis" if _CTX_CACHE["from"] else None
    if not edges:
        _tgt = await repository.get_target(target_id) if target_id else None
        _name = _tgt.name if _tgt else None
        try:
            edges = await _metric_fallback_edges(target_id, _name)
        except Exception:  # noqa: BLE001
            edges = []
        if edges:
            import time as _t2
            _CTX_CACHE.update({"at": _t2.time(), "target": target_id, "edges": edges,
                               "abnormal": [], "nodes": len({x for e in edges
                                                             for x in (e["from"], e["to"])}),
                               "from": "metrics"})
            graph_from = "metrics"

    graph = {"nodes": [], "edges": edges, "abnormal": _CTX_CACHE["abnormal"],
             "from": graph_from}
    if edges:
        graph["nodes"] = sorted({x for e in edges for x in (e.get("from"), e.get("to"))})
    # 真实速率刻度:查得到就挂到每条边上(次/秒),流量图的涌动速度/粗细按真实量走;
    # 查不到就让前端退回调用量相对刻度——如实缺数,不编
    try:
        _tgt_rate = await repository.get_target(target_id) if target_id else None
        _rates = await _edge_rates(target_id, _tgt_rate.name if _tgt_rate else None)
    except Exception:  # noqa: BLE001
        _rates = {}
    if _rates:
        graph["edges"] = [{**e, "rate": round(_rates.get((e.get("from"), e.get("to")), 0.0), 3)}
                          for e in edges]

    topo = {"available": bool(edges), "from_diagnosis": _CTX_CACHE["from"],
            "reason": "" if edges else "还没有调用关系:跑一次诊断,或等指标侧 span 数据积累",
            "upstream": [], "downstream": [], "abnormal": [], "node_count": len(graph["nodes"])}
    if edges:
        names = {e.get("from") for e in edges} | {e.get("to") for e in edges}
        svc = service if (service and service in names) else ""
        if not svc:
            # 没指定服务(选了"全部")时,用图上出现最多的那个当中心,免得空着
            from collections import Counter
            cnt = Counter([e.get("to") for e in edges])
            svc = cnt.most_common(1)[0][0] if cnt else ""
        if svc:
            topo = {
                "available": True, "from_diagnosis": _CTX_CACHE["from"], "service": svc,
                "upstream": sorted({e["from"] for e in edges if e.get("to") == svc}),
                "downstream": sorted({e["to"] for e in edges if e.get("from") == svc}),
                "abnormal": sorted(set(_CTX_CACHE["abnormal"])),
                "node_count": _CTX_CACHE["nodes"],
            }

    events: list[dict] = []
    async with async_session() as sess:
        srows = (await sess.execute(select(Sentinel).order_by(Sentinel.id))).scalars().all()
        mine = {x.id: x.name for x in srows
                if not service or not x.services or service in (x.services or [])}
        if mine:
            erows = (await sess.execute(
                select(IntakeEvent).where(IntakeEvent.intake_id.in_(list(mine)))
                .order_by(IntakeEvent.id.desc()).limit(12))).scalars().all()
            events = [{"at": e.fired_at.isoformat() if e.fired_at else "",
                       "intake": mine.get(e.intake_id, ""), "trigger": e.trigger,
                       "service": e.service, "diagnosis_id": e.diagnosis_id,
                       "skipped": e.skipped} for e in erows]

    # 错误日志聚类(要具体服务;"全部"时提示先选服务;窗口跟随看板时间档,
    # 日志通道本身 5~120 分钟有效,超界夹紧)
    logs: dict = {"note": "选一个具体服务后展示错误日志聚类", "clusters": [],
                  "total": 0, "minutes": minutes}
    if service:
        if target_id is None:
            _rows = [r for r in await repository.list_targets() if r.enabled]
            _tgt2 = _rows[0] if _rows else None
        else:
            _tgt2 = await repository.get_target(target_id)
        logs = await _error_log_clusters(service, _tgt2.name if _tgt2 else None,
                                         max(5, min(int(minutes or 60), 120)))

    return {"service": topo.get("service") or service, "topology": topo,
            "graph": graph, "events": events, "logs": logs}
