"""诊断判据的统计基础:波动归一化 + 样本量纪律。

为什么要有这个模块(血泪史,2026-10-14 跑批实测):
粗筛原本用"当前窗值 / 基线窗值"的**裸比值**判异常,这在真实靶子上是坏的——
把 6 个服务 24 小时内的 p95 分布拉出来看:

    recommendation   p50 16ms   p90 257ms   max 4666ms   (max/p50 = 296x)
    cart             p50  3ms   p90   4ms   max  612ms   (max/p50 = 239x)
    ad               p50  2ms   p90   6ms   max10000ms   (max/p50 = 5218x)

**每个服务自己的正常范围就横跨两个数量级**(低频服务在 5 分钟窗里请求数很少,
histogram_quantile 估出来的 p95 基本等于"那一窗最慢的那几个请求",方差极大)。
于是拿"当前窗"和"24 小时前的某一个窗"相除,商可以是任意大——**比值大小反映的是
服务的固有波动,不是异常程度**。后果:
  - recommendation 这种天然波动 296 倍的服务,永远赢过 cart 这种 p90 只有 4ms 的
    稳定服务身上发生的真实故障(cartFailure 把 cart 打到 30ms,10 倍真实故障,
    在"倍数排序"里排不上号);
  - 对照组(没注入任何故障)照样报出 recommendation 为根因、置信 high——**误报**。

根因不是"排序判据不够聪明",是**在噪声很大的序列上拿两个点做除法,然后按商排序**。
这个模块提供正确的做法:

1. **基线是分布,不是点**:把该服务该指标过去 N 小时的窗口值收成一个经验分布,
   而不是取某一个窗口当基线。单点基线对波动大的服务毫无统计意义。
2. **偏离度是分位位置,不是比值**:问"当前值在自己历史的分布里排在哪",
   而不是"当前值是基线的几倍"。分位位置天然有界、跨服务可比、对波动自动钝化——
   波动大的服务分母自然大,波动小的服务分母自然小。
3. **样本量纪律**:历史样本不足就不给判断(返回 None),而不是给一个噪声值。
   这条纪律 trace 层早就有(deviation.TRACE_MIN_SAMPLES),指标层一直缺,这里补上。

本模块**零 IO、纯函数**——取数由调用方负责,方便单测。

判据参数(全部可推导,见《裁判层整改方案》§3.1):

**分位门槛不是一个"拍值",而是由目标误报率反解出来的量**。设单次"某服务某维异常"
判定允许的误报率为 α,则:

    POSITION_MIN        = 1 − α            分位门槛(当前值必须落在自身历史的前 α 分位内)
    MIN_HISTORY_SAMPLES = ceil(1 / α)      分位门槛可分辨所需的最小样本量
                                           (n 个点最多分辨 1/n 的分位,低于它门槛不可解)

**这两个数以前是各拍各的(0.99 / 24),且互不相容**:n 只到 24 时,位置只能是 k/24,
满足 ≥0.99 的只有 k=24,也就是"超过历史最大值"——门槛静默变成了另一个判据,
而 `POSITION_BEYOND_MAX` 已经是那个判据。门槛的严格程度**由样本量悄悄决定**,
代码里看不出来,这是"阈值语义随无关参数漂移"的典型病。现在两者同源于 α,
样本量不足(不可分辨该分位)就如实不判,而不是给一个悄悄变味的结论。

当前 α = 1%(单次判定),n=288(24h @5min)时门槛 = 落在自身历史前 3/288。
**全平台扫描的族错误率**是 M×K×α(M 个服务、每天 K 次判定),需要按预算反解时
用 `platform_false_alarm_budget()`。**改 α 之前先跑 scripts/eval_detect_offline.py 看数字。**
"""
import bisect
import math
from dataclasses import dataclass, field

# 单次"某服务某维异常"判定允许的目标误报率(策略参数)。
# 语义明确、可反解:调大 → 更敏感(多报、可能误);调小 → 更保守(少报、可能漏)。
FALSE_ALARM_PER_TEST = 0.01

# 分位位置门槛 = 1 − α(由 α 反解,不是拍值)。
POSITION_MIN = 1.0 - FALSE_ALARM_PER_TEST


