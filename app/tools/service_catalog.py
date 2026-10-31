"""真实服务数据层(接 targets 表接入的数据源,取代旧 mock 目录)。

服务发现与快照全部现查现用:Jaeger 服务清单是权威来源(链路里真实出现过的服务,
v1/v3 口径自动适配);CPU/内存水位来自该平台 Prometheus;错误链路数来自 Jaeger。
平台没接、不可达、服务不存在,都如实返回/标注,绝不编造——「状态」不再是随机数,
而是从真实观测数据派生的启发式判定(派生规则在 service_snapshot docstring)。

值班归属说明:mock 阶段 owns_service 是假值班清单;真实环境没有 CMDB 数据源,
语义退化为「服务在接入平台中存在」的存在性校验——报一个平台里查无此名的服务
照样被挡。后续接真实 CMDB 后可升级回归属校验,签名保持不变。"""
import asyncio
import re
import time

import httpx

from app.db import repository

UPSTREAM_TIMEOUT = 8.0


async def _none2() -> tuple[None, None]:
    """并行 gather 里"这一路没有数据源"的占位,返回形状与真实查询一致。"""
    return None, None
# 启发式阈值:容器 CPU 常用核数口径(秒/秒),单核打满≈1.0。没有 limit 标签算不出
# 真实百分比,这里只做「明显偏高」的粗判,不做精确水位。
CPU_DEGRADED_CORES = 1.0
SNAPSHOT_WINDOW_MIN = 10   # 快照取最近 N 分钟平均当「当前水位」
# 指标族语义优先级:计数器(rate 后=每秒用量,即核数)与 usage_total_bytes 才是水位。
# 不能按字母序挑(sorted 会选中 kernelmode_nanoseconds_total 这类拆分计数器,算出天文数字
# 误判劣化)。两套命名都收:cadvisor(seconds_total/working_set)与 docker_stats(nanoseconds/
# usage_total,collector docker_stats receiver 的口径)。计数器自动套 rate()
_CPU_FAMILIES = ("container_cpu_usage_seconds_total", "container_cpu_usage_nanoseconds_total",
                 "process_cpu_seconds_total", "container_cpu_utilization_ratio")
_MEM_FAMILIES = ("container_memory_working_set_bytes", "container_memory_usage_total_bytes",
                 "container_memory_rss", "container_memory_usage_bytes")


def _client() -> httpx.AsyncClient:
    # trust_env=False:直连,防本机代理把 localhost 截胡
    # 统一从 net 建客户端:诊断的服务发现要能被录制与回放
    from app.diagnosis import net
    return net.client(UPSTREAM_TIMEOUT)


async def _enabled_targets() -> list:
    rows = await repository.list_targets()
    return [r for r in rows if r.enabled]


# Jaeger 服务清单口径(与探活/查询工具同一组候选):v2.19+ 删了 v1 搜索接口,
# otel-demo 还给 query 挂了 base_path=/jaeger/ui——路径按序探测,第一个通的算数
_JAEGER_SERVICE_PATHS = ("/api/services", "/api/v3/services", "/jaeger/ui/api/v3/services")


async def _jaeger_service_names(base: str, headers: dict | None = None) -> tuple[list[str], str | None]:
    """从一路 Jaeger 拉服务清单,自动适配 v1({data:[]})/v3({services:[]})口径。
    返回 (names, error):error 是人话级原因名(类型名/HTTP码),None=成功。"""
    last_code = "?"
    async with _client() as c:
        for path in _JAEGER_SERVICE_PATHS:
            r = await c.get(f"{base}{path}", headers=headers)
            last_code = r.status_code
            if r.status_code != 200:
                continue
            body = r.json()   # 非 JSON 抛 ValueError,由调用方按口径异常记账
            names = body.get("data") if isinstance(body.get("data"), list) else body.get("services")
            if isinstance(names, list):
                return [str(n) for n in names if str(n).strip()], None
    return [], f"HTTP{last_code}(v1/v3 服务清单端点都不通)"


