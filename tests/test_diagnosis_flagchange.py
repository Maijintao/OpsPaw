"""开关变更探测(flagchange)解析规则的单测:纯函数 _parse,不碰网络。

规则:某 (服务, 开关, 取值) 当前窗有样本、且"窗前历史且不在已知故障窗内"从未
出现过 → 变更。跑批注入窗必须从基线里剔除——否则昨天注入过的开关"历史里见过",
今天的同款翻转会被判成"没变"。
"""
from app.diagnosis.flagchange import _parse

W_START, W_END = 1000.0, 1600.0          # 当前窗
HIST_CLEAN = 500.0                        # 窗前、非故障期
HIST_FAULTY = 700.0                       # 窗前、已知故障窗内
INCIDENTS = [(690.0, 710.0)]


def _row(svc, flag, variant, ts_list):
    return {"metric": {"feature_flag_key": flag, "feature_flag_result_variant": variant,
                       "service_name": svc},
            "values": [[t, "1"] for t in ts_list]}


def test_new_variant_in_window_is_a_change():
    rows = [_row("cart", "cartFailure", "off", [HIST_CLEAN, 900.0]),
            _row("cart", "cartFailure", "100%", [1200.0, 1500.0])]
    out = _parse(rows, W_START, W_END, INCIDENTS)
    assert len(out) == 1
    ch = out[0]
    assert (ch.service, ch.flag, ch.variant) == ("cart", "cartFailure", "100%")
    assert ch.prev_variants == ("off",)
    assert ch.onset_us == 1_200_000_000


def test_variant_seen_in_clean_history_is_not_a_change():
    rows = [_row("cart", "cartFailure", "on", [HIST_CLEAN, 1200.0])]
    assert _parse(rows, W_START, W_END, INCIDENTS) == []


def test_variant_seen_only_in_incident_history_is_a_change():
    """昨天故障期见过的取值不算"平时就有"——基线必须洗掉故障窗。"""
    rows = [_row("cart", "cartFailure", "on", [HIST_FAULTY, 1200.0])]
    out = _parse(rows, W_START, W_END, INCIDENTS)
    assert len(out) == 1 and out[0].variant == "on"
    assert out[0].prev_variants == ()


def test_series_without_service_label_is_skipped():
    rows = [{"metric": {"feature_flag_key": "x", "feature_flag_result_variant": "on"},
             "values": [[1200.0, "1"]]}]
    assert _parse(rows, W_START, W_END, []) == []


def test_no_samples_in_window_is_not_a_change():
    rows = [_row("cart", "cartFailure", "on", [HIST_CLEAN])]
    assert _parse(rows, W_START, W_END, []) == []