def position_min(tests: int = 1) -> float:
    """分位门槛的**多重比较版**:Bonferroni 把"每次扫描 1%"的预算摊到 m 次判断上。

    为什么必须摊:一次扫描同时判 14 个服务 × 6 个维度 = 84 次,单次给 1% 的话,
    "至少冒一个假警报"的概率是 1 − 0.99^84 ≈ 57%——**系统完全健康时,一半以上的
    扫描都会蹦出一个假锚点**(2026-10-21 夜间对照组实测:frontend 错误率 0.16%、
    随后 cart 0.12% 连续两轮被误报成根因,就是这个病)。

    m 次判断下,单次门槛收紧到 α/m;等价于"只有过去从没出现过的才算异常"
    (样本量 209 时,分位分辨率 1/209,0.99988 只有位置 1.0 够得着)。

    代价:锚点变少,召回下降。这是拿召回换精度,不是白拿的。
    """
    return 1.0 - FALSE_ALARM_PER_TEST / max(int(tests), 1)
# 超过历史最大值——最硬的一档("这个值在过去 N 小时里从没出现过")。这是**定义**,
# 不是阈值:位置 1.0 的含义就是"大于历史全部样本"。
POSITION_BEYOND_MAX = 1.0
# 历史样本下限 = ceil(1/α):低于它,分位门槛不可分辨(见模块头推导),不做判断(宁缺勿错)。
# 反解式保证它与 POSITION_MIN 同源——不会再出现"门槛随样本量变味"。
MIN_HISTORY_SAMPLES = math.ceil(1.0 / FALSE_ALARM_PER_TEST)

# 三倍法则:某事件在 n 次观测里一次都没出现时,它的发生率 95% 置信上界是 3/n
# (泊松分布,0 次观测的标准结论)。用在"历史全 0"的退化分布上:此时任何非零值都会
# 拿到位置 1.0,"超过最大值"这个判据失效,靠它兜住"一两个偶发失败"的噪声。
# 这是推导出来的数,不是拍的:3 来自 95% 置信水平,不是调参调出来的。
ZERO_BASELINE_EVENTS = 3.0

# 判据边界 = 高分位 + BAND_SENSITIVITY × IQR(四分位距)。
# 这是灵敏度档位,不是阈值:边界值由数据算出来(IQR 是该服务自己的波动),这里只决定
# "要几倍波动才算异常"。取 4 与 Dynatrace 自适应基线的默认灵敏度同档——同一类判据
# 在同一量纲下取同一个灵敏度,不是为本靶子调出来的数。
# 它与 POSITION_MIN 的关系:后者判"在历史里排到多靠前",前者判"偏离平时的波动有多远"。
# 前者对历史极值不敏感(不被一次大故障废掉),所以粗筛的**准入**改用它。
BAND_SENSITIVITY = 4.0

# 清洗基线用的离群点界限:偏离中位数超过 ROBUST_MAD_K × MAD 的窗口判为故障期,
# 不进基线。取 6:MAD 对正态分布约等于 0.6745σ,6×MAD ≈ 4σ,正常数据基本不会被误剔,
# 而故障窗通常比中位数高几个数量级(15 秒 vs 3 毫秒),6 倍足够把它们挡在外面。
ROBUST_MAD_K = 6.0

# 清洗的**总量上限**:最多只允许把历史里最极端的这一小撮当故障期剔掉。
#
# 为什么必须有上限(2026-10-27 实测,这条是当时误报的主因):
# 清洗掉的比例一旦变大,剩下的样本就不再是"这个指标的正常样子"了——边界取的是
# **残样**的分位,而残样只占原始样本的一部分,于是"正常上界"实际卡在原始历史的
# 低得多的分位上,每次扫描都有远超 α 的窗被判异常。实测:ad 的 p95 正常在
# 1.9~5.3ms 之间晃(p99 3.9ms),MAD 只有 0.02ms(它九成窗被直方图桶量化成同一个值),
# 6×MAD 的界限把上界压到 2.14ms——于是 3.07ms、2.48ms 这种日常抖动每一轮都成锚点,
# 一天 20 轮里能误报十几轮。
#
# 取 1% 与单次判定的误报预算同源(同一个 α,不是新拍的数):我们只承认"历史里最极端的
# 1% 可能是故障期",超出这个量的偏离属于这个指标本来的样子,不是故障留下的痕迹。
# 代价说清楚:历史里混进超过 1% 的故障窗时,清洗救不回来——那种情况要靠
# known_incidents 把故障窗登记进来(那是声明,不是猜)。
CONTAMINATION_MAX = FALSE_ALARM_PER_TEST


