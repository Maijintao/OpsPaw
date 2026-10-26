"""接入服务的观测查询工具:Agent 拿真实数据的入口(数据面消费,配 api/targets.py 的注册层)。

范式沿 ch08(见 services.py / faq.py):参数中文说明、docstring 讲清「是什么/什么时候用」、
结果瘦身只留回答所需字段、错误统一 {"error","code"} 形状由引擎回灌。

隔离边界(与 api/targets.py 的约定一致):模型只说「服务名 + 指标语义」,PromQL 与
Jaeger 查询串由我们侧模板生成——模型不接触裸查询串。目标解析规则:
按数据源类型过滤启用的接入服务,唯一→直接用;多个→如实报歧义并列出可选名,让模型问用户;
零个→如实报「未接入」。工具不吹没有的牛:没接数据源就返回 error,绝不编造数据。

指标查询的实现刻意不猜 label 键(各平台叫 name/container/pod 的都有):
先用 /api/v1/label/__name__/values 做能力盘点(有哪些指标族),选中最匹配的一族,
再 topk 拉序列、在我们侧按「label 值包含服务名」过滤——label 键差异被消化在适配器里。"""
import re
import time

import httpx
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.db import repository
from app.tools import registry

UPSTREAM_TIMEOUT = 8.0   # 单次上游查询封顶;工具层还有引擎的统一超时兜底
TRACE_TIMEOUT = 20.0     # 链路查询单独放宽:OTLP JSON 按条整包返回,窄带宽公网上 MB 级响应
                         # 8s 传不完;引擎侧 timeout=20s 兜底,这里对齐它,不虚耗重试预算
LOOKBACK_MIN = 30        # 指标默认回看窗口(分钟)


def _client(timeout: float = UPSTREAM_TIMEOUT) -> httpx.AsyncClient:
    # trust_env=False:探活/查询一律直连,防本机代理(Clash)把 localhost 截胡成 502
    # 统一从 net 建客户端:诊断的日志/trace 查询要能被录制与回放
    from app.diagnosis import net
    return net.client(UPSTREAM_TIMEOUT)


async def _resolve(kind: str, target_name: str | None):
    """按数据源类型解析要打的接入服务。
    返回约定:成功 (row, source_dict),失败 (None, error_dict)——调用方必须按
    `row is None` 判成败,不许按第二个元素真值判(dict 永远真值,会把 source 误当错误)。
    隔离要点:target 是后台配置的,这里只做「类型过滤 + 指名选择」,不猜不串。"""
    try:
        rows = await repository.list_targets()
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"接入服务注册表不可读:{type(e).__name__}", "code": "registry_down"}
    candidates = [(r, s) for r in rows if r.enabled
                  for s in (r.sources or []) if s.get("type") == kind]
    if not candidates:
        return None, {"error": f"当前没有任何接入服务配置了 {kind} 数据源,请先在后台「服务检测」里接入",
                      "code": "no_target"}
    if target_name:
        candidates = [(r, s) for r, s in candidates if r.name == target_name]
        if not candidates:
            names = sorted({r.name for r, _ in candidates} or
                           {r.name for r in rows if r.enabled})
            return None, {"error": f"没有叫「{target_name}」的接入服务,现有:{'、'.join(names) or '无'}",
                          "code": "target_not_found"}
    if len(candidates) > 1:
        names = sorted({r.name for r, _ in candidates})
        return None, {"error": f"有 {len(candidates)} 个接入服务都有 {kind} 数据源,请指明 target_name,可选:{'、'.join(names)}",
                      "code": "ambiguous_target"}
    return candidates[0]


def _err_upstream(e: Exception) -> dict:
    return {"error": f"数据源查询失败:{type(e).__name__}", "code": "upstream_error"}


