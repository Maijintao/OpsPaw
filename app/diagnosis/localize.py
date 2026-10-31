"""异常传播定位(管线第④段,MicroRCA 简化版):异常子图 + 字典序候选排序 + Top-K 证据包。

两个因果信号(SOP §4):
- 时间先后:果不先于因——异常开始早于症状服务的节点更可能是根因;
- 拓扑方向:症状服务 ±2 跳的上下游进候选(远跳排序靠后,证据包写明方向);偏离表里
  其余异常服务也一并纳入(召回优先,排序纠错)。

排序是**字典序**(结构判据,不是加权打分):先比严重层级,同级再比时间先后、
跳数、层内偏离度。不再用权重把不同量纲的偏离混进一个加法——旧 W_ERROR/W_LATENCY/
W_CPU/W_MEM 四个拍值只编码了「error>latency>resource」一个排序,却要靠校准维持,
跨维污染(5 倍 CPU 偏离压过时延)就是这么来的。层内偏离度各有天然刻度
(error_dev 对数、latency_dev 倍数、resource 比值),不需要换算权重。
"""
from dataclasses import dataclass, field

from app.diagnosis.collect import Span
from app.diagnosis.deviation import ServiceDeviation, time_anchor_ok
from app.diagnosis.topology import ServiceGraph

# MAX_CANDIDATES 是**预算保险丝**不是判据:异常候选**全部**给 LLM(排序只决定呈现
# 顺序)——之前 TOP_K=3 截断把真因从菜单上划掉了两次(实测:adFailure 真因 ad 是症状
# 节点本尊、recommendationCacheFailure 真因 recommendation 都被排到第 4 名挤掉,
# LLM 面对的菜单里根本没有正确答案)。上限 10 只防"全平台级联异常"把 prompt 撑爆,
# 正常故障场景(异常服务 3~6 个)碰不到它。
MAX_CANDIDATES = 10
# 候选范围不再有"跳数硬截断":症状 ±2 跳给出方向标签,但**偏离表里任何异常的服务都会被
# 纳入**(见 localize 末段的 scan_anchor 并入)——"真因没进候选集"是检索失败,无解;
# 排错序还能救,所以召回优先。跳数只作排序特征(_HOPS_BY_DIRECTION)。

# direction → 与症状服务的跳数(排序用:近的排前)。旧实现 symptom/error_ref/一跳
# 全记 0、只有两跳记 1——"症状服务比一跳邻居更近"这个事实没表达出来,rank_key 的
# hops 键形同虚设。错误指认/平台锚点按 0 跳算:它们不经过症状拓扑,没有"远近"可言。
_HOPS_BY_DIRECTION = {"symptom": 0, "error_ref": 0, "scan_anchor": 0,
                      "edge_blame": 0, "change": 0,
                      "upstream": 1, "downstream": 1,
                      "upstream2": 2, "downstream2": 2}


_SCAN_TIER_TO_LOCALIZE = {0: 0, 1: 1, 2: 6, 3: 1, 4: 2, 5: 4}
"""粗筛层级(detect.TIER_*)→ 定位层级(severity)的换算。两套表的语义对照写在上面的
调用点注释里;缺的键(未来新增的层级)退回 severity 自己算的结果,不猜。"""


def severity(d: ServiceDeviation, ref_count: int,
             error_relay: bool = False) -> tuple[int, float]:
    """(严重层级, 层内偏离度)。层级即因果优先级,数字越小越严重:
    0 被失败调用点名的挂死/影子——调用持续发生、目标无响应,结构置顶,
      不参与任何分数比较(旧实现伪造一个 HANG_BLAMED_SCORE=82.5 去赢比较,
      1.1 的唯一存在理由是"刚好压过上限×1.5",规则靠两个拍值勉强成立);
    1 报错——错误是红信号,压过一切非错误形态;
    2 时延且是慢的源头(self time)——下游解释不了的耗时只能是它自己的活;
    3 时延纯中继——它的慢是被下游带的,排源头之后;
    4 资源偏离(CPU/内存)——疑点,不是红信号;
    5 其他异常(不在上述形态内);
    6 沉默挂死/影子——只是静默、无人指认:流量塌陷本身说明不了问题
      (整条链被上游堵住时下游一样没量),垫底观察,不因沉默给分
      (旧 HANG_SILENT_SCORE 的语义,直接表达成层级)。
    层内偏离度:各维保留自己的天然刻度,只在同层级内比大小。"""
    dims = d.abnormal_dims
    if "shadow" in dims or "hang" in dims:
        # hang 维只有 runner 挂死检测在"静默实锤"(停声后仍有失败调用指向它)时才打,
        # 它本身就是"被点名"——err_ref 的 refs 只是影子路(零 span)的同一信号,两条路
        # 任一成立即置顶。真实教训:currency 停机早期旧 span 未淘汰,不走影子指认、
        # refs 为空,按"沉默"垫底被 Top-K 截掉——runner 的实锤 hang 不能被这条路吞掉。
        return (0, 0.0) if (ref_count or "hang" in dims) else (6, 0.0)
    if "error" in dims:
        # 错误中继:它的错误 span **全部**点名了下游,说明它只是把下游的失败原样传上去
        # (调用方在它身上报错 = 它在它下游身上报错)。这是时延维"纯中继"在错误维上的
        # 同一条道理,不另立阈值:全部 = 事实,不是"比例很高"。
        # 实测(2026-10-17):payment 挂掉时 checkout 错误率 10.8%、payment 26.4%,
        # 排序按"离症状近"把 checkout 排前,LLM 也停在 checkout 上——真凶是下游的 payment。
        return (3 if error_relay else 1), d.error_dev
    if "latency" in dims:
        return (2, d.latency_dev) if d.self_latency_abnormal else (3, d.latency_dev)
    if "cpu" in dims or "mem" in dims:
        return 4, max(d.cpu_ratio or 0.0, d.mem_ratio or 0.0)
    return 5, 0.0


