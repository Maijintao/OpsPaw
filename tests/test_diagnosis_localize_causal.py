"""定位层两条"因果实锤"通道的单测:边级承载(edgeview)与开关变更(flagchange)。

规则(localize):
- 边级承载:调用方失败调用集中指向的被调方,强制入候选、提级到报错层、同层排前;
- 开关变更:变更落点服务强制入候选;被调侧(图上有人调它)且不晚于发病的,
  提级到报错层、同层排前——纯流量源(只出不进)只给证据不兑换(它翻开关改变的是
  施加给系统的负载,故障落点在被调侧)。
"""
from app.diagnosis.collect import Span
from app.diagnosis.deviation import ServiceDeviation
from app.diagnosis.edgeview import EdgeBlame
from app.diagnosis.flagchange import FlagChange
from app.diagnosis.localize import localize
from app.diagnosis.topology import ServiceGraph


def _span(svc: str, op: str, *, error: bool = False, sid: str = "s") -> Span:
    return Span(trace_id="t", span_id=sid, parent_span_id=None, service=svc,
                operation=op, start_us=1_000_000, duration_us=1000, error=error)


def _dev(svc: str, *, error_rate: float = 0.0, abnormal: bool = False) -> ServiceDeviation:
    d = ServiceDeviation(service=svc)
    d.error_rate = error_rate
    d.error_dev = 3.0 if abnormal else 0.0
    if abnormal:
        d.abnormal_dims.add("error")
    return d


def _graph(*edges: tuple[str, str]) -> ServiceGraph:
    g = ServiceGraph()
    for a, b in edges:
        g.nodes.update({a, b})
        g.edges[(a, b)] += 1
    return g


_REC_BLAME = EdgeBlame(callee="recommendation", caller="frontend", count=27,
                       operations={"oteldemo.RecommendationService/ListRecommendations": 27})


def test_edge_blame_admits_and_promotes_over_anchor():
    """症状锚点 frontend-web 在报错层,但失败承载在 recommendation 边上:
    recommendation 提级到报错层并靠承载实锤排到锚点前面。"""
    g = _graph(("frontend-web", "frontend"), ("frontend", "recommendation"))
    devs = {"frontend-web": _dev("frontend-web", error_rate=0.09, abnormal=True),
            "frontend": _dev("frontend", error_rate=0.10, abnormal=True)}
    loc = localize(g, devs, "frontend-web", [], edge_blames=[_REC_BLAME], symptom_tier=1)
    order = [c["service"] for c in loc["candidates"]]
    assert order[0] == "recommendation", f"承载方应排第一,实际:{order}"
    rec = loc["candidates"][0]
    assert rec["tier"] == 1 and rec["causal_lead"] is True
    assert any("承载" in e for e in rec["evidence"])


def test_edge_blame_admits_service_without_deviation():
    """被指认服务哪怕不在偏离表里(自身指标不越线),也要进候选——错误集中在
    指向它的边上,这个事实不需要它自己异常来背书。"""
    g = _graph(("frontend", "recommendation"))
    devs = {"frontend": _dev("frontend", error_rate=0.09, abnormal=True)}
    loc = localize(g, devs, "frontend", [], edge_blames=[_REC_BLAME])
    rec = next(c for c in loc["candidates"] if c["service"] == "recommendation")
    assert rec["tier"] == 1 and rec["direction"] == "edge_blame"


def test_flag_change_promotes_serving_service():
    """cart 的开关翻了新值(被调侧、无发病信息不拦)→ 提级报错层并排到报错层
    调用方前面。"""
    g = _graph(("frontend", "cart"))
    devs = {"frontend": _dev("frontend", error_rate=0.09, abnormal=True)}
    fc = FlagChange(service="cart", flag="cartFailure", variant="100%",
                    prev_variants=("off",), onset_us=1_000_000)
    loc = localize(g, devs, "frontend", [], flag_changes=[fc])
    order = [c["service"] for c in loc["candidates"]]
    assert order[0] == "cart", f"变更落点应排第一,实际:{order}"
    cart = loc["candidates"][0]
    assert cart["tier"] == 1 and cart["causal_lead"] is True
    assert any("cartFailure" in e for e in cart["evidence"])


