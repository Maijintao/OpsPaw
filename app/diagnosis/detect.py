"""全平台粗筛(检测层):没指定服务时,先便宜地把平台上所有服务扫一遍,挑出最可疑的
作为诊断锚点。三种入口共用——人工"平台崩了"、人工"下单失败了"、后续哨兵定时巡检。

为什么用指标而不是逐个拉 trace:粗筛要的是"全平台每服务一眼看过去",一条 PromQL
就能拿到全量;逐个服务拉 trace 是十几二十次公网大 JSON(云主机窄带宽的教训见
collect.py),粗筛阶段不该这么贵。

**判据是"当前值在服务自身历史分布里的分位位置",不是"当前窗 / 基线窗"的比值**
(2026-10-14 重构,详见 stats 模块与 HISTORY_HOURS 注释)。四条信号:
- 调用量塌陷:当前调用量低于自身历史分布 → 服务失联/挂死的强信号;
- 错误率异常:错误率高于自身历史分布 → 报错型故障。**从不出错的服务首次出错会
  直接判异常**(历史恒 0 → 当前 > 0 = 超过历史最大值),不需要任何绝对地板——
  旧实现用 `ERROR_RISE_MIN=5%` 这种地板,把 adFailure 那种 3.8% 的真实信号夹死了;
- 时延异常:当前 p95 高于自身历史分布 → 慢劣化 / 网络延迟 / CPU 限流;
- CPU 异常:当前窗均值高于自身历史分布 → 资源耗尽型故障。

**排序是字典序分层,不是加权打分**(见 TIER_*)。旧实现"各维幅度取 max 当分数排序"
的病:级联里受害者必然比真凶响、慢性变慢的服务永远比稳定服务响——对照组(零故障)
照样报出 high 置信根因。localize 早就改成分层了,粗筛这次补齐。

分层里"错误指认"是第二档:被"调用方的失败调用"点名的服务——它不但有异常,调用方
还正在它身上报错;只是跟着没量、没人往它上报错的,是被带的受害者,排后面。payment
挂了 checkout 不往下走、email 跟着没量,但没人往 email 上报错,所以根因是 payment
不是 email。指认关系从 CLIENT span 指标推(CLIENT span 的 service_name=调用方,
span_name 点名被调方)。推不出来就跳过,不硬凑。

只出锚点,不做归因——归因永远是诊断层那条确定性管线的事。
"""
import logging
import re
import time

import httpx

from app.db import repository
from app.diagnosis import net as _net

logger = logging.getLogger("app")

# ⚠️ 粗筛阈值全部是拍值,待校准。调整语义:调大 → 更保守(少报、可能漏);调小 → 更敏感(多报、可能误)。
SCAN_TIMEOUT = 10.0
RANGE_TIMEOUT = 150.0
"""range 查询的独立超时。**实测依据**(靶机 2 核 Prometheus):

    2026-10-18:24h histogram_quantile 0.95 @step=300     41.2 秒 → 当时定 90 秒
    2026-10-25 复测:同一查询 24h 跨度 81s / 48h 跨度 120s(数据量已涨到 48h 保留上限,
    且重算开销随实际样本数线性涨)——90 秒压不住,**2026-10-25 凌晨跑批时延维全部超时
    失明**(log 实锤"粗筛时延维查询失败"),还白烧两倍超时的重试预算。定 150 = 实测
    120 + 25% 余量。

    根治不靠继续调这里:多日分位数每次现算的路线本身撑不住数据增长,改走历史采样
    落库(Prometheus 只答轻量即时查询,历史分布读本地库),那之后 range 只剩轻量
    场景,本值再降回去。"""
# 可重试的上游错误:超时/连接抖动/协议中断。HTTP 4xx/5xx 不在此列——那是请求本身
# 有问题,重试只会再错一次。
_RETRY_ERRORS = (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError,
                 httpx.RemoteProtocolError)
SCAN_WINDOW_S = 300                 # 当前窗:近 5 分钟(同时是"紧邻前窗"的偏移量)
# (2026-10-15 注:曾在此加"分钟级持续性确认"(近 3 格 60s 窗至少 2 格越线才判异常)
# 压对照组的单窗噪声误报,离线回放实证净负收益后回退:GC 型间歇尖峰故障被误杀成
# 零锚点,锚点命中 4/12→3/12。单窗噪声与真故障在"24h×5min"的统计深度上不可分,
# 这条路不要重走——待办里"确定性层 vs LLM 权力分配"的 A/B 是它的正解。)

# ---------- 历史分布(判据的地基,2026-10-14 重构) ----------
# 基线从"某一个历史窗口的单点值"改成"过去 HISTORY_HOURS 小时的窗口值分布"。
# 为什么必须改:实测靶子上每个服务自己的 p95 就横跨两个数量级(recommendation
# p50 16ms / max 4666ms;ad p50 2ms / max 10000ms),拿"当前窗 / 24 小时前某个窗"
# 的比值排序,比的是**服务固有波动**而不是异常程度——对照组(零故障)照样能报出
# 一个 high 置信的根因。分布 + 分位位置(见 stats 模块)才有可比性。
#
# 2026-10-22 改成 7 天:24 小时的基线经不起一次故障——跑批一轮就把 24 小时里的
# 1/5 变成故障期,同款故障第二次发生时"历史里见过",判据失明(实测 ad 3.8% 判正常)。
# 基线长度是**污染稀释倍率**:7 天里坏 1 天只占 1/7。Datadog 用最多 6 周、Dynatrace
# 用 7~14 天,都是同一个道理。Prometheus 保留多久就用多久,查不到的部分自然为空。
HISTORY_HOURS = 168                 # 历史窗长度(7 天)
HISTORY_STEP_S = 300                # 历史采样步长(与 SCAN_WINDOW_S 对齐,不重叠)
# ⚠️ 上面两个是策略参数。语义:调长/调细 → 分布更稳但更慢;调短/调粗 → 更快但样本少。
# (被调方比调用方慢的容差已改为"调用方自身 p95 的摆动",见 scan_one——不再是倍数拍值。)

# "被失败调用指认"的强度门槛:**已废弃**(2026-10-15)——旧口径拿"该服务全量调用量"当
# 分母,被高频无关操作稀释(实测:cartFailure 的真因指认被 GetCart 稀释到 0.4% 永远过不了
# 线),现改为操作级占比 + 绝对量双门槛,复用 attribution.REF_STRONG_*(见 scan_one)。

# 全平台调用量指标族:OTel spanmetrics 连接器默认吐 traces_span_metrics_calls_total;
# 不同栈命名不同,按特征发现,不写死名字(与 collect.metric_avg 的族发现同一套思路)
_CALL_FAMILY_HINTS = ("traces_span_metrics_calls_total",)
_CALL_FAMILY_PATTERNS = (r"span.*metrics.*calls", r"spanmetrics.*calls", r"calls_total")
# 时延直方图族:粗筛必须看时延——纯时延故障(网络延迟/CPU 限流)不报错、不塌量,
# 只看调用量和错误率会把它整类判成"未见异常"(真实事故:product-catalog 注入 500ms 延迟,
# 粗筛报"各服务与自身基线比未见异常",整条诊断白跑)
_DUR_FAMILY_HINTS = ("traces_span_metrics_duration_milliseconds_bucket",)
_DUR_FAMILY_PATTERNS = (r"span.*metrics.*duration.*bucket", r"duration.*bucket")
_SERVICE_LABELS = ("service_name", "service", "service_id")
_STATUS_LABELS = ("status_code", "http_status_code", "status")
_KIND_LABELS = ("span_kind", "span.kind")
# 时延 p95 预计算序列(录制规则,scripts/gen_prom_rules.py 对环境发现后生成部署)。
# 存在时粗筛的**历史读取**改读它:多日直方图分位每次现算已实测压不住数据增长
# (2026-10-25:48h 跨度 120s > 90s 超时,时延维整维失明,见 RANGE_TIMEOUT 注释;
# 7 天数据外推单条 5min+,必死),预计算把同一表达式变成每 5 分钟一次的增量求值,
# 历史读取毫秒级。名字与生成脚本的 STAGE2 互为镜像,改名两处同步。
# 缺失时读侧退回原始表达式——慢路径兜底,功能不缺;旧录制件回放也自然走这条
# (录制里没有这个名字),回放兼容不靠特殊分支。
_LAT_P95_RECORDED = "service:span_latency_p95:5m"
_ERROR_STATUS_RE = r"(?i).*error.*|5.."


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
            async with _net.client(timeout) as c:
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
    from app.diagnosis import collect
    collect._spend()      # 粗筛/画像的查询走同一份预算——不能自己另开一条路绕开闸门
    params = {"query": expr}
    if at is not None:
        params["time"] = at
    body = await _get_json(f"{prom['url']}/api/v1/query", params, prom.get("headers"))
    return ((body or {}).get("data") or {}).get("result") or []


async def _discover(prom: dict) -> dict | None:
    """发现可用的调用量指标族 + 服务/状态/kind 标签。发现不了返回 None(如实说粗筛不可用)。"""
    from app.diagnosis import collect
    collect._spend()
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


def _latency_hist_expr(info: dict, raw_expr: str) -> str:
    """时延**历史**读取的表达式选择:预计算序列存在(录制规则已部署)用之,否则退回
    原始表达式(慢路径)。当前窗读取不走这里——预计算样本最多滞后一个规则周期,
    当前窗要新鲜;发病时刻检测也不走——它要分钟粒度,预计算只有 5 分钟粒度。"""
    return info.get("lat_p95_recorded") or raw_expr


