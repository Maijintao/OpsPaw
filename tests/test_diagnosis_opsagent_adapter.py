"""OpsAgent 适配层单测:结论解析合同(服务名识别/诚实降级)。"""
from app.diagnosis import opsagent_adapter


def test_attr_from_answers_known_service():
    cands = [{"service": "checkout"}, {"service": "payment"}]
    attr = opsagent_adapter.attr_from_answers(
        "root cause is payment, error rate 90%", ["root cause is payment"],
        cands, "checkout", services_all={"checkout", "payment"})
    assert attr.verdict == "root_cause" and attr.root_cause_service == "payment"


def test_attr_from_answers_normalizes_punctuation_consistently():
    cands = [{"service": "product-catalog"}, {"service": "checkout"}]
    attr = opsagent_adapter.attr_from_answers(
        "the root cause is ProductCatalogService",
        ["the root cause is ProductCatalogService"],
        cands, "checkout", services_all={"product-catalog", "checkout"})
    assert attr.verdict == "root_cause"
    assert attr.root_cause_service == "product-catalog"


def test_attr_from_answers_prefers_longer_overlapping_service_name():
    cands = [{"service": "cart-service"}, {"service": "cart"}]
    attr = opsagent_adapter.attr_from_answers(
        "root cause is cart-service",
        ["root cause is cart-service"],
        cands, "cart", services_all={"cart-service", "cart"})
    assert attr.root_cause_service == "cart-service"


def test_attr_from_answers_tied_names_are_inconclusive_and_stable():
    cands = [{"service": "payment"}, {"service": "checkout"}]
    text = "payment and checkout both show the same evidence"
    results = [opsagent_adapter.attr_from_answers(
        text, [text], cands, "checkout",
        services_all={"payment", "checkout"}).root_cause_service
               for _ in range(3)]
    assert results == [None, None, None]


    """OpsAgent 的结论可能指向候选集外的服务(它的数据特征覆盖全平台)——
    只要该服务在平台服务清单里,结论就要被接受,不被旧候选集卡死。"""
    cands = [{"service": "checkout"}]
    attr = opsagent_adapter.attr_from_answers(
        "flagd configuration fault is the root cause", ["flagd ..."],
        cands, "checkout", services_all={"checkout", "flagd", "frontend"})
    assert attr.verdict == "root_cause" and attr.root_cause_service == "flagd"


def test_attr_from_answers_unknown_service_degrades():
    """答案里的服务名全平台都没有 → 不猜,inconclusive + 缺口说明。"""
    attr = opsagent_adapter.attr_from_answers(
        "mysql02 disk full", ["mysql02 disk full"],
        [{"service": "checkout"}], "checkout",
        services_all={"checkout", "payment"})
    assert attr.verdict == "inconclusive" and attr.root_cause_service is None
    assert attr.missing


def test_attr_from_answers_explicit_no_root():
    text = "no root cause identified, evidence insufficient"
    attr = opsagent_adapter.attr_from_answers(
        text, [text], [{"service": "checkout"}, {"service": "checkout2"}],
        "checkout", services_all={"checkout"})
    assert attr.verdict == "inconclusive"


def test_log_timestamp_converts_without_crashing():
    """观测工具吐的日志 ts 是 RFC3339 字符串,不是数字。

    真事故:cartFailure 那轮归因阶段对 '2026-10-16T06:54:12Z' 直接 float() 抛
    ValueError,整条诊断崩在归因段——日志格式不该弄死诊断。"""
    assert opsagent_adapter._epoch_seconds("2026-10-16T06:54:12Z", 0.0) == 1792133652.0
    assert opsagent_adapter._epoch_seconds(1792150880.5, 0.0) == 1792150880.5
    assert opsagent_adapter._epoch_seconds(None, 42.0) == 42.0
    assert opsagent_adapter._epoch_seconds("not-a-time", 42.0) == 42.0


def test_attr_from_answers_out_of_candidate_unsupported_falls_back():
    """结论指向候选集外、且自身无异常信号 → 退回证据支撑的首选候选。

    真事故:cartFailure 的候选只有 cart/checkout,OpsAgent 却答 frontend,
    而 frontend 在整张调用图上没有任何异常维。"""
    cands = [{"service": "cart"}, {"service": "checkout"}]
    attr = opsagent_adapter.attr_from_answers(
        "the root cause is frontend", ["the root cause is frontend"],
        cands, "cart", services_all={"cart", "checkout", "frontend"},
        abnormal={"cart", "checkout"})
    assert attr.root_cause_service == "cart"
    assert "frontend" in attr.confidence_note


def test_attr_from_answers_out_of_candidate_but_abnormal_is_kept():
    """结论指向候选集外、但该服务自己确实有异常信号 → 采纳,不把 LLM 的视野锁死。"""
    cands = [{"service": "cart"}]
    attr = opsagent_adapter.attr_from_answers(
        "root cause is flagd", ["root cause is flagd"],
        cands, "cart", services_all={"cart", "flagd"}, abnormal={"cart", "flagd"})
    assert attr.root_cause_service == "flagd"


def test_attr_from_answers_weaker_candidate_falls_back_to_top():
    """LLM 挑的候选因果层级弱于首选候选 → 退回首选。

    真事故:adHighCpu 那组锚点 ad(CPU 是自身历史的 60 倍)、候选第一也是 ad,
    LLM 却答了同表里更弱的 checkout(层级 6 vs 5)。"""
    cands = [{"service": "ad", "tier": 5}, {"service": "checkout", "tier": 6}]
    attr = opsagent_adapter.attr_from_answers(
        "root cause is checkout", ["root cause is checkout"],
        cands, "ad", services_all={"ad", "checkout"}, abnormal={"ad", "checkout"})
    assert attr.root_cause_service == "ad"
    assert "checkout" in attr.confidence_note


def test_attr_from_answers_same_tier_lateral_swap_falls_back_to_top():
    """同层横向换人也退回首选——同层先后是确定性层已经算过的账。

    真事故:paymentFailure 那组候选第一是 payment(被 checkout 的失败调用指认、层内偏离
    更大),LLM 却答了它的调用方 checkout——正好停在症状上,把因果方向搞反了。
    旧实现只拦"层级更弱",同层换人放行,于是这一组判错。
    """
    cands = [{"service": "payment", "tier": 1}, {"service": "checkout", "tier": 1}]
    attr = opsagent_adapter.attr_from_answers(
        "root cause is checkout", ["root cause is checkout"],
        cands, "checkout", services_all={"payment", "checkout"},
        abnormal={"payment", "checkout"})
    assert attr.root_cause_service == "payment"
    assert "checkout" in attr.confidence_note


def test_attr_from_answers_stronger_candidate_is_kept():
    """LLM 挑的候选证据更强(层级更小)→ 采纳:改判只允许往更强的方向走。"""
    cands = [{"service": "cart", "tier": 4}, {"service": "payment", "tier": 1}]
    attr = opsagent_adapter.attr_from_answers(
        "root cause is payment", ["root cause is payment"],
        cands, "cart", services_all={"cart", "payment"}, abnormal={"cart", "payment"})
    assert attr.root_cause_service == "payment"
