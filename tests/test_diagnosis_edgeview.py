"""边级错误归属(edgeview)归属规则的单测:纯函数 _attribute,不碰网络。

规则:每个调用方的客户端错误按操作名归属到被调服务;最大归属项就是某条边、
且不小于该调用方无法归属的错误量 → 这条边的被调方是失败承载方。
"""
from app.diagnosis.edgeview import _attribute

KNOWN = {"frontend", "recommendation", "payment", "checkout", "ad", "load-generator"}


def _row(svc, op, n):
    return {"metric": {"service_name": svc, "span_name": op},
            "value": [1792793428, str(n)]}


def test_dominant_edge_names_the_callee():
    """frontend 的客户端错误全部落在指向 recommendation 的操作上 → 指认 recommendation。"""
    rows = [_row("frontend", "oteldemo.RecommendationService/ListRecommendations", 27),
            _row("frontend", "oteldemo.AdService/GetAds", 0),
            _row("load-generator", "POST", 18)]
    out = _attribute(rows, KNOWN)
    assert len(out) == 1
    assert out[0].callee == "recommendation" and out[0].caller == "frontend"
    assert out[0].count == 27


def test_unattributed_majority_suppresses_blame():
    """调用方自己一身的错(操作名不指向任何服务)远多于任何单边 → 指认说不出口。"""
    rows = [_row("frontend-web", "GET", 50),
            _row("frontend-web", "oteldemo.AdService/GetAds", 5)]
    assert _attribute(rows, KNOWN) == []


def test_equal_max_edges_both_blamed():
    rows = [_row("frontend", "oteldemo.RecommendationService/ListRecommendations", 10),
            _row("frontend", "oteldemo.AdService/GetAds", 10)]
    out = _attribute(rows, KNOWN)
    assert {o.callee for o in out} == {"recommendation", "ad"}


def test_zero_and_empty_rows_ignored():
    assert _attribute([], KNOWN) == []
    assert _attribute([_row("frontend", "GET", 0)], KNOWN) == []


def test_operation_not_matching_any_known_service_is_unattributed():
    """操作名指向不了任何已知服务(如裸 POST/GET) → 不产生指认。"""
    rows = [_row("load-generator", "POST", 12)]
    assert _attribute(rows, KNOWN) == []


def test_blame_takes_the_chain_endpoint():
    """受害链层层指认(proxy→frontend→recommendation)时只取末端:中间节点
    (既被指认又在指认别人)是传播路径,不是承载源头。"""
    rows = [_row("frontend-proxy", "router frontend egress", 12),
            _row("frontend", "oteldemo.RecommendationService/ListRecommendations", 27)]
    out = _attribute(rows, KNOWN)
    assert [o.callee for o in out] == ["recommendation"]


def test_chronic_noise_gate_drops_baselined_edges():
    """慢性噪声闸:当前窗错误与基线窗相当的边(flagd 长连接流断连)不算新证据。"""
    rows = [_row("ad", "flagd.evaluation.v2.Service/EventStream", 8)]
    base = [_row("ad", "flagd.evaluation.v2.Service/EventStream", 8)]
    KNOWN = {"ad", "flagd"}
    assert _attribute(rows, KNOWN, base) == [], "与基线相当的错误不是新证据"


def test_chronic_noise_gate_passes_new_edges():
    """真故障边(基线为零)放行;基线查不到(为 None)也不闸。"""
    rows = [_row("frontend", "oteldemo.RecommendationService/ListRecommendations", 27)]
    KNOWN = {"frontend", "recommendation"}
    assert len(_attribute(rows, KNOWN, [])) == 1
    assert len(_attribute(rows, KNOWN, None)) == 1


def test_history_band_drops_sporadic_chronic_edges():
    """七天历史带:偶发慢性断连(这窗 1 笔那窗 0 笔)超不过自己的历史带,
    不产生指认——两条恰好同窗也凑不成法定人数。"""
    from app.diagnosis.stats import Distribution
    KNOWN2 = {"cart", "payment", "flagd"}
    rows = [_row("cart", "flagd.evaluation.v2.Service/EventStream", 1),
            _row("payment", "flagd.evaluation.v2.Service/EventStream", 1)]
    # 历史分布:这两对的错误速率常年就在 ~1 笔/窗 的水平(0.0037/s 上下浮动)
    hist = {("cart", "flagd.evaluation.v2.Service/EventStream"):
            Distribution.build([0.0037] * 40),
            ("payment", "flagd.evaluation.v2.Service/EventStream"):
            Distribution.build([0.0037] * 40)}
    assert _attribute(rows, KNOWN2, [], hist) == [], "历史带内的偶发噪声不该指认"


def test_history_band_passes_genuinely_new_errors():
    from app.diagnosis.stats import Distribution
    KNOWN2 = {"frontend", "recommendation"}
    rows = [_row("frontend", "oteldemo.RecommendationService/ListRecommendations", 27)]
    hist = {("frontend", "oteldemo.RecommendationService/ListRecommendations"):
            Distribution.build([0.0] * 40)}   # 历史干净
    out = _attribute(rows, KNOWN2, [], hist)
    assert len(out) == 1 and out[0].callee == "recommendation"