async def _incident_windows(now: float) -> list[tuple[float, float]]:
    """历史窗内**已知**的故障时段,两条来源合并:
    1. 已确诊的诊断采样窗(存档里 verdict=root_cause/critical_failure);
    2. 外部登记的已知故障窗(known_incidents:跑批脚本注入窗/运维维护窗)——
       诊断判 inconclusive 的故障其指标值同样会污染历史分布,只靠确诊窗洗刷
       漏掉它们(2026-10-14 跑批实测:中午 5 例未确诊故障把晚上同款故障的分位
       判定全部稀释,localization 45.5%→2/11)。
    基线分布要剔除这些时段:故障期的指标值混进"正常范围"的估计,同款故障
    第二次发生时当前值会落回被污染的正常范围里,粗筛失明(实测:早间 cart 故障把
    历史错误率 max 抬到 0.33%,下午同款 0.14% 判成正常)。查不到/库不可读返回空,
    基线洗刷是增强,失败不拖垮粗筛。"""
    from datetime import datetime
    from app.db import repository
    out: list[tuple[float, float]] = []
    try:
        out.extend(await repository.recent_incident_windows(
            since=datetime.fromtimestamp(now - HISTORY_HOURS * 3600),
            until=datetime.fromtimestamp(now)))
    except Exception:  # noqa: BLE001
        pass
    from app.diagnosis import known_incidents
    out.extend(known_incidents.windows(now))
    return out


def _excluded(ts: float, windows: list[tuple[float, float]]) -> bool:
    """时刻是否落在任一已确诊故障窗内(前后各放宽 60s,覆盖窗口边界的采样对齐误差)。"""
    return any(ws - 60 <= ts <= we + 60 for ws, we in windows)


async def _history_dist(prom: dict, expr: str, now: float, label: str,
                        hours: int = HISTORY_HOURS,
                        exclude: list[tuple[float, float]] | None = None) -> dict:
    """一条**按服务聚合**的表达式在历史窗上的取值序列 → 每服务一个经验分布。

    这是"基线是分布不是点"的落点:一次 range query 拿全平台所有服务的历史窗口值,
    本地建分布——**查询条数与服务数无关**(按服务聚合,不是逐服务查)。
    表达式必须 `sum by (label)` 聚合过,否则这里拿不到服务名。
    exclude:已确诊故障时段,这些时刻的样本不进分布(见 _incident_windows)。"""
    from app.diagnosis import stats
    rows = await _range(prom, expr, now - hours * 3600, now, HISTORY_STEP_S)
    acc: dict[str, list] = {}
    for s in rows:
        svc = (s.get("metric") or {}).get(label)
        if not svc:
            continue
        vals = [v for ts, v in (s.get("values") or [])
                if not _excluded(float(ts), exclude or [])]
        acc.setdefault(svc, []).extend(vals)
    return {svc: stats.Distribution.build(vals) for svc, vals in acc.items()}


async def _cur_by_service(prom: dict, expr: str, label: str,
                          now: float | None = None) -> dict[str, float]:
    """即时查询并按服务聚合。NaN/±Inf 一律剔除——它们不是观测值。"""
    out: dict[str, float] = {}
    for s in await _query(prom, expr, now):
        svc = (s.get("metric") or {}).get(label)
        v = (s.get("value") or [None, None])[1]
        if not svc or v in (None, "NaN", "+Inf", "-Inf"):
            continue
        out[svc] = float(v)
    return out


async def _error_refs(prom: dict, info: dict, known: set[str],
                      now: float | None = None) -> dict[str, dict]:
    """被失败调用点名的服务:调用方 CLIENT span 报错时,它的 span_name 指向谁,谁就是被指认方。
    与诊断管线 localize 的错误指认同一套语义——"调用方正在它身上报错"比"它自己没量"硬得多,
    是区分"根因"和"被根因带的受害者"的关键(真实案例:payment 挂了 → checkout 的 Charge
    全失败,错误指认落在 payment 头上;email 只是跟着没量,没人往 email 上报错)。

    返回 {callee: {"rate", "ratio"}}:
    - rate:指向该服务的失败调用总速率(展示用);
    - ratio:**操作级错误占比**——分母是"同一 (调用方, span_name) 操作对的总调用量",
      不是该服务全量调用量。为什么必须改(2026-10-15 探针实验实锤):cartFailure 注入时,
      cart 的 server span 状态恒为 UNSET(错误只出现在 frontend 的 AddItem client span 上),
      而 AddItem 只占 cart 全量调用的 ~4%(高频 GetCart/valkey 稀释)——旧实现拿服务全量当
      分母,占比 0.4% 永远过不了门槛,真因被结构性淹没。分母砍到操作粒度:AddItem 错误率
      =100%(与 TraceRCA 的 confidence 同粒度)。
    now:求值时刻。缺省=此刻;离线回放必须传历史时刻——漏传的话回放拿到的是"现在"的
    错误指认,整场回放的锚点全被污染(实测:12 组锚点全被同一个 live 指认顶成 tier 1)。

    慢性背景错误过滤(2026-10-16 实测补):当前失败率还要**超出这条操作自己**的历史门槛
    才算指认。靶子上有长期存在的失败调用——checkout→CartService/EmptyCart 常年 100% 失败
    (0.008/s)、ad→flagd EventStream 常年 100% 失败(0.004/s)。旧的"占比 ≥50% + 绝对量
    ≥2 条/窗"两个门槛都拦不住它们(占比恒 100%,量又在 0.0067/s 的地板上下浮动),结果是
    **健康靶子上 cart 被永久顶成 tier 1**、跑批时与 flagd 轮流赢——锚点成了随机数,真故障
    的锚点也就丢了。判据只留一套:跟其余四维同源,问"当前值在自己历史上排第几"。慢性错误
    超不过自己的历史,新发故障超得过(cartFailure 把 AddItem 从 0 打到 100%)。该操作没有
    历史(新操作)时退回旧口径,不静默失明。"""
    kind, status = info.get("kind_label"), info.get("status_label")
    if not kind or not status:
        return {}
    from app.diagnosis import stats
    from app.diagnosis.localize import _op_points_to
    err_expr = (f'sum by ({info["service_label"]}, span_name) '
                f'(rate({{__name__="{info["family"]}",{kind}="SPAN_KIND_CLIENT",'
                f'{status}=~"{_ERROR_STATUS_RE}"}}[{SCAN_WINDOW_S}s]))')
    try:
        err_res = await _query(prom, err_expr, now)
        # 同操作对的总调用量(全状态):操作级占比的分母
        all_res = await _query(prom, f'sum by ({info["service_label"]}, span_name) '
                                     f'(rate({{__name__="{info["family"]}",{kind}="SPAN_KIND_CLIENT"}}'
                                     f'[{SCAN_WINDOW_S}s]))', now)
    except Exception:  # noqa: BLE001  排序修正尽力而为,失败不影响粗筛
        return {}
    # 历史基线:同一个错误表达式在过去 HISTORY_HOURS 上的分布,按 (调用方, 操作名) 建。
    # 一条 range query 拿全平台,查询条数与操作数无关。
    _hist: dict[tuple, object] = {}
    try:
        _now = time.time() if now is None else now
        rows = await _range(prom, err_expr, _now - HISTORY_HOURS * 3600, _now, HISTORY_STEP_S)
        _acc: dict[tuple, list] = {}
        for s in rows:
            m = s["metric"]
            _acc.setdefault((m.get(info["service_label"], "?"),
                             m.get("span_name", "")), []).extend(
                float(v) for _, v in (s.get("values") or []) if v not in ("NaN", "+Inf", "-Inf"))
        _hist = {k: stats.Distribution.build(v) for k, v in _acc.items()}
    except Exception:  # noqa: BLE001  历史取不到 → 退回旧口径(宁可多报,不静默失明)
        _hist = {}
    op_total: dict[tuple, float] = {}
    for s in all_res:
        m = s["metric"]
        op_total[(m.get(info["service_label"], "?"), m.get("span_name", ""))] = float(s["value"][1])
    out: dict[str, dict] = {}
    for s in err_res:
        caller = s["metric"].get(info["service_label"], "?")
        op = s["metric"].get("span_name", "")
        err_rate = float(s["value"][1])
        callee = next((k for k in sorted(known) if k != caller and _op_points_to(op, k)), None)
        if not callee:
            continue
        _thr = stats.anomaly_threshold(_hist.get((caller, op)))
        if _thr is not None and err_rate <= _thr:
            continue      # 慢性背景错误:没超出它自己的历史水平,不是新故障
        total = op_total.get((caller, op), 0.0)
        ratio = (err_rate / total) if total > 0 else 0.0
        prev = out.get(callee)
        out[callee] = {"rate": (prev or {}).get("rate", 0.0) + err_rate,
                       "ratio": max((prev or {}).get("ratio", 0.0), ratio)}
    return out


