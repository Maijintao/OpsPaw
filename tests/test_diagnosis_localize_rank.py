"""候选排序:同层内先信粗筛的排位。

## 为什么(2026-10-18 adHighCpu 实测)

粗筛把 ad 选成第一名锚点(CPU 是自身历史最大值的 1.02 倍、中位的 105 倍),但定位层
用自己的键重排:ad 与 frontend-web、load-generator 同层,胜负落到"谁发病早"上——
调用方在级联里天然先报错,于是 ad 连续两轮被这两个噪声候选顶掉。

粗筛已经按"层级 + 层内幅度"把全平台比过一遍才选出锚点,定位层不该在自己的键上把它
的结论推翻;除非有更强的因果证据(中继降级、指认),那些在层级键里已经表达过了。
"""
from app.diagnosis.deviation import ServiceDeviation
from app.diagnosis.localize import Candidate, rank_key


def _cand(svc: str, *, tier: int, scan_pos: int | None, earlier: bool = False,
          dev: float = 1.0) -> Candidate:
    return Candidate(service=svc, dev=ServiceDeviation(service=svc), direction="symptom",
                     tier=tier, dev_metric=dev, earlier=earlier, hops=0, score=0.0,
                     scan_pos=scan_pos)


def test_scan_anchor_outranks_noise_in_same_tier():
    """同层:粗筛第一名(scan_pos=0) 排在粗筛没提名(None)的候选前面。"""
    anchor = _cand("ad", tier=2, scan_pos=0)
    noise = _cand("frontend-web", tier=2, scan_pos=None, earlier=True, dev=0.6)
    assert sorted([noise, anchor], key=rank_key)[0].service == "ad"


def test_scan_rank_order_is_preserved():
    """同层:粗筛第二名排在第三名前面。"""
    second = _cand("frontend-web", tier=2, scan_pos=1)
    third = _cand("load-generator", tier=2, scan_pos=2, earlier=True)
    assert sorted([third, second], key=rank_key)[0].service == "frontend-web"


def test_stronger_tier_still_beats_scan_rank():
    """层级优先:报错层(1)的非锚点候选仍排在资源层(4)的粗筛第一名前面。"""
    error_cand = _cand("payment", tier=1, scan_pos=None)
    resource_anchor = _cand("ad", tier=4, scan_pos=0)
    assert sorted([resource_anchor, error_cand], key=rank_key)[0].service == "payment"


def test_symptom_is_never_dropped_even_without_own_anomaly():
    """症状服务必须留在候选里,哪怕它自己的指标完全不越线。

    真事故(2026-10-18):cartFailure 的 server span 状态恒为 UNSET,错误只出现在调用方
    的 client span 上,cart 自身错误率恒为 0 → 被"自身没异常就丢"的过滤器踢出候选,
    候选里全是受害者,最后答了 load-generator。症状是诊断的起点,不能丢。
    """
    from app.diagnosis.collect import Span
    from app.diagnosis.deviation import ServiceDeviation
    from app.diagnosis.localize import localize
    from app.diagnosis.topology import ServiceGraph

    g = ServiceGraph()
    g.nodes.update({"cart", "checkout"})
    g.edges[("checkout", "cart")] = 10
    devs = {"cart": ServiceDeviation(service="cart"),           # 自身零异常维
            "checkout": ServiceDeviation(service="checkout")}
    devs["checkout"].abnormal_dims.add("error")
    spans = [Span(trace_id="t", span_id="s1", parent_span_id=None, service="checkout",
                  operation="oteldemo.CartService/AddItem", start_us=1, duration_us=1,
                  error=True)]
    loc = localize(g, devs, "cart", spans, symptom_tier=1)
    cart = next((c for c in loc["candidates"] if c["service"] == "cart"), None)
    assert cart is not None, "症状服务被丢出候选名单了"
    assert cart["tier"] == 1, "症状层级应取粗筛给的更强层级"