def _op_head(operation: str) -> str:
    """grpc 风格操作名首段:flagd.evaluation.v2.Service/EventStream → flagd。"""
    return (operation or "").split("/")[0].split(".")[0]


def _op_points_to(operation: str, service: str) -> bool:
    """操作名是否点名某服务。段级匹配(按 . / _ - 切段):段==服务名,或段==服务名+service
    后缀(otel-demo 的全名是 oteldemo.CurrencyService/Convert——首段是包名 oteldemo,
    首段规则咬不住,必须按段对服务名模糊匹配;真实教训:currency 挂死整晚指认不到)。

    比对前两边都归一化成纯字母数字:服务名里的连字符在操作名里往往被吃掉
    (product-catalog ↔ ProductCatalogService),不归一化的话带连字符的服务名永远匹配不上
    ——真实事故:product-catalog / frontend-proxy / load-generator / image-provider /
    frontend-web 这几个服务在"错误指认 + 挂死影子"那条路上整类失明,挂了也没人指认得出来。"""
    op = (operation or "").lower()
    svc = (service or "").lower()
    if not svc or not op:
        return False
    import re
    svc_key = re.sub(r"[^a-z0-9]", "", svc)
    if not svc_key:
        return False
    segs = [re.sub(r"[^a-z0-9]", "", s) for s in re.split(r"[^a-z0-9]+", op)]
    return any(seg == svc_key or seg == svc_key + "service" for seg in segs)


@dataclass
class Candidate:
    service: str
    dev: ServiceDeviation
    direction: str   # symptom | upstream/downstream(±1 跳) | upstream2/downstream2(±2 跳) | error_ref | scan_anchor
    tier: int        # 严重层级(见 severity):排序第一键
    dev_metric: float  # 层内偏离度:只在同层级内比
    earlier: bool    # 异常早于症状服务(窗边界纪律内可信)
    hops: int        # 0=症状本身/一跳/指认,1=两跳远——排序第三键
    score: float     # 纯展示:排序键的单调投影,前端"得分"只看相对次序,不参与任何判定
    evidence: list[str] = field(default_factory=list)   # [E#] 编号证据,归因引用完整性校验的锚
    ref_spans: list[Span] = field(default_factory=list) # 指认本服务的错误 span(error_ref 用)
    error_relay: bool = False   # 错误全部由下游引起 → 它是中继,不是源头(见 severity)
    scan_pos: int | None = None  # 粗筛锚点里的排位(0=粗筛第一名);非锚点为 None
    tier_demoted: bool = False   # 症状锚点的兑换层级被时间因果裁决撤销(见 localize 末段)
    causal_lead: bool = False    # 边级承载实锤或开关变更实锤(见 localize):同层内排前
    edge_blames: list = field(default_factory=list)     # 指认本服务的边级错误(edgeview)
    flag_changes: list = field(default_factory=list)    # 落在本服务身上的开关变更(flagchange)


def rank_key(c: "Candidate") -> tuple:
    """候选排序(字典序,替代旧的连续乘法打分)。键序即因果语义:
    1. severity 层级——被点名的挂死 > 报错 > 慢的源头 > 中继 > 资源 > 其他 > 沉默挂死;
    1.5 因果实锤(2026-10-24 新增)——同层内,带**边级承载实锤**(调用方的客户端错误
       集中指向它,失败就发生在它身上)或**开关变更实锤**(开关新取值落在它身上、
       非流量源且不晚于发病)的排前。为什么插在粗筛排位之前:粗筛的排位本身按
       "谁异常最响"排,那正是症状偏置——实测 recommendationCacheFailure 里
       frontend-web 靠锚点位排第一压住真因 recommendation 两次;硬事实(错误集中在
       指向它的边上 / 变更落在它身上)比"响度排序"更靠近因果。
    2. 粗筛排位——同层内先信粗筛的排序(0=粗筛第一名,None=不是锚点排最后)。
       为什么:粗筛已经把全平台候选按"层级+层内幅度"排过一遍,并据此选出锚点;定位层
       如果用自己的键重新排,同层的锚点会和噪声候选并列,胜负落到"谁发病早"上——
       实测 2026-10-18 adHighCpu:锚点 ad(CPU 是历史最大值的 1.02 倍、中位的 105 倍)
       与 frontend-web、load-generator 同为 tier 2,连续两轮被这两个噪声候选顶掉。
    3. 时间先后——发病早于症状服务的排前(果不先于因);
    4. 跳数——近的排前;
    5. 层内偏离度——同层级同远近同时序,谁的偏离更狠谁排前;
    6. 服务名——定序保可复现。"""
    return (c.tier, not c.causal_lead,
            c.scan_pos is None, c.scan_pos if c.scan_pos is not None else 0,
            -c.earlier, c.hops, -c.dev_metric, c.service)


def display_score(c: "Candidate") -> float:
    """排序键的单调数值投影,仅供前端展示相对次序——排序、门禁、证伪都不消费它。
    层级主导(每层 100),层内偏离封顶 100,保证与 rank_key 严格同序。"""
    return round((6 - c.tier) * 100 + min(c.dev_metric, 100.0), 3)