async def _call_edges(prom: dict, info: dict, known: set[str],
                      now: float | None = None) -> dict[str, set[str]]:
    """调用方 → 它调到的服务集合(从 CLIENT span 指标的 span_name 推,与错误指认同一套映射)。

    只用于级联排序修正:时延型故障里受害者的 p95 可能涨得比真凶还多(上游把下游的延迟又叠了
    一层),按"涨了多少倍"排会选到最响的受害者(实测:frontend 涨 107 倍、真凶
    product-catalog 只涨 58 倍)。真凶的判据是"它的涨法无法被下游解释"。
    推不出来返回空 dict,不硬凑。"""
    kind = info.get("kind_label")
    if not kind:
        return {}
    try:
        res = await _query(prom, f'sum by ({info["service_label"]}, span_name) '
                                f'(rate({{__name__="{info["family"]}",{kind}="SPAN_KIND_CLIENT"}}'
                                f'[{SCAN_WINDOW_S}s]))', now)
    except Exception:  # noqa: BLE001  排序修正尽力而为,失败不影响粗筛
        return {}
    from app.diagnosis.localize import _op_points_to
    out: dict[str, set[str]] = {}
    for s in res:
        caller = s["metric"].get(info["service_label"], "?")
        op = s["metric"].get("span_name", "")
        callee = next((k for k in sorted(known) if k != caller and _op_points_to(op, k)), None)
        if callee:
            out.setdefault(caller, set()).add(callee)
    return out


async def _role_rates(prom: dict, info: dict, now: float | None = None) -> dict[str, tuple[float, float]]:
    """每个服务的 (入站 SERVER 速率, 出站 CLIENT 速率)。

    用来判**角色**,不认名字:一个真正的服务会收到请求(SERVER span),而压测机、
    浏览器探针这类"纯调用方"只有出站调用、永远没有入站。它们出错是**别人坏了的
    表现**,不是根因——但它们的错误率/时延往往比真凶还显眼(浏览器端把整页的耗时
    都算在自己头上),所以会长期霸占锚点(2026-10-22 实测:frontend-web 和
    load-generator 一条 SERVER span 都没有,却接连被当成根因)。

    查不到就返回空:角色判不了时不排除任何服务,退化成原来的行为。
    """
    kind = info.get("kind_label")
    if not kind:
        return {}
    expr = (f'sum by ({info["service_label"]}, {kind}) '
            f'(rate({{__name__="{info["family"]}"}}[{SCAN_WINDOW_S}s]))')
    try:
        rows = await _query(prom, expr, now)
    except Exception:  # noqa: BLE001  角色识别是增强,失败不影响粗筛
        return {}
    out: dict[str, list[float]] = {}
    for s in rows:
        svc = (s.get("metric") or {}).get(info["service_label"])
        k = (s.get("metric") or {}).get(kind)
        v = float((s.get("value") or [None, 0])[1] or 0.0)
        slot = out.setdefault(svc, [0.0, 0.0])
        if k == "SPAN_KIND_SERVER":
            slot[0] += v
        elif k == "SPAN_KIND_CLIENT":
            slot[1] += v
    return {k: (v[0], v[1]) for k, v in out.items()}


def _med(dist) -> float | None:
    """历史分布的中位数,给旧字段(base_*)用——下游(deviation/faulttype/证据行)
    读的是这些字段,值换成"历史中位数"语义更稳(单点基线没了)。"""
    v = dist.p50 if dist is not None and dist.values else None
    return round(v, 4) if v is not None else None


def _r4(v) -> float | None:
    return round(v, 4) if v is not None else None


def _r1(v) -> float | None:
    return round(v, 1) if v is not None else None


async def _profile_all(prom: dict, info: dict, known: set[str] | None = None,
                       now: float | None = None, hours: int = HISTORY_HOURS,
                       ) -> dict[str, dict]:
    """全平台画像:每个服务的「当前窗值 + 自身历史分布 + 统计判据」。

    **判据是分位位置,不是窗口比值**(理由见 stats 模块头部与 HISTORY_HOURS 注释)。
    每个 profile 里的 `dev` 是四条统计判据(错误率/时延/调用量/CPU),`abnormal_dims`
    由它们导出——与 deviation.ServiceDeviation.abnormal_dims 同构,下游消费方式不变。

    旧实现是"当前窗 / 基线窗"的比值,基线还要在"紧邻前窗 / 24h 前 / 6h 前"之间挑,
    挑的时候带一层"前窗是否已被污染"的自检——那套逻辑在**慢性变慢的服务**上有反作用:
    服务近几小时一直慢 → 自检判定"前窗已含故障" → 退回 24h 前 → 拿 24h 前的低点当基线
    → 把"漂移"报成"阶跃"。对照组(零故障)误报 high 置信根因就是这么来的。

    known:已知服务名集合(CPU 族靠标签值匹配服务名,需要它)。
    now :求值时刻。缺省=此刻;离线回放传历史时刻。

    返回 (profiles, gaps):gaps 是本轮查询失败而不可见的维度清单(人话,直接进
    证据/事件流)——查询失败绝不冒充健康,也不冒充异常,如实说"这维看不到"。
    调用量维除外:它挂了整场粗筛没有意义,异常继续向上冒(见函数体内注释)。
    """
    from app.diagnosis import stats
    now = time.time() if now is None else now
    label = info["service_label"]
    fam = info["family"]
    status = info.get("status_label")
    dur = info.get("dur_family")
    empty = stats.Distribution()
    # 基线洗刷:已确诊故障时段的样本不进历史分布(取一次,四条维共用)
    incidents = await _incident_windows(now)
    if incidents:
        logger.info("基线剔除 %d 个已确诊故障时段", len(incidents))

    calls_expr = f'sum by ({label}) (rate({{__name__="{fam}"}}[{SCAN_WINDOW_S}s]))'
    calls_cur = await _cur_by_service(prom, calls_expr, label, now)
    calls_hist = await _history_dist(prom, calls_expr, now, label, hours=hours,
                                     exclude=incidents)
    # CPU 族靠"标签值匹配服务名"聚合,需要已知服务名集合;不传就用调用量里出现的服务。
    if known is None:
        known = set(calls_cur) | set(calls_hist)

    err_cur: dict[str, float] = {}
    err_hist: dict = {}
    gaps: list[str] = []
    # 单维失败不再拖垮整场粗筛(2026-10-18 实测:时延维那条 24h 直方图 range 在靶机忙时
    # 超时,异常一路冒到 scan_platforms,整组被判 no_data → 整单 inconclusive——
    # "一条查询超时,全平台失明")。单维查询失败 → 该维记缺口、本轮不可见,其余维
    # 照常判定;缺口如实上报,**绝不把查询失败当成健康**。
    # 调用量维不降级:服务清单与画像的服务全集都靠它,它挂了等于什么都没有,整场失败
    # 是唯一诚实的出口(异常继续向上冒,由 scan_platforms 记"粗筛查询失败")。
    if status:
        num = (f'sum by ({label}) (rate({{__name__="{fam}",'
               f'{status}=~"{_ERROR_STATUS_RE}"}}[{SCAN_WINDOW_S}s]))')
        den = f'sum by ({label}) (rate({{__name__="{fam}"}}[{SCAN_WINDOW_S}s]))'
        # `or` 兜底:从不出错的服务在 PromQL 里**没有序列**(分子不存在 → 整条表达式为空)。
        # 不兜底,"首次出错"这类最重要的信号会整类消失——补 0 让分布可建,于是
        # "当前值 > 历史最大值(0)"自然判异常。**这就是不需要绝对量地板的原因。**
        err_expr = f'({num} / {den}) or (0 * {den})'
        try:
            err_cur = await _cur_by_service(prom, err_expr, label, now)
            err_hist = await _history_dist(prom, err_expr, now, label, hours=hours,
                                           exclude=incidents)
        except Exception as exc:  # noqa: BLE001  错误维失败 → 记缺口,其余维继续
            hint = "(回放未录制该查询,属预期)" if _net.current_mode() == "replay" else ""
            logger.warning("粗筛错误率维查询失败%s:%s", hint, exc)
            gaps.append(f"错误率维查询失败({type(exc).__name__}),该维度本轮不可见")

    lat_cur: dict[str, float] = {}
    lat_hist: dict = {}
    if dur:
        lat_expr = (f'histogram_quantile(0.95, sum by (le, {label}) '
                    f'(rate({dur}[{SCAN_WINDOW_S}s])))')
        try:
            lat_cur = await _cur_by_service(prom, lat_expr, label, now)
            lat_hist = await _history_dist(prom, _latency_hist_expr(info, lat_expr),
                                           now, label, hours=hours,
                                           exclude=incidents)
        except Exception as exc:  # noqa: BLE001  时延维失败 → 记缺口,其余维继续
            hint = "(回放未录制该查询,属预期)" if _net.current_mode() == "replay" else ""
            logger.warning("粗筛时延维查询失败%s:%s", hint, exc)
            gaps.append(f"时延维查询失败({type(exc).__name__}),该维度本轮不可见")

    cpu_cur, cpu_hist, cpu_gap = await _res_profile(prom, known, metric="cpu",
                                                    now=now, exclude=incidents, hours=hours)
    mem_cur, mem_hist, mem_gap = await _res_profile(prom, known, metric="memory",
                                                    now=now, exclude=incidents, hours=hours)
    gaps += [g for g in (cpu_gap, mem_gap) if g]

    out: dict[str, dict] = {}
    services = sorted(set(calls_cur) | set(calls_hist) | set(err_cur) | set(lat_cur)
                      | set(cpu_cur) | set(mem_cur))
    # 本次扫描一共判多少次:每个服务 6 个维度。门槛要按这个数摊(Bonferroni),
    # 否则"每次扫描 1% 误报"摊到 84 次判断上,健康平台每次扫描都有过半概率冒假锚点。
    n_tests = len(services) * 6
    for svc in services:
        dist = {
            "error": err_hist.get(svc, empty),
            "latency": lat_hist.get(svc, empty),
            "calls": calls_hist.get(svc, empty),
            "cpu": cpu_hist.get(svc, empty),
            "memory": mem_hist.get(svc, empty),
        }
        # 判"异常"用边界(高分位 + n×自身波动),分位位置只做层内排序(见 stats.band)。
        # 错误率维额外传调用速率:错误率是比率,折成"本窗几次事件"必须乘调用量,
        # 否则"1 个请求失败"也能撑起一个 50% 的错误率(低流量保护,见 stats.deviation)。
        dev = {
            "error": stats.deviation(err_cur.get(svc), dist["error"], label="错误率",
                                     window_s=SCAN_WINDOW_S, tests=n_tests,
                                     calls_rate=calls_cur.get(svc)),
            "latency": stats.deviation(lat_cur.get(svc), dist["latency"], label="p95 时延",
                                       window_s=SCAN_WINDOW_S, tests=n_tests),
            "calls": stats.deviation(calls_cur.get(svc), dist["calls"],
                                     label="调用量", direction="low",
                                     window_s=SCAN_WINDOW_S, tests=n_tests),
            # 同一份分布再看一次"高出历史":塌陷和暴涨是两种完全不同的故障形态
            # (失联 vs 被洪泛),不能只判一侧。
            "calls_up": stats.deviation(calls_cur.get(svc), dist["calls"],
                                        label="调用量", direction="high",
                                        window_s=SCAN_WINDOW_S, tests=n_tests),
            "cpu": stats.deviation(cpu_cur.get(svc), dist["cpu"], label="CPU",
                                   window_s=SCAN_WINDOW_S, tests=n_tests),
            "memory": stats.deviation(mem_cur.get(svc), dist["memory"], label="内存",
                                      window_s=SCAN_WINDOW_S, tests=n_tests),
        }
        dims = sorted(name for name, d in dev.items() if d.abnormal)
        c_now = calls_cur.get(svc, 0.0)
        out[svc] = {
            "calls": round(c_now, 4),
            "base_calls": _med(dist["calls"]),
            "err_rate": _r4(err_cur.get(svc)),
            "base_err_rate": _med(dist["error"]),
            "p95_ms": _r1(lat_cur.get(svc)),
            "base_p95_ms": _med(dist["latency"]),
            "cpu": _r4(cpu_cur.get(svc)),
            "base_cpu": _med(dist["cpu"]),
            "mem": _r4(mem_cur.get(svc)),
            "base_mem": _med(dist["memory"]),
            "traffic_collapse": "calls" in dims,
            "abnormal_dims": dims,
            "dev": dev,
            # 内部结构:分布对象给 metric_onsets 定阈值用。**不进任何序列化路径**
            # (profile 只在进程内传给 deviation/faulttype/证据行)。
            "_dist": dist,
            "history_n": dist["calls"].n,
            "baseline": f"自身历史分布({HISTORY_HOURS}h)",
            # 逐维明细(可序列化):没结论的时候,用户最想知道"你到底查了什么、
            # 每个服务离边界多远"。dev 里的 Deviation 对象不能进序列化路径,
            # 这里抽成纯数字供卡片展示。
            "dev_view": {k: {"v": _r4(d.value), "edge": _r4(d.band),
                             "ab": bool(d.abnormal),
                             "pos": (round(d.position, 3) if d.position is not None else None)}
                         for k, d in dev.items()},
        }
    # 样本不足 ≠ 未见异常:历史窗还没攒够(判据要 100 个 5 分钟窗)时,每个服务每一维
    # 都会返回"样本不足,不做判断",position 全是 None。这时候上层如果只说"未见异常",
    # 值班员会当成"平台是健康的"——而实际上我们根本没看。2026-10-23 清库后现场复现过:
    # 只有半小时数据,诊断报"平台正常"。这里如实返回"没得判"。
    if out and not any(d.position is not None
                       for p in out.values() for d in p["dev"].values()):
        return {}, ["基线样本不足:历史窗还没攒够判据要求的下限,本轮无法判断异常"]
    return out, gaps


