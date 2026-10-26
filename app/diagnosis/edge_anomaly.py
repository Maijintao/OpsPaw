"""调用边时延异常检测(架构移植自 MicroRCA-Agent,CCF AIOps 2025 决赛 Top5,48.52 分;
检测核按实测替换为 MAD 稳健判线)。

为什么要有这一层:deviation 的时延判线是**服务级**(该服务 p95 vs 基线),看不出
"哪条调用边慢"。纯时延型故障里,"frontend 慢是因为 checkout 慢还是它自己的问题"
靠服务级 p95 分不清。本模块按(调用方, 被调方, 操作名)粒度给每条跨服务调用边
建立正常期画像,检出"这条边在这个窗里慢得不像它自己"的异常窗——这是原方案
localization 拿分的确定性件,也是我们打分体系里数据驱动的缺口。

架构(与原方案对齐):正常期 span 按(调用方, 被调方, 操作名)分组 → 30s 滑动窗
取 duration 均值 → 每边训一个正常画像;推理时同样分组滑窗,超判线的窗为异常窗。

检测核为什么不是原方案的 IsolationForest(实测教训,探针数据留档在测试里):
- IF 的 score 是树结构的产物,单维、每边仅 ~20-80 个窗的小样本上**不是偏离度的
  单调函数**——实测 5 倍时延窗的 score(-0.596)与正常窗(-0.4~-0.7)完全重叠,
  sklearn 的 predict 在 float 与 auto 两种 offset 下分别把典型窗判异常、把大偏离
  放行(样本量远离其 ψ=256 的假设);
- 单维数据上数学上正确的做法是稳健统计:窗均值近似正态(CLT,每窗 ≥15 个样本),
  判线 = median ± k·σ̂,σ̂ = 1.4826·MAD(1.4826 = Φ⁻¹(0.75),正态一致性常数),
  k = Φ⁻¹(1 − FALSE_ALARM_PER_TEST/2) 由项目的目标误报率反解——单窗误报率
  与全系统统一控制,无常量可拍。
原方案的架构贡献(调用边分组/滑窗特征/正常期建模)全部保留,检测核替换在
面试叙事里是"移植 + 实测改进",不是"照抄"。

本地化差异(原方案没有的场景):
- 流式 span 排除:flagd EventStream 这类长连接的 duration 是连接存活不是请求
  耗时,混进训练/推理全错(deviation.span_stats 同一条纪律);
- 只取跨服务调用边:服务内部父子与根入口 span 的时延已由服务级 p95 覆盖,
  增量价值只在"哪条边慢";
- 错误 span 不过滤:超时错误的 duration 本身就是慢的证据,瞬断错误的 duration≈0
  也是"这条边不健康"的形态,都不该被洗掉。

纪律:检测结果**只进证据包,不改判排序**——与门禁/证伪同一原则:代码给事实,
LLM 做判定。画像缺失/未训练时如实返回原因,不假装检测过。
"""
import logging
import pickle
import statistics
from bisect import bisect_left, bisect_right
from pathlib import Path
from statistics import NormalDist

from app.diagnosis.collect import Span
from app.diagnosis.deviation import is_streaming_span
from app.diagnosis.stats import FALSE_ALARM_PER_TEST

logger = logging.getLogger("app")

# 画像存放:data/iforest/stats.pkl(训练脚本生成;目录名保留 iforest 交代出处)
MODEL_DIR = Path(__file__).resolve().parents[2] / "data" / "iforest"

# ---------- 超参:架构参数来自 MicroRCA-Agent 原方案,出处见 thresholds 登记 ----------
WIN_SIZE_S = 30             # 原方案:滑动窗口长(秒)
# 本地化策略参数:单边最少训练窗数。低于它说明这条边在正常期几乎没有流量,
# 画像不可信,跳过训练(推理时该边如实报"无画像",不当异常报)。
MIN_TRAIN_WINDOWS = 5
# 本地化策略参数:单边最少异常窗数。单窗超界在目标误报率下是预期内噪声
# (α=1% 时 20 窗期望 0.2 窗误标);≥2 窗(10% 的窗异常)才构成边级事件。
MIN_ANOMALY_WINDOWS = 2
# 单次诊断进证据包的事件上限:prompt 预算保险丝(同 MAX_CANDIDATES 的定位),不是判据
EVENT_CAP = 8


def _k() -> float:
    """判线系数:双侧 α 检验的临界值,由 FALSE_ALARM_PER_TEST 反解(无常量可拍)。"""
    return NormalDist().inv_cdf(1.0 - FALSE_ALARM_PER_TEST / 2.0)


def _group_edge_spans(spans: list[Span]) -> dict[tuple[str, str, str], list[Span]]:
    """跨服务调用边分组:(caller, callee, operation) → 该边的 server span 列表。
    parent 解析与 topology.build 同一套 (trace_id, span_id) 映射;流式 span 排除。"""
    by_id = {(sp.trace_id, sp.span_id): sp for sp in spans
             if sp.trace_id and sp.span_id}
    groups: dict[tuple[str, str, str], list[Span]] = {}
    for sp in spans:
        if sp.operation and is_streaming_span(sp.operation):
            continue
        parent = by_id.get((sp.trace_id, sp.parent_span_id)) \
            if sp.trace_id and sp.parent_span_id else None
        if parent is None or parent.service == sp.service:
            continue
        groups.setdefault((parent.service, sp.service, sp.operation), []).append(sp)
    return groups


