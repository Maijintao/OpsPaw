"""偏离度计算(管线第③段):四维偏离 error/latency/cpu/mem,动态基线不预设阈值。

**判据只有一套**:四维都问同一个问题——"当前值在服务自身历史分布里排在什么位置"
(见 stats 模块)。没有历史分布就如实不判,不拿"倍数 + 绝对地板"兜底猜。

- error:错误率超出自身历史范围即异常(从不出错的服务首次出错 = 超过历史最大值,
  不需要任何绝对地板);
- latency:p95 时延超出自身历史范围即异常;"慢"的阈值同样取历史分布的异常门槛分位;
- cpu/mem:当前窗均值超出自身历史范围即异常(与前三维同一入口,见 detect._res_profile)。

比值(当前/基线)只做**展示与层内排序**,不参与"是否异常"的判断——拿它当准入
就是"同一份数据两套判据",比没有数据更糟。

阈值状态:本模块**已无拍值**。四维判据同源于 stats 的分布判据;剩余三个常量各有出处——
ERROR_SMOOTH(0 除保护)、ERROR_DEV_LOG_BASE / LATENCY_QUANTILE(展示与定义分位)、
TRACE_MIN_SAMPLES(由分位反解)。详见 app/diagnosis/thresholds.py 的出处登记表。
"""
import math
import re
import statistics
from dataclasses import dataclass, field

from app.diagnosis.collect import Span

ERROR_SMOOTH = 0.01        # 错误率分母的噪声底(1%):全服务都没错时 median=0,靠它防 0 除
ERROR_DEV_LOG_BASE = 2.0   # 错误维刻度取对数,理由见 compute() 里的说明
LATENCY_QUANTILE = 0.95    # "慢"的定义分位(策略参数:我们说的 p95 就是这个分位)
# 稀疏兜底:样本不足不做统计推断(SOP §2)。
# 门槛 = ceil(1/(1−q)):p95 之上平均至少要有 1 个观测,估出来的 p95 才有意义
# (q=0.95 → 20 条)。由分位定义反解,不再是拍值。
TRACE_MIN_SAMPLES = math.ceil(1.0 / (1.0 - LATENCY_QUANTILE))
# 流式/长连接的操作名特征(**不写死时长**):这类 span 是"连接存活"而非"请求耗时",
# 进时延统计会把 p95 和全图中位数基线一起拉飞(真实案例:flagd EventStream 单条
# 600000ms 污染整窗)。调用量/错误率照常计入,只剔时延。
_STREAM_OP_RE = re.compile(r"stream|watch|subscribe|longpoll|server.?push", re.I)


def is_streaming_span(operation: str) -> bool:
    """该 span 是否是流式/长连接(按操作名语义判,不看时长)。"""
    return bool(_STREAM_OP_RE.search(operation or ""))


def gap_fence(gaps: list[int]) -> int:
    """场次切分的间隔上界 = **观测间隔中位数 × ln(1/α)**(α = 目标误报率)。

    在"间隔近似指数分布"的假设下,间隔超过 中位数×ln(1/α) 的概率 < α——界线由数据
    与目标误报率共同决定,不再写死"300 秒算两场病"。α 越小 → 界线越高 → 越不容易切场。
    没有间隔(只有一条记录)返回 0 = 不可判(不切场);两个间隔时中位数是两者的均值,
    永远切不开(样本不足,不硬切)——这也是有意为之:宁少切不多切。"""
    if not gaps:
        return 0
    from app.diagnosis import stats as _stats
    return int(statistics.median(gaps) * _stats.tail_multiple())


def last_episode_start(times: list[int]) -> int | None:
    """最近一段连续异常的起点——时间锚点的正确取法。

    为什么不取最早:采样窗(10m→最多 24h)里可能装着**上一场故障**的旧异常——跑批场景
    上一组注入刚复位 240s 就跑下一组,生产场景同一服务反复发病。取 min 会把上一场的首发
    时刻当成"本次发病时刻"(真实事故:checkout 的最早错误属于上一组 kafka 故障,真根因
    payment 因此比这个假锚点晚 1440s,被"时间颠倒"反证误杀)。
    按观测到的间隔切场(界线 = 间隔分布自己的 Tukey 上界,见 gap_fence),取**最后一场**
    的起点——"这场病从什么时候开始"才是时间先后信号要的东西。空列表返回 None。"""
    if not times:
        return None
    ts = sorted(times)
    fence = gap_fence([b - a for a, b in zip(ts, ts[1:])])
    start = prev = ts[0]
    for t in ts[1:]:
        if fence and t - prev > fence:
            start = t
        prev = t
    return start


