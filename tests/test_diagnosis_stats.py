"""诊断统计基础(stats)的单测。

重点覆盖三类真实踩过的坑,防止回归:
1. 退化分布(错误率常年恒 0)——"首次出错"必须判异常,且不能靠绝对地板;
2. 波动大的服务——"看着吓人但落在自身正常范围里"必须判正常(对照组误报的根因);
3. 样本量纪律——历史样本不足必须返回"不做判断",不许给噪声值。
"""
from app.diagnosis import stats as S


def _dist(values):
    return S.Distribution.build(values)


def test_quantile_interpolates():
    d = _dist([1, 2, 3, 4, 5])
    assert d.p50 == 3
    assert d.quantile(0.0) == 1
    assert d.quantile(1.0) == 5
    assert 1 < d.quantile(0.25) < 3


def test_build_drops_non_finite():
    d = _dist([1, float("nan"), 2, float("inf"), None, 3, "x"])
    assert d.values == [1, 2, 3]


def test_insufficient_history_gives_no_judgement():
    """样本不足必须返回 None 并说明,不能拿噪声当判据。"""
    d = _dist(list(range(S.MIN_HISTORY_SAMPLES - 1)))
    assert not d.enough
    assert d.position(999) is None
    dev = S.deviation(999, d, label="x")
    assert dev.position is None and not dev.abnormal
    assert "样本不足" in dev.basis


def test_degenerate_zero_history_first_error_is_anomalous():
    """错误率常年恒 0 的服务首次出错 = 历史上从未出现过,必须判异常。
    这正是旧实现被绝对地板(ERROR_RISE_MIN=5%)挡死的那类信号。"""
    d = _dist([0.0] * 288)
    dev = S.deviation(0.038, d, label="ad 错误率")
    assert dev.abnormal
    assert dev.beyond_history
    assert dev.position == 1.0


def test_zero_current_value_on_zero_history_is_normal():
    """全 0 历史 + 当前 0 必须判正常(用"严格小于"的语义,不能用"小于等于")。"""
    d = _dist([0.0] * 288)
    dev = S.deviation(0.0, d, label="x")
    assert not dev.abnormal
    assert dev.position == 0.0


def test_volatile_service_within_own_range_is_not_anomalous():
    """波动大的服务:一个看着很大的值若落在自身历史范围内,不许判异常。
    对照组 recommendation(862ms,p90 224ms/max 4660ms)就是这么被误报的。"""
    d = _dist([15] * 200 + [224] * 50 + [4660] * 10 + [862] * 28)
    dev = S.deviation(862, d, label="recommendation 时延")
    assert not dev.abnormal
    assert dev.position < S.POSITION_MIN


def test_stable_service_small_shift_is_anomalous():
    """稳定服务的小幅真实变化必须被判异常——这是旧"倍数排序"最吃亏的场景:
    cart 的 p50 只有 3ms,涨到 30ms 是 10 倍真实故障,却比不过波动服务的噪声。"""
    d = _dist([2, 3, 3, 4] * 70 + [3] * 8)
    dev = S.deviation(30, d, label="cart 时延")
    assert dev.abnormal
    assert dev.beyond_history


def test_no_data_is_not_normal():
    """当前值无数据 ≠ 正常:必须返回"不做判断",basis 如实说明。"""
    d = _dist([1] * 100)
    dev = S.deviation(None, d, label="cpu")
    assert dev.position is None and not dev.abnormal
    assert "无数据" in dev.basis


def test_layer_ordering():
    """层级:超过历史最大值(1)比只超出正常波动带(0)更可疑;不异常为 -1。

    2026-10-22 改:准入从"分位位置"换成"高分位 + n×IQR 的边界"。所以"等于历史最大值"
    不再自动算异常——如果这个服务平时就横跨 1~100,那 100 属于正常波动(旧实现会因为
    它排在最末位而报异常,对照组误报就是这么来的)。"""
    tight = _dist([1, 2, 3, 4, 5] * 20)
    assert S.layer_of(S.deviation(999, tight)) == 1
    # 平时恒定在 1,偶尔出现 100 的尖峰:边界只有 1,50 超出边界但没破历史最大值
    spiky = _dist([1] * 285 + [100] * 3)
    assert S.layer_of(S.deviation(50, spiky)) == 0
    # 波动本身就横跨 1~100:100 在正常带内
    wide = _dist(list(range(1, 101)))
    assert S.layer_of(S.deviation(100, wide)) == -1
    assert S.layer_of(S.deviation(1, wide)) == -1
    assert S.layer_of(None) == -1


def test_position_monotonic():
    """分位位置必须单调不减——否则排序会乱。"""
    d = _dist(list(range(0, 100)))
    prev = -1.0
    for v in (-5, 0, 25, 50, 99, 100, 1000):
        pos = d.position(v)
        assert pos >= prev
        prev = pos


def test_basis_is_human_readable():
    """判据必须能被人看懂——证据包要拿它解释"为什么判它异常"。"""
    d = _dist([0.0] * 288)
    dev = S.deviation(0.038, d, label="ad 错误率")
    assert "最大值" in dev.basis and "0.038" in dev.basis