async def service_profiles(prom: dict, services: list[str] | None = None,
                           now: float | None = None,
                           hours: int = HISTORY_HOURS) -> dict[str, dict]:
    """取"指标画像",供诊断主链使用(trace 是抽样,指标是连续量,互相印证)。

    services=None 表示**全平台**(诊断主链就该拿全量——错误率/时延的纵向基线要对所有服务生效,
    而且"指标上异常但不在症状链路里"的服务也要能被召回,召回不能被症状链路的跳数锁死)。

    顺带补一个盲区:自身零 span 的服务,_target_silence 的"窗内至少 5 条自身 span"
    前提不成立,判不出挂死;而"调用量相对自己的历史分布塌了"是实打实的证据。
    指标族发现不了或查询失败就返回空 dict——调用方如实记缺口,不编数。

    now:求值时刻。缺省=此刻;离线回放传历史时刻,让同一套判据能跑在过去。"""
    try:
        info = await _discover(prom)
        if info is None:
            return {}
        profiles, gaps = await _profile_all(prom, info,
                                            known=set(services) if services else None,
                                            now=now, hours=hours)
        if gaps:
            logger.warning("指标画像部分维度不可见:%s", "；".join(gaps))
    except Exception:  # noqa: BLE001  证据增强尽力而为,失败不打断诊断
        logger.warning("指标画像查询失败", exc_info=True)
        return {}
    if services is None:
        return profiles
    return {s: profiles[s] for s in services if s in profiles}


async def metric_edges(prom: dict, known: set[str]) -> set[tuple[str, str]]:
    """指标推的全平台调用边(caller → callee),给诊断层补全 trace 图用。

    只用 trace 建图的话,图只覆盖"穿过症状服务的链路"——不在症状链路上的根因天然不可见,
    候选召回被锁死("真因没进候选集"是检索失败,无解;排错序还能救,所以召回优先)。
    指标边只增不减,不会让本来可达的变不可达。推不出来返回空集。"""
    try:
        info = await _discover(prom)
        if info is None:
            return set()
        edges = await _call_edges(prom, info, known)
    except Exception:  # noqa: BLE001
        logger.warning("指标边查询失败", exc_info=True)
        return set()
    return {(a, b) for a, bs in edges.items() for b in bs}


async def _range(prom: dict, expr: str, start: float, end: float, step: int) -> list[dict]:
    from app.diagnosis import collect
    collect._spend()
    body = await _get_json(f"{prom['url']}/api/v1/query_range",
                           {"query": expr, "start": start, "end": end, "step": step},
                           prom.get("headers"), timeout=RANGE_TIMEOUT)
    return ((body or {}).get("data") or {}).get("result") or []