def _evidence_pack(cand: Candidate, graph: ServiceGraph, symptom: str,
                   spans: list[Span], eid_start: int,
                   window_start_us: int | None = None) -> list[str]:
    """紧凑证据投影:每条带全局 [E#] 编号,原始 span/序列不进 LLM(SOP §4)。
    window_start_us 用于窗边界纪律:异常起点贴窗起点时明示"可能更早"。"""
    d = cand.dev
    eid = eid_start
    items: list[str] = []

    def add(text: str) -> str:
        nonlocal eid
        items.append(f"[E{eid}] {text}")
        eid += 1
        return f"E{eid - 1}"

    if d.calls == 0:
        add(f"{d.service} 在采样窗内没有任何自身 span(服务可能整体失联),"
            f"以下证据全部来自调用方的失败 client span")
    else:
        add(f"{d.service} 在采样窗口内被调用 {d.calls} 次,错误率 {d.error_rate:.1%},"
            f"p95 时延 {d.latency_p95_us / 1000:.0f}ms")
    if "error" in d.abnormal_dims:
        add(f"{d.service} 存在错误 span(错误率 {d.error_rate:.1%},偏离度 {d.error_dev:.1f}"
            f"——对数刻度:0=与全服务错误率中位数持平,越大越高、不封顶)")
        if cand.error_relay:
            add(f"{d.service} 的错误**全部**由它的下游引起(错误中继):它只是把下游的"
                f"失败原样传给自己的调用方,自身没有产生错误——根因要在它的下游找")
    if "latency" in d.abnormal_dims:
        add(f"{d.service} p95 时延为{d.dev_basis}的 {d.latency_dev:.1f} 倍"
            f"(绝对 p95 {d.latency_p95_us / 1000:.0f}ms)"
            + (";时延被下游完全解释,它是纯中继(时延不计入它的异常度)"
               if not d.self_latency_abnormal else ";下游解释不了这部分耗时(它是慢的源头)"))
    if d.cpu_ratio is not None:
        add(f"{d.service} CPU 当前窗均值为基线的 {d.cpu_ratio:.2f} 倍"
            + ("(异常)" if "cpu" in d.abnormal_dims else "")
            + ("(T-2h 回退基线,可能含故障期,比值仅供参考)" if d.res_baseline == "t2h" else ""))
    if d.mem_ratio is not None:
        add(f"{d.service} 内存当前窗均值为基线的 {d.mem_ratio:.2f} 倍"
            + ("(异常)" if "mem" in d.abnormal_dims else "")
            + ("(T-2h 回退基线,可能含故障期,比值仅供参考)" if d.res_baseline == "t2h" else ""))
    if cand.ref_spans:
        if cand.direction == "error_ref":
            add(f"{d.service} 不在症状服务 {symptom} 的候选范围内,"
                f"但被错误 span 的操作名直接指认,强制入候选")
        else:
            add(f"{d.service} 虽在拓扑范围内,但自身零遥测,由调用方失败 client span 指认")
        for sp in cand.ref_spans[:3]:
            add(f"{sp.service} 的错误 span 操作名 {sp.operation} 指向 {d.service},"
                f"耗时 {sp.duration_us / 1000:.0f}ms,trace {sp.trace_id[:12]}")
    elif cand.direction == "symptom":
        add(f"{d.service} 是症状指向的服务本身")
    elif cand.direction == "scan_anchor":
        add(f"{d.service} 是平台粗筛独立发现的异常锚点(与症状并列的异常,不经过症状服务的调用拓扑)"
            f"——平台可能同时存在多单故障,或症状服务只是被它波及")
    elif cand.direction in ("upstream", "downstream", "upstream2", "downstream2"):
        hops = "两跳" if cand.direction.endswith("2") else "一跳"
        side = "上游" if cand.direction.startswith("upstream") else "下游"
        add(f"{d.service} 位于症状服务 {symptom} 的{side}{hops}"
            f"(调用 {'←' if side == '上游' else '→'} 方向)")
    if d.metric_onset_us is not None:
        add(f"{d.service} 指标侧发病时刻 {d.metric_onset_us}(分钟粒度)——比 trace 的 span 时刻"
            f"更可靠:trace 里父 span 必然早于子 span,级联时上游会假性显得更早")
    if cand.tier_demoted:
        add(f"注意:{d.service} 原为粗筛症状锚点,但它自身没有错误/自身时延/资源/被点名"
            f"任何一项实锤,且已有下游候选自身实锤异常并发病更早——粗筛的层级兑换加成"
            f"已按因果裁决撤销,它更可能是故障的传播表现而非根因;它没有被排除,"
            f"若归因发现它自身维度的独立证据,仍可选它")
    for eb in cand.edge_blames:
        _ops = "+".join(f"{_op}×{_n:.0f}"
                        for _op, _n in sorted(eb.operations.items(),
                                              key=lambda x: -x[1])[:3])
        add(f"调用方 {eb.caller} 的客户端失败调用集中在指向 {d.service} 的边上"
            f"(指标侧 spanmetrics,窗内 {eb.count:.0f} 笔:{_ops})"
            + ("——失败承载在它身上,已按承载实锤与报错同级" if cand.causal_lead
               else "——失败集中指向它,但按现状只作证据"))
    for fc in cand.flag_changes:
        import time as _time
        _t = _time.strftime("%H:%M:%S", _time.gmtime((fc.onset_us or 0) / 1e6))
        add(f"{d.service} 的功能开关 {fc.flag} 于 {_t}(UTC) 出现新取值 \"{fc.variant}\""
            + (f"(此前取值 {'/'.join(fc.prev_variants)})"
               if fc.prev_variants else "(基线窗内未见该开关有取值)")
            + "——变更事件来自 flag 求值遥测,变更落点服务"
            + ("不满足兑换条件(流量源或晚于发病),仅作证据" if not cand.causal_lead
               else ";变更是因果先验最强的信号,已按变更实锤与报错同级"))
    if d.self_latency_abnormal:
        add(f"{d.service} 的时延超出其下游能解释的范围(self time)——它自己是慢的源头,"
            f"不是被下游带慢的")
    # 时间锚点给"当前场次"的起点,不是窗内最早记录——采样窗(10m→最多24h)里可能装着
    # 上一场故障的旧异常,取最早会把上一场首发时刻当成本次发病,时间信号整个错位
    # (真实事故:checkout 的最早错误属于上一组 kafka 故障,真根因 payment 被比出
    # "晚 1440s"的假颠倒,遭时间反证误杀)。
    onset_ts = [t for t in (d.error_onset_us, d.latency_onset_us) if t is not None]
    if onset_ts:
        onset = min(onset_ts)
        which = "错误 span" if onset == d.error_onset_us else "时延异常"
        add(f"{d.service} 本次异常({which})起点 {onset}——最近一段连续异常的开始,"
            f"时间先后信号以此为准")
        older = [t for t in (d.earliest_error_us, d.earliest_latency_us)
                 if t is not None and t < onset]
        if older:
            add(f"{d.service} 采样窗内还有更早的异常记录({min(older)}),疑似上一场故障的残留,"
                f"不计入本次发病时刻")
    if not d.onset_trusted(window_start_us) and d.first_anomaly_us is not None:
        add(f"注意:{d.service} 的异常起点紧贴采样窗起点,实际开始时间可能更早,"
            f"时间先后信号不可用")
    err_spans = sorted((sp for sp in spans if sp.service == d.service and sp.error),
                       key=lambda sp: sp.start_us)[:5]
    for sp in err_spans:
        add(f"{d.service} 错误 span 样本:操作 {sp.operation},耗时 {sp.duration_us / 1000:.0f}ms,"
            f"trace {sp.trace_id[:12]}")
    # 错误 span 携带的错误信息(error.type/grpc 状态码/异常消息,OTel 标准埋点自带):
    # 平台没接 Loki 时日志维是盲的,这条通道不依赖任何额外采集——日志缺失不等于无故障
    from app.diagnosis import evidence as _evidence, logsig as _logsig
    _err_text = _logsig.classify(_evidence.error_lines(spans, d.service))
    _err_sig = _logsig.summary(_err_text)
    if _err_sig:
        add(f"{d.service} 错误 span 携带的错误信息归类:{_err_sig}")
    else:
        add(f"{d.service} 的错误 span 未携带可定性的错误信息(错误码/异常消息为空),"
            "错误机制需要日志或人工确认")
    if graph.edges:
        rel = [f"{a}→{b}({n} 次)" for (a, b), n in graph.edges.items()
               if d.service in (a, b)]
        if rel:
            add(f"{d.service} 的拓扑连接:" + "、".join(sorted(rel)[:4]))
    return items