async def discover_services() -> tuple[list[dict], str | None]:
    """从所有启用接入平台发现真实服务。返回 (services, error):
    - services: [{key, service_id, name, target, status, ambiguous}]
      **服务身份是"平台 + 服务名"(key = "平台/服务名"),不是裸服务名**——不同平台完全可以有
      同名服务(两个集群都叫 checkout),裸名去重会让其中一个整类隐形(真实缺陷:同名跨平台
      去重"先发现的平台胜出",第二个平台的同名服务永远查不到、也永远不会被诊断)。
    - error: 平台全不可达/注册表不可读时的人话说明;部分不可达不算 error(拿到的仍是真实的)
    同名服务标记 ambiguous=True,由调用方决定怎么处置:诊断走全平台扫描(不问用户),
    变更/查询必须让用户指明平台(改错平台是真事故)。"""
    try:
        rows = await _enabled_targets()
    except Exception as e:  # noqa: BLE001
        return [], f"接入服务注册表不可读:{type(e).__name__}"
    services: list[dict] = []
    errors: list[str] = []
    for row in rows:
        for src in (row.sources or []):
            if src.get("type") != "jaeger":
                continue
            base = str(src.get("url", "")).rstrip("/")
            try:
                names, err = await _jaeger_service_names(base, src.get("headers"))
            except Exception as e:  # noqa: BLE001
                names, err = None, type(e).__name__
            if err:
                errors.append(f"{row.name}({err})")
                continue
            for n in names or []:
                name = str(n)
                if not name:
                    continue
                if any(s["service_id"] == name and s["target"] == row.name for s in services):
                    continue          # 同一平台内的重复才去重;跨平台同名一律保留
                services.append({"key": f"{row.name}/{name}", "service_id": name, "name": name,
                                 "target": row.name, "cluster": getattr(row, "cluster", "") or "",
                                 "status": None})
    counts: dict[str, int] = {}
    for s in services:
        counts[s["service_id"]] = counts.get(s["service_id"], 0) + 1
    for s in services:
        s["ambiguous"] = counts[s["service_id"]] > 1
    if not services and errors:
        return [], "接入平台均不可达:" + "、".join(errors)
    return services, None


def find_service(services: list[dict], sid: str, target: str | None = None) -> dict | None:
    """按服务身份找服务。接受三种写法(歧义时**不猜**,返回 None,由调用方决定报错还是扫描):
    - "平台/服务名"全名 → 唯一命中;
    - 裸服务名 + target → 在指定平台内找;
    - 裸服务名(跨平台唯一时才认)。"""
    sid = str(sid or "")
    if not sid:
        return None
    if "/" in sid:
        return next((s for s in services if s.get("key") == sid), None)
    if target:
        return next((s for s in services
                     if s["service_id"] == sid and s["target"] == target), None)
    hits = [s for s in services if s["service_id"] == sid]
    return hits[0] if len(hits) == 1 else None


async def _metric_last_value(base: str, service: str, metric: str) -> tuple[float | None, str | None]:
    """从该平台 Prometheus 取服务最近窗口的指标均值(核数/字节原值)。

    返回 (值, 失败原因):值查不到不算失败(平台接了 ≠ 部署了对应采集器),原因记 "no_data";
    超时记 "timeout"、其他异常记 "unreachable"。**超时和不可达必须分开**——前者是上游
    被挤慢了,后者是接不上,排查方向完全不同(旧实现一律返回 None,上层只能猜)。
    计数器族(seconds_total)自动套 rate() 换算成每秒用量,仪表(gauge)取原值。"""
    pattern = "cpu" if metric == "cpu" else "memory|rss|working_set"
    preferred = _CPU_FAMILIES if metric == "cpu" else _MEM_FAMILIES
    try:
        async with _client() as c:
            r = await c.get(f"{base}/api/v1/label/__name__/values")
            families = (r.json() or {}).get("data") or []
            picked = [f for f in preferred if f in families] \
                or [f for f in families
                    if re.search(pattern, f) and re.search(r"container|process|node|system", f)] \
                or [f for f in families if re.search(pattern, f)]
            if not picked:
                return None, "no_data"
            family = picked[0]   # 命中优先级就按语义来,兜底才看字母序
            # topk(100):先捞全候选再在我们侧按服务名过滤——topk(5) 会让排名靠后的容器直接消失
            expr = (f'topk(100, rate({{__name__="{family}"}}[2m]))' if family.endswith("_total")
                    else f'topk(100, {{__name__="{family}"}})')
            end = time.time()
            r = await c.get(f"{base}/api/v1/query_range", params={
                "query": expr,
                "start": end - SNAPSHOT_WINDOW_MIN * 60, "end": end, "step": 60,
            })
            result = ((r.json() or {}).get("data") or {}).get("result") or []
            matched = [s for s in result
                       if any(service.lower() in str(v).lower() for v in s.get("metric", {}).values())]
            # values 元素是 [ts, "值"] 双元素:解构取 v(字符串原样 float),别用 p[1] 索引——
            # 那会取到字符串的第二个字符(如 "0.42"[1]=="."→ValueError,"12.3"[1]=="2"→算出错值)
            vals = [float(v) for s in matched for _, v in (s.get("values") or [])]
            if "nanoseconds" in family:   # docker_stats 计数器是纳秒:rate 结果 ÷1e9 = 核数
                vals = [v / 1e9 for v in vals]
            if not vals:
                return None, "no_data"
            return round(sum(vals) / len(vals), 4), None
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception:  # noqa: BLE001
        return None, "unreachable"


