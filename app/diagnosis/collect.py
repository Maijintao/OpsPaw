"""诊断层数据采集(管线第②段):trace 摊平成 span 列表 + 指标当前窗/基线窗均值。

只拉数和换算,不做任何推断——推断在 deviation/localize。
trace 口径自适应:先试 v3(OTLP JSON,/api/v3/traces 与 otel-demo 的 /jaeger/ui 前缀),
都不通再回退 v1(/api/traces)——换靶子不改引擎。
"""
import contextvars
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger("app")

UPSTREAM_TIMEOUT = 10.0
# trace 拉取单独放宽:单窗最多 30 条 trace 的 OTLP JSON 轻松上 MB,公网窄带宽(云主机
# 常见 1-2Mbps 出口)传完要十几秒——10s 必超时,还把共享同一管道的聊天层查询一起饿死。
# 诊断是后台任务,等得起;指标/日志查询仍用 UPSTREAM_TIMEOUT。
TRACE_FETCH_TIMEOUT = 30.0
# otel-demo 的 query 配了 base_path=/jaeger/ui,裸部署没有——两个前缀都试
_V3_PREFIXES = ("/jaeger/ui", "")

# ---------- 单次诊断的上游查询预算(商用底线:诊断工具不能把客户的监控打挂) ----------
# 用 contextvar 承载,不侵入各查询函数的签名:每个真实上游查询前记一笔,各循环用
# budget_left() 检查、用完就地早停,把已拿到的数据交出去,而不是继续往上打。
# 未开预算(None)时全部放行——单测/离线回放不受影响。
_QUERY_BUDGET: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "diag_query_budget", default=None)


def budget_start(max_queries: int, max_seconds: float) -> None:
    """开一次诊断的预算:查询次数上限 + 总时长上限(两者谁先到算谁)。"""
    _QUERY_BUDGET.set({"used": 0, "max_queries": max_queries,
                       "deadline": time.monotonic() + max_seconds})


def budget_reset() -> None:
    _QUERY_BUDGET.set(None)


def _spend() -> None:
    """记一次上游查询。额度用完不抛异常——不打断管线,由各循环自己看 budget_left() 收手。"""
    b = _QUERY_BUDGET.get()
    if b is not None:
        b["used"] += 1


def budget_left() -> bool:
    b = _QUERY_BUDGET.get()
    if b is None:
        return True
    return b["used"] < b["max_queries"] and time.monotonic() < b["deadline"]


def budget_report() -> dict:
    """本次诊断的预算消耗,落进结论供审计("这次结论偏保守是因为预算用完了")。"""
    b = _QUERY_BUDGET.get()
    if b is None:
        return {"used": 0, "max_queries": None, "exhausted": False}
    return {"used": b["used"], "max_queries": b["max_queries"],
            "exhausted": not budget_left()}


def _client(timeout: float = UPSTREAM_TIMEOUT) -> httpx.AsyncClient:
    # 统一从 net 建客户端:录制/回放要靠它把上游响应存下来/喂回去。
    # (net.client 内部仍带 trust_env=False:直连,不吃环境代理——本机 Clash 会把远端
    #  IP 截胡成 502)
    from app.diagnosis import net
    return net.client(timeout)