def false_alarm_rate(n: int) -> float:
    """给定历史样本量,单次"超出自身历史经验范围"判定的误报概率。
    顺序统计/保形预测的标准结论:n 个样本时,新样本超过其最大值的概率 = 1/(n+1)。
    这是**由数据算出来的**误报率,不需要任何阈值常量。"""
    return 1.0 / (n + 1) if n > 0 else 1.0


def platform_false_alarm_budget(services: int, judgements_per_day: int,
                                budget_per_day: float) -> float:
    """全平台扫描的误报预算反解:给定"每天最多容忍几次误报",算单次判定该用多大的 α
    (Bonferroni:α = 预算 / 判定次数)。供配置与文档引用;不改变判据本身。"""
    trials = max(1, services * judgements_per_day)
    return budget_per_day / trials


def tail_multiple() -> float:
    """到达间隔的尾部倍数:在"间隔近似指数分布"的假设下,静默超过 m·ln(1/α) 的概率 < α。
    倍数由目标误报率 α 反解,不是拍值——α 越小,要求的静默越长。"""
    return math.log(1.0 / FALSE_ALARM_PER_TEST)


def min_samples_for_median() -> int:
    """估"典型间隔(中位数)"所需的最小观测数:样本中位数的秩标准误 ≈ 0.5·√n,
    要求它 ≤ 1 个秩 → n ≥ 4 个间隔,即 **5 条到达记录**。低于它,间隔的典型值估不准,
    宁缺勿错(不判)。与 MIN_HISTORY_SAMPLES 同一性质的纪律,只是样本对象是间隔而非窗口。"""
    return 5