def time_anchor_ok(first_anomaly_us: int | None, window_start_us: int | None,
                   resolution_us: int | None = None) -> bool:
    """时间锚点是否可信:None(无锚点)返回 False(没有信号);
    异常起点落在窗起点**一个采样间隔以内**时返回 False——真实开始时间可能早于窗,此时
    "候选晚于症状"可能只是采样截断的假象,时间信号正反两个方向都不许用。

    容差 = **该 onset 信号自己的采样分辨率**(指标为采样步长、trace 为到达间隔中位数),
    不再写死 60s:采样密就判得细,采样疏就判得宽,跟着数据走。"""
    if first_anomaly_us is None:
        return False
    if window_start_us is None:
        return True
    return first_anomaly_us > window_start_us + int(resolution_us or 0)


@dataclass
class ServiceDeviation:
    service: str
    calls: int = 0
    error_rate: float = 0.0
    latency_p95_us: float = 0.0
    latency_dev: float = 0.0
    latency_abnormal: bool = False
    error_abnormal: bool = False      # 错误维是否异常。有指标侧历史分布时用统计判据
    # (与 detect 同一套),没有时退回"有错即信号"的旧口径。保留成独立字段是为了让
    # 证据包能如实说清"这条是按哪套口径判的"。
    error_dev: float = 0.0
    cpu_ratio: float | None = None    # 当前/基线;基线缺失 None(不编数)
    mem_ratio: float | None = None
    res_baseline: str | None = None   # "t24h" | "t2h"(回退窗,可能含故障期——证据里必须知情)
    dev_basis: str = ""               # 错误/时延偏离是跟谁比:"自身基线" | "全图中位数(无自身基线)"
    metric_only: bool = False         # 该服务的偏离来自指标(不在本次 trace 采样里)
    metric_onset_us: int | None = None   # 指标侧发病时刻(分钟粒度,没有父子偏置)
    self_latency_abnormal: bool = False  # 自身时延异常、且下游解释不了它 → 它自己就是慢的源头
    abnormal_dims: set = field(default_factory=set)
    hang_since_us: int | None = None   # 挂死起点=自身最后一条 span 的时刻(静默即异常,
    # 时间先后信号据此参与排序与反证:挂死服务的"发病时刻"早于调用方开始报错)
    earliest_error_us: int | None = None
    earliest_latency_us: int | None = None   # 时延异常的最早出现时刻。只挂在 error 维上
    # 会让纯时延异常(checkout 慢但无错)没有时间锚点,归因员拿不到"果不先于因"信号
    error_onset_us: int | None = None    # 错误维**当前场次**起点(最后一段连续错误的第一条)
    latency_onset_us: int | None = None  # 时延维当前场次起点。first_anomaly_us 优先用它们;
    # earliest_* 是窗内最早记录,可能属于上一场故障,只作展示与残留提示
    onset_resolution_us: int | None = None   # 该服务 onset 信号的采样分辨率(指标=步长、
    # trace=到达间隔中位数)。"起点贴窗边"的可信度判据按它算,不再写死 60s。

    @property
    def abnormal(self) -> bool:
        return bool(self.abnormal_dims) or self.latency_abnormal

    @property
    def first_anomaly_us(self) -> int | None:
        """本服务任意异常维的最早时刻(时间先后信号统一入口)。

        **优先用指标侧锚点**:trace 锚点有父子偏置——同一个 trace 里父 span 必然早于子 span,
        级联里会让上游系统性地显得更早(实测:症状 frontend 时,纯受害者 frontend-proxy
        因"更早"被加权到候选第一)。指标是分钟粒度,同刻异常会并列,没有这个假象。
        trace 锚点用**当前场次**起点(error_onset/latency_onset),没有才退回
        earliest_*——旧测试直接给 earliest_* 赋值,不能一改字段就全断。"""
        if self.metric_onset_us is not None:
            return self.metric_onset_us
        err_t = self.error_onset_us if self.error_onset_us is not None else self.earliest_error_us
        lat_t = self.latency_onset_us if self.latency_onset_us is not None else self.earliest_latency_us
        cands = [t for t in (self.hang_since_us, err_t, lat_t) if t is not None]
        return min(cands) if cands else None

    @property
    def onset_source(self) -> str:
        """时间锚点来自哪个源——证据里必须说清楚,别让人以为都是同一种精度。"""
        return "指标(分钟粒度)" if self.metric_onset_us is not None else "trace(span 时刻)"

    def onset_trusted(self, window_start_us: int | None) -> bool:
        """时间锚点是否可信。**按来源分别判**:
        - 指标锚点:已经在生成时按"搜索窗起点"自检过(落在搜索起点的压根没给),有就是可信——
          它能看见采样窗之前,所以不该套用"贴采样窗起点即不可信"那条;
        - trace 锚点:看不见采样窗之前,贴窗起点就是采样截断的假象 → 交给 time_anchor_ok,
          容差用该服务自己的采样分辨率(onset_resolution_us),不再写死 60s。"""
        if self.metric_onset_us is not None:
            return True
        return time_anchor_ok(self.first_anomaly_us, window_start_us,
                              self.onset_resolution_us)

    def dims_view(self) -> dict:
        """给证据包/前端的紧凑投影:比值保留两位,缺的如实标 null。"""
        return {"error_rate": round(self.error_rate, 4), "error_dev": round(self.error_dev, 2),
                "latency_dev": round(self.latency_dev, 2),
                "cpu_ratio": round(self.cpu_ratio, 2) if self.cpu_ratio is not None else None,
                "mem_ratio": round(self.mem_ratio, 2) if self.mem_ratio is not None else None,
                "abnormal_dims": sorted(self.abnormal_dims)}