def _window_means(spans: list[Span], win_us: int) -> tuple[list[int], list[float]]:
    """30s 滑窗取 duration 均值(与原方案 _slide_window 同语义:窗内无数据跳过)。
    返回 (窗起点 us 列表, 窗均值列表)。排序 + 前缀和,不引 pandas。"""
    pairs = sorted((sp.start_us, sp.duration_us) for sp in spans)
    starts = [p[0] for p in pairs]
    pre = [0]
    for _, d in pairs:
        pre.append(pre[-1] + d)
    out_t, out_m = [], []
    t0, t_max = starts[0], starts[-1]
    t = t0
    while t <= t_max:                      # 原版闭区间 [i, i+win]
        l = bisect_left(starts, t)
        r = bisect_right(starts, t + win_us)
        if r > l:
            out_t.append(t)
            out_m.append((pre[r] - pre[l]) / (r - l))
        t += win_us
    return out_t, out_m


def train(spans: list[Span], model_dir: Path = MODEL_DIR) -> dict:
    """正常期 span → 每边一个正常画像(median/MAD/窗数),存 model_dir/stats.pkl。
    返回 stats dict。样本不足的边跳过(字典里没有它,推理时如实报无画像)。"""
    win_us = WIN_SIZE_S * 1_000_000
    stats: dict = {}
    for (caller, callee, op), group in sorted(_group_edge_spans(spans).items()):
        _, means = _window_means(group, win_us)
        if len(means) < MIN_TRAIN_WINDOWS:
            continue
        key = f"{caller}>{callee}>{op}"
        med = statistics.median(means)
        mad = statistics.median(abs(m - med) for m in means)
        stats[key] = {"median": med, "mad": mad, "windows": len(means),
                      "mean": statistics.fmean(means)}
    if stats:
        model_dir.mkdir(parents=True, exist_ok=True)
        with open(model_dir / "stats.pkl", "wb") as f:
            pickle.dump(stats, f)
    return stats


def load(model_dir: Path = MODEL_DIR) -> tuple[dict | None, str | None]:
    """加载正常期画像。返回 (stats|None, 失败原因)。"""
    try:
        with open(model_dir / "stats.pkl", "rb") as f:
            return pickle.load(f), None
    except FileNotFoundError:
        return None, "调用边画像未训练(缺 data/iforest/stats.pkl,先跑 scripts/train_edge_stats.py)"
    except Exception as exc:  # noqa: BLE001
        return None, f"调用边画像加载失败:{type(exc).__name__}: {exc}"


def detect(spans: list[Span], stats: dict | None) -> tuple[list[dict], str | None]:
    """诊断窗 span → 异常边事件列表(按被调方聚合,一行/边,供证据包注入):
    {caller, callee, operation, anomaly_windows, total_windows, mean_ms,
     normal_mean_ms, first_anomaly_us}。返回 (events, 未检测原因)。
    stats=None(未训练/未加载)时如实拒检,不静默。
    MAD=0 的边跳过:正常期窗均值完全恒定的边(真实系统罕见,人造数据才有),
    没有方差就没有"偏离"的参照系,任何判线都是拍值——如实不检,不当异常报。"""
    if stats is None:
        return [], "调用边时延检测不可用:正常期画像未加载"
    win_us = WIN_SIZE_S * 1_000_000
    k = _k()
    events: list[dict] = []
    for (caller, callee, op), group in sorted(_group_edge_spans(spans).items()):
        key = f"{caller}>{callee}>{op}"
        ns = stats.get(key)
        if ns is None:
            continue                    # 正常期没这条边的流量,没有画像,不猜
        if not ns.get("mad"):
            continue                    # 零方差边无参照系,跳过(见函数 docstring)
        times, means = _window_means(group, win_us)
        if not means:
            continue
        sigma = 1.4826 * ns["mad"]      # MAD → 正态 σ 的一致性换算
        med = ns["median"]
        anom = [i for i, m in enumerate(means) if abs(m - med) > k * sigma]
        if len(anom) < MIN_ANOMALY_WINDOWS:
            continue                    # 单窗超界当噪声滤掉(误报控制,见常量注释)
        events.append({
            "caller": caller, "callee": callee, "operation": op,
            "anomaly_windows": len(anom), "total_windows": len(means),
            "mean_ms": statistics.fmean(means[i] for i in anom) / 1000.0,
            "normal_mean_ms": ns["mean"] / 1000.0,
            "first_anomaly_us": times[anom[0]],
        })
    return events, None


def evidence_line(ev: dict, symptom_onset_us: int | None = None,
                  symptom_onset_trusted: bool = True) -> str:
    """事件 → 证据行([E#] 编号由调用方拼)。时间先后只在症状侧锚点可信时给结论,
    与 deviation 的窗边界纪律同一口径——不可信就不写先后,只报事实。"""
    base = (f"调用边时延检测(调用边画像,正常期训练):{ev['caller']}→{ev['callee']}"
            f"(操作 {ev['operation']})有 {ev['anomaly_windows']}/{ev['total_windows']} "
            f"个 30s 窗的均值超出该边正常期分布的误报率判线(异常窗均值 "
            f"{ev['mean_ms']:.0f}ms,正常期均值 {ev['normal_mean_ms']:.0f}ms)"
            "——偏离幅度看两个均值的差")
    if symptom_onset_us is not None and symptom_onset_trusted and ev["first_anomaly_us"]:
        earlier = ev["first_anomaly_us"] < symptom_onset_us
        base += (";该边异常窗起点早于症状服务发病时刻" if earlier
                 else ";该边异常窗起点晚于症状服务发病时刻")
    return base


def cap_events(events: list[dict], cap: int) -> list[dict]:
    """单次诊断进证据包的事件上限:按异常窗数降序取前 N。超过的丢弃并在调用方
    说明——不是判据,是 prompt 预算保险丝(同 MAX_CANDIDATES 的定位)。"""
    return sorted(events, key=lambda e: (-e["anomaly_windows"], e["callee"]))[:max(cap, 0)]
