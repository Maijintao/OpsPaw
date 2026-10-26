from app.diagnosis import confidence
from app.diagnosis.attr_contract import Attribution
from app.diagnosis.deviation import ServiceDeviation


def _dev(name, dims, **kw):
    d = ServiceDeviation(name)
    for dim in dims:
        d.abnormal_dims.add(dim)
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def _attr(verdict="root_cause", root="currency"):
    return Attribution(verdict=verdict, root_cause_service=root, chain=["[E1] x"])


def test_high_and_definitive_for_blamed_shadow_service():
    """挂死形态:零遥测 + 被错误 span 点名——直接证据,高档 + 肯定语气。"""
    devs = {"frontend": _dev("frontend", ["error"], earliest_error_us=2_000_000)}
    cands = [{"service": "currency", "direction": "error_ref"}]
    out = confidence.assess(attr=_attr(), service="frontend", devs=devs,
                            candidates=cands, edges={("frontend", "currency")},
                            window_start_us=None, data_quality={}, log_result={},
                            sparse=False)
    assert out["tier"] == "high"
    assert out["tone"] == "definitive"
    assert out["evidence"][0]["category"] == "shadow"
    assert out["evidence"][0]["kind"] == "direct"


def test_high_definitive_self_diagnosis_with_own_latency():
    """自查:只慢不报错,单一维度实锤——高档 + 肯定语气,不列"与自己相连"这种伪证据。"""
    devs = {"checkout": _dev("checkout", ["latency"], latency_p95_us=900_000)}
    cands = [{"service": "checkout", "direction": "symptom"}]
    out = confidence.assess(attr=_attr(root="checkout"), service="checkout", devs=devs,
                            candidates=cands, edges=set(), window_start_us=None,
                            data_quality={}, log_result={}, sparse=False)
    assert out["tier"] == "high"
    assert out["tone"] == "definitive"
    cats = [e["category"] for e in out["evidence"]]
    assert "latency" in cats
    assert "topology" not in cats


def test_hedged_when_causation_is_inferred():
    """甩锅但没被点名:候选自己时延炸了,因果靠时间先后+调用关系推——
    事实条目里有直接证据(时延实锤它坏了),但语气仍是对冲:
    直接证据说"它坏了",不说"是它害的"。"""
    devs = {"frontend": _dev("frontend", ["error"], earliest_error_us=2_000_000),
            "paymentservice": _dev("paymentservice", ["latency"],
                                   latency_p95_us=800_000, earliest_latency_us=500_000)}
    cands = [{"service": "paymentservice", "direction": "downstream"}]
    out = confidence.assess(attr=_attr(root="paymentservice"), service="frontend",
                            devs=devs, candidates=cands,
                            edges={("frontend", "paymentservice")},
                            window_start_us=None, data_quality={}, log_result={},
                            sparse=False)
    assert out["tone"] == "hedged"
    assert out["tier"] == "high"
    kinds = {e["category"]: e["kind"] for e in out["evidence"]}
    assert kinds["latency"] == "direct"
    assert kinds["time"] == "inference"


def test_medium_when_samples_sparse():
    devs = {"frontend": _dev("frontend", ["error"])}
    cands = [{"service": "frontend", "direction": "symptom"}]
    out = confidence.assess(attr=_attr(root="frontend"), service="frontend", devs=devs,
                            candidates=cands, edges=set(), window_start_us=None,
                            data_quality={}, log_result={}, sparse=True)
    assert out["tier"] == "medium"
    assert any("样本不足" in b for b in out["blind_spots"])


def test_medium_when_resource_relies_on_fallback_baseline():
    """结论靠资源偏离、基线却是回退窗——依赖的维度打折,降中档。"""
    devs = {"frontend": _dev("frontend", ["error"]),
            "paymentservice": _dev("paymentservice", ["cpu"], res_baseline="t2h")}
    cands = [{"service": "paymentservice", "direction": "downstream"}]
    out = confidence.assess(attr=_attr(root="paymentservice"), service="frontend",
                            devs=devs, candidates=cands,
                            edges={("frontend", "paymentservice")},
                            window_start_us=None, data_quality={}, log_result={},
                            sparse=False)
    assert out["tier"] == "medium"
    assert "回退基线" in out["summary"]


def test_low_for_inconclusive_with_blind_spots_listed():
    """没定位到根因 → 低档;采集缺口照列(平台没接的资源/日志采集是运维待办,
    不参与档位,但必须让人看见盲在哪)。"""
    out = confidence.assess(
        attr=_attr(verdict="inconclusive", root=None), service="frontend",
        devs={}, candidates=[], edges=set(), window_start_us=None,
        data_quality={"cpu": {"current": "no_data", "baseline": "no_data"}},
        log_result={"status": "no_loki_source"}, sparse=False)
    assert out["tier"] == "low"
    assert out["evidence"] == []
    assert any("CPU" in b for b in out["blind_spots"])
    assert any("日志" in b for b in out["blind_spots"])


def test_medium_for_critical_failure():
    """关键故障坐实但根因指向没过门槛 → 中档 + 对冲语气。"""
    devs = {"frontend": _dev("frontend", ["error"]), "cart": _dev("cart", ["error"])}
    cands = [{"service": "cart", "direction": "upstream"}]
    out = confidence.assess(attr=_attr(verdict="critical_failure", root="cart"),
                            service="frontend", devs=devs, candidates=cands,
                            edges={("cart", "frontend")}, window_start_us=None,
                            data_quality={}, log_result={}, sparse=False)
    assert out["tier"] == "medium"
    assert out["tone"] == "hedged"
    assert any(e["category"] == "error" for e in out["evidence"])