def localize(graph: ServiceGraph, devs: dict[str, ServiceDeviation], symptom: str,
             spans: list[Span], window_start_us: int | None = None,
             span_profiles: dict | None = None,
             forced_extra: list | None = None,
             if_events: list[dict] | None = None,
             symptom_tier: int | None = None,
             edge_blames: list | None = None,
             flag_changes: list | None = None) -> dict:
    """从症状服务提 ±2 跳异常子图,按严重层级排序候选,给 Top-K 候选+证据包。
    window_start_us:采样窗起点(窗边界纪律,证据里明示"可能更早")。
    span_profiles:span 指标画像(键集=span 指标里出现过的服务),影子准入用——
    零 span 只有在"曾经有过遥测"时才是挂死形态;从不上报的服务(Redis 这类不埋点
    依赖)零 span 是常态(真实事故:valkey-cart 从无 span 指标,被按"整体失联"塞进
    证据包,归因员编出"下游挂死拖垮 cart"的因果故事,真根因是 cart 自己 CPU 耗尽)。
    None=未提供画像,保持旧行为(离线回放兼容)。
    if_events:调用边时延检测事件(edge_anomaly.detect 的输出),证据行注入候选包——
    异常承载者是被调方(callee),它的响应慢得不像自己的常态。只进证据包不改判排序。
    返回 {subgraph, candidates, evidence}(subgraph 给前端画图,evidence 喂 LLM)。"""
    # 候选范围:症状服务 ±2 跳。为什么是两跳——纯时延型的根因若离症状 ≥2 跳,错误指认那条
    # 路够不着(没有失败调用点名它),只取一跳会让它压根进不了候选集;而"真因没进候选集"
    # 比"排错序"更致命(检索失败无解,重排序失败还能靠重排救)。远跳排序靠后
    # (rank_key 的 hops 键),近处能解释时轮不到它们。
    up1, down1 = graph.neighbors(symptom)
    up2: set[str] = set()
    for _u in up1:
        _uu, _ud = graph.neighbors(_u)
        up2 |= _uu
    down2: set[str] = set()
    for _d in down1:
        _du, _dd = graph.neighbors(_d)
        down2 |= _dd
    _seen = {symptom} | up1 | down1
    up2 -= _seen
    down2 -= _seen | up2
    scope = ({symptom: "symptom"}
             | {u: "upstream" for u in up1}
             | {d_: "downstream" for d_ in down1}
             | {u: "upstream2" for u in up2}
             | {d_: "downstream2" for d_ in down2})
    # 粗筛多锚点并入:平台粗筛独立发现的异常是实锤,不该被"症状 ±2 跳"锁死。
    # 双重故障场景真实教训:锚点 cart(自身 CPU 耗尽)+第二锚点 product-catalog(网络
    # 时延 92 倍),后者是 checkout 的下游、不在 cart 拓扑范围内——不并入就整单失明,
    # 归因员面对的全是受害者候选,引用校验两轮打回降 inconclusive。
    # "真因没进候选集是检索失败,无解;排错序还能救"(本函数内注释)——这里补的就是检索。
    # 粗筛锚点强制入候选。可以带层级( (service, scan_tier) 元组):锚点是粗筛已经
    # 确认的平台级异常(被点名/塌陷/资源偏离),它们之外的场景不该被"自身无异常维"
    # 滤掉——ServiceDeviation 只带错误/时延/资源三维,粗筛的"被点名/塌陷"证据这里
    # 根本没有字段承载,按维度滤等于把粗筛的结论丢掉(实测 2026-10-17:payment 组
    # 粗筛锚点 checkout+payment 都进了锚点名单,localize 却把两个全滤掉,候选为空,
    # 结论 inconclusive)。层级的换算见 _SCAN_TIER_TO_LOCALIZE。
    extra_tiers: dict[str, int] = {}
    extra_pos: dict[str, int] = {}
    for _x in (forced_extra or []):
        _svc, _tier, _pos = (_x + (None, None))[:3] if isinstance(_x, tuple) else (_x, None, None)
        # **无条件**覆盖 direction 为 scan_anchor——锚点即使已在症状 ±2 跳的 scope 里
        # (如 payment 是 checkout 的一跳下游),也要拿免检金牌。为什么不能保留原方向
        # (实测 2026-10-22 演示实锤):paymentFailure 注入,粗筛锚点 payment(tier 1 错误
        # 指认),但它在 scope 里 direction=downstream,被下方"d.abnormal=False"的普通过滤
        # 剔除——真因在粗筛名单里却被定位层反悔,候选只剩受害者。锚点的证据(被点名/
        # 错误指认)在粗筛手里,本函数的维度字段承载不了,没有资格反悔(与 416 行注释
        # 同一条哲学:粗筛锚点不看偏离线)。
        scope[_svc] = "scan_anchor"
        if _tier is not None:
            extra_tiers[_svc] = int(_tier)
        if _pos is not None:
            extra_pos[_svc] = int(_pos)

    # 错误指认准入:症状服务的错误 span 操作名若点名另一服务,强制入候选。三条路:
    # ①grpc 全名首段==服务名(flagd.evaluation.../EventStream → flagd);
    # ②段==已知服务名或段==服务名+service 后缀(已知集合=图节点∪devs);
    # ③段以 service 结尾→剥后缀造影子服务名(currencyservice → currency)——挂死服务
    #   自身零 span、grpc client span 又不带 peer 属性,操作名是它存在的唯一痕迹
    # (真实验证:docker stop currency 后 checkout 错误 span op=oteldemo.CurrencyService/Convert)。
    known = set(graph.nodes) | set(devs.keys())
    err_ref: dict[str, list[Span]] = {}
    for sp in spans:
        if not sp.error:
            continue
        # 扫所有拉回来的错误 span,不限于 scope 内服务——影子场景(目标自身零遥测)
        # 的 scope 里只有目标自己,调用方的失败 span 全在 scope 外,不扫就永远指认不到
        # (真实案例:payment 停机到记忆环淘汰,checkout 的 Charge 失败全被跳过)。
        # 误伤防护靠 _eligible:有正常遥测的服务一律不走指认。
        self_svc = sp.service.lower()
        head = _op_head(sp.operation)
        # 指认目标:不在 scope,或虽在 scope 但零样本(影子/挂死)。有正常遥测的服务
        # 不走 peer/段级指认(peer 属性语义不可靠,曾把 frontend-proxy 误伤成 60 分候选)
        def _eligible(name: str) -> bool:
            return (name != self_svc and name not in scope
                    or (name in scope and devs.get(name) is None))
        if head in known and _eligible(head):
            err_ref.setdefault(head, []).append(sp)
        if sp.peer and _eligible(sp.peer):
            err_ref.setdefault(sp.peer, []).append(sp)
        import re
        _canon_known = {re.sub(r"[^a-z0-9]", "", k.lower()): k for k in known}
        for seg in re.split(r"[^a-z0-9]+", sp.operation.lower()):
            if seg.endswith("service") and len(seg) > 8:
                cand = seg[:-7]
                if len(cand) < 4:
                    continue
                # 归一化对齐:剥后缀名与已知服务名去掉连字符后相同 → 就是同一个服务,
                # 用真名,不造影子(实测 2026-10-17:oteldemo.ProductCatalogService/GetProduct
                # 剥出 "productcatalog",而真服务叫 "product-catalog"——同名不同字,两个都进
                # 候选,影子还因"零遥测"拿了 tier 0 压过真名)。
                cand = _canon_known.get(cand, cand)
                if _eligible(cand):
                    err_ref.setdefault(cand, []).append(sp)
    # 下面几处遍历都按服务名定序:scope/err_ref 是从 set 派生的,遍历顺序受哈希随机化
    # 影响——不定序的话子图节点、影子边、候选的初始顺序每次进程重启都可能变。
    for svc, refs in sorted(err_ref.items()):
        scope[svc] = "error_ref"

    # 边级错误承载准入(edgeview,指标侧):调用方的客户端错误集中在"它调 X"这条边,
    # X 就是失败发生的地方,强制入候选。与粗筛锚点同一条哲学:**无条件覆盖 direction**——
    # 承载证据在定位层的维度字段里没有承载,若保留它原有的拓扑方向(downstream 这类),
    # 下方"自身无异常维"的普通过滤会把真因滤掉(实测 recommendationCacheFailure 里
    # recommendation 自身错误率不越线,被滤掉后整单停在受害者身上)。
    for _eb in (edge_blames or []):
        scope[_eb.callee] = "edge_blame"

    # 功能开关变更落点准入(flagchange):开关新取值落在哪个服务身上,哪个服务就是
    # 变更落点——"故障前有变更"是生产环境最强的因果先验(Google SRE/各家 APM 共识)。
    # 同样无条件覆盖 direction,理由同上。
    for _fc in (flag_changes or []):
        scope[_fc.service] = "change"

    # 指标侧异常全量入候选:偏离表(devs)里已经判异常、但不在症状 ±2 跳和锚点名单里的
    # 服务,同样强制入候选——召回优先,排序纠错。这些服务与"scan_anchor"同语义:
    # 平台级独立发现的异常,不经过症状服务的调用拓扑(实测:recommendationCacheFailure
    # 的真因 recommendation 就在 devs 里异常,只因不在 ±2 跳内被锁在门外)。
    for svc, d in sorted(devs.items()):
        if d.abnormal and svc not in scope:
            scope[svc] = "scan_anchor"

    # 错误中继:一个服务的错误 span 若**全部**点名了别的已知服务(它的下游),那它只是
    # 把下游的失败原样传给自己的调用方——它自己没出错。判据与时延维"纯中继"同源
    # (self time ≈ 0 → 时延不算它的可疑度),这里换成"错误全部可被下游解释"。
    # 为什么需要它:级联里调用方先报错、离症状更近,排序键(hops 在偏离度之前)会把
    # 它顶到前面,LLM 又不敢越权改判,于是整单停在受害者身上。
    own_err: dict[str, int] = {}
    down_err: dict[str, int] = {}
    for sp in spans:
        if not sp.error:
            continue
        own_err[sp.service] = own_err.get(sp.service, 0) + 1
        if any(k != sp.service and _op_points_to(sp.operation, k) for k in known):
            down_err[sp.service] = down_err.get(sp.service, 0) + 1
    error_relay = {s for s, n in own_err.items() if n > 0 and down_err.get(s, 0) >= n}

    # 纯中继指认:调用方的错误**全部**指向 X,那失败就发生在"它调用 X"这一步,X 是根因。
    #
    # 为什么现有 err_ref 接不住:那条路只认"零遥测"的服务(误伤防护:有正常遥测的不指认)。
    # 但缓存失效、依赖超时这类故障,坏在 X、响在调用方,**X 自己的错误率是 0**——
    # 它有遥测,于是不被指认;它没异常维,于是进不了候选;整单就停在受害者身上。
    # 实测 2026-10-24 跑批 recommendationCacheFailure:recommendation 0.017 次/秒、
    # frontend-web 1.05 次/秒,结论给了 frontend-web(症状),真因 recommendation 不在候选里。
    #
    # 条件用"全部"而不是比例:和上面 error_relay、下面"纯中继"同一条口径——
    # 全部 = 事实,不是"比例很高",不引入新阈值。
    relay_blame: dict[str, int] = {}
    for _s in sorted(error_relay):
        for _sp in spans:
            if not _sp.error or _sp.service != _s:
                continue
            for _k in known:
                if _k != _s and _op_points_to(_sp.operation, _k):
                    relay_blame[_k] = relay_blame.get(_k, 0) + 1
                    if _k not in scope:
                        scope[_k] = "error_ref"
                    err_ref.setdefault(_k, []).append(_sp)

    nodes = []
    for svc, direction in sorted(scope.items()):
        d = devs.get(svc)
        if d is None:
            if direction != "error_ref":
                continue
            d = ServiceDeviation(service=svc)   # 影子服务:零样本,也要进子图给前端画
        nodes.append({"service": svc, "direction": direction,
                      "abnormal": d.abnormal, "dims": d.dims_view(),
                      "calls": d.calls})
    edges = [{"from": a, "to": b, "calls": n} for (a, b), n in graph.edges.items()
             if a in scope and b in scope]
    # 影子边:错误指认的候选与指认它的调用方之间,补一条前端可见的连线(零 span 服务
    # 在 parent-child 图里天然没有边,不补就是又一根孤立框)
    shadow_edges: dict[tuple[str, str], int] = {}
    for svc, refs in sorted(err_ref.items()):
        for sp in refs:
            shadow_edges[(sp.service, svc)] = shadow_edges.get((sp.service, svc), 0) + 1
    edges += [{"from": a, "to": b, "calls": n, "shadow": True}
              for (a, b), n in shadow_edges.items() if a in scope]

    sym_dev = devs.get(symptom)
    # 症状发病时刻(变更兑换的"果不先于因"闸门用):指标侧发病时刻优先,退回 trace 锚点
    _sym_onset_us = None
    if sym_dev is not None:
        _sym_onset_us = sym_dev.metric_onset_us or sym_dev.first_anomaly_us

    def _has_inbound(svc: str) -> bool:
        """图上有服务调它(被调侧)。纯流量源(load-generator 这类只出不进)不算。"""
        return any(b == svc for (_, b) in graph.edges)

    def _is_pure_caller(svc: str) -> bool:
        """只出不进:发过请求,但从没被调用。压测机、浏览器探针都属于这类。

        它们不可能是根因——自己报错只能是别人坏了的传导。而它们的失败往往归不到边上
        (2026-10-26 实测:load-generator 的客户端 span 名字就是 "POST",不带被调方,
        24 笔失败在服务级聚合里成了第一名,把 cartFailure 答成了 load-generator)。

        只在"确实见过它发请求"时才算纯调用方:图不完整时不误伤。
        """
        has_out = any(a == svc for (a, _) in graph.edges)
        return has_out and not _has_inbound(svc)

    def _corroborated(svc: str, d: ServiceDeviation) -> bool:
        """指认佐证闸(2026-10-25 夜跑教训:四道题+对照组全被怪罪到 flagd——高入度
        共享中间件是"怪罪磁铁",OpenRCA 2.0/图增强 RCA 的共识:单调用方的指认
        不足以给共享依赖定罪)。三居其一才算佐证齐:
        ① 自身任一维越线;② 完全静默(零调用,挂死形态);③ 至少两个独立
        调用方同时指认(法定人数)。佐证不足的指认只进证据包,不兑换层级。"""
        if d.abnormal or d.calls == 0:
            return True
        callers = {eb.caller for eb in (edge_blames or []) if eb.callee == svc}
        return len(callers) >= 2

    candidates: list[Candidate] = []
    for svc, direction in sorted(scope.items()):
        d = devs.get(svc)
        if d is None:
            # 影子准入:零 span 只有在"曾经有过遥测"(span 指标画像里有它,或它被失败
            # 调用点名)时才算服务失联形态;从不上报的服务零 span 是常态,不造异常维、
            # 不进候选——它真挂了的话,调用方的失败调用会把它(error_ref)点名进来。
            was_seen = span_profiles is None or svc in span_profiles \
                or svc in err_ref
            d = ServiceDeviation(service=svc)
            if was_seen:
                d.abnormal_dims.add("shadow")
        # 纯调用方(只出不进)不可能是根因:它报错只能是别人坏了的传导。
        # 这条比"锚点免检"更优先——锚点也可能是压测机(2026-10-26 cartFailure 实测)。
        if svc != symptom and _is_pure_caller(svc):
            continue
        if not d.abnormal and direction not in ("error_ref", "scan_anchor",
                                                "edge_blame", "change") \
                and svc != symptom:
            # 错误指认的候选不看偏离线;粗筛锚点同理——它进这份名单靠的是粗筛自己的
            # 证据(被点名/塌陷/资源),本函数的维度字段承载不了那些证据,不能拿来反悔。
            # **症状服务永不剔除**:它是本次诊断的起点,而且有一类故障它自己的指标
            # 根本不越线——cartFailure 的 server span 状态恒为 UNSET,错误只出现在
            # 调用方的 client span 上,于是 cart 自身错误率恒为 0(2026-10-18 实测:
            # 症状被自己的过滤器丢掉,候选里全是受害者,最后答了 load-generator)。
            continue
        # 时间先后信号(窗边界纪律:任一侧锚点贴采样窗起点即停用,deviation.time_anchor_ok)
        earlier = bool(sym_dev and sym_dev.first_anomaly_us and d.first_anomaly_us
                       and d.first_anomaly_us < sym_dev.first_anomaly_us
                       and d.onset_trusted(window_start_us)
                       and sym_dev.onset_trusted(window_start_us))
        refs = err_ref.get(svc, [])
        _relay = svc in error_relay
        tier, dev_metric = severity(d, len(refs), error_relay=_relay)
        if svc in relay_blame and not d.abnormal:
            # 被纯中继点名的下游:自己没有异常维(错误率没动),但"调用方全在它身上失败"
            # 本身就是证据。按报错级(1)进候选——调用方是纯中继会被降到 3,
            # 于是它自然排到调用方前面,不会被"离症状更近"的规则压回去。
            tier, dev_metric = 1, 0.0
        _eb_hit = [_eb for _eb in (edge_blames or []) if _eb.callee == svc]
        _fc_hit = [_fc for _fc in (flag_changes or []) if _fc.service == svc]
        _causal_lead = False
        if _eb_hit:
            # 承载实锤:调用方的失败调用集中在指向它的边上——错误就发生在它身上,
            # 报错同级(自身自然层级更强的保持不变)。**先过佐证闸**:被指认方
            # 自身健康、又只有一个调用方在指认时,不定罪(只留证据行)。
            if _corroborated(svc, d):
                tier = min(tier, 1)
                _causal_lead = True
        if _fc_hit:
            # 变更实锤的兑换条件(不满足的只给证据不给层级):
            # ① 落点在服务拓扑的被调侧(有人调它)。纯流量源翻开关改变的是施加给
            #    系统的负载,故障落点在被调侧,不当自动兑换(因果方向:变更改变自身
            #    行为 vs 变更改变输出负载是两类变更);
            # ② 变更不晚于症状发病——果不先于因,对变更同样成立。
            _sym_onset = _sym_onset_us if _sym_onset_us is not None else float("inf")
            _promotable = [fc for fc in _fc_hit
                           if _has_inbound(svc) and fc.onset_us <= _sym_onset]
            if _promotable:
                tier = min(tier, 1)
                _causal_lead = True
        if svc == symptom and symptom_tier is not None:
            # 症状服务的层级:取"定位层自己算的"和"粗筛给的"里更强的那个。
            # 粗筛 tier 1(被点名)是**指认类**证据,同样要过佐证闸——共享中间件
            # 被单个调用方的慢性流错误点名,不该只凭指认登顶(2026-10-25 夜跑
            # 教训);其余 tier(重启/自身报错/塌陷/资源)是自身实锤,照常兑换。
            _st = _SCAN_TIER_TO_LOCALIZE.get(int(symptom_tier), tier)
            if int(symptom_tier) != 1 or _corroborated(svc, d):
                tier = min(tier, _st)
        if not d.abnormal and direction == "scan_anchor" and svc in extra_tiers:
            # 自身无异常维的粗筛锚点:层级用粗筛给的,按两套层级表的语义换算
            # (detect.TIER_* 与本函数 severity 的 docstring 一一对得上):
            #   0 状态变化→0 实锤 | 1 被点名→1 报错 | 3 自身报错→1 报错
            #   4 慢的源头→2 自身时延 | 5 资源→4 资源疑点
            #   2 塌陷→6 沉默观察(localize 语义:没量本身说明不了问题,垫底但不删)
            tier = _SCAN_TIER_TO_LOCALIZE.get(extra_tiers[svc], tier)
        candidates.append(Candidate(service=svc, dev=d, direction=direction,
                                    tier=tier, dev_metric=dev_metric, earlier=earlier,
                                    hops=_HOPS_BY_DIRECTION.get(direction, 0),
                                    score=0.0, ref_spans=refs, error_relay=_relay,
                                    causal_lead=_causal_lead, edge_blames=_eb_hit,
                                    flag_changes=_fc_hit,
                                    scan_pos=(0 if direction == "symptom"
                                              else extra_pos.get(svc))))
    # 时间因果裁决(排序前最后一步):症状服务的层级若是粗筛锚点兑换来的、自身没有
    # 任何实锤维,且存在下游候选满足「自身实锤异常 + 指标发病早于症状」,则撤销兑换
    # 加成,症状层级恢复自身证据水平。
    # 为什么需要它(实测 2026-10-21 recommendationCacheFailure):frontend 作为症状锚点
    # 自身零异常维(错误率 0%、CPU 1.08 倍),靠兑换拿到 tier 1;真因 recommendation
    # CPU+内存双实锤、指标发病早 880 秒,却困在 tier 4——层级是排序第一键,时间信号
    # 再硬也翻不了 3 级的层级差,归因 agent 顺着第一名判错(LagRCA/TopoEvo 对这个
    # 现象的命名是 symptom-amplification bias,共识修法是条件化裁决而非调权重)。
    # 降级资格只有三种自然层级:纯中继(3,语义本来就是「被下游带的」)、无实锤维(5)、
    # 沉默(6)。自身有错误/自身时延/资源实锤/被点名的(0/1/2/4)永不降——cart CPU
    # 耗尽这类「症状自己就是实锤」的场景在这里被挡住(2026-10-22 刚修好的 cart 组
    # 不能被这条裁决回退)。
    # 三条判据全是相对判据(时序先后/维类实锤/方向),不引入任何绝对数值阈值。
    if sym_dev is not None:
        _sym_cand = next((c for c in candidates if c.service == symptom), None)
        if _sym_cand is not None:
            _nat_tier, _ = severity(sym_dev, len(err_ref.get(symptom, [])),
                                    error_relay=symptom in error_relay)
            if _nat_tier in (3, 5, 6) and _sym_cand.tier < _nat_tier:
                _sym_onset = sym_dev.metric_onset_us or sym_dev.first_anomaly_us
                if _sym_onset is not None and sym_dev.onset_trusted(window_start_us):
                    for _c in candidates:
                        if (_c.service == symptom
                                or not _c.direction.startswith("downstream")):
                            continue
                        _c_onset = _c.dev.metric_onset_us or _c.dev.first_anomaly_us
                        if (_c.tier <= 4 and _c.tier != 3
                                and _c_onset is not None
                                and _c.dev.onset_trusted(window_start_us)
                                and _c_onset < _sym_onset):
                            _sym_cand.tier = _nat_tier
                            _sym_cand.tier_demoted = True
                            break
    # 字典序排序(rank_key):层级→时间先后→跳数→层内偏离→服务名。最后的服务名键保证
    # 可复现:候选是从 set 派生的,遍历顺序受哈希随机化影响,不定序的话同一故障、同一份
    # 数据,进程重启后同键候选会换位——Top-K 截断就换人,喂给 LLM 的顺序也变,结论可能
    # 跟着变。商用不能"同一故障跑出两个答案"。
    # 注意:排序现在只决定呈现顺序,不淘汰候选(见 MAX_CANDIDATES 注释)。
    candidates.sort(key=rank_key)
    for c in candidates:
        c.score = display_score(c)
    if len(candidates) > MAX_CANDIDATES:
        candidates = candidates[:MAX_CANDIDATES]

    evidence: list[str] = []
    packs = []
    for c in candidates:
        c.evidence = _evidence_pack(c, graph, symptom, spans, eid_start=len(evidence) + 1,
                                    window_start_us=window_start_us)
        evidence.extend(c.evidence)
        packs.append({"service": c.service, "score": c.score, "direction": c.direction,
                      "tier": c.tier, "earlier": c.earlier, "evidence": c.evidence,
                      "ref_count": len(c.ref_spans), "calls": c.dev.calls,
                      "tier_demoted": c.tier_demoted, "causal_lead": c.causal_lead})

    # 调用边时延检测事件注入(证据行,不改排序):服务级 p95 看不出"哪条边慢",
    # 边级异常是被调方时延异常的独立证据。编号续全局证据表,引用完整性校验
    # 按同一套 [E#] 算。超上限的事件按异常窗数截断(prompt 预算,非判据)。
    if if_events:
        from app.diagnosis import edge_anomaly as _edge
        _sym_onset = None
        _sym_trusted = True
        if sym_dev is not None:
            _onsets = [t for t in (sym_dev.error_onset_us, sym_dev.latency_onset_us)
                       if t is not None]
            _sym_onset = min(_onsets) if _onsets else None
            _sym_trusted = sym_dev.onset_trusted(window_start_us)
        for ev in _edge.cap_events(if_events, _edge.EVENT_CAP):
            line = f"[E{len(evidence) + 1}] {_edge.evidence_line(ev, _sym_onset, _sym_trusted)}"
            evidence.append(line)
            pack = next((p for p in packs if p["service"] == ev["callee"]), None)
            if pack:
                pack["evidence"].append(line)

    return {"subgraph": {"nodes": nodes, "edges": edges}, "candidates": packs, "evidence": evidence,
            "shadow_edges": shadow_edges}