@tool
async def query_platform_metrics(
    service: Annotated[str, Field(description="要查的服务/容器/进程名,例如 frontend、cartservice、paymentservice")],
    metric: Annotated[str, Field(description="指标语义:cpu=CPU 使用,memory=内存使用,二选一")],
    minutes: Annotated[int, Field(description="回看窗口分钟数,默认 30,范围 5-120")] = 30,
    target_name: Annotated[str | None, Field(description="接入服务名(后台「服务检测」里的名字)。只接入了一个含 Prometheus 的服务时可留空")] = None,
) -> dict:
    """查询已接入服务的真实指标时序(经 Prometheus)。用于用户询问某个接入服务的 CPU、内存等
    运行水位时。只有后台「服务检测」接入过 Prometheus 数据源才能查;没接入会如实返回 error,
    请告知用户需要先接入,不要编造数据。返回最近 N 分钟的逐分钟序列与统计。"""
    # _resolve 约定:成功返回 (row, source dict),失败返回 (None, error dict)——按 row 判成败
    row, src = await _resolve("prometheus", target_name)
    if row is None:
        return src
    base = src["url"]
    hdrs = src.get("headers") or {}
    if metric not in ("cpu", "memory"):
        return {"error": f"metric 只支持 cpu/memory,收到:{metric}", "code": "bad_metric"}
    pattern = "cpu" if metric == "cpu" else "memory|rss|working_set"
    # 语义优先级:裸字母序会选中 kernelmode_nanoseconds_total 这种拆分计数器(天文数字),
    # 计数器(rate 后=核数)/usage_total_bytes 才是水位。两套命名都收:cadvisor 与
    # docker_stats receiver(collector 的容器指标来源);计数器自动套 rate()
    preferred = ("container_cpu_usage_seconds_total", "container_cpu_usage_nanoseconds_total",
                 "process_cpu_seconds_total", "container_cpu_utilization_ratio") \
        if metric == "cpu" else \
        ("container_memory_working_set_bytes", "container_memory_usage_total_bytes",
         "container_memory_rss", "container_memory_usage_bytes")
    minutes = max(5, min(120, minutes or LOOKBACK_MIN))

    try:
        async with _client() as c:
            # ① 能力盘点:这个 Prometheus 里到底有哪些指标族(接入了不等于有 CPU 数据)
            r = await c.get(f"{base}/api/v1/label/__name__/values", headers=hdrs)
            body = _json_or(r)
            families = body.get("data") or []
            picked = [f for f in preferred if f in families] \
                or [f for f in families
                    if re.search(pattern, f) and re.search(r"container|process|node|system", f)] \
                or [f for f in families if re.search(pattern, f)]
            if not picked:
                return {"error": f"该 Prometheus 没有与 {metric} 相关的指标族(共 {len(families)} 个指标族),"
                                 "通常是没有部署对应采集器", "code": "capability_missing"}
            family = picked[0]   # 命中优先级按语义来,兜底才字母序
            # topk(100):先捞全候选再在我们侧按服务名过滤——topk(5) 会让排名靠后的容器直接消失
            expr = (f'topk(100, rate({{__name__="{family}"}}[2m]))' if family.endswith("_total")
                    else f'topk(100, {{__name__="{family}"}})')
            # ② topk 拉序列(不猜 label 键),我们侧按 label 值含服务名过滤
            end = time.time()
            r = await c.get(f"{base}/api/v1/query_range", headers=hdrs, params={
                "query": expr,
                "start": end - minutes * 60, "end": end, "step": 60,
            })
            body = _json_or(r)
            result = (body.get("data") or {}).get("result") or []
            matched = [s for s in result
                       if any(service.lower() in str(v).lower() for v in s.get("metric", {}).values())]
            if not matched:
                all_vals = sorted({str(v) for s in result for v in s.get("metric", {}).values()
                                   if not str(v).startswith(("up", "0", "1"))})[:12]
                return {"error": f"指标族 {family} 里没有匹配「{service}」的序列",
                        "code": "service_not_found", "label_samples": all_vals}
            ns = "nanoseconds" in family   # docker_stats 计数器是纳秒:÷1e9 换算成核数再给模型
            series = [{"labels": s.get("metric", {}),
                       "points": [[p[0], round(float(p[1]) / 1e9, 6) if ns else p[1]]
                                  for p in (s.get("values") or [])]}
                      for s in matched]
            # values 元素是 [ts, "值"]:解构取 v 再 float。不能用 p[1] 索引——
            # p 已解构成值字符串,p[1] 是它的第二个字符("12.3"[1]=="2" 会算出错值且极难察觉)
            vals = [float(v) for s in matched for _, v in (s.get("values") or [])]
            if ns:   # 与 series 同口径:纳秒计数器 ÷1e9 换算成核数
                vals = [v / 1e9 for v in vals]
            return {"service": service, "metric_family": family, "minutes": minutes,
                    "series": series,
                    "statistics": {"avg": round(sum(vals) / len(vals), 2) if vals else None,
                                   "max": max(vals) if vals else None,
                                   "min": min(vals) if vals else None},
                    "promql": expr}
    except httpx.TimeoutException:
        return {"error": f"Prometheus 查询超时({UPSTREAM_TIMEOUT}s)", "code": "upstream_timeout"}
    except (httpx.HTTPError, ValueError) as e:
        return _err_upstream(e)