async def metric_onsets(prom: dict, *, window_start: float, window_end: float,
                        baselines: dict[str, dict],
                        lookback_min: int = 30) -> dict[str, dict]:
    """指标侧发病时刻:按**分钟粒度**找"第一次越过自身基线"的时刻。

    为什么要它:trace 侧的 first_anomaly_us 有**父子偏置**——同一个 trace 里父 span 必然早于
    子 span,级联里上游永远显得更早,时间信号系统性偏袒上游(实测:症状 frontend 时,纯受害者
    frontend-proxy 因"更早"被加权到候选第一)。指标是连续量、按分钟采样,同刻异常在分钟粒度
    上会并列,父子顺序的假象就没了。

    搜索范围**往前扩 lookback_min 分钟**:采样窗只看得到窗内,故障早于窗开始时,trace 锚点会
    贴窗起点而被判不可信(越是快速故障越吃亏);指标能看见窗之前。落在搜索起点的锚点仍然
    不可信(说明范围内看不到更早),直接不给——宁可没有,不给假的。

    只花 2 条 range query(按服务聚合,不是逐服务查)。取不到返回空 dict——调用方退回
    trace 锚点,不硬凑。返回 {service: {"onset_us", "what"}}。

    阈值判据与粗筛**同一套**:取该服务自身历史分布的异常门槛分位
    (`stats.anomaly_threshold`,与异常判定同一个入口),不再用"基线 + 固定涨幅"那套拍值
    ——同一份数据两处判据不一致,是比没有数据更糟的事。"""
    from app.diagnosis import stats
    from app.diagnosis.deviation import gap_fence, last_episode_start
    info = await _discover(prom)
    if info is None or not baselines:
        return {}
    start = window_start - lookback_min * 60
    span = max(window_end - start, 60.0)
    step = int(max(30.0, min(120.0, span / 30)))         # 粒度:分钟级,最多 ~30 个点
    label = info["service_label"]
    fam = info["family"]
    if info.get("status_label"):
        # 与 _profile_all 同一套 `or` 兜底:从不出错的服务在 PromQL 里没有序列,
        # 不兜底的话"首次出错"的时刻整类取不到。
        num = (f'sum by ({label}) (rate({{__name__="{fam}",'
               f'{info["status_label"]}=~"{_ERROR_STATUS_RE}"}}[{SCAN_WINDOW_S}s]))')
        den = f'sum by ({label}) (rate({{__name__="{fam}"}}[{SCAN_WINDOW_S}s]))'
        err_expr: str | None = f'({num} / {den}) or (0 * {den})'
    else:
        err_expr = None
    lat_expr = (f'histogram_quantile(0.95, sum by (le, {label}) '
                f'(rate({info["dur_family"]}[{SCAN_WINDOW_S}s])))') if info.get("dur_family") else None
    out: dict[str, dict] = {}
    try:
        series: dict[str, list[dict]] = {}
        if err_expr:
            series["error"] = await _range(prom, err_expr, start, window_end, step)
        if lat_expr:
            series["latency"] = await _range(prom, lat_expr, start, window_end, step)
    except Exception:  # noqa: BLE001  时间锚是增强项,取不到就退回 trace 锚
        logger.warning("指标发病时刻查询失败", exc_info=True)
        return {}
    for what, rows in series.items():
        for s in rows:
            svc = s["metric"].get(label, "?")
            dist = ((baselines.get(svc) or {}).get("_dist") or {}).get(what)
            thr = stats.anomaly_threshold(dist)   # 与粗筛异常判定同一入口,判据不分叉
            if thr is None:
                continue      # 该服务该维没有历史分布 → 不给阈值,宁可没有也不给假的
            # 取**最近一场**越限的起点,不是第一次越限——搜索窗(前扩 30min)里同样装着
            # 上一场故障的旧越限,取第一次会把发病时刻定到上一场头上(与 trace 侧
            # deviation.last_episode_start 同一条纪律)。
            crossings = [int(float(ts) * 1e6) for ts, raw in (s.get("values") or [])
                         if raw not in ("NaN", "+Inf", "-Inf") and float(raw) > thr]
            onset_us = last_episode_start(crossings)
            # 场次必须**还活着**才算数:最后一条越限距当前窗超过"一场病"的间隔,说明这场病
            # 已经结束——那是上一场故障的残留记录,不能当"当前发病时刻"
            # (真实事故:image_slow_load 那场把 checkout 的时延 p99 打穿过一次,30min
            # 前视窗里留下唯一一条越限,被当成 checkout 的"当前发病时刻";当前场
            # payment 故障是快速失败、checkout 时延根本不越限,于是真根因 payment
            # 被这个旧锚点比出 1440s 的假颠倒,遭时间反证误杀)。
            # 界线 = 越限时刻**自己**的间隔 Tukey 上界(见 deviation.gap_fence),不再写死 300s;
            # 样本不足(切不出场次)时 fence=0 → 不做"是否已结束"的推断(宁少切不多切)。
            last_us = max(crossings) if crossings else None
            fence = gap_fence([b - a for a, b in zip(sorted(crossings), sorted(crossings)[1:])])
            if (onset_us is None or last_us is None
                    or (fence and last_us < window_end * 1e6 - fence)
                    or onset_us <= (start + step) * 1e6):
                # 落在搜索起点 = 这个范围内看不到更早的时刻 → 不可信,宁可没有也不给假的
                continue
            onset = onset_us / 1e6
            cur = out.get(svc)
            if cur is None or onset < cur["onset_us"] / 1e6:
                # step_us 随 onset 一起给出:它就是这个 onset 信号的采样分辨率,
                # 下游判"起点贴窗边"的可信度时按它算(不再写死 60s)
                out[svc] = {"onset_us": int(onset * 1e6), "what": what,
                            "step_us": int(step * 1e6)}
    return out


async def _res_profile(prom: dict, known: set[str], *, metric: str = "cpu",
                       now: float | None = None,
                       exclude: list[tuple[float, float]] | None = None,
                       hours: int = HISTORY_HOURS
                       ) -> tuple[dict[str, float], dict, str | None]:
    """全平台资源画像:当前窗均值 + **自身历史分布**。metric ∈ {"cpu","memory"}。

    族发现与"标签值匹配服务名"和 collect.metric_avg 同一套口径——同一份数据两处
    算法不一致,是比没有数据更糟的事。**两条查询**(当前窗一条即时 + 历史一条 range),
    不逐服务查。取不到返回空(平台没采该指标),调用方如实跳过这一维。
    返回 (cur, hist, gap):gap 非 None 表示**查询失败**(区别于"平台没采该指标"),
    与错误率/时延维的缺口同一套上报口径——查询失败不当健康。

    旧实现是"当前窗均值 / 远基线窗均值"的裸比值,和时延维同一个病:服务自身资源
    本来就抖,比值大小反映波动不反映异常。改成分布 + 分位位置后口径与其余三维一致
    ——CPU 与内存走同一条路,不再有"内存靠 2 倍阈值"这种两套判据。
    """
    import re
    from app.diagnosis.collect import _CPU_FAMILIES, _MEM_FAMILIES
    from app.diagnosis import stats
    now = time.time() if now is None else now
    is_cpu = metric == "cpu"
    pref = _CPU_FAMILIES if is_cpu else _MEM_FAMILIES
    # 兜底正则:优先挑"容器/进程级"的族(与 collect.metric_avg 的发现口径一致)
    hint = r"cpu" if is_cpu else r"memory|rss|working_set"
    scope = r"container|process|node|system"
    try:
        async with _net.client(SCAN_TIMEOUT) as c:
            from app.diagnosis import collect
            collect._spend()
            r = await c.get(f"{prom['url']}/api/v1/label/__name__/values",
                            headers=prom.get("headers"))
            r.raise_for_status()
            families = (r.json() or {}).get("data") or []
        picked = [f for f in pref if f in families] \
            or [f for f in families if re.search(hint, f, re.I) and re.search(scope, f, re.I)] \
            or [f for f in families if re.search(hint, f, re.I)]
        if not picked:
            return {}, {}, None     # 平台没采这类指标,不是查询失败,不算缺口
        family = picked[0]
        inner = (f"rate({{__name__=\"{family}\"}}[{SCAN_WINDOW_S}s])"
                 if family.endswith("_total")
                 else f"avg_over_time({{__name__=\"{family}\"}}[{SCAN_WINDOW_S}s])")

        def _to_cores(svc: str, vals: list[float]) -> list[float]:
            # 只有 CPU 的纳秒计数要折算成核;内存族保持原单位(字节)
            return [v / 1e9 for v in vals] if (is_cpu and "nanoseconds" in family) else vals

        def _merge(acc: dict[str, list[float]], rows: list[dict], key: str) -> None:
            for s in rows:
                svc = _match_service(s.get("metric") or {}, known)
                if svc is None:
                    continue
                if key == "values":
                    raw = []
                    for ts, v in (s.get("values") or []):
                        if v in ("NaN", "+Inf", "-Inf") or _excluded(float(ts), exclude or []):
                            continue
                        raw.append(float(v))
                else:
                    raw = ([float(s["value"][1])] if (s.get("value") and
                           s["value"][1] not in ("NaN", "+Inf", "-Inf")) else [])
                if raw:
                    acc.setdefault(svc, []).extend(_to_cores(svc, raw))

        cur_acc: dict[str, list[float]] = {}
        _merge(cur_acc, await _query(prom, inner, now), "value")
        hist_acc: dict[str, list[float]] = {}
        _merge(hist_acc, await _range(prom, inner, now - hours * 3600,
                                      now, HISTORY_STEP_S), "values")
        cur = {svc: sum(v) / len(v) for svc, v in cur_acc.items() if v}
        hist = {svc: stats.Distribution.build(v) for svc, v in hist_acc.items()}
        return cur, hist, None
    except Exception as exc:  # noqa: BLE001  资源维尽力而为,失败不影响其余三维粗筛
        # 回放时"未命中"是预期(录制里没有这条新查询),别打整段栈吓人。
        # 注意:**这里不能写 `from app.diagnosis import net as _net`** —— 函数内出现同名
        # 导入会让 `_net` 在整个函数作用域里变成局部名,上面 try 里的 `_net.client(...)`
        # 会直接 UnboundLocalError,资源维整条哑掉(真事故:补录那轮 CPU/内存全空)。
        # 模块级已经 `from app.diagnosis import net as _net`,直接用。
        hint = "(回放未录制该查询,属预期)" if _net.current_mode() == "replay" else ""
        logger.warning("粗筛资源维查询失败%s:%s", hint, exc)
        dim = "CPU" if is_cpu else "内存"
        return {}, {}, f"{dim}维查询失败({type(exc).__name__}),该维度本轮不可见"


def _match_service(labels: dict, known: set[str]) -> str | None:
    """指标标签值 → 服务名,与 collect.metric_avg 同一条匹配规则:
    服务名 == 标签值,或服务名是标签值按 / 切段后的其中一段。"""
    for v in labels.values():
        v = str(v).lower()
        for k in known:
            if k.lower() == v or k.lower() in v.split("/"):
                return k
    return None


# 重启信号的标准指标族(K8s kube-state-metrics 与 cAdvisor 两套命名都收):
# 重启计数器增量是"窗内重启过"的直接实锤,比"遥测静默"更机制级;OOMKilled 原因
# 直接点名内存耗尽。平台没采这些指标就如实跳过,不编数(与 CPU 画像同一发现模式)。
_RESTART_FAMILY = "kube_pod_container_status_restarts_total"
_OOM_FAMILY = "kube_pod_container_status_last_terminated_reason"
_START_FAMILY = "container_start_time_seconds"