def span_stats(spans: list[Span]) -> dict[str, dict]:
    """按服务聚合:调用量、错误率、p95 时延。样本总数不足 TRACE_MIN_SAMPLES 时整体打标。
    时延统计剔掉流式/长连接 span(按操作名语义判,见 is_streaming_span)——它们是
    "连接存活"不是"请求耗时",混进来会把 p95 拉飞。调用量/错误率照常计入。"""
    per: dict[str, list[Span]] = {}
    for sp in spans:
        per.setdefault(sp.service, []).append(sp)
    stats = {}
    for svc, sps in per.items():
        errs = [sp for sp in sps if sp.error]
        durs = sorted(sp.duration_us for sp in sps if not is_streaming_span(sp.operation))
        p95 = durs[min(len(durs) - 1, int(len(durs) * LATENCY_QUANTILE))] if durs else 0.0
        stats[svc] = {"calls": len(sps), "error_rate": len(errs) / len(sps),
                      "p95_us": p95, "error_spans": errs}
    return stats


def onset_resolution_us(svc: str, spans: list[Span], onsets: dict | None) -> int | None:
    """该服务 onset 信号的采样分辨率:
    - 指标 onset:用指标的采样步长(由 detect.metric_onsets 随 onset 一起给出);
    - trace onset:用该服务自身 span 到达间隔的中位数(它决定了"最早异常"能精确到多细)。
    都没有则 None——此时 onset 本身也不存在,分辨率无从谈起。"""
    step = (onsets or {}).get(svc) or {}
    if step.get("step_us"):
        return int(step["step_us"])
    own = sorted(sp.start_us for sp in spans if sp.service == svc)
    if len(own) < 2:
        return None
    return int(statistics.median([b - a for a, b in zip(own, own[1:])]))


