"""定位层"错误中继"规则:错误全部由下游引起 → 它是中继,不是源头。

## 为什么要有这条(2026-10-17 跑批实测)

payment 挂掉时,现场是:checkout 错误率 10.8%、payment 26.4%,而 checkout 的失败
**全部**是它调 payment 的 Charge 失败(错误 span 的操作名直接点名 payment)。

旧排序按"层级→时间→跳数→偏离"排,同层里"离症状近"的 checkout 排第一,LLM 又
不敢越权改判(改判只许升级),于是整单停在受害者身上,连续两次跑批都判错。

修法与时延维的"纯中继"同源:时延被下游完全解释 → 时延不算它的可疑度;错误全部
被下游解释 → 错误不算它的"源头"证据。判据是**事实**(全部 vs 不是全部),不是比例阈值。
"""
from app.diagnosis.collect import Span
from app.diagnosis.deviation import ServiceDeviation
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


def test_error_relay_is_demoted_below_its_downstream():
    """checkout 的错误全是"调 payment 失败"→ checkout 降为中继,payment 排第一。"""
    spans = [
        _span("checkout", "oteldemo.PaymentService/Charge", error=True, sid="c1"),
        _span("checkout", "oteldemo.PaymentService/Charge", error=True, sid="c2"),
        _span("payment", "Charge", error=True, sid="p1"),
        _span("frontend", "POST /cart/checkout", error=True, sid="f1"),
    ]
    devs = {"checkout": _dev("checkout", error_rate=0.1, abnormal=True),
            "payment": _dev("payment", error_rate=0.3, abnormal=True)}
    loc = localize(_graph(("frontend", "checkout"), ("checkout", "payment")),
                   devs, "checkout", spans)
    order = [c["service"] for c in loc["candidates"]]
    assert order[0] == "payment", f"真凶是下游 payment,实际排序:{order}"
    checkout = next(c for c in loc["candidates"] if c["service"] == "checkout")
    assert checkout["tier"] == 3, "错误全部由下游引起的服务应降到中继层"
    assert any("错误中继" in e for e in checkout["evidence"])


def test_own_errors_are_not_demoted():
    """cart 自己产生的错误(不是调别人失败)→ 保持报错层,排第一。"""
    spans = [
        _span("cart", "AddItem", error=True, sid="c1"),
        _span("cart", "GetCart", error=True, sid="c2"),
        _span("checkout", "oteldemo.CartService/AddItem", error=True, sid="k1"),
    ]
    devs = {"cart": _dev("cart", error_rate=0.5, abnormal=True),
            "checkout": _dev("checkout", error_rate=0.1, abnormal=True)}
    loc = localize(_graph(("checkout", "cart")), devs, "checkout", spans)
    order = [c["service"] for c in loc["candidates"]]
    assert order[0] == "cart", f"cart 自己出错,不该被下游解释掉:{order}"
    cart = next(c for c in loc["candidates"] if c["service"] == "cart")
    assert cart["tier"] == 1


def test_mixed_errors_keep_the_service_as_source():
    """错误里有一部分是自己产生的(没有点名任何下游)→ 不算纯中继,仍是源头候选。"""
    spans = [
        _span("checkout", "oteldemo.PaymentService/Charge", error=True, sid="c1"),
        _span("checkout", "POST /cart/checkout", error=True, sid="c2"),   # 自己的错误
        _span("payment", "Charge", error=True, sid="p1"),
    ]
    devs = {"checkout": _dev("checkout", error_rate=0.2, abnormal=True),
            "payment": _dev("payment", error_rate=0.3, abnormal=True)}
    loc = localize(_graph(("frontend", "checkout"), ("checkout", "payment")),
                   devs, "checkout", spans)
    checkout = next(c for c in loc["candidates"] if c["service"] == "checkout")
    assert checkout["tier"] == 1, "混合来源的错误不能当成纯中继"
