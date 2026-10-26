"""监控面板使用的指标发现与查询。"""
import re
import httpx
SCAN_TIMEOUT = 10.0
RANGE_TIMEOUT = 150.0
_RETRY_ERRORS = (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError,
                 httpx.RemoteProtocolError)
_CALL_FAMILY_HINTS = ("traces_span_metrics_calls_total",)
_CALL_FAMILY_PATTERNS = (r"span.*metrics.*calls", r"spanmetrics.*calls", r"calls_total")
_DUR_FAMILY_HINTS = ("traces_span_metrics_duration_milliseconds_bucket",)
_DUR_FAMILY_PATTERNS = (r"span.*metrics.*duration.*bucket", r"duration.*bucket")
_SERVICE_LABELS = ("service_name", "service", "service_id")
_STATUS_LABELS = ("status_code", "http_status_code", "status")
_KIND_LABELS = ("span_kind", "span.kind")
_LAT_P95_RECORDED = "service:span_latency_p95:5m"

async def _get_json(url: str, params: dict | None = None,
                    headers: dict | None = None,
                    timeout: float = SCAN_TIMEOUT) -> dict:
    """GET + **一次**重试。偶发超时不该让整条诊断判"查不到"。

    实测 2026-10-18:粗筛一次 ReadTimeout 直接把 adHighCpu 整组判成 inconclusive
    (事件里写着"粗筛查询失败(ReadTimeout)")。上游是共享的 Prometheus,24 小时
    range 查询在它忙的时候超过 10 秒是常态;重试一次的成本远低于整单丢失。
    重试仍失败就如实抛出,由调用方记缺口——不静默给空数据。"""
    last: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
                r = await c.get(url, params=params or None, headers=headers)
                r.raise_for_status()
                return r.json() or {}
        except _RETRY_ERRORS as exc:
            last = exc
            if attempt == 1:
                raise
    raise last  # pragma: no cover  循环里已经抛了,这里只为类型收口

async def _query(prom: dict, expr: str, at: float | None = None) -> list[dict]:
    """即时查询。at 给定则在**历史时刻**求值——离线回放(scripts/eval_detect_offline.py)
    靠它把同一套判据跑在故障发生的那一刻上,不必每次改动都重跑一遍实时跑批。"""
    params = {"query": expr}
    if at is not None:
        params["time"] = at
    body = await _get_json(f"{prom['url']}/api/v1/query", params, prom.get("headers"))
    return ((body or {}).get("data") or {}).get("result") or []

async def _discover(prom: dict) -> dict | None:
    """发现可用的调用量指标族 + 服务/状态/kind 标签。发现不了返回 None(如实说粗筛不可用)。"""
    body = await _get_json(f"{prom['url']}/api/v1/label/__name__/values",
                           headers=prom.get("headers"))
    names = (body or {}).get("data") or []
    fam = next((f for f in _CALL_FAMILY_HINTS if f in names), None)
    if fam is None:
        for pat in _CALL_FAMILY_PATTERNS:
            fam = next((f for f in names if re.search(pat, f, re.I)), None)
            if fam:
                break
    if fam is None:
        return None
    dur = next((f for f in _DUR_FAMILY_HINTS if f in names), None)
    if dur is None:
        for pat in _DUR_FAMILY_PATTERNS:
            dur = next((f for f in names if re.search(pat, f, re.I)), None)
            if dur:
                break
    sample = await _query(prom, f'{{__name__="{fam}"}}')
    if not sample:
        return None
    labels = sample[0].get("metric") or {}
    svc = next((k for k in _SERVICE_LABELS if k in labels), None)
    if svc is None:
        return None
    return {"family": fam, "service_label": svc, "dur_family": dur,
            "status_label": next((k for k in _STATUS_LABELS if k in labels), None),
            "kind_label": next((k for k in _KIND_LABELS if k in labels), None),
            "lat_p95_recorded": _LAT_P95_RECORDED if _LAT_P95_RECORDED in names else None}

async def _range(prom: dict, expr: str, start: float, end: float, step: int) -> list[dict]:
    body = await _get_json(f"{prom['url']}/api/v1/query_range",
                           {"query": expr, "start": start, "end": end, "step": step},
                           prom.get("headers"), timeout=RANGE_TIMEOUT)
    return ((body or {}).get("data") or {}).get("result") or []
