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
