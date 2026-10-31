"""服务名机械闸:答案不能编造证据里没有的服务标识。"""
from app.core import model_guard as g

EVIDENCE = ("[1] data-sync-service 由平台组负责,依赖 api-gateway-service。\n"
            "[2] payment-service 当前有 3 个运行实例。")


def test_flags_service_name_missing_from_evidence():
    assert g.unsupported_models("data-sync-service 依赖 order-service", EVIDENCE) == ["order-service"]


def test_all_service_names_in_evidence_pass():
    answer = "data-sync-service 依赖 api-gateway-service。"
    assert g.unsupported_models(answer, EVIDENCE) == []


def test_no_service_name_passes():
    assert g.unsupported_models("值班期间禁止直接重启生产服务[1]。", EVIDENCE) == []


def test_multiple_service_names_are_deduplicated_in_order():
    answer = "order-service 与 cache-service,再提一次 order-service"
    assert g.unsupported_models(answer, EVIDENCE) == ["order-service", "cache-service"]


def test_comparison_is_case_sensitive():
    assert g.unsupported_models("Data-sync-service", EVIDENCE) == ["Data-sync-service"]


def test_does_not_capture_non_service_hyphenated_terms():
    for text in ("api-gateway", "2.4G Wi-Fi", "no-answer", "payment-"):
        assert g.models_in(text) == [], text


def test_repair_hint_does_not_guess_a_replacement():
    hint = g.repair_hint(["order-service"])
    assert "order-service" in hint and "data-sync-service" not in hint