def _rfc3339(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class Span:
    """摊平后的 span:拓扑要 parent,偏离要时长/错误/时间,归因证据要操作名。
    peer=client span 指向的对端服务(挂死服务自身零 span,只存在于调用方的失败
    client span 里——影子节点靠它才进得了拓扑,否则归因对"整个挂死"永久失明)。"""
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    operation: str
    start_us: int          # 起始时间(微秒),时间先后信号用
    duration_us: int
    error: bool
    peer: str | None = None
    kind: str | None = None
    status_message: str | None = None   # 错误 span 的状态消息(错误定性比错误率高一个量级)
    attributes: dict[str, str] | None = None
    resource: dict[str, str] | None = None


@dataclass
class TraceFetchResult:
    """一次阶梯采集的结果。空列表不能同时表示无数据、过期和查询失败。"""
    spans: list[Span]
    status: str  # available | sparse | empty | data_stale | query_failed
    lookback_min: int
    window_start: float
    window_end: float
    requested_lookbacks: list[int]
    error: str | None = None


_PEER_ATTR_KEYS = ("peer.service", "net.peer.name", "server.socket.domain", "server.address")


def _attr(sp: dict, keys) -> str | None:
    for k in keys:
        v = next((a.get("value", {}).get("stringValue") for a in sp.get("attributes") or []
                  if a.get("key") == k), None)
        if v:
            return v
    return None


def _peer_host(raw: str | None) -> str | None:
    """peer 值归一成服务名:去掉端口/协议部分(currency:7001 → currency)。
    取不到像主机名的(纯 IP/空)就放弃——宁缺勿错。"""
    if not raw:
        return None
    host = raw.split("://")[-1].split("/")[0].split(":")[0].strip().lower()
    if not host or host.replace(".", "").isdigit():   # 纯 IP 对不上服务名
        return None
    return host


def _string_attributes(items: list[dict] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items or []:
        key = item.get("key")
        value = item.get("value") or {}
        raw = value.get("stringValue")
        if key and raw is not None:
            values[str(key)] = str(raw)
    return values


def _otlp_kind(sp: dict) -> str | None:
    raw = sp.get("kind")
    if isinstance(raw, str):
        return raw
    kinds = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
    return kinds.get(raw)


def _otlp_error(sp: dict) -> bool:
    code = (sp.get("status") or {}).get("code")
    return code in (2, "STATUS_CODE_ERROR") or any(
        a.get("key") == "error" and a.get("value", {}).get("boolValue")
        for a in sp.get("attributes") or [])


def _spans_from_v3(body: dict) -> list[Span]:
    out: list[Span] = []
    for rs in (body.get("result") or {}).get("resourceSpans") or []:
        svc = next((a["value"].get("stringValue") for a in (rs.get("resource") or {}).get("attributes") or []
                    if a.get("key") == "service.name"), "?")
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                start = int(sp.get("startTimeUnixNano", 0)) // 1000
                end = int(sp.get("endTimeUnixNano", 0)) // 1000
                out.append(Span(
                    trace_id=sp.get("traceId", ""), span_id=sp.get("spanId", ""),
                    parent_span_id=sp.get("parentSpanId") or None,
                    service=svc, operation=sp.get("name", "?"),
                    start_us=start, duration_us=max(end - start, 0),
                    error=_otlp_error(sp),
                    peer=_peer_host(_attr(sp, _PEER_ATTR_KEYS)),
                    kind=_otlp_kind(sp),
                    status_message=(sp.get("status") or {}).get("message") or None,
                    attributes=_string_attributes(sp.get("attributes")),
                    resource=_string_attributes((rs.get("resource") or {}).get("attributes"))))
    return out


def _v1_error(tags: list[dict]) -> bool:
    return any(t.get("key") == "error" and t.get("value") for t in tags or [])


def _v1_kind(tags: list[dict]) -> str | None:
    value = next((str(t.get("value", "")).upper() for t in tags or []
                  if t.get("key") == "span.kind"), None)
    return value.removeprefix("SPAN_KIND_") if value else None


def _v1_values(tags: list[dict]) -> dict[str, str]:
    return {str(t["key"]): str(t.get("value")) for t in tags or []
            if t.get("key") and t.get("value") is not None}


def _spans_from_v1(body: dict) -> list[Span]:
    procs = body.get("processes") or {}
    # process 级 tags 并进 resource:v1 的 service.version 等语义属性挂在 process tags 上,
    # 不合并的话版本变更检测在 v1 靶子上整条失效
    proc_attrs = {pid: _v1_values((p or {}).get("tags"))
                  for pid, p in procs.items()}
    out: list[Span] = []
    for tr in body.get("data") or []:
        proc_names = {pid: (p or {}).get("serviceName", "?") for pid, p in procs.items()}
        for sp in tr.get("spans") or []:
            pid = sp.get("processID")
            parent = next((ref.get("spanID") for ref in sp.get("references") or []
                           if ref.get("refType") == "CHILD_OF"), None)
            tags = _v1_values(sp.get("tags"))
            out.append(Span(
                trace_id=sp.get("traceID", ""), span_id=sp.get("spanID", ""),
                parent_span_id=parent,
                service=proc_names.get(pid, "?"),
                operation=sp.get("operationName", "?"),
                start_us=int(sp.get("startTime", 0)),
                duration_us=int(sp.get("duration", 0)),
                error=_v1_error(sp.get("tags")),
                peer=_peer_host(next((t.get("value") for t in sp.get("tags") or []
                                      if t.get("key") in _PEER_ATTR_KEYS), None)),
                kind=_v1_kind(sp.get("tags")), attributes=tags,
                status_message=next((tags[k] for k in
                                     ("otel.status_description", "status.description",
                                      "error.message") if tags.get(k)), None),
                resource={**proc_attrs.get(pid, {}),
                          "service.name": proc_names.get(pid, "?")}))
    return out


async def _retry_get(c, url: str, *, params: dict | None = None, headers: dict | None = None,
                     tries: int = 2):
    """带重连的 GET:瞬时网络失败(拒连/超时/DNS 抖动)退避后重试一次。
    对端怎么折腾都尽力拉到数据——连接韧性是采集层的第一职责。"""
    import asyncio
    for i in range(tries):
        _spend()          # 每次真实请求都记一笔,重试也算
        try:
            return await c.get(url, params=params, headers=headers)
        except httpx.TransportError:
            if i == tries - 1:
                raise
            await asyncio.sleep(1.5)


async def _fetch_spans_once(jaeger_base: str, headers: dict | None, service: str,
                            lookback_min: int, limit: int, window_end: float,
                            error_only: bool = False) -> tuple[list[Span], str, str | None]:
    """单窗口查询,返回 spans / status / error。
    error_only:带 error tag 过滤的错误优先采样——普通采样按"最新 N 条 trace"截断,
    错误停了几分钟后新流量会把错误现场挤出条数上限(真实教训);错误通道只搜含
    错误的 trace,同一份条数额度全花在错误现场上。过滤串是 Jaeger 官方查询参数
    (v3=query.tags, v1=tags,JSON 编码);上游不认就按普通查询失败处理,调用方跳过。"""
    window_start = window_end - lookback_min * 60
    tags = '{"error":"true"}' if error_only else None
    async with _client(TRACE_FETCH_TIMEOUT) as c:
        for prefix in _V3_PREFIXES:
            params = {"query.serviceName": service, "query.searchDepth": limit,
                      "query.startTimeMin": _rfc3339(window_start),
                      "query.startTimeMax": _rfc3339(window_end)}
            if tags:
                params["query.tags"] = tags
            r = await _retry_get(c, f"{jaeger_base}{prefix}/api/v3/traces", headers=headers,
                                 params=params)
            if r.status_code == 200:
                try:
                    spans = _spans_from_v3(r.json())
                    return spans, ("ok" if spans else "empty"), None
                except ValueError:
                    continue
            if r.status_code == 404:
                continue
            return [], "query_failed", f"Jaeger v3 HTTP {r.status_code}"
        r = await _retry_get(c, f"{jaeger_base}/api/traces", headers=headers,
                             params={"service": service, "limit": limit,
                                     "lookback": f"{lookback_min}m",
                                     **({"tags": tags} if tags else {})})
        if r.status_code == 200:
            try:
                body = r.json()
                spans = _spans_from_v1(body)
                return spans, ("ok" if spans else "empty"), None
            except ValueError:
                return [], "query_failed", "Jaeger v1 返回无效 JSON"
        if r.status_code == 404:
            return [], "empty", None
        return [], "query_failed", f"Jaeger v1 HTTP {r.status_code}"
    return [], "query_failed", "Jaeger v3/v1 查询接口均不可用或响应无法解析"


async def _merge_error_spans(jaeger_base: str, headers: dict | None, service: str,
                             lookback_min: int, limit: int, window_end: float,
                             start_us: int, end_us: int,
                             base_spans: list[Span]) -> list[Span]:
    """错误优先采样并入:同一窗带 error tag 过滤再搜一轮,把被"最新 N 条"挤出普通
    采样的错误 trace 补回来(按 span 去重)。尽力而为:查询失败/预算尽/零返回都
    原样交还普通采样结果,错误通道的缺席不阻断诊断。"""
    if not budget_left():
        return base_spans
    try:
        espans, state, _ = await _fetch_spans_once(
            jaeger_base, headers, service, lookback_min, limit, window_end,
            error_only=True)
    except Exception:  # noqa: BLE001  错误通道失败不拖垮主采集
        return base_spans
    if state != "ok" or not espans:
        return base_spans
    extra = [sp for sp in espans
             if start_us <= sp.start_us <= end_us
             and (sp.trace_id, sp.span_id) not in {(s.trace_id, s.span_id) for s in base_spans}]
    return base_spans + extra


DEFAULT_LOOKBACKS = (10, 60, 360, 1440)
"""采集的扩窗阶梯(分钟):10m→1h→6h→24h,样本够就停。

第一档就是"诊断固定会读的最近窗口"。跑批/演练要重启靶子时,洗窗时长必须盖住它
(见 scripts/aiopslab_bench._wash_seconds):重启瞬间的 503/连接拒绝会留在这一档里,
被 agent 当成故障证据。"""


async def fetch_spans(jaeger_base: str, headers: dict | None, service: str,
                      lookback_min: int = 60, limit: int = 30,
                      window_end: float | None = None,
                      lookbacks: tuple[int, ...] = DEFAULT_LOOKBACKS,
                      min_samples: int = 20) -> TraceFetchResult:
    """按 10m→1h→6h→24h 扩窗。固定同一终点，并过滤服务端返回的窗口外 span。
    每窗额外并入一轮错误优先采样(error tag 过滤):错误现场不被"最新 N 条"截断。"""
    end = window_end if window_end is not None else time.time()
    steps = tuple(dict.fromkeys(max(1, int(x)) for x in lookbacks)) or (lookback_min,)
    requested: list[int] = []
    stale_seen = False
    budget_hit = False
    last_error: str | None = None
    last_spans: list[Span] = []
    for minutes in steps:
        if not budget_left():        # 预算用完就地收手,别再往客户上游打
            budget_hit = True
            break
        requested.append(minutes)
        try:
            spans, state, error = await _fetch_spans_once(
                jaeger_base, headers, service, minutes, limit, end)
        except Exception as exc:
            spans, state, error = [], "query_failed", f"{type(exc).__name__}: {exc}"
        if state == "query_failed":
            last_error = error
            continue
        start_us, end_us = int((end - minutes * 60) * 1e6), int(end * 1e6)
        in_window = [sp for sp in spans if start_us <= sp.start_us <= end_us]
        if spans and not in_window:
            stale_seen = True
            continue
        if in_window:
            # 错误优先并入:普通采样按"最新 N 条"截断,错误现场会被新流量挤出条数上限
            # ——同窗再搜一轮 error tag,错误 trace 一个不漏(预算闸与失败兜底都在内层)
            in_window = await _merge_error_spans(
                jaeger_base, headers, service, minutes, limit, end,
                start_us, end_us, in_window)
            last_spans = in_window
        if len(in_window) >= min_samples:
            return TraceFetchResult(in_window, "available", minutes, end - minutes * 60,
                                    end, requested)
    if last_spans:
        status = "sparse"
    elif budget_hit:
        # 预算用完了 ≠ 没有数据——把采集缺口包装成"空",等于把故障说成健康
        status = "budget_exhausted"
    elif stale_seen:
        status = "data_stale"
    elif last_error:
        status = "query_failed"
    else:
        status = "empty"
    return TraceFetchResult(last_spans, status, steps[-1], end - steps[-1] * 60,
                            end, requested, last_error)


async def list_services(jaeger_base: str, headers: dict | None) -> list[str]:
    """平台服务清单(v3 优先,v1 回退,与 trace 查询同一套前缀适配)。
    v3 的 services 在响应顶层(不是 result 下),v1 在 data[].serviceName。"""
    async with _client() as c:
        for prefix in _V3_PREFIXES:
            r = await _retry_get(c, f"{jaeger_base}{prefix}/api/v3/services", headers=headers)
            if r.status_code == 200:
                try:
                    body = r.json()
                    names = body.get("services") or (body.get("result") or {}).get("services") or []
                    if names:
                        return [str(n) for n in names]
                except ValueError:
                    continue
        r = await _retry_get(c, f"{jaeger_base}/api/services", headers=headers)
        if r.status_code == 200:
            try:
                return [str(p.get("serviceName", "?"))
                        for p in (r.json().get("data") or [])] or []
            except ValueError:
                return []
    return []


async def fetch_platform_spans(jaeger_base: str, headers: dict | None, exclude: str, *,
                               window_end: float, minutes: int = 10, limit_per: int = 10,
                               max_services: int = 12, enough=None) -> tuple[list[Span], list[str]]:
    """影子救援采样:目标自身在 Jaeger 已无任何 trace(从未上报/记忆环淘汰)时,
    按平台服务清单逐个拉短窗 trace,找"指向目标的失败调用"——挂死服务的痕迹
    只在调用方身上。enough(spans) 回调返回 True 提前收工(归因现场够了就省窄带宽)。
    返回 (spans, 已查服务列表)。单服务失败跳过,不抬异常。"""
    services = await list_services(jaeger_base, headers)
    out: list[Span] = []
    queried: list[str] = []
    for svc in services:
        if svc == exclude:
            continue
        if not budget_left():        # 影子救援要逐个服务拉,最吃查询额度——用完就停
            break
        try:
            spans, _state, _err = await _fetch_spans_once(
                jaeger_base, headers, svc, minutes, limit_per, window_end)
        except Exception:  # noqa: BLE001
            continue
        queried.append(svc)
        out.extend(spans)
        if (enough and enough(out)) or len(queried) >= max_services:
            break
    return out, queried


async def fetch_recent(jaeger_base: str, headers: dict | None, service: str,
                       minutes: int = 3, limit: int = 30,
                       window_end: float | None = None) -> tuple[list[Span], str]:
    """轻量独立采样(挂死检测用):单短窗一查,不阶梯扩窗。返回 (spans, status)。
    探测失败如实降级为空列表,不抬异常打断诊断管线。"""
    end = window_end if window_end is not None else time.time()
    try:
        spans, state, _ = await _fetch_spans_once(jaeger_base, headers, service,
                                                  minutes, limit, end)
        return spans, state
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_recent 采样失败 service=%s:%s", service, exc)
        return [], "query_failed"


# 指标族语义优先级:与 service_catalog.py 同一套(cadvisor 与 docker_stats 两套命名都收)
from app.tools.service_catalog import _CPU_FAMILIES, _MEM_FAMILIES  # noqa: E402

_METRIC_PREF = {"cpu": _CPU_FAMILIES, "memory": _MEM_FAMILIES}


async def metric_avg(prom_base: str, headers: dict | None, service: str, metric: str,
                     start: float, end: float) -> tuple[float | None, str]:
    """返回窗口均值与状态:available / no_data / query_failed / budget_exhausted。"""
    if not budget_left():
        return None, "budget_exhausted"
    import re
    pattern = "cpu" if metric == "cpu" else "memory|rss|working_set"
    pref = _METRIC_PREF.get(metric, ())
    try:
        async with _client() as c:
            _spend()
            r = await c.get(f"{prom_base}/api/v1/label/__name__/values", headers=headers)
            r.raise_for_status()
            families = (r.json() or {}).get("data") or []
            picked = [f for f in pref if f in families] \
                or [f for f in families
                    if re.search(pattern, f) and re.search(r"container|process|node|system", f)] \
                or [f for f in families if re.search(pattern, f)]
            if not picked:
                return None, "no_data"
            family = picked[0]
            expr = (f'topk(100, rate({{__name__="{family}"}}[2m]))' if family.endswith("_total")
                    else f'topk(100, {{__name__="{family}"}})')
            _spend()
            r = await c.get(f"{prom_base}/api/v1/query_range", headers=headers, params={
                "query": expr, "start": start, "end": end, "step": 60})
            r.raise_for_status()
            result = ((r.json() or {}).get("data") or {}).get("result") or []
            matched = [s for s in result
                       if any(service.lower() == str(v).lower()
                              or service.lower() in str(v).lower().split("/")
                              for v in s.get("metric", {}).values())]
            def inside_window(sample_ts: str) -> bool:
                ts = float(sample_ts)
                return start <= ts <= end
            vals = [float(v) for s in matched for ts, v in (s.get("values") or [])
                    if inside_window(ts)]
            if "nanoseconds" in family:
                vals = [v / 1e9 for v in vals]
            return (round(sum(vals) / len(vals), 6), "available") if vals else (None, "no_data")
    except Exception:  # noqa: BLE001  异常不许拿来当零,同时返回失败状态供诊断结果展示
        logger.warning("metric_avg 查询失败 service=%s metric=%s window=[%s,%s]",
                       service, metric, start, end, exc_info=True)
        return None, "query_failed"