async def restart_signals(prom: dict, known: set[str],
                          window_start: float, window_end: float) -> dict[str, dict]:
    """窗内重启/OOM 信号(标准 K8s/cAdvisor 指标,平台采了才查,≤3 条查询):
    - kube_pod_container_status_restarts_total 窗内增量 >0 → 容器重启过;
    - 增量序列带 OOMKilled 原因(或 OOM 原因族窗内增量>0)→ 内存耗尽被杀,机制直接点名;
    - 无 kube-state-metrics 时退回 container_start_time_seconds(启动时刻落在窗内 → 新启动)。
    服务映射用 _match_service(与 CPU 画像同一条规则)。指标族不存在返回空 dict。"""
    try:
        async with _net.client(SCAN_TIMEOUT) as c:
            from app.diagnosis import collect
            collect._spend()
            r = await c.get(f"{prom['url']}/api/v1/label/__name__/values",
                            headers=prom.get("headers"))
            r.raise_for_status()
            families = (r.json() or {}).get("data") or []
        out: dict[str, dict] = {}
        window_s = max(int(window_end - window_start), 60)
        if _RESTART_FAMILY in families:
            expr = (f'topk(200, increase({{_name__="{_RESTART_FAMILY}"}}[{window_s}s]))')
            oom_expr = (f'topk(200, increase({{_name__="{_OOM_FAMILY}",reason="OOMKilled"}}'
                        f'[{window_s}s]))') if _OOM_FAMILY in families else None
            rows = await _query(prom, expr)
            oom_rows = await _query(prom, oom_expr) if oom_expr else []
            oom_svcs = {s for s in (_match_service(x.get("metric") or {}, known)
                                    for x in oom_rows)
                        if s is not None and float(x.get("value", [0, 0])[1] or 0) > 0}
            for s in rows:
                svc = _match_service(s.get("metric") or {}, known)
                if svc is None or not s.get("value"):
                    continue
                inc = float(s["value"][1])
                if inc <= 0:
                    continue
                oom = svc in oom_svcs
                prev = out.get(svc)
                out[svc] = {"restarts": inc + ((prev or {}).get("restarts") or 0),
                            "oom": oom or bool((prev or {}).get("oom"))}
        elif _START_FAMILY in families:
            rows = await _query(prom, f'topk(200, {{__name__="{_START_FAMILY}"}})')
            for s in rows:
                svc = _match_service(s.get("metric") or {}, known)
                if svc is None or not s.get("value"):
                    continue
                started = float(s["value"][1])
                if window_start < started <= window_end:
                    out.setdefault(svc, {"restarts": 1, "oom": False})
        return out
    except Exception:  # noqa: BLE001  重启维尽力而为,失败不影响其余维度
        logger.warning("重启信号查询失败", exc_info=True)
        return {}


# ---------- 粗筛可疑度分层(与 localize 同一套判据:分层优先,幅度只做层内排序) ----------
# 为什么必须分层(2026-10-14 跑批实测):旧实现是"各维偏离幅度取 max 当分数,按分数
# 排序"——谁最响谁当锚点。级联故障里受害者必然比真凶响,慢性变慢的服务永远比稳定
# 服务响:对照组(零故障)照样报出 high 置信根因。分层表达的是**固定的因果优先级**,
# 幅度只在同层内比大小,跨层不可比。
#
# 层级定义(数字越小越可疑):
#   0 状态变化   窗内有容器重启/OOM **且**自身另有劣化——机制级实锤。依据 Datadog
#                Watchdog 的规则:根因是"状态变化",性能劣化本身不是根因。
#   1 错误指认   调用方的失败调用直接指向它("调用方正在它身上报错")——因果向硬证据。
#   2 流量塌陷   调用量低于自身历史分布——挂死/失联形态。
#   3 错误率     错误率高于自身历史分布。
#   4 时延(源头) 时延高于自身历史分布,且下游解释不了(self time)。
#   5 资源       CPU 高于自身历史分布——疑点,不是红信号。
#   6 时延(中继) 时延异常但被下游完全解释——受害者,不作锚点。
TIER_STATE_CHANGE = 0
TIER_BLAMED = 1
TIER_TRAFFIC = 2
# 调用量暴涨:和"塌陷"分开一层。塌陷=服务失联(强信号),暴涨=有人在猛打它
# (压测/洪泛/重试风暴)。两者都直接指向"谁出事了",所以都排在错误/时延之前。
TIER_TRAFFIC_SURGE = 3
TIER_ERROR = 4
TIER_LATENCY_SELF = 5
TIER_RESOURCE = 6
TIER_LATENCY_RELAY = 7
# 出口门槛:最高层不高于这一档才认锚点。纯中继(tier 6)的异常完全被下游解释,
# 它是受害者——把它当锚点等于"从受害者身上开始查",方向就错了。
TIER_ANCHOR_MAX = TIER_RESOURCE


def _load_explained(dev: dict) -> bool:
    """资源偏离是否被自身负载解释:吞吐在涨,且资源涨得**没比吞吐多**。

    只看"流量涨了"不行——ad 的 CPU 涨到自身历史 58 倍时,它的调用量也只多了 5%,
    用"流量在涨"一句话就把它解释掉,整组回归成 inconclusive(2026-10-18 实测)。
    成比例才是"被负载推上去"的形态:压测机内存 ×1.19、吞吐 ×1.35 → 解释得掉。
    """
    calls_ratio = dev["calls"].ratio
    res_ratio = max([r for r in (dev["cpu"].ratio, dev["memory"].ratio)
                     if r is not None], default=None)
    return bool(not dev["calls"].abnormal and calls_ratio is not None and calls_ratio > 1.0
                and res_ratio is not None and res_ratio <= calls_ratio)


def _tier(state_change: bool, blamed: bool, dev: dict,
          self_latency: bool | None, load_explained: bool = False) -> int | None:
    """可疑度分层。返回 None = 不构成锚点。

    state_change 必须**另有劣化**才算 tier 0——正常滚动重启不该单独成锚
    (旧实现的注释就写着"重启本身不足以独立成锚",这里把同一语义表达成层级)。
    blamed 必须是**过了强度门槛的**指认(见 REF_MIN_RATIO),否则大流量下几条偶发
    失败就能把任意服务顶成锚点。
    load_explained:CPU/内存涨、同时自身吞吐也在涨 → 资源是被负载推上去的,不是故障。
    判据与"时延被下游解释就不算它的可疑度"同源:**能被别人解释的异常,不单独成锚**。
    真实代价(2026-10-17 对照组):压测机被洪峰组反复折腾后内存 841MB 涨到自身 p99.2,
    健康平台上被报成根因、置信 high——它不是坏了,是活干多了。"""
    surge = dev.get("calls_up")
    has_other = (blamed or dev["calls"].abnormal or (surge is not None and surge.abnormal)
                 or dev["error"].abnormal
                 or dev["latency"].abnormal or dev["cpu"].abnormal or dev["memory"].abnormal)
    if state_change and has_other:
        return TIER_STATE_CHANGE
    if blamed:
        return TIER_BLAMED
    if dev["calls"].abnormal:
        return TIER_TRAFFIC
    # 调用量暴涨:同样直接指认"谁被打了"。放在错误/时延之前——洪泛场景下它的
    # 错误率和时延可能都还在自身历史范围内(2026-10-21 实测:首页洪泛那组,
    # 只认塌陷的旧判据报"全平台未见异常",整组漏报)。
    if surge is not None and surge.abnormal:
        return TIER_TRAFFIC_SURGE
    if dev["error"].abnormal:
        return TIER_ERROR
    if dev["latency"].abnormal and self_latency:
        return TIER_LATENCY_SELF
    if dev["cpu"].abnormal or dev["memory"].abnormal:
        return None if load_explained else TIER_RESOURCE
    if dev["latency"].abnormal:
        return TIER_LATENCY_RELAY
    return None


