"""指标侧边级错误归属(按调用边归因的数据半壁,2026-10-24 实证后新增)。

为什么做在指标侧而不是 trace 采样:spanmetrics(traces_span_metrics_calls_total)
按 (服务, span_kind, span_name, 状态) 连续计数,**客户端 span 的错误就是权威的
失败调用记录**——实测本靶机 recommendation/cart 的服务端 span 从不标错误
(状态恒 STATUS_CODE_UNSET),错误只落在调用方的客户端 span 上;且 trace 采样窗里
错误 span 常常一条都抽不到(2026-10-23 夜跑 recommendationCacheFailure:trace 侧
各服务错误率全为 0.0%,指标侧 frontend→ListRecommendations 0.096/s)。Istio 的
reporter=source/destination、TraceRCA 的调用级异常,同一件事。

归属规则(纯比较,零阈值):调用方的客户端错误按操作名段级匹配(localize.
_op_points_to)归属到"它调的某服务";**最大归属项就是某条边、且不小于该调用方
无法归属的错误量** → 这条边的被调方是失败承载方。旧口径(detect 的 REF_STRONG_*
比例门槛)把真因指认稀释在"全量调用量"分母里(cartFailure 的教训),这里只比
"错误内部谁最大",不引入分母。

只解决"错误集中在哪条边"的归属方向;"客户端报错、服务端报成功=调用方谎报"
这类双侧矛盾判定**刻意不做**——本靶机两种相反真相(埋点不标错 vs 真不可达)的
签名一模一样,做了必错,等遥测质量配得上再上。
"""
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("app")


@dataclass
class EdgeBlame:
    callee: str                        # 失败承载方(被调服务)
    caller: str                        # 报错的调用方
    count: int                         # 该调用方指向它的失败调用量(窗内增量)
    operations: dict[str, int] = field(default_factory=dict)   # 操作名 → 失败量


def _pair_totals(rows: list[dict] | None, known: set[str]) -> dict[tuple[str, str], float]:
    """(调用方, 被调方) → 错误总量。给基线窗复用同一套归属逻辑。"""
    from app.diagnosis.localize import _op_points_to
    out: dict[tuple[str, str], float] = {}
    for r in rows or []:
        m = r.get("metric") or {}
        caller = next((m[k] for k in ("service_name", "service", "service_id")
                       if m.get(k)), "")
        op = m.get("span_name") or ""
        n = float(r.get("value", [0, 0])[1] or 0)
        if not caller or n <= 0:
            continue
        target = next((k for k in sorted(known)
                       if k != caller and _op_points_to(op, k)), None)
        if target is not None:
            out[(caller, target)] = out.get((caller, target), 0.0) + n
    return out


def _attribute(rows: list[dict], known: set[str],
               base_rows: list[dict] | None = None,
               hist: dict | None = None,
               window_s: float = 300.0) -> list[EdgeBlame]:
    """把"客户端错误按 (服务, 操作) 聚合"的查询结果归属到调用边(纯函数,单测在这里)。

    rows: sum by (服务标签, span_name)(increase(...)) 的即时查询结果。
    规则见模块 docstring:每个调用方取归属项最大值;并列最大都算(事实并列);
    最大项必须不小于该调用方无法归属的错误量(噪声闸:自己一身的错远多于任何
    单边时,指认说不出口)。
    base_rows: **基线窗**(紧邻诊断窗之前的等长窗)的同型查询——慢性噪声闸
    (2026-10-25 夜跑教训:四道题+对照组全被怪罪到 flagd,根因是 ad→flagd 的
    长连接流在健康期就有断连错误,Istio/Envoy 社区的已知噪声类别):当前窗
    错误量不**严格大于**基线窗的边,不算新证据、不产生指认。真故障边基线
    为零,天然放行;纯比较,零阈值。
    hist: {(调用方, 操作名): stats.Distribution}——**七天自历史带**(与粗筛
    _error_refs 同一把尺子,2026-10-25 二轮验证实锤:cart→flagd、payment→flagd
    的偶发断连"这窗 1 笔、那窗 0 笔",单窗基线拦不住、两条同窗还凑成法定人数;
    粗筛的历史带每轮都把它们滤成 blamed=0.0——尺子现成,搬来用):操作对当前
    错误速率没超出自己历史异常带的,不产生指认。
    window_s: rows 里 increase() 的窗长(秒)。窗总量换算回速率要除以**真实窗长**,
    诊断窗是 600s,写死 300 会把当前速率虚增一倍、闸变松。
    """
    from app.diagnosis import stats as _stats
    from app.diagnosis.localize import _op_points_to
    per_caller: dict[str, dict[str, int]] = {}
    unattributed: dict[str, float] = {}
    for r in rows:
        m = r.get("metric") or {}
        caller = next((m[k] for k in ("service_name", "service", "service_id") if m.get(k)), "")
        op = m.get("span_name") or ""
        n = float(r.get("value", [0, 0])[1] or 0)
        if not caller or n <= 0:
            continue
        target = next((k for k in sorted(known)
                       if k != caller and _op_points_to(op, k)), None)
        # 七天历史带:慢性/偶发性背景错误超不过自己的历史,不是新证据。
        # rows 是 increase() 窗口总量,换算回速率再比(历史带是速率刻度)
        if hist:
            _thr = _stats.anomaly_threshold(hist.get((caller, op)))
            if _thr is not None and n / max(window_s, 1.0) <= _thr:
                continue
        if target is None:
            unattributed[caller] = unattributed.get(caller, 0.0) + n
        else:
            ops = per_caller.setdefault(caller, {}).setdefault(target, {})
            ops[op] = ops.get(op, 0) + n
    base = _pair_totals(base_rows, known)
    blames: list[EdgeBlame] = []
    for caller, targets in sorted(per_caller.items()):
        top = max(sum(ops.values()) for ops in targets.values())
        if top < unattributed.get(caller, 0.0):
            continue
        for callee, ops in sorted(targets.items()):
            if sum(ops.values()) == top:
                # 慢性噪声闸:与基线相当(未严格超过)的边错误不是新证据
                if sum(ops.values()) <= base.get((caller, callee), 0.0):
                    continue
                blames.append(EdgeBlame(callee=callee, caller=caller,
                                        count=int(sum(ops.values())), operations=dict(ops)))
    # 指认取传播链的**末端**:受害链会层层报错、层层指认(实测 recommendationCacheFailure:
    # frontend-proxy 指认 frontend,frontend 指认 recommendation)——链条中段(既被指认、
    # 又在指认别人)只是传播路径,自己不指认任何人的被指认方才是失败承载的源头。
    # 全员都在指认别人(成环)时退回原样,不猜。
    blamers = {b.caller for b in blames}
    endpoints = [b for b in blames if b.callee not in blamers]
    return endpoints or blames