def test_flag_change_on_traffic_source_is_evidence_only():
    """load-generator 只出不进(纯流量源):开关变更只作证据,不兑换层级——
    它翻开关改变的是施加给系统的负载,故障落点在被调侧,不当自动指认。"""
    g = _graph(("load-generator", "frontend"))
    devs = {"frontend": _dev("frontend", error_rate=0.09, abnormal=True)}
    fc = FlagChange(service="load-generator", flag="loadGeneratorFloodHomepage",
                    variant="on", prev_variants=("off",), onset_us=1_000_000)
    loc = localize(g, devs, "frontend", [], flag_changes=[fc])
    order = [c["service"] for c in loc["candidates"]]
    assert order[0] == "frontend", f"流量源的变更不该顶掉承载侧:{order}"
    lg = next(c for c in loc["candidates"] if c["service"] == "load-generator")
    assert lg["tier"] != 1 and lg["causal_lead"] is False
    assert any("loadGeneratorFloodHomepage" in e for e in lg["evidence"])


def test_flag_change_after_symptom_onset_is_evidence_only():
    """变更晚于症状发病(果不先于因,对变更同样成立)→ 只给证据不兑换。"""
    g = _graph(("frontend", "cart"))
    devs = {"frontend": _dev("frontend", error_rate=0.09, abnormal=True)}
    devs["frontend"].error_onset_us = 500_000
    fc = FlagChange(service="cart", flag="cartFailure", variant="100%",
                    prev_variants=("off",), onset_us=900_000)
    loc = localize(g, devs, "frontend", [], flag_changes=[fc])
    cart = next(c for c in loc["candidates"] if c["service"] == "cart")
    assert cart["tier"] != 1 and cart["causal_lead"] is False
    assert any("cartFailure" in e for e in cart["evidence"])


def test_edge_blame_without_corroboration_is_evidence_only():
    """佐证闸:被指认方自身健康、有量、且只有一个调用方指认(怪罪磁铁形态)
    → 不兑换层级,只留证据。"""
    g = _graph(("frontend", "flagd"))
    d = ServiceDeviation(service="flagd")
    d.calls = 60   # 有量、不静默
    devs = {"frontend": _dev("frontend", error_rate=0.09, abnormal=True),
            "flagd": d}
    eb = EdgeBlame(callee="flagd", caller="frontend", count=8,
                   operations={"flagd.evaluation.v2.Service/EventStream": 8})
    loc = localize(g, devs, "frontend", [], edge_blames=[eb], symptom_tier=1)
    flagd = next(c for c in loc["candidates"] if c["service"] == "flagd")
    assert flagd["causal_lead"] is False, "无佐证的指认不该登顶"
    assert flagd["tier"] != 1, "无佐证不兑换报错级"
    assert any("EventStream" in e for e in flagd["evidence"]), "证据行保留"


def test_quorum_of_two_callers_counts_as_corroboration():
    """两个独立调用方同时指认(法定人数)= 佐证齐,可兑换——真中间件挂了要能抓到。"""
    g = _graph(("frontend", "flagd"), ("checkout", "flagd"))
    d = ServiceDeviation(service="flagd")
    d.calls = 60
    devs = {"frontend": _dev("frontend", error_rate=0.09, abnormal=True),
            "checkout": _dev("checkout", error_rate=0.09, abnormal=True),
            "flagd": d}
    ebs = [EdgeBlame(callee="flagd", caller="frontend", count=8, operations={"a": 8}),
           EdgeBlame(callee="flagd", caller="checkout", count=6, operations={"b": 6})]
    loc = localize(g, devs, "frontend", [], edge_blames=ebs)
    flagd = next(c for c in loc["candidates"] if c["service"] == "flagd")
    assert flagd["causal_lead"] is True and flagd["tier"] == 1


def test_blamed_scan_tier_exchange_needs_corroboration():
    """粗筛"被点名"(tier 1)给症状的报错级兑换同样过佐证闸:健康的共享中间件
    被点名后不因锚点身份登顶;自身实锤类 tier(资源)不受影响。"""
    g = _graph(("frontend", "flagd"))
    d = ServiceDeviation(service="flagd")
    d.calls = 60
    devs = {"frontend": _dev("frontend", error_rate=0.09, abnormal=True),
            "flagd": d}
    loc = localize(g, devs, "flagd", [], symptom_tier=1)   # flagd 是被点名的锚点
    flagd = next(c for c in loc["candidates"] if c["service"] == "flagd")
    assert flagd["tier"] != 1, "无佐证的点名锚点不兑换报错级"
    loc2 = localize(g, devs, "flagd", [], symptom_tier=5)  # 资源锚点=自身实锤
    flagd2 = next(c for c in loc2["candidates"] if c["service"] == "flagd")
    assert flagd2["tier"] == 4, "资源类锚点照常兑换"