async def scan_one(prom: dict, *, platform: str, cluster: str = "",
                   now: float | None = None) -> dict:
    """扫一个平台,返回 {anchors, note}。anchors 按**字典序分层**排序(见 TIER_*)。

    两个判据层面的改动(2026-10-14 重构,不是打补丁):
    1. **异常判定**:从"当前窗 / 基线窗"的裸比值改成"当前值在服务自身历史分布里的
       分位位置"(stats 模块)。实测每个服务自己的 p95 就横跨两个数量级(ad 2ms→10000ms),
       裸比值比的是**固有波动**不是异常程度——对照组(零故障)报出 high 置信根因就是这么来的。
    2. **排序**:从"各维幅度取 max"改成字典序分层。这与 localize 早已采用的做法一致
       (localize 的注释写着"旧 W_ERROR/W_LATENCY/W_CPU/W_MEM 四个拍值只编码了一个排序,
       却要靠校准维持,跨维污染就是这么来的")——粗筛层当时没跟上,这次补齐。

    now:求值时刻。缺省=此刻;离线回放传历史时刻。
    """
    now = time.time() if now is None else now
    info = await _discover(prom)
    if info is None:
        return {"anchors": [], "observed": False,
                "note": "该平台没有可用的全平台调用量指标,无法自动粗筛"}
    profiles, gaps = await _profile_all(prom, info, now=now)
    if not profiles:
        # 一个服务都查不到 = 平台整体没遥测(全站挂 / 观测链路断),或者**基线还没攒够**
        # 导致每一维都判不了。这两种都不能当"健康"放行,但说法要分开。
        _why = gaps[0] if gaps else "调用量指标一个服务都查不到(平台可能整体无遥测)"
        return {"anchors": [], "observed": False, "gaps": gaps, "note": _why}

    # 错误指认、重启信号、调用图先取:前两者是分层的前两档,调用图是时延"源头 vs 中继"
    # 的判据——都必须在排序之前拿到,不能事后给分数打补丁。
    known = set(profiles)
    refs = await _error_refs(prom, info, known, now=now)
    restarts = await restart_signals(prom, known, now - SCAN_WINDOW_S, now)
    edges = await _call_edges(prom, info, known, now=now)
    callers: dict[str, set[str]] = {}
    for _a, _bs in edges.items():
        for _b in _bs:
            callers.setdefault(_b, set()).add(_a)
    # 角色识别:只有出站、没有入站的是"纯调用方"(压测机、浏览器探针),不当根因候选。
    roles = await _role_rates(prom, info, now=now)
    client_only = sorted(s for s, (srv, cli) in roles.items() if srv <= 0 < cli)

    raw: list[dict] = []
    for svc, p in sorted(profiles.items()):
        if svc in client_only:
            continue
        dev = p["dev"]
        reasons: list[str] = []
        r = restarts.get(svc) or {}
        state_change = bool(r.get("restarts"))
        if state_change:
            reasons.append(f"窗内容器重启 {r['restarts']:.0f} 次"
                           + ("(OOMKilled,内存耗尽被点名)" if r["oom"] else "")
                           + "(机制级实锤)")

        blamed_info = refs.get(svc) or {}
        blamed = (blamed_info.get("rate") or 0.0) if isinstance(blamed_info, dict) else 0.0
        calls_rate = p["calls"] or 0.0
        # 指认占比:操作级(分母=同操作对的调用量,见 _error_refs 注释)。
        # 强指认 = 双门槛,与 attribution.REF_STRONG_RATIO 同一套占比拍值:
        #   占比(≥REF_STRONG_RATIO)是主判据——区分"整条操作在失败"(真故障,AddItem 100%)
        #   和"低频操作的慢性背景错误"(实测:checkout EmptyCart 慢性 18%,被占比线拦住);
        #   绝对量(≥2 条/窗)只防"单条偶发"——低于它连占比都不可信。不可再高:低频操作
        #   (AddItem)故障期也只有 2~3 条,门槛过高等于对这类故障失明(实测踩过)。
        REF_STRONG_RATIO = 0.5   # 强指认占比门槛(与 attribution.REF_STRONG_RATIO 同值,attribution 模块退役后本地持有;出处见 thresholds.REGISTRY)
        rate_floor = 2 / SCAN_WINDOW_S
        blame_ratio = (blamed_info.get("ratio") or 0.0) if isinstance(blamed_info, dict) else 0.0
        # 零调用的服务被失败调用点名:分母为 0 但指认是实打实的(挂死形态),占比记 100%
        if calls_rate <= 0 and blamed > 0:
            blame_ratio = 1.0
        blamed_strong = bool(blamed >= rate_floor and blame_ratio >= REF_STRONG_RATIO)
        if blamed > 0:
            mark = "(错误指认)" if blamed_strong else "(指认占比过低,不计入分层)"
            reasons.append(f"调用方有 {blamed:.3f} 次/秒 的失败调用直接指向它"
                           f"(失败调用占同操作调用量的 {blame_ratio:.0%}){mark}")
        if dev["calls"].abnormal:
            reasons.append("调用量塌陷:" + dev["calls"].basis)
        if dev["calls_up"].abnormal:
            reasons.append("调用量暴涨:" + dev["calls_up"].basis)
        if dev["error"].abnormal:
            reasons.append("错误率异常:" + dev["error"].basis)
        if dev["cpu"].abnormal:
            reasons.append("CPU 异常:" + dev["cpu"].basis)
        if dev["memory"].abnormal:
            reasons.append("内存异常:" + dev["memory"].basis)

        # 时延的因果一致性(通用规则,不是为个例打的补丁):正常调用链里 **下游 ≤ 自己 ≤ 上游**
        # ——调用方要等被调方,所以被调方不该比调用方还慢。两侧都用来剔除假信号:
        # ① 自己 ≈ 下游 → 纯中继(self time ≈ 0),时延信号作废,是受害者;
        # ② 自己 >> 所有上游 → 它记录的不是自己的耗时,而是**客户端超时/取消留下的痕迹**
        #    (真实事故:payment p95 15000ms 而其调用方 checkout 只有 647ms——被调方比调用方
        #    慢 23 倍物理上不成立;锚点被它骗走后真凶离锚点 3 跳,整条诊断白跑)。
        self_latency: bool | None = None
        if dev["latency"].abnormal:
            own = p.get("p95_ms") or 0.0
            down = max((profiles.get(d, {}).get("p95_ms") or 0.0
                        for d in (edges.get(svc) or set())), default=0.0)
            up, up_svc = 0.0, None
            for u in (callers.get(svc) or set()):
                v = profiles.get(u, {}).get("p95_ms") or 0.0
                if v > up:
                    up, up_svc = v, u
            from app.diagnosis.deviation import p95_spread_ms
            # 物理约束:调用方要等被调方 → **同一次请求里** 被调方不该比调用方慢。
            # 容差 = 调用方自身 p95 的正常摆动(历史各窗 p95 的四分位距):超出这个
            # 抖动才说明"这不是被调方的真实耗时,而是超时/取消留下的痕迹"。不写死"慢 2 倍"。
            #
            # 但这里比的是**服务级聚合 p95**,不是同一次请求:一个服务内部有多种操作,
            # 聚合 p95 由占比最大的那些操作决定。被调方的调用只占调用方总流量的一小片时,
            # 它再慢也推不动调用方的聚合 p95——此时"被调方 > 调用方"不是物理矛盾,而是
            # 聚合口径的必然结果(实测:ad 手动 GC 时 ad 自身 4564ms、其调用方 frontend
            # 聚合 p95 只有 364ms,因为首页广告请求占 frontend 全部 span 不到 5%;
            # 旧实现据此把真实故障当成超时假象丢掉,整组结构性失明)。
            # 所以只在**被调方自身也有失败信号**时才采用"超时痕迹"这个解释——超时/取消
            # 必然留下错误或调用量塌陷;没有失败信号的高时延,就是它自己慢。
            callee_has_failure = (dev["error"].abnormal or dev["calls"].abnormal
                                  or blamed_strong)
            if up and callee_has_failure and own > up + p95_spread_ms(profiles.get(up_svc) or {}):
                reasons.append(
                    f"时延不可信:自身 {own:.0f}ms 高于其调用方 {up_svc} 的 {up:.0f}ms,"
                    f"且该调用方本身在报错/掉量——被调方不该比调用方慢,这是超时/取消痕迹")
                self_latency = None
            else:
                self_ms = own - down
                self_latency = self_ms > 0
                if up and own > up + p95_spread_ms(profiles.get(up_svc) or {}):
                    reasons.append(
                        f"自身 {own:.0f}ms 高于调用方 {up_svc} 的 {up:.0f}ms,但调用方"
                        f"没有失败信号——聚合口径下被调方慢不等于调用方该慢,按自身耗时算")
                reasons.append(dev["latency"].basis + (
                    f";扣掉下游({down:.0f}ms)后自身仍 {self_ms:.0f}ms(不是被下游带的)"
                    if self_latency else
                    f";被下游({down:.0f}ms)完全解释,它是纯中继(时延不计入它的可疑度)"))

        # 资源涨、吞吐也涨且**成比例** → 是负载推上去的,不单独成锚(见 _load_explained)
        load_explained = _load_explained(dev)
        if load_explained and (dev["cpu"].abnormal or dev["memory"].abnormal):
            reasons.append(f"资源偏离被负载解释:调用量是自身基线的 {dev['calls'].ratio:.2f} 倍,"
                           f"资源只涨到 {max([r for r in (dev['cpu'].ratio, dev['memory'].ratio) if r is not None], default=0):.2f} 倍"
                           f"(涨得没比流量多)——资源维不单独立锚")

        tier = _tier(state_change, blamed_strong, dev, self_latency,
                     load_explained=load_explained)
        if tier is None:
            continue
        # 层内幅度:只用于同层排序与展示,不参与"是否异常"的判断。
        # 分位位置是主判据(有界、跨服务可比),比值只做层内的细排。
        mags = [d.ratio for d in dev.values() if d.abnormal and d.ratio is not None]
        raw.append({
            "service": svc, "platform": platform, "cluster": cluster or "", "tier": tier,
            "magnitude": round(max(mags), 3) if mags else 0.0,
            "calls_now": p["calls"], "error_rate_now": p["err_rate"],
            "silent": dev["calls"].abnormal,
            "state_change": state_change, "blamed": round(blamed, 4),
            "blamed_strong": blamed_strong,
            "dev": {k: {"position": d.position, "ratio": d.ratio,
                        "abnormal": d.abnormal} for k, d in dev.items()},
            "reasons": reasons,
        })

    if not raw:
        # 有维度查询失败时,"未见异常"只对看得见的维度成立——缺口必须写进 note,
        # 否则"时延维瞎了"会被读成"平台健康"(查询失败绝不冒充健康)。
        skip = f"(纯调用方 {'、'.join(client_only)} 不计入根因候选)" if client_only else ""
        if gaps:
            return {"anchors": [], "observed": True, "gaps": gaps,
                    "client_only": client_only, "digest": _digest(profiles),
                    "note": "各服务与自身历史分布比未见异常(仅限看得见的维度);"
                            + "；".join(gaps) + "——对应故障类型本轮可能漏报" + skip}
        return {"anchors": [], "observed": True, "gaps": [], "client_only": client_only,
                "digest": _digest(profiles),
                "note": "各服务与自身历史分布比未见异常" + skip}

    # 因果剪枝:能被下游(或流量场景下的上游)解释的异常,从锚点里摘掉。
    # 剪完可能一个不剩——那说明"看到了异常,但都是别人传过来的",如实说,不硬选。
    raw, pruned = _prune_by_causality(raw, edges, callers)
    if not raw:
        why = "；".join(f"{p['service']} 被{p['direction']} {p['explained_by']} 解释"
                        for p in pruned)
        return {"anchors": [], "observed": True, "pruned": pruned,
                "digest": _digest(profiles),
                "note": ("平台有异常,但都能被调用链上的邻居解释(不是源头):" + why)}

    # 出口门槛:最高层不能只是"纯中继"。全平台只有受害者级别的信号时,如实说
    # "看到了异常但都在中继位置",而不是硬选一个受害者当根因。
    best = min(a["tier"] for a in raw)
    if best > TIER_ANCHOR_MAX:
        return {"anchors": [], "observed": True, "pruned": pruned,
                "digest": _digest(profiles),
                "note": ("平台有异常,但最高只到「纯时延中继」层——异常都能被下游解释,"
                         "当前证据指向的是受害者而不是源头,不选锚点")}
    # 字典序排序:层级 → 层内幅度 → 服务名(定序保可复现——候选从 set 派生,
    # 不定序的话进程重启后同键候选会换位)
    raw.sort(key=lambda a: (a["tier"], -a["magnitude"], a["service"]))
    # profiles 一并返回:诊断主链直接复用这份画像,**同一次诊断里"谁异常"只算一次**。
    # 之前扫描算一遍、进诊断又重算一遍,两次隔几分钟,临界时候说翻脸就翻脸(实测:
    # recommendationCacheFailure 扫描时 recommendation 时延超历史 p99,诊断时重算没超,
    # 真因就这么从异常名单上掉了)。画像里含 _dist 内部对象,只在本进程内传递,
    # 进序列化路径前由调用方摘走。
    return {"anchors": raw, "observed": True, "profiles": profiles,
            "gaps": gaps, "pruned": pruned, "note": None,
            "client_only": client_only, "digest": _digest(profiles)}