def _rfc3339(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _json_or(resp) -> dict:
    """响应体解析兜底:上游 404/5xx 可能回 HTML(Jaeger v2.21 的 /api/services 就是),
    r.json() 会抛 JSONDecodeError 被当成查询失败——解析失败按空体处理,状态码才是口径判据。"""
    try:
        return resp.json()
    except ValueError:
        return {}


def _parse_v3_traces(body: dict) -> list[dict]:
    """解析 Jaeger v3(OTLP JSON)响应为与 v1 同形的链路列表。
    v3 结构:resourceSpans 按「服务」分组,组内 spans 靠 traceId 关联成链路;error 看
    span status.code==2 或 error=true 属性。输出契约与 v1 一致,上层不用关心版本。"""
    traces: dict[str, dict] = {}
    for rs in (body.get("result") or {}).get("resourceSpans") or []:
        svc = next((a["value"].get("stringValue") for a in (rs.get("resource") or {}).get("attributes") or []
                    if a.get("key") == "service.name"), "?")
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                tr = traces.setdefault(sp.get("traceId"), {"span_count": 0, "spans": []})
                tr["span_count"] += 1
                status_code = (sp.get("status") or {}).get("code")
                err = status_code in (2, "STATUS_CODE_ERROR") or any(
                    a.get("key") == "error" and a.get("value", {}).get("boolValue")
                    for a in sp.get("attributes") or [])
                dur_us = (int(sp.get("endTimeUnixNano", 0)) - int(sp.get("startTimeUnixNano", 0))) // 1000
                tr["spans"].append({"service": svc, "operation": sp.get("name"),
                                    "duration_us": dur_us, **({"error": True} if err else {})})
    return [{"trace_id": tid, **tr} for tid, tr in traces.items()]


@tool
async def query_platform_traces(
    service: Annotated[str, Field(description="要查的服务名,例如 frontend、cartservice")],
    operation: Annotated[str | None, Field(description="可选:按操作名过滤,如 GET /cart、CheckoutService.Checkout")] = None,
    target_name: Annotated[str | None, Field(description="接入服务名(后台「服务检测」里的名字)。只接入了一个含 Jaeger/APM 的服务时可留空")] = None,
) -> dict:
    """查询已接入服务的真实调用链路(经 Jaeger/APM)。用于定位慢请求慢在哪个下游、
    错误从哪个服务传播上来时。只有后台「服务检测」接入过 Jaeger/APM 数据源才能查;
    没接入会如实返回 error,不要编造链路。返回最近 1 小时最多 5 条链路的 span 分解。"""
    row, src = await _resolve("jaeger", target_name)
    if row is None:
        return src
    base = src["url"]
    hdrs = src.get("headers") or {}
    try:
        async with _client(TRACE_TIMEOUT) as c:
            # 口径探测:v2.19+ 删了 v1 搜索接口,只剩 /api/v3(OTLP JSON);otel-demo 还给
            # query 挂了 base_path /jaeger/ui。按 v1→v3→v3(base_path) 顺序,一次调用内适配。
            flavor, prefix = "v1", ""
            r = await c.get(f"{base}/api/services", headers=hdrs)
            if not (r.status_code == 200 and isinstance(_json_or(r).get("data"), list)):
                flavor = None
                for p in ("/api/v3/services", "/jaeger/ui/api/v3/services"):
                    r = await c.get(f"{base}{p}", headers=hdrs)
                    if r.status_code == 200 and isinstance(_json_or(r).get("services"), list):
                        # prefix 只存 base_path(如 /jaeger/ui),后面统一拼 /api/v3/xxx——
                        # 存到 /api/v3 会双重叠加成 /api/v3/api/v3/traces(404 → 假"查无链路")
                        flavor, prefix = "v3", p.removesuffix("/api/v3/services")
                        break
                if flavor is None:
                    return {"error": f"Jaeger 服务列表不可查(HTTP {r.status_code}),探活通过过但现在不通了?"
                                     "请到后台「服务检测」复核该数据源", "code": "upstream_error"}

            if flavor == "v1":
                params = {"service": service, "limit": 5, "lookback": "1h"}
                r = await c.get(f"{base}/api/traces", headers=hdrs, params=params)
                traces = (_json_or(r).get("data") or []) if r.status_code == 200 else []
                if not traces:
                    return await _jaeger_hint(c, base, hdrs=hdrs, service=service)
                out = []
                for tr in traces:
                    raw = tr.get("spans") or []
                    spans = [{"service": ((tr.get("processes") or {}).get(sp.get("processID")) or {})
                              .get("serviceName", "?"),
                              "operation": sp.get("operationName"),
                              "duration_us": sp.get("duration"),
                              **({"error": True} if any(t.get("key") == "error" and t.get("value")
                                                        for t in sp.get("tags") or []) else {})}
                             for sp in raw]
                    out.append(_shape_trace(tr.get("traceID"), spans, len(raw), operation))
                return {"service": service, "total": len(traces), "traces": out}

            # v3:no-match 返回 404(不是空数组),同样按「没查到」处理并给服务名候选
            now = time.time()
            r = await c.get(f"{base}{prefix}/api/v3/traces", headers=hdrs, params={
                "query.serviceName": service, "query.searchDepth": 5,
                "query.startTimeMin": _rfc3339(now - 3600), "query.startTimeMax": _rfc3339(now)})
            if r.status_code == 404:
                return await _jaeger_hint(c, base, hdrs=hdrs, service=service, prefix=prefix)
            if r.status_code != 200:
                return {"error": f"Jaeger 链路查询异常 HTTP {r.status_code}", "code": "upstream_error"}
            parsed = _parse_v3_traces(_json_or(r))
            out = [_shape_trace(t["trace_id"], t["spans"], t["span_count"], operation) for t in parsed]
            return {"service": service, "total": len(out), "traces": out}
    except httpx.TimeoutException:
        return {"error": f"Jaeger 查询超时({UPSTREAM_TIMEOUT}s)", "code": "upstream_timeout"}
    except (httpx.HTTPError, ValueError) as e:
        return _err_upstream(e)


def _shape_trace(trace_id, spans: list[dict], total: int, operation: str | None) -> dict:
    """统一收口输出形状:span_count=链路总 span 数(过滤前);operation 是 span 级子串
    过滤,过滤后为空说明操作名拼错,由模型看 spans 候选自查重试。"""
    if operation:
        spans = [s for s in spans if operation.lower() in str(s.get("operation", "")).lower()]
    return {"trace_id": trace_id, "span_count": total, "spans": spans[:12]}


async def _jaeger_hint(c: httpx.AsyncClient, base: str, service: str, hdrs: dict | None = None,
                       prefix: str = "") -> dict:
    """空结果给「服务名候选」:多半是服务名拼错,让模型自己纠正再查。"""
    r2 = await c.get(f"{base}{prefix}/api/v3/services" if prefix else f"{base}/api/services",
                     headers=hdrs)
    body = _json_or(r2)
    names = body.get("services") if prefix else body.get("data")
    return {"service": service, "total": 0, "traces": [],
            "hint": "该 APM 里没有这个服务的链路;APM 现有服务:" + "、".join(map(str, names or []))}


# Loki 的服务标签叫法不统一(OTLP 摄入映射成 service_name,别的部署叫 job/app/container):
# 沿用指标适配器的「不猜 label 键」原则,按优先级在标签值里找服务名
_LOKI_SERVICE_LABELS = ("service_name", "service", "job", "app", "container", "pod")


@tool
async def query_platform_logs(
    service: Annotated[str, Field(description="要查日志的服务名,例如 frontend、checkout")],
    minutes: Annotated[int, Field(description="回看窗口分钟数,默认 30,范围 5-120")] = 30,
    filter_text: Annotated[str | None, Field(description="可选:行内关键词过滤,如 error、timeout")] = None,
    target_name: Annotated[str | None, Field(description="接入服务名(后台「服务检测」里的名字)。只接入了一个含 Loki 的服务时可留空")] = None,
) -> dict:
    """查询已接入服务的真实日志(经 Loki)。用于错误定位时看异常详情、报错堆栈与上下文——
    trace 告诉你「哪个服务错了」,日志告诉你「具体错在哪」。只有后台「服务检测」接入过
    Loki 数据源才能查;没接入会如实返回 error,不要编造日志。返回最近 N 分钟的日志行。"""
    row, src = await _resolve("loki", target_name)
    if row is None:
        return src
    base = src["url"]
    hdrs = src.get("headers") or {}
    minutes = max(5, min(120, minutes or LOOKBACK_MIN))
    try:
        async with _client() as c:
            # ① 找出「值包含服务名」的标签:先试常见优先级,再全量标签兜底(不猜键,找证据)
            r = await c.get(f"{base}/loki/api/v1/labels", headers=hdrs)
            labels = (_json_or(r).get("data") or []) if r.status_code == 200 else []
            if not labels:
                return {"error": "该 Loki 没有任何日志标签(通常是没有日志流入)", "code": "capability_missing"}
            picked = None
            for lb in [l for l in _LOKI_SERVICE_LABELS if l in labels] + \
                      [l for l in labels if l not in _LOKI_SERVICE_LABELS]:
                rv = await c.get(f"{base}/loki/api/v1/label/{lb}/values", headers=hdrs)
                vals = (_json_or(rv).get("data") or []) if rv.status_code == 200 else []
                if any(service.lower() in str(v).lower() for v in vals):
                    picked = lb
                    break
            if picked is None:
                return {"error": f"Loki 的 {len(labels)} 个标签里没有匹配「{service}」的值",
                        "code": "service_not_found", "label_candidates": labels}
            # ② 用选中的标签构造 LogQL 拉日志;行内关键词过滤是 LogQL 原生语义(|=)
            now = time.time()
            selector = f'{{{picked}="{service}"}}'
            if filter_text:
                selector += f' |= `{filter_text}`'
            r = await c.get(f"{base}/loki/api/v1/query_range", headers=hdrs, params={
                "query": selector, "limit": 100,
                "start": _rfc3339(now - minutes * 60), "end": _rfc3339(now)})
            body = _json_or(r) if r.status_code == 200 else {}
            streams = ((body.get("data") or {}).get("result")) or []
            lines = []
            for st in streams:
                meta = st.get("stream") or {}
                level = meta.get("detected_level") or meta.get("level")
                for ts_ns, line in (st.get("values") or []):
                    lines.append({"ts": _rfc3339(int(ts_ns) / 1e9), "line": line,
                                  **({"level": level} if level else {})})
            lines.sort(key=lambda x: x["ts"], reverse=True)
            return {"service": service, "minutes": minutes,
                    "label": picked, "total": len(lines), "lines": lines[:60],
                    "logql": selector}
    except httpx.TimeoutException:
        return {"error": f"Loki 查询超时({UPSTREAM_TIMEOUT}s)", "code": "upstream_timeout"}
    except (httpx.HTTPError, ValueError) as e:
        return _err_upstream(e)


# 单次上游 HTTP 调用,引擎默认重试有意义;但查询本身有语义(能力盘点+两段查询),
# 中途超时重放代价高 → max_retries=0,超时如实回灌让模型决定要不要再查
registry.register(registry.spec_from_langchain_tool(
    query_platform_metrics, source="builtin", timeout=20.0, max_retries=0))
registry.register(registry.spec_from_langchain_tool(
    query_platform_traces, source="builtin", timeout=20.0, max_retries=0))
registry.register(registry.spec_from_langchain_tool(
    query_platform_logs, source="builtin", timeout=20.0, max_retries=0))