@dataclass
class Distribution:
    """一条"服务 × 指标"的历史窗口值分布。

    构造后 values 有序,查分位用 bisect,不重复排序。"""
    values: list[float] = field(default_factory=list)

    @classmethod
    def build(cls, raw: list) -> "Distribution":
        """从原始窗口值序列构造。NaN/Inf/None 一律剔除——它们不是观测值,
        参与统计只会污染分位。"""
        clean = []
        for v in raw or []:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f):
                clean.append(f)
        clean.sort()
        return cls(values=clean)

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def enough(self) -> bool:
        """样本量够不够做判断。不够时调用方必须返回 None,不许给噪声值。"""
        return self.n >= MIN_HISTORY_SAMPLES

    @property
    def false_alarm_rate(self) -> float:
        """这条分布判"超出经验范围"时的单次误报概率(= 1/(n+1),由样本量算出)。
        它跟着样本量走,不是配出来的——历史越长,判得越准、误报越低。"""
        return false_alarm_rate(self.n)

    def quantile(self, q: float) -> float | None:
        """经验分位数(线性插值)。空分布返回 None。"""
        if not self.values:
            return None
        if q <= 0:
            return self.values[0]
        if q >= 1:
            return self.values[-1]
        pos = q * (self.n - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, self.n - 1)
        frac = pos - lo
        return self.values[lo] * (1 - frac) + self.values[hi] * frac

    @property
    def p50(self) -> float | None:
        return self.quantile(0.5)

    @property
    def p90(self) -> float | None:
        return self.quantile(0.9)

    @property
    def p99(self) -> float | None:
        return self.quantile(0.99)

    @property
    def p01(self) -> float | None:
        return self.quantile(0.01)

    @property
    def iqr(self) -> float | None:
        """四分位距(75 分位 − 25 分位)= 这个服务自己的**波动尺度**。

        判据需要一把"多大算大"的尺子,而绝对量不行(错误率 0.1% 对 A 是噪声、
        对 B 是灾难)。IQR 由数据自己算出来,天然带量纲、跨服务可比,不需要换算权重。
        """
        if not self.values:
            return None
        return self.quantile(0.75) - self.quantile(0.25)

    @property
    def max_value(self) -> float | None:
        return self.values[-1] if self.values else None

    def _bulk(self, sparse: bool = False) -> list[float]:
        """剔除离群点后的样本:基线只该由"正常那些窗口"构成。

        为什么必须剔:判据取的是高分位(p99),而 **p99 按定义就是"最差的 1%"**。
        历史里只要混进 1% 以上的故障窗,分位数就落在故障水平上,边界永远够不着——
        2026-10-23 实测:frontend 587 个窗里有 12 个(2.0%)未登记的故障窗,于是
        p99 = 15000ms、错误率边界 = 28%,而正常值是 360ms / 0。判据不是太严,
        是拿"最坏的一次故障"当了"正常上限"。

        剔法用中位数 + MAD(绝对中位差):MAD 不受极端值影响,拿它当尺子量"偏离
        平时多远"。

        MAD = 0(大半窗口的值完全相同)时,两类数据要分开处理:
        - sparse(错误率这类计数比率):中位数就是正常水平(常年 0),不等于中位数的
          那些值本身是可疑事件,不能拿来当基线 → 只留等于中位数的窗口。
        - 连续量(时延这类):MAD=0 往往是**多峰**而不是"恒定"(15ms 为主、偶尔
          224ms、极少数 4.6s,是真实的服务画像),剔掉就等于把它的正常波动删了
          → 退回原样本,不做清洗。

        剔多少也有上限(见 CONTAMINATION_MAX):界限能圈出来的离群点超过上限时,
        只丢最靠外的那些,剩下的放回样本。否则"正常上界"会跟着残样的分位一起塌到
        原始历史的低分位上,把日常抖动报成故障。
        """
        if not self.values:
            return []
        med = self.p50
        devs = sorted(abs(v - med) for v in self.values)
        k = len(devs)
        mad = devs[k // 2] if k % 2 else (devs[k // 2 - 1] + devs[k // 2]) / 2
        if mad <= 0:
            if sparse:
                return [v for v in self.values if v == med]
            return list(self.values)
        lo, hi = med - ROBUST_MAD_K * mad, med + ROBUST_MAD_K * mad
        outside = [i for i, v in enumerate(self.values) if not lo <= v <= hi]
        if sparse:
            # 稀疏计数比率(错误率)不受上限约束:它的"正常"就是中位数本身,
            # 不等于中位数的窗口是可疑事件而不是这个指标的日常样子(见上)。
            return [v for i, v in enumerate(self.values) if i not in set(outside)]
        allowed = int(len(self.values) * CONTAMINATION_MAX)
        if len(outside) > allowed:
            outside.sort(key=lambda i: -abs(self.values[i] - med))
            outside = outside[:allowed]
        drop = set(outside)
        return [v for i, v in enumerate(self.values) if i not in drop]

    def robust_stats(self, sparse: bool = False
                     ) -> tuple[float | None, float | None, float | None, int]:
        """清洗后的 (p99, p01, IQR, 用到的样本数)。

        清洗后样本太少(< 判据下限)就退回原样本——宁可边界偏宽,也不能拿十几条
        样本硬算分位。"""
        vals = self._bulk(sparse)
        if len(vals) < MIN_HISTORY_SAMPLES:
            vals = self.values
        if not vals:
            return None, None, None, 0
        p99, p01 = self._q(vals, 0.99), self._q(vals, 0.01)
        iqr = (self._q(vals, 0.75) or 0.0) - (self._q(vals, 0.25) or 0.0)
        return p99, p01, iqr, len(vals)

    def _q(self, vals: list[float], q: float) -> float | None:
        if not vals:
            return None
        if q <= 0:
            return vals[0]
        if q >= 1:
            return vals[-1]
        pos = q * (len(vals) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)

    def band(self, sensitivity: float = BAND_SENSITIVITY,
             direction: str = "high", sparse: bool = False) -> float | None:
        """判据边界:高分位 + 灵敏度 × 自身波动。

        为什么不是"历史最大值":最大值是单点,一个服务只要在历史里被打崩过一次,
        之后任何没打崩的故障都永远够不着它(实测:ad 历史 max 8.4%,真实故障 3.8%
        被判正常;frontend 历史 min 0.07 次/秒,调用量掉一半判正常)。
        换成"高分位 + n×IQR"之后,边界跟着这个服务**平时怎么波动**走,而不是跟着
        历史里最极端的那一次走。

        分位与波动都取自**清洗后的样本**(见 _bulk):历史里没登记的故障窗不进基线。

        direction="low"(调用量塌陷)取下界,并在地板 0 处截断——调用量不可能为负。
        """
        if not self.values:
            return None
        p99, p01, iqr, _ = self.robust_stats(sparse)
        # IQR 退化(=0)时不能直接用,否则边界退化成"等于历史最大值"。
        # 为什么:IQR 是四分位距,要求"过半窗口有差异"才有值。可有些指标常年被量化在
        # 同一档上——2026-10-30 对照题(没注入任何故障)误报就是这么来的:quote 的 p95
        # 时延 540 个窗口里过半完全等于 1.9ms,于是 Q1=Q3=1.9、IQR=0、边界=1.949,
        # 而当前值 1.951 只高出 0.002 毫秒就被判成"网络时延"。
        # 改法:退化时拿"中位数到高分位(或低分位)的距离"当波动尺度——这也是从数据
        # 本身取的,不是拍一个百分比。错误率这类基线恒 0 的指标 p99-p50 仍是 0,
        # 行为完全不变("历史上从没出现过"依然算异常)。
        scale = iqr or 0.0
        if scale <= 0:
            med = self.p50 or 0.0
            scale = max(0.0, (med - (p01 or 0.0)) if direction == "low"
                        else ((p99 or 0.0) - med))
        spread = scale * sensitivity
        if direction == "low":
            return max(0.0, (p01 or 0.0) - spread)
        return (p99 or 0.0) + spread

    def position(self, cur: float, *, direction: str = "high") -> float | None:
        """当前值在历史分布里的分位位置,0..1。

        direction="high":位置 = **严格小于**当前值的历史窗口比例(越大越异常,如时延/错误率/资源);
        direction="low" :位置 = **严格大于**当前值的历史窗口比例(越小越异常,如调用量塌陷)。

        取"严格"而不是"小于等于",是为了让退化分布(错误率常年恒 0 的服务)表现正确:
        全 0 的历史 + 当前 0 → high 方向位置 0.0(不异常);全 0 的历史 + 当前 0.038
        → 1.0(异常)。**这就是不需要绝对量地板的原因**——"这个值在我自己历史上从没
        出现过"比"这个值超过 5%"是强得多、也稳得多的判据。

        代价:连续分布下历史极值本身只能拿到 (n-1)/n,拿不到 1.0,所以门槛必须设在
        1.0 以下(见 POSITION_MIN)。

        样本不足返回 None(调用方据此跳过,而不是当成 0)。
        """
        if not self.enough:
            return None
        if not math.isfinite(cur):
            return None
        below = bisect.bisect_left(self.values, cur)
        if direction == "low":
            # 严格大于当前值的比例 = 1 - (小于等于它的比例)
            above = self.n - bisect.bisect_right(self.values, cur)
            return above / self.n
        return below / self.n

    def describe(self) -> dict:
        """给证据包用的紧凑投影——判据要能被人看懂"为什么判它异常"。"""
        if not self.values:
            return {"n": 0}
        return {"n": self.n, "p50": round(self.p50, 4), "p90": round(self.p90, 4),
                "p99": round(self.p99, 4), "max": round(self.max_value, 4)}


@dataclass
class Deviation:
    """一条"当前值 vs 自身历史分布"的偏离判定结果。

    **是否异常**由 band 决定(当前值有没有超出"高分位 + n×IQR"这条边界);
    position 只做**同层内的排序**和"是否超过历史极值"的分档,不再单独当准入。

    为什么改(2026-10-22 离线回放实测):position 当准入时,判据实际退化成"必须打破
    24 小时极值"——真故障 3.8% 够不着历史 max 8.4%,而噪声级的 0.18% 错误率、+1%
    的内存因为"历史没见过"照样触发。既漏又误,根子是没有"偏离多少算多"的尺度。"""
    position: float | None      # 分位位置,None = 样本不足/无数据
    ratio: float | None         # 当前/历史中位数,仅用于层内排序与展示
    basis: str                  # 人话说明,进证据包
    # 判"异常"用的分位门槛。band 生效时它只作为回退路径(手工构造 Deviation 时用)。
    min_position: float = POSITION_MIN
    value: float | None = None  # 当前值,判边界用
    band: float | None = None   # 判据边界(high 是上界,low 是下界)
    direction: str = "high"

    @property
    def abnormal(self) -> bool:
        if self.band is None or self.value is None:
            # 回退:手工构造的 Deviation(单测/旧调用)仍按分位位置判
            return self.position is not None and self.position >= self.min_position
        if self.direction == "low":
            return self.value < self.band
        return self.value > self.band

    @property
    def beyond_history(self) -> bool:
        """超过历史最大值——最硬的一档。"""
        return self.position is not None and self.position >= POSITION_BEYOND_MAX


def deviation(cur: float | None, dist: Distribution, *,
              label: str = "值", direction: str = "high",
              window_s: float | None = None, tests: int = 1,
              calls_rate: float | None = None,
              sensitivity: float = BAND_SENSITIVITY,
              min_samples: int = MIN_HISTORY_SAMPLES) -> Deviation:
    """算当前值相对自身历史分布的偏离。样本不足或无数据一律返回"不异常"且 basis
    说明原因——**不许拿"没有数据"当"正常",也不许拿它当"异常"**,如实陈述。

    direction="high"(默认):越大越异常(时延/错误率/资源用量);
    direction="low"          :越小越异常(调用量塌陷——服务失联/挂死的信号)。
    window_s + calls_rate:本窗的**事件数** = 当前比率 × 调用速率 × 窗长。
    错误率是比率不是速率,光有 window_s 折不出事件数(旧实现直接拿比率乘秒数,
    量纲是错的);给了调用速率才算得对。事件数不足 ZERO_BASELINE_EVENTS 就不判——
    "1 个请求失败 = 50% 错误率"这种样本撑不起结论(Dynatrace 的低流量保护同理)。
    tests:同时判了多少次。仅用于分位门槛的回退路径(见 position_min),band 生效时不用。
    min_samples:样本量下限。缺省是 MIN_HISTORY_SAMPLES(长历史基线的口径);拿**最近
    一小段**的分布当基线时(见 detect 的近期复核)要放宽——那段本来就只有十几个窗,
    用长历史的门槛会直接判"样本不足",复核永远不生效。这里放宽的只是"能不能判",
    判据本身(边界公式、事件数地板)完全不变。

    判"异常"用 band(高分位 + n×IQR),不用"分位位置":
    位置问的是"这个值在历史里排第几",一旦历史里混进过一次同类故障,位置就永远上不去;
    band 问的是"这个值偏离平时的波动有多远",对历史极值不敏感。
    (2026-10-22 实测:位置判据下 ad 3.8% 判正常、frontend-web 0.18% 判异常——既漏又误。)"""
    if cur is None or not math.isfinite(cur):
        return Deviation(None, None, f"{label}当前窗无数据,不做判断", direction=direction)
    if dist.n < max(int(min_samples), 1):
        return Deviation(None, None,
                         f"{label}历史样本不足({dist.n}<{min_samples}),不做判断",
                         direction=direction)
    # 分位位置只在样本够解分位时才有意义(见 MIN_HISTORY_SAMPLES);没有它不影响边界判定,
    # 边界是"高分位 + n×波动",十几个窗也估得出来。
    pos = dist.position(cur, direction=direction) if dist.enough else None
    med = dist.p50
    ratio = round(cur / med, 3) if med else None

    pmin = position_min(tests)
    # 错误率是"稀疏计数比率":正常值常年是 0,不等于 0 的窗口多半就是故障窗,
    # 不能拿来当基线(见 _bulk 的 sparse 分支)。
    sparse = calls_rate is not None
    p99r, p01r, iqr_r, used = dist.robust_stats(sparse)
    spread = iqr_r or 0.0
    edge = dist.band(sensitivity, direction, sparse)
    base = (f"历史 {used}/{dist.n} 窗(已剔离群点) "
            f"p50 {med:.4g} / p99 {p99r:.4g} / 波动(IQR){spread:.4g}")

    # 错误率这种"计数类比率"另算波动:它的 IQR 经常是 0(大半窗口一次错都没有),
    # 此时"p99 + 4×IQR"退化成 p99 本身——而 p99 按定义就有 1% 的窗口超过它,
    # 96 次判断摊下来,健康平台每次扫描必冒一个假警报(2026-10-22 对照组实测:
    # frontend 错误率 0.16% 反复被报成根因,就是这么来的)。
    # 计数过程的波动有解析解:λ 次事件的泊松标准差是 √λ。折回比率空间,
    # 就是 √(p99/本窗调用次数)——由数据算,不需要拍值,而且永远不会塌成 0。
    if direction == "high" and calls_rate is not None and window_s and calls_rate > 0:
        scale = max(calls_rate * window_s, 1.0)
        lam = (p99r or 0.0) * scale
        poisson = math.sqrt(lam) / scale if lam > 0 else 0.0
        edge = (p99r or 0.0) + sensitivity * poisson
        base = (f"历史 {used}/{dist.n} 窗(已剔离群点) "
                f"p50 {med:.4g} / p99 {p99r:.4g} / "
                f"本窗调用约 {scale:.0f} 次(泊松波动 {poisson:.4g})")

    # 样本量纪律:事件数不够就不判,不管边界算出来多低。
    # 返回的判定对象必须**真的不可判**——position/value/band 全空,abnormal 天然为
    # False。旧实现保留了 value 与 band:全零历史算出 band=0,1.25 个噪声事件照样
    # "越界"成异常,保护形同虚设(2026-10-25 夜跑实锤:noop 对照组与 loadgen 组
    # 都被 flagd 的 ~1 次/窗错误事件顶成锚点,理由栏里明写着"不做判断"却判了)
    if direction == "high" and calls_rate is not None and window_s:
        events = cur * max(calls_rate, 0.0) * window_s
        if events < ZERO_BASELINE_EVENTS:
            return Deviation(None, ratio,
                             f"{label}当前值 {cur:.4g},折成本窗只有 {events:.2f} 次事件"
                             f"({base});不足 {ZERO_BASELINE_EVENTS:.0f} 次撑不起判断,不做判断")

    if direction == "low":
        if edge is not None and cur < edge:
            basis = (f"{label}当前值 {cur:.4g} 低于自身正常下界 {edge:.4g}"
                     f"(p01 {p01r:.4g} − {sensitivity:.0f}×波动 {spread:.4g};{base})")
        else:
            basis = (f"{label}当前值 {cur:.4g} 在正常下界 {edge:.4g} 之上({base}),属正常波动")
        return Deviation(pos, ratio, basis, min_position=pmin,
                         value=cur, band=edge, direction=direction)

    if edge is not None and cur > edge:
        hard = (" 且高于历史最大值"
                if pos is not None and pos >= POSITION_BEYOND_MAX else "")
        basis = (f"{label}当前值 {cur:.4g} 超出自身正常上界 {edge:.4g}"
                 f"(p99 {p99r:.4g} + {sensitivity:.0f}×波动 {spread:.4g};{base}){hard}")
    else:
        basis = (f"{label}当前值 {cur:.4g} 在正常上界 {edge:.4g} 之内({base}),属正常波动")
    return Deviation(pos, ratio, basis, min_position=pmin,
                     value=cur, band=edge, direction=direction)


def layer_of(dev: Deviation | None) -> int:
    """把偏离判定折算成离散层级,供字典序排序(与 localize 同一套"分层 + 层内幅度")。
    层级语义固定、跨服务可比;幅度只做层内排序。

    -1 不异常 / 0 超过历史 p99 / 1 超过历史最大值。数字越小越可疑。"""
    if dev is None or not dev.abnormal:
        return -1
    return 1 if dev.beyond_history else 0


def anomaly_threshold(dist: "Distribution | None") -> float | None:
    """判"超出正常范围"的**统一阈值入口** = 该服务自身历史分布的 POSITION_MIN 分位。

    粗筛的异常判定(`Distribution.position`)与指标发病时刻(onset)的越限检测共用
    这一个入口——同一份数据两处判据分叉,是比没有数据更糟的事(见 deviation.compute
    的说明)。分布为空/不存在时返回 None:调用方据此"不给阈值",而不是硬凑一个。"""
    if dist is None or not dist.values:
        return None
    return dist.quantile(POSITION_MIN)