def _digest(profiles: dict) -> list[dict]:
    """逐服务逐维的"当前值 / 边界 / 是否超界",供卡片展示排查过程。

    没有结论的时候,用户最想知道的是"你到底查了什么、每个服务离边界多远"。
    以前这些数字只活在事件流的一句话里,用户得自己去翻。
    """
    out = []
    for svc, p in sorted(profiles.items()):
        dv = p.get("dev_view") or {}
        if not dv:
            continue
        out.append({"service": svc, "dims": dv,
                    "abnormal": [k for k, v in dv.items() if v.get("ab")]})
    return out


def _prune_by_causality(raw: list[dict], edges: dict, callers: dict) -> tuple[list[dict], list[dict]]:
    """因果剪枝(CauseInfer 式):能被别人解释的异常,不当根因。

    判据只有一条——**这个服务的异常,有没有更可疑的邻居能解释它**:

    - 性能/可靠性/资源异常:看**下游**。调用方要等被调方,所以下游同样可疑时,
      上面那个是受害者,不是源头(2026-10-21 实测:图片慢导致首页慢,首页被判根因)。
    - 流量异常(QPS 涨):看**上游**。流量是从上游压下来的,不是从下游传上来的
      (首页被洪泛时,开关服务 flagd 的调用量跟着涨,但那是被压出来的)。

    两类方向相反,是 CauseInfer 把"流量异常"单独列一类的根本原因。

    这条规则**不认名字、不认角色、不认深度**:真根因再显眼,只要它下游没有更可疑的,
    就照样保留(ad 的 CPU 真炸、flagd 自己真挂,都会被留下)。
    """
    by_svc = {a["service"]: a for a in raw}
    kept: list[dict] = []
    dropped: list[dict] = []
    for a in raw:
        svc, tier, dev = a["service"], a["tier"], a["dev"]
        # 方向由"异常落在哪一维"决定,不由层级决定:同时有错误和流量异常的服务,
        # 错误那一半是真劣化,仍按下游解释(别让流量方向把它误剪)。
        #
        # "被调用方强指认"也算劣化:这种服务自己四个指标维可能全绿,唯一的证据是
        # "别人调它的时候在报错"。它同样可能是**受害者**——上游调它报错,是因为它
        # 调的下游坏了。旧实现只认指标维,于是这类服务直接跳过剪枝(2026-10-22 实测:
        # payment 故障时 checkout 和 payment 同时被指认、同为 tier 1,剪枝没跑,
        # 按服务名排序把 checkout 排到了前面,根因判错)。
        degraded = any(dev.get(k, {}).get("abnormal")
                       for k in ("error", "latency", "cpu", "memory")) \
            or bool(a.get("blamed_strong"))
        if degraded:
            neighbors, direction = [by_svc[d] for d in (edges.get(svc) or set())
                                    if d in by_svc], "下游"
        elif dev.get("calls_up", {}).get("abnormal"):
            # 流量异常:看**下游谁在这个流量下坏了**。施压方自己不会坏,受害者会——
            # 首页被洪泛时,压测机调用量暴涨但自身健康;frontend 同样暴涨,而且时延
            # 被压上去了。所以"流量涨"本身不指认任何人,只有"流量涨 + 自己坏了"才算。
            neighbors = [by_svc[d] for d in (edges.get(svc) or set())
                         if d in by_svc and any(
                             by_svc[d]["dev"].get(k, {}).get("abnormal")
                             for k in ("error", "latency", "cpu", "memory"))]
            direction = "下游劣化"
        else:
            kept.append(a)
            continue
        hit = next((n for n in neighbors if n["tier"] <= tier), None)
        if hit:
            dropped.append({"service": svc, "explained_by": hit["service"],
                            "direction": direction, "tier": tier})
        else:
            kept.append(a)
    return kept, dropped


async def scan_platforms(now: float | None = None, platform: str | None = None) -> dict:
    """扫所有启用平台。返回 {status, anchors, note}:
    - available:有可疑锚点;
    - clear:平台健康,各服务与自身历史分布比都在正常范围;
    - victims_only:平台有异常,但**最高只到"纯时延中继"层**——异常都能被下游解释,
      证据指向的是受害者不是源头。**这一档不等于健康,也不能硬选一个受害者当锚点**,
      所以单列出来如实说"看到了但指不出源头"(旧实现会从受害者里挑最响的那个)。
    - no_data:粗筛取不到数据(没平台/没指标/查询失败),此时不能声称"健康"。

    now:求值时刻。缺省=此刻;离线回放传历史时刻。"""
    try:
        rows = await repository.list_targets()
    except Exception as e:  # noqa: BLE001
        return {"status": "no_data", "anchors": [],
                "note": f"接入平台注册表不可读:{type(e).__name__}"}
    enabled = [r for r in rows if r.enabled and (not platform or r.name == platform)]
    if not enabled:
        return {"status": "no_data", "anchors": [], "note": "没有启用的接入平台"}
    anchors: list[dict] = []
    notes: list[str] = []
    gaps_all: list[str] = []             # 跨平台汇总的单维缺口(查询失败≠健康,必须浮上来)
    observed = 0
    profiles_map: dict[str, dict] = {}   # 平台名 → 画像(供诊断主链复用,见 scan_one 注释)
    for row in enabled:
        prom = next((s for s in (row.sources or []) if s.get("type") == "prometheus"), None)
        if not prom:
            notes.append(f"{row.name}:未接入 Prometheus,无法粗筛")
            continue
        try:
            res = await scan_one(prom, platform=row.name,
                                 cluster=getattr(row, "cluster", "") or "", now=now)
        except Exception as e:  # noqa: BLE001  单平台失败不抬异常,如实记缺口
            logger.warning("粗筛失败 platform=%s", row.name, exc_info=True)
            notes.append(f"{row.name}:粗筛查询失败({type(e).__name__})")
            continue
        if res.get("note"):
            notes.append(f"{row.name}:{res['note']}")
        if res.get("gaps"):
            gaps_all.extend(f"{row.name}:{g}" for g in res["gaps"])
            if not res.get("note"):
                # 无锚点时 note 已把缺口写进去(scan_one),这里只补"有锚点"的场景,
                # 避免同一段缺口文案拼两遍。
                notes.append(f"{row.name}:{'；'.join(res['gaps'])}")
        if not res.get("observed"):
            continue                      # 这个平台看不到数据,不算"扫过",更不能算健康
        observed += 1
        anchors.extend(res["anchors"])
        if res.get("profiles"):
            profiles_map[row.name] = res["profiles"]
    if observed == 0:
        return {"status": "no_data", "anchors": [], "gaps": gaps_all,
                "note": "；".join(notes) or "粗筛不可用"}
    if not anchors:
        note = "；".join(notes)
        # 区分"没异常"和"有异常但只有受害者":两者处置完全不同,不能混成一句"未见异常"
        if "纯时延中继" in note:
            return {"status": "victims_only", "anchors": [], "gaps": gaps_all, "note": note}
        return {"status": "clear", "anchors": [], "gaps": gaps_all,
                "note": note or "各服务与自身历史分布比未见异常"}
    # 跨平台汇总:层级 → 层内幅度 → 服务名(定序保可复现)
    anchors.sort(key=lambda a: (a["tier"], -a["magnitude"], a["service"]))
    # 锚点**不再截断**:之前 anchors[:3] 把第 4 名以后的异常直接扔掉,而真因可能就在
    # 那里面(实测:adFailure 锚点里明明有 ad,还是被挤出了诊断视野;多锚点并行是
    # "双故障/锚点选偏"场景的唯一兜底)。数量交给消费方(localize 的候选上限)管。
    return {"status": "available", "anchors": anchors,
            "profiles": profiles_map, "gaps": gaps_all, "note": "；".join(notes)}