def p95_spread_ms(profile: dict) -> float:
    """该服务历史各窗 p95 的四分位距(ms)——"平时窗口之间就有的起伏"。
    判"自己是慢的源头"时拿它当噪声底:残差要比这台服务平时的自然摆动还大才算实锤。
    估不出(样本不足)返回 0:此时退回最朴素的物理判据(残差 > 0),不编一个噪声底。"""
    dist = (profile.get("_dist") or {}).get("latency")
    vals = list(getattr(dist, "values", None) or [])
    if len(vals) < 4:
        return 0.0
    s = sorted(vals)
    return s[(3 * len(s)) // 4] - s[len(s) // 4]


def compute(stats: dict[str, dict], res_ratio: dict[str, dict[str, float | None]],
            spans: list[Span] | None = None,
            profiles: dict[str, dict] | None = None,
            window_s: float = 600.0,
            edges: set[tuple[str, str]] | None = None,
            onsets: dict[str, dict] | None = None) -> dict[str, ServiceDeviation]:
    """四维偏离。**错误率/时延一律"跟自己比"(纵向)**,横向中位数只在没有自身基线时兜底。

    为什么必须纵向:横向比有两个死穴——① 慢性病服务(currency 长期 100% 错误率)会把中位数
    抬高、盖掉新发故障;② 大面积劣化时全图中位数整体上移,谁都不算异常。自身基线来自 span
    指标(detect.service_profiles),与 CPU/内存同一条口径。

    profiles: {service: {calls, err_rate, base_err_rate, p95_ms, base_p95_ms, ...}}。
    传了就用它做纵向基线,并给"指标上异常、但不在本次 trace 采样里"的服务补条目——
    候选召回不再被"穿过症状服务的链路 + ±N 跳"锁死(真因没进候选集是检索失败,无解;
    排错序还能救,所以召回优先)。
    spans 传入时为时延异常服务补最早异常时刻(与异常判定同一条阈值线)。"""
    prof = profiles or {}
    _edges = edges or set()
    _onsets = onsets or {}
    p95s = [s["p95_us"] for s in stats.values() if s["p95_us"] > 0]
    med_p95 = statistics.median(p95s) if p95s else 0.0
    rates = [s["error_rate"] for s in stats.values()]
    med_rate = statistics.median(rates) if rates else 0.0

    def _fill(d: ServiceDeviation, p: dict, cur_rate: float, cur_p95_us: float) -> None:
        """算错误/时延偏离。**优先用指标侧的历史分布判据(与 detect 同一套统计)**,
        没有历史分布时(离线 fixture 回放:没有 Prometheus 可查)才退回"跟自身基线比"的比值。

        为什么要统一:同一个服务,粗筛按指标说它异常、定位层按 trace 说它正常——两套
        口径打架,候选集直接是空的(真实事故:productCatalogFailure 粗筛锚点挑对了
        product-catalog,但 localize 的候选集为空,结论 inconclusive)。同一份数据
        两处判据不一致,是比没有数据更糟的事。"""
        from app.diagnosis import stats as _stats
        dist = p.get("_dist") or {}
        own_base_rate = p.get("base_err_rate")
        own_base_p95 = (p.get("base_p95_ms") or 0.0) * 1000
        d.dev_basis = ("自身历史分布(指标侧)" if dist
                       else ("自身基线" if (own_base_rate is not None or own_base_p95)
                             else "全图中位数(无自身基线)"))
        # 错误维:对数刻度(线性 + 封顶会让高错误率区失去分辨力)
        base_rate = own_base_rate if own_base_rate is not None else med_rate
        _er = cur_rate / (base_rate + ERROR_SMOOTH)
        d.error_dev = round(math.log(1.0 + _er, ERROR_DEV_LOG_BASE), 4) if _er > 0 else 0.0
        # 时延维:相对自身正常水平(1.5 倍 = 比平时慢 50%),没有自身基线才退回全图中位数
        base_p95 = own_base_p95 or med_p95
        d.latency_dev = cur_p95_us / (base_p95 + 1) if base_p95 else 0.0

        err_dist = dist.get("error")
        lat_dist = dist.get("latency")
        pdev = p.get("dev") or {}
        if pdev:
            # 异常判定**只认一个来源**:指标画像在(连续量、与粗筛同一套判据)就直接取它的
            # 结论。旧实现拿 trace 抽样的当前值去比指标的历史分布——那是两套口径
            # (抽样 vs 连续),同一份数据两处判据不一致,是比没有数据更糟的事。
            _e, _l = pdev.get("error"), pdev.get("latency")
            d.error_abnormal = bool(_e is not None and _e.abnormal)
            d.latency_abnormal = bool(_l is not None and _l.abnormal)
        else:
            # 画像缺失(离线无指标)才退回 trace 抽样判据;仍然走同一个统计内核
            # (stats.deviation),只是输入换成 trace 的当前值与指标侧的历史分布。
            if err_dist is not None and err_dist.enough:
                d.error_abnormal = _stats.deviation(cur_rate, err_dist, label="错误率").abnormal
            else:
                d.error_abnormal = cur_rate > 0      # 无历史分布:有错即信号
            if lat_dist is not None and lat_dist.enough:
                d.latency_abnormal = _stats.deviation(
                    cur_p95_us / 1000.0, lat_dist, label="p95 时延").abnormal
            else:
                # 没有可用历史分布 → 无从判断时延是否异常。如实不判,不拿"倍数 + 绝对地板"
                # 兜底猜:那是拍值,而且会与分布判据分叉(同一份数据两套口径)。
                d.latency_abnormal = False
        if d.latency_abnormal:
            d.abnormal_dims.add("latency")
        if d.error_abnormal:
            d.abnormal_dims.add("error")
        # 时延的因果一致性(与粗筛同一套物理原则):自身时延异常、下游却解释不了 → 它就是源头。
        # 调用方要等被调方(下游 ≤ 自己),所以"下游解释不了的那部分" = own − down(残差,物理上 ≥0)。
        # 残差要**超过这台服务自身 p95 的正常摆动**(历史各窗 p95 的四分位距)才算实锤——
        # 噪声底来自数据,不再用"1.5 倍"这种跨量纲的倍数拍值。
        if "latency" in d.abnormal_dims:
            downs = {b for (a_, b) in _edges if a_ == d.service and b != d.service}
            down_p95 = max(((prof.get(x) or {}).get("p95_ms") or 0.0) for x in downs) \
                if downs else 0.0
            own_p95 = (p.get("p95_ms") or 0.0) or (cur_p95_us / 1000.0)
            d.self_latency_abnormal = bool(down_p95 <= 0
                                           or (own_p95 - down_p95) > p95_spread_ms(p))
        d.metric_onset_us = (_onsets.get(d.service) or {}).get("onset_us")

    out: dict[str, ServiceDeviation] = {}
    for svc, s in stats.items():
        d = ServiceDeviation(service=svc, calls=s["calls"], error_rate=s["error_rate"],
                             latency_p95_us=s["p95_us"])
        _fill(d, prof.get(svc) or {}, s["error_rate"], s["p95_us"])
        r = res_ratio.get(svc) or {}
        d.cpu_ratio = r.get("cpu")      # 仅展示:当前窗 / 基线点的倍数,不参与判异常
        d.mem_ratio = r.get("mem")
        tags = {r.get("cpu_baseline"), r.get("mem_baseline")} - {None}
        d.res_baseline = "t2h" if "t2h" in tags else ("t24h" if "t24h" in tags else None)
        # 资源维异常与其余三维**同一套分布判据**(画像里的统计判定,见 detect._res_profile)。
        # 没有画像 / 没有历史分布时如实不判。不再用"当前/基线 > N 倍"的兜底拍值——
        # 判据只留一套,比值退回去只做展示。
        _pdev = (prof.get(svc) or {}).get("dev") or {}
        if _pdev.get("cpu") is not None and _pdev["cpu"].abnormal:
            d.abnormal_dims.add("cpu")
        if _pdev.get("memory") is not None and _pdev["memory"].abnormal:
            d.abnormal_dims.add("mem")
        if d.latency_abnormal and spans:
            from app.diagnosis import stats as _stats
            # "慢"的阈值与异常判定**同一个入口**(历史分布的异常门槛分位),不再另配
            # "倍数 × 基线 + 绝对地板"——那三个数既没出处,又会与判据分叉。
            _lat_dist = ((prof.get(svc) or {}).get("_dist") or {}).get("latency")
            threshold = _stats.anomaly_threshold(_lat_dist) or \
                (((prof.get(svc) or {}).get("base_p95_ms") or 0.0) * 1000 or med_p95)
            # 流式/长连接 span 不算"请求耗时"(按操作名语义剔,不看时长)
            slow = [sp for sp in (spans or []) if sp.service == svc
                    and threshold <= sp.duration_us and not is_streaming_span(sp.operation)]
            if slow:
                d.earliest_latency_us = min(sp.start_us for sp in slow)
                d.latency_onset_us = last_episode_start([sp.start_us for sp in slow])
        # onset 信号的采样分辨率:可信度判据(贴窗起点)按它算,不再写死 60s
        d.onset_resolution_us = onset_resolution_us(svc, spans or [], _onsets)
        errs = s.get("error_spans") or []
        # 错误维的最早时刻——时间先后信号(候选加权/门禁/时间反证/置信)全靠它。
        # 这行曾经是空的(取了 errs 却没赋值),导致"只报错、不慢"的服务
        # first_anomaly_us 恒为 None,时间维整条失效:真实事故里 checkout 只报错不慢,
        # 门禁因此只拿到 topology 一类信号,把指认正确的 payment 降级成了"关键故障"。
        if errs:
            d.earliest_error_us = min(sp.start_us for sp in errs)
            d.error_onset_us = last_episode_start([sp.start_us for sp in errs])
        out[svc] = d

    # 指标上异常、但不在本次 trace 采样里的服务:补条目,让候选召回不被症状链路的跳数锁死。
    # 调用量按指标速率折算(窗口秒数),避免被误当成"零遥测的挂死服务"。
    for svc, p in prof.items():
        if svc in out:
            continue
        cur_rate = p.get("err_rate") or 0.0
        cur_p95_us = (p.get("p95_ms") or 0.0) * 1000
        d = ServiceDeviation(service=svc, calls=int(round((p.get("calls") or 0.0) * window_s)),
                             error_rate=cur_rate, latency_p95_us=cur_p95_us, metric_only=True)
        _fill(d, p, cur_rate, cur_p95_us)
        # 指标侧服务不在 trace 采样里,onset 分辨率只能取指标的采样步长
        _on = _onsets.get(svc) or {}
        if _on.get("step_us"):
            d.onset_resolution_us = int(_on["step_us"])
        if d.abnormal:
            out[svc] = d
    return out


def insufficient(spans: list[Span]) -> bool:
    return len(spans) < TRACE_MIN_SAMPLES