def test_ratio_is_none_when_median_is_zero():
    """历史中位数为 0 时比值无定义 → 如实返回 None,不许编一个数。
    调用方(层内排序)必须能接受 ratio 为 None——这也是"分位位置才是主判据、
    比值只做层内排序"的原因:主判据不能依赖一个可能不存在的量。"""
    d = _dist([0.0] * 288)
    dev = S.deviation(0.038, d, label="x")
    assert dev.ratio is None
    assert dev.abnormal          # 主判据(分位位置)照样成立


# ---------- 推导锁:阈值必须由 α 反解,不许退回拍值 ----------

def test_thresholds_are_derived_from_false_alarm_target():
    """分位门槛与样本量下限必须同源于 FALSE_ALARM_PER_TEST,不能各拍各的。
    这条锁死《裁判层整改方案》§3.1 的整改:两者若不同源,门槛会随样本量静默变味。"""
    import math
    alpha = S.FALSE_ALARM_PER_TEST
    assert S.POSITION_MIN == 1.0 - alpha
    assert S.MIN_HISTORY_SAMPLES == math.ceil(1.0 / alpha)


def test_trace_min_samples_derived_from_quantile():
    """trace 样本下限由"慢"的定义分位反解:p95 之上平均至少要有 1 个观测。"""
    import math
    from app.diagnosis import deviation as D
    assert D.TRACE_MIN_SAMPLES == math.ceil(1.0 / (1.0 - D.LATENCY_QUANTILE))


def test_false_alarm_rate_follows_sample_size():
    """误报率由样本量算出(顺序统计:1/(n+1)),不是配出来的。"""
    assert S.false_alarm_rate(24) == 1 / 25
    assert S.false_alarm_rate(288) == 1 / 289
    assert _dist([1.0] * 288).false_alarm_rate == 1 / 289
    assert S.false_alarm_rate(0) == 1.0


def test_platform_false_alarm_budget_bonferroni():
    """全平台误报预算反解:α = 每天容忍次数 / 判定次数。"""
    assert S.platform_false_alarm_budget(20, 288, 1.0) == 1.0 / (20 * 288)
    assert S.platform_false_alarm_budget(0, 0, 0.5) == 0.5   # 退化输入不炸


def test_anomaly_threshold_shared_entry():
    """判"超范围"的阈值只有一个入口,粗筛判定与 onset 检测共用(判据不分叉)。"""
    d = _dist(list(range(1, 101)))
    assert S.anomaly_threshold(d) == d.quantile(S.POSITION_MIN)
    assert S.anomaly_threshold(None) is None
    assert S.anomaly_threshold(_dist([])) is None


def test_baseline_ignores_unregistered_fault_windows():
    """历史里混进 2% 的故障窗,不能把边界撑到故障水平。

    2026-10-23 实测:frontend 587 个窗里有 12 个(2.0%)未登记的故障窗,p99 就被
    撑到 15000ms,而正常值只有 360ms —— 判据拿"最坏的一次故障"当了"正常上限",
    于是什么都检不出来。清洗后边界必须回到正常量级。
    """
    # 真实时延是连续量(有抖动),不是"五百个一模一样的 3.0"——后者 MAD=0,
    # 走的是"多峰不清洗"那条分支,测不到这里要测的东西。
    normal = [3.0 + (i % 10) * 0.1 for i in range(500)]
    d = _dist(normal + [4.0] * 50 + [15000.0] * 12)
    p99, _p01, _iqr, used = d.robust_stats()
    assert used < d.n, "应该剔掉离群窗"
    assert p99 < 100, f"边界被离群窗带跑了:{p99}"


def test_sparse_error_rate_baseline_stays_at_zero():
    """错误率常年 0、偶发 100% 的历史:正常水平仍然是 0。

    稀疏计数比率的"正常"就是中位数(0),不等于 0 的窗口本身就是可疑事件,
    不能反过来当基线——否则 ad 那类服务的历史故障会把边界顶到 100%。
    """
    d = _dist([0.0] * 500 + [1.0] * 10)
    p99, _p01, _iqr, _used = d.robust_stats(sparse=True)
    assert p99 == 0.0


def test_multimodal_latency_keeps_its_spread():
    """多峰的时延分布不许被当离群点剔掉。

    15ms 为主、偶尔 224ms、极少数 4.6s 是真实的服务画像(MAD 会是 0),
    剔掉就等于把它的正常波动删了,862ms 这种正常值会被误判成异常。
    """
    d = _dist([15] * 200 + [224] * 50 + [4660] * 10 + [862] * 28)
    p99, _p01, _iqr, used = d.robust_stats(sparse=False)
    assert used == d.n, "连续量 MAD=0 时不应清洗"
    assert p99 > 862, "多峰的尾部必须留在基线里"


def test_insufficient_events_is_genuinely_not_abnormal():
    """样本量保护必须真的"不做判断":全零历史 + 不足 3 次事件 → abnormal 必须为 False。
    2026-10-25 夜跑实锤的回归:保护只写了"不做判断"的话,返回的对象仍带 value/band
    (全零历史 band=0),1.25 个噪声事件照样越界成异常,把 flagd 顶成锚点。"""
    from app.diagnosis.stats import Distribution, deviation
    dist = {"error": Distribution.build([0.0] * 50)}   # 历史全零
    dev = deviation(0.0005, dist["error"], label="错误率",
                    calls_rate=2.5, window_s=300)       # 0.0005×2.5×300≈0.38 次事件
    assert dev.abnormal is False, "不足 3 次事件必须是真·不可判,不是嘴上说说"
    assert "不做判断" in dev.basis