async def _trace_error_stats(base: str, service: str,
                             headers: dict | None = None) -> tuple[tuple[int, int] | None, str | None]:
    """该服务近 1 小时链路抽样里的 (总链路, 含错误 span 的链路) 数。

    返回 ((总数, 错误数), 失败原因)。原因同 _metric_last_value:超时与不可达分开记,
    上层才能如实说"上游慢"而不是"数据源挂了"。
    口径自动适配:v1({data:[trace]},span tags 判错)/ v3(OTLP JSON,status.code==2 判错,
    路径 /api/v3/traces 与 /jaeger/ui/api/v3/traces)。"""
    try:
        async with _client() as c:
            r = await c.get(f"{base}/api/traces", headers=headers,
                            params={"service": service, "limit": 20, "lookback": "1h"})
            if r.status_code == 200:
                traces = (r.json() or {}).get("data") or []
                err = sum(1 for tr in traces
                          if any(t.get("key") == "error" and t.get("value")
                                 for sp in (tr.get("spans") or [])
                                 for t in (sp.get("tags") or [])))
                return (len(traces), err), None
            # v1 不通:试 v3(404 也可能是「没查到」,两种形态都当无数据回退到 v3 探测)
            for prefix in ("/jaeger/ui", ""):
                now = time.time()
                r = await c.get(f"{base}{prefix}/api/v3/traces", headers=headers, params={
                    "query.serviceName": service, "query.searchDepth": 20,
                    "query.startTimeMin": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
                    "query.startTimeMax": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))})
                if r.status_code == 404:   # v3 无匹配=空数据,不是口径错误
                    return (0, 0), None
                if r.status_code != 200:
                    continue
                spans_per_trace: dict[str, list[dict]] = {}
                for rs in (r.json().get("result") or {}).get("resourceSpans") or []:
                    for ss in rs.get("scopeSpans") or []:
                        for sp in ss.get("spans") or []:
                            spans_per_trace.setdefault(sp.get("traceId"), []).append(sp)
                err = sum(1 for spans in spans_per_trace.values()
                          if any((sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
                                 or any(a.get("key") == "error" and a.get("value", {}).get("boolValue")
                                        for a in sp.get("attributes") or [])
                                 for sp in spans))
                return (len(spans_per_trace), err), None
            return None, "no_data"
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception:  # noqa: BLE001
        return None, "unreachable"


async def service_snapshot(service_name: str, discovered: list[dict] | None = None) -> dict:
    """真实服务快照:水位+错误链路现查,状态从观测数据派生,查不到的维度如实置 None。

    状态派生规则(启发式,判定依据一并返回供 agent 复述):
    近 1h 链路抽样有错误 span → 异常;CPU 均值 > 1 核 → 性能劣化;
    观测全不可达 → 未知;否则 → 运行中。"""
    name = str(service_name or "")
    if discovered is None:
        discovered, _ = await discover_services()
    hit = find_service(discovered, name)
    if hit is None:
        dup = [s for s in discovered if s["service_id"] == name]
        if len(dup) > 1:      # 歧义:同名跨平台,不猜,让调用方指明平台
            return {"service_id": name, "name": name, "status": "未知",
                    "reason": f"「{name}」在多个平台上都有("
                              + "、".join(s["key"] for s in dup)
                              + "),请用「平台/服务名」指明是哪一个"}
        return {"service_id": name, "name": name, "status": "未知",
                "reason": f"接入平台中未发现服务「{name}」"}

    # 用发现该服务的平台的数据源拉真实观测。服务名一律用**裸名**:调用方可能传
    # "平台/服务名"(前端目录就是这么标的),而上游 Jaeger/Prometheus 的 service_name
    # 是裸名——拿带前缀的串去查,上游不报错、只回空,快照就会误判成"运行中"
    # (2026-10-22 实测:product-catalog 正挂着,快照却说未见异常)。
    name = str(hit.get("service_id") or name)
    target = None
    try:
        for row in await _enabled_targets():
            if row.name == hit["target"]:
                target = row
                break
    except Exception:  # noqa: BLE001
        pass
    prom = next((s for s in (target.sources or []) if s.get("type") == "prometheus"), None) \
        if target else None
    jaeger_src = next((s for s in (target.sources or []) if s.get("type") == "jaeger"), None) \
        if target else None
    prom = prom["url"].rstrip("/") if prom else None
    jaeger = jaeger_src["url"].rstrip("/") if jaeger_src else None
    jaeger_hdrs = (jaeger_src or {}).get("headers")

    # 三路查询**并行**:串行时总耗时是三者相加(实测故障高峰期 7s+10s+9s=26s,
    # 单步超时才 8s,任何一步抖一下就整条超时)。并行后总耗时≈最慢的那一步。
    cpu, mem, trace_stats = await asyncio.gather(
        _metric_last_value(prom, name, "cpu") if prom else _none2(),
        _metric_last_value(prom, name, "memory") if prom else _none2(),
        _trace_error_stats(jaeger, name, jaeger_hdrs) if jaeger else _none2(),
    )
    (cpu, cpu_err), (mem, mem_err), (trace_stats, trace_err) = cpu, mem, trace_stats
    traces_1h, error_traces_1h = trace_stats if trace_stats else (None, None)

    if traces_1h is not None and error_traces_1h > 0:
        status, reason = "异常", f"近 1h 链路抽样 {error_traces_1h}/{traces_1h} 条含错误"
    elif cpu is not None and cpu > CPU_DEGRADED_CORES:
        status, reason = "性能劣化", f"CPU 均值 {cpu} 核,超过 {CPU_DEGRADED_CORES} 核阈值"
    elif cpu is None and mem is None and traces_1h is None:
        # 三样全空有两种完全不同的原因,不能混着说:超时是"上游慢/被挤"、不可达是"接不上"。
        # 混成一句"数据源均不可达"会把排查带偏(2026-10-22 实测:靶机负载 10+ 时全部超时,
        # 界面上却报"数据源不可达",看着像接入配错了)。
        errs = {e for e in (cpu_err, mem_err, trace_err) if e}
        if "timeout" in errs:
            status, reason = "未知", "查询超时:上游响应过慢(不是数据源不可达),可稍后重试"
        elif errs:
            status, reason = "未知", "指标与链路数据源不可达(" + "、".join(sorted(errs)) + ")"
        else:
            status, reason = "未知", "指标与链路都查不到该服务的观测数据"
    elif traces_1h is None:
        # 指标拿到了、链路没拿到:只能说"指标正常",不能替链路下"未见异常"的结论
        status, reason = ("运行中",
                          f"指标正常,但近 1h 链路抽样未取到({trace_err or '无数据'})")
    else:
        status, reason = "运行中", "指标与链路抽样均未见异常"

    return {"service_id": name, "name": name, "target": hit["target"],
            "status": status, "reason": reason,
            "cpu_usage_cores": cpu, "mem_usage_bytes": mem,
            "traces_1h": traces_1h, "error_traces_1h": error_traces_1h,
            # 真实环境暂无稳定的实例数来源,置 None(前端已兼容缺省)
            "instances": None}


async def owns_service(user_id: str, service_id: str) -> bool:
    """这个服务能不能被当前会话操作。mock 阶段是值班清单,现在语义=「服务在接入平台中
    存在」——user_id 参数保留以兼容调用方,等接了真实 CMDB 再恢复归属判断。
    空 user_id(身份注入失败)或空 service_id 一律不放行:身份缺失=引擎没注入成功,
    这种时候放行等于把 IDOR 防线整个拆了,宁可误拒。"""
    if not user_id or not service_id:
        return False
    services, err = await discover_services()
    if err:
        return False
    # 身份是"平台+服务名":裸名跨平台歧义时不放行(宁可误拒,也不能改错平台的服务)
    return find_service(services, str(service_id)) is not None


async def list_selectable_services() -> list[dict]:
    """服务选择器(fetch_service interrupt 卡)的数据源:全部接入平台发现的真实服务。"""
    services, _ = await discover_services()
    return services