async def client_edge_errors(prom: dict, known: set[str], window_start: float,
                             window_end: float) -> tuple[list[EdgeBlame], str | None]:
    """当前诊断窗内,各调用方的客户端错误集中在哪条调用边。返回 (指认列表, 说明或 None)。

    增强信号,任何失败都不抛:拿不到就返回空列表 + 一句如实说明。
    """
    from app.diagnosis import collect, detect as _detect
    collect._spend()
    try:
        disc = await _detect._discover(prom)
    except Exception as exc:  # noqa: BLE001   回放未命中/上游抖动都不抛,如实说明
        return [], f"指标族发现失败({type(exc).__name__})"
    if not disc:
        return [], "指标族发现失败(spanmetrics 不可用)"
    collect._spend()
    w = max(int(window_end - window_start), 1)
    kind = disc.get("kind_label") or "span_kind"
    status = disc.get("status_label") or "status_code"
    svc = disc["service_label"]
    # kind/status 的取值两套写法都认(OTLP 的 SPAN_KIND_CLIENT/STATUS_CODE_ERROR 与
    # 裸 client/5xx),正则一次覆盖,不多花发现查询
    expr = (f'sum by ({svc}, span_name) (increase({disc["family"]}'
            f'{{{kind}=~"SPAN_KIND_CLIENT|client",'
            f'{status}=~"STATUS_CODE_ERROR|error|5.."}}[{w}s]))')
    # 历史带的 range 查询表达式:与上面的窗内统计同族同标签口径,rate 窗对齐粗筛的
    # 300s 步长(detect._error_refs 同款)——历史带是速率刻度,两边必须同一把尺子
    err_expr = (f'sum by ({svc}, span_name) (rate({disc["family"]}'
                f'{{{kind}=~"SPAN_KIND_CLIENT|client",'
                f'{status}=~"STATUS_CODE_ERROR|error|5.."}}[{_detect.SCAN_WINDOW_S}s]))')
    try:
        body = await _detect._get_json(f"{prom['url']}/api/v1/query",
                                       params={"query": expr, "time": window_end},
                                       headers=prom.get("headers"))
    except Exception as exc:  # noqa: BLE001
        return [], f"边级错误查询失败({type(exc).__name__})"
    rows = ((body or {}).get("data") or {}).get("result") or []
    if not rows:
        return [], "窗内无客户端错误(或 spanmetrics 无错误样本)"
    # 慢性噪声闸的基线窗:紧贴诊断窗之前的等长窗,同式再查一次。
    # 查不到基线(超时/失败)时**不闸**——闸是收紧方向的增强,拿不到基线就
    # 退回无闸行为,不因增强信号的缺失堵死主判据
    base_rows: list[dict] = []
    collect._spend()
    try:
        bbody = await _detect._get_json(f"{prom['url']}/api/v1/query",
                                        params={"query": expr, "time": window_start},
                                        headers=prom.get("headers"))
        base_rows = ((bbody or {}).get("data") or {}).get("result") or []
    except Exception:  # noqa: BLE001
        base_rows = []
    # 七天自历史带(与粗筛 _error_refs 同一把尺子):一条 range 查询按 (调用方,操作) 建分布。
    # 取不到历史 → 不带这道闸(增强信号缺失不堵死主判据,前两道闸仍在)
    hist: dict = {}
    collect._spend()
    try:
        import time as _t
        _now2 = _t.time() if window_end is None else window_end
        hrows = await _detect._range(prom, err_expr,
                                     _now2 - _detect.HISTORY_HOURS * 3600, _now2,
                                     _detect.HISTORY_STEP_S)
        _acc: dict = {}
        for s in hrows:
            m = s.get("metric") or {}
            _acc.setdefault((m.get(disc["service_label"], "?"),
                             m.get("span_name", "")), []).extend(
                float(v) for _, v in (s.get("values") or [])
                if v not in ("NaN", "+Inf", "-Inf"))
        from app.diagnosis import stats as _stats2
        hist = {k: _stats2.Distribution.build(v) for k, v in _acc.items()}
    except Exception:  # noqa: BLE001
        hist = {}
    blames = _attribute(rows, known, base_rows, hist, w)
    if not blames:
        return [], "窗内客户端错误未集中指向任何服务(无主导调用边,或与基线相当属慢性噪声)"
    return blames, None
