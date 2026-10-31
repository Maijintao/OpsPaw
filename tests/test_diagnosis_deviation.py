"""偏离度计算的回归:四维偏离 + 时间锚点。

时间锚点是时间先后信号的唯一入口(候选加权/门禁/时间反证/置信都吃它),
所以"每种异常维都要能给出最早时刻"是硬约束——这里钉住它。
"""
from app.diagnosis import deviation
from app.diagnosis.collect import Span


def _span(service: str, start_us: int, *, error: bool = False, sid: str = "s",
          duration_us: int = 1000, trace_id: str = "t", op: str = "op") -> Span:
    return Span(trace_id=trace_id, span_id=sid, parent_span_id=None, service=service,
                operation=op, start_us=start_us, duration_us=duration_us, error=error)


def test_error_only_service_still_gets_time_anchor():
    """只报错、不慢的服务也必须有时间锚点。

    真事故:earliest_error_us 曾经没被赋值(compute 里取了 error_spans 却没用),
    导致"只有错误维"的服务 first_anomaly_us 恒为 None → 门禁拿不到 time 信号、
    只凑出 topology 一类 → 把指认正确的根因降级成了 critical_failure。
    """
    spans = [
        _span("checkout", 1_000_000, sid="c", error=False),
        _span("checkout", 5_000_000, sid="a", error=True),
        _span("checkout", 9_000_000, sid="b", error=True),
    ]
    devs = deviation.compute(deviation.span_stats(spans), {}, spans=spans)
    d = devs["checkout"]
    assert d.earliest_error_us == 5_000_000          # 取最早的那条错误 span
    assert d.first_anomaly_us == 5_000_000           # 时间锚点因此可用
    assert "error" in d.abnormal_dims


def test_no_error_service_has_no_time_anchor():
    """没有错误维的服务不该凭空生出时间锚点——锚点必须来自真实异常。"""
    spans = [_span("cart", 1_000_000, sid="a"), _span("cart", 2_000_000, sid="b")]
    devs = deviation.compute(deviation.span_stats(spans), {}, spans=spans)
    assert devs["cart"].earliest_error_us is None
    assert devs["cart"].first_anomaly_us is None


def test_first_anomaly_takes_earliest_across_dims():
    """多个异常维都有锚点时取最早的那个——"果不先于因"比较的就是这个时刻。"""
    d = deviation.ServiceDeviation("svc")
    d.hang_since_us = 3_000_000
    d.earliest_error_us = 9_000_000
    d.earliest_latency_us = 5_000_000
    assert d.first_anomaly_us == 3_000_000


def test_error_onset_takes_last_episode_not_earliest():
    """发病时刻必须取**最近一场**异常的起点,不是窗内最早记录。

    真事故:跑批场景上一组故障(kafka)的旧错误还躺在 checkout 的采样窗里,最早错误
    落在上一场,真根因 payment 因此被比出"晚 1440s"的假颠倒,遭时间反证误杀。
    切场界线由观测间隔自己给出(中位数 × ln(1/α)),不再写死 300s。"""
    # 上一场:t=0~10s 连续报错;本场:t=1450s 起再次报错(间隔 1440s,远大于 9s/5s)
    spans = [
        _span("checkout", 1_000_000, sid="a", error=True),
        _span("checkout", 10_000_000, sid="b", error=True),
        _span("checkout", 1_450_000_000, sid="c", error=True),
        _span("checkout", 1_455_000_000, sid="d", error=True),
    ]
    devs = deviation.compute(deviation.span_stats(spans), {}, spans=spans)
    d = devs["checkout"]
    assert d.earliest_error_us == 1_000_000        # 窗内最早记录(上一场)照旧保留
    assert d.error_onset_us == 1_450_000_000       # 当前场次起点 = 最后一场的第一条
    assert d.first_anomaly_us == 1_450_000_000     # 时间锚点用当前场次,不用上一场


def test_last_episode_start_clusters_by_observed_gaps():
    """切场界线 = 观测间隔中位数 × ln(1/α)(数据自证 + α 反解),不是写死的 300s。
    两个间隔时中位数是两者的均值,永远切不开——样本不足就不硬切(宁少切不多切)。"""
    f = deviation.last_episode_start
    assert f([]) is None
    assert f([5_000_000]) == 5_000_000
    # 只有两个间隔:切不开,起点保持第一条
    assert f([0, 299_000_000, 300_000_000]) == 0
    assert f([0, 301_000_000, 305_000_000]) == 0
    # 三个间隔且对比明显(5s/5s/1440s):大间隔处切场
    assert f([0, 5_000_000, 10_000_000, 1_450_000_000]) == 1_450_000_000


# ---------- 推导锁:阶段一整改的判据必须由数据/α 算出,不许退回拍值 ----------

def test_gap_fence_derived_from_median_and_alpha():
    """切场界线 = 间隔中位数 × ln(1/α),α 来自 stats.FALSE_ALARM_PER_TEST。"""
    import math
    from app.diagnosis import stats
    gaps = [5_000_000, 5_000_000, 5_000_000]
    assert deviation.gap_fence(gaps) == int(5_000_000 * math.log(1 / stats.FALSE_ALARM_PER_TEST))
    assert deviation.gap_fence([]) == 0


def test_streaming_span_excluded_by_semantics_not_duration():
    """流式 span 按操作名语义剔(不看时长)——flagd EventStream 那类长连接不再污染 p95。"""
    assert deviation.is_streaming_span("flagd.evaluation.v2.Service/EventStream")
    assert deviation.is_streaming_span("WatchResources")
    assert not deviation.is_streaming_span("oteldemo.CurrencyService/Convert")
    spans = [
        _span("flagd", 1_000_000, sid="a", duration_us=600_000_000, trace_id="t1",
              op="flagd.evaluation.v2.Service/EventStream"),
        _span("flagd", 2_000_000, sid="b", duration_us=2_000, trace_id="t1",
              op="flagd.evaluation.v2.Service/Resolve"),
    ]
    # 长连接那条被剔,时延只剩正常那条
    assert deviation.span_stats(spans)["flagd"]["p95_us"] == 2_000


def test_onset_resolution_prefers_metric_step_then_span_gap():
    """onset 分辨率:有指标步长用它,否则用该服务自身 span 到达间隔的中位数。"""
    spans = [_span("cart", 1_000_000), _span("cart", 3_000_000), _span("cart", 5_000_000)]
    assert deviation.onset_resolution_us("cart", spans, {"cart": {"step_us": 80_000_000}}) \
        == 80_000_000
    assert deviation.onset_resolution_us("cart", spans, {}) == 2_000_000
    assert deviation.onset_resolution_us("cart", [], {}) is None


def test_p95_spread_is_iqr_of_historical_windows():
    """噪声底 = 该服务历史各窗 p95 的四分位距;样本不足返回 0(不编)。"""
    class _D:
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
    # n=5:下四分位取 s[1]=20、上四分位取 s[3]=40 → 距 20
    assert deviation.p95_spread_ms({"_dist": {"latency": _D}}) == 20.0
    assert deviation.p95_spread_ms({"_dist": {"latency": type("X", (), {"values": [1.0]})}}) == 0.0
    assert deviation.p95_spread_ms({}) == 0.0


