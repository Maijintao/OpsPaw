"""调用边时延检测(MicroRCA-Agent 架构移植 + MAD 检测核)单测。

MAD 检测核替换原方案 IF 的实测依据:单维小样本上 IF score 不是偏离度的
单调函数(5 倍时延窗的 score 与正常窗重叠),MAD 判线无此问题。
"""
from app.diagnosis import edge_anomaly
from app.diagnosis.collect import Span
from app.diagnosis.localize import localize
from app.diagnosis.topology import ServiceGraph


def _span(sid, tid, svc, op, start_us, dur_us, parent=None, error=False):
    return Span(trace_id=tid, span_id=sid, parent_span_id=parent, service=svc,
                operation=op, start_us=start_us, duration_us=dur_us,
                error=error)


def _traffic(t0_us: int, n_minutes: int = 10, seed: int = 7):
    """正常流量:frontend→currency 每 2s 一条,时长 50ms + 连续随机抖动。
    抖动必须是连续分布——真实服务的窗均值有连续方差。"""
    import random
    rng = random.Random(seed)
    spans = []
    i = 0
    t = t0_us
    end = t0_us + n_minutes * 60 * 1_000_000
    while t < end:
        d = 50_000 + rng.uniform(-8_000, 25_000)         # 42~75ms 连续抖动
        spans.append(_span(f"s{i}", f"t{i}", "frontend", "server", t, 80_000))
        spans.append(_span(f"c{i}", f"t{i}", "currency", "oteldemo.CurrencyService/Convert",
                           t + 5_000, d, parent=f"s{i}"))
        i += 1
        t += 2_000_000
    return spans


def test_group_edge_spans_cross_service_only():
    t0 = 1_000_000_000
    spans = _traffic(t0, n_minutes=1)
    groups = edge_anomaly._group_edge_spans(spans)
    assert list(groups) == [("frontend", "currency", "oteldemo.CurrencyService/Convert")]
    # 服务内部父子(frontend server → frontend 自己的子 span)不入组:根入口时延
    # 已由服务级 p95 覆盖,边级检测只管跨服务边


def test_group_edge_spans_excludes_streaming():
    t0 = 1_000_000_000
    spans = _traffic(t0, n_minutes=1)
    spans.append(_span("x1", "tx", "flagd", "flagd.evaluation.v2.Service/EventStream",
                       t0, 3_000_000_000, parent="s0"))
    groups = edge_anomaly._group_edge_spans(spans)
    assert all("EventStream" not in k[2] for k in groups)


def test_train_detect_closes_loop():
    """正常流量建画像 → 同分布窗放行 → 注入 10 倍时延的边被点名。"""
    import tempfile
    from pathlib import Path
    t0 = 1_000_000_000
    normal = _traffic(t0)
    with tempfile.TemporaryDirectory() as td:
        stats = edge_anomaly.train(normal, model_dir=Path(td))
        key = "frontend>currency>oteldemo.CurrencyService/Convert"
        assert key in stats and stats[key]["windows"] >= edge_anomaly.MIN_TRAIN_WINDOWS
        assert stats[key]["mad"] > 0

        # 同分布新流量:不应报异常
        calm = _traffic(t0 + 20 * 60 * 1_000_000)
        assert edge_anomaly.detect(calm, stats)[0] == []

        # 注入故障:currency 响应 10 倍慢,持续 3 分钟。慢 span 必须挂在同 trace 的
        # frontend server span 下(与真实调用链一致,parent 解析按 (trace_id, span_id))
        faulty = list(calm)
        j = 0
        t = faulty[0].start_us
        while j < 90:                       # 3 分钟 @2s
            faulty.append(_span(f"bad{j}", f"t{j}", "currency",
                                "oteldemo.CurrencyService/Convert",
                                t + j * 2_000_000, 500_000, parent=f"s{j}"))
            j += 1
        events, note = edge_anomaly.detect(faulty, stats)
        assert note is None
        assert events, "10 倍时延的边没被点名"
        ev = events[0]
        assert ev["callee"] == "currency" and ev["caller"] == "frontend"
        assert ev["anomaly_windows"] >= edge_anomaly.MIN_ANOMALY_WINDOWS
        assert ev["mean_ms"] > ev["normal_mean_ms"], "异常窗均值应高于正常期均值"


def test_detect_without_stats_returns_reason():
    events, note = edge_anomaly.detect([], None)
    assert events == [] and note


def test_zero_variance_edge_skipped():
    """正常期窗均值完全恒定的边(MAD=0):没有偏离参照系,如实跳过,不硬造判线。"""
    import tempfile
    from pathlib import Path
    t0 = 1_000_000_000
    spans = []
    for i in range(120):                    # 4 分钟 @2s(8 窗 ≥ MIN_TRAIN_WINDOWS),duration 恒定 40ms
        spans.append(_span(f"s{i}", f"t{i}", "frontend", "server", t0 + i * 2_000_000, 80_000))
        spans.append(_span(f"c{i}", f"t{i}", "cache", "Get",
                           t0 + i * 2_000_000 + 5_000, 40_000, parent=f"s{i}"))
    with tempfile.TemporaryDirectory() as td:
        stats = edge_anomaly.train(spans, model_dir=Path(td))
        key = "frontend>cache>Get"
        assert key in stats and stats[key]["mad"] == 0
        # 推理时哪怕全部窗都翻倍,也不报——零方差边没有参照系,检测不可信
        doubled = list(spans)
        for i in range(30):
            doubled.append(_span(f"b{i}", f"t{i}", "cache", "Get",
                                 t0 + i * 2_000_000 + 5_000, 80_000, parent=f"s{i}"))
        assert edge_anomaly.detect(doubled, stats)[0] == []


def test_localize_injects_edge_evidence():
    """边级事件进 localize:被调方的候选证据包与全局证据表都有,编号连续。"""
    from app.diagnosis.deviation import ServiceDeviation
    t0 = 1_000_000_000
    devs = {"currency": ServiceDeviation(service="currency"),
            "frontend": ServiceDeviation(service="frontend")}
    devs["currency"].abnormal_dims.add("latency")
    devs["currency"].latency_dev = 5.0
    devs["currency"].calls = 300
    devs["currency"].latency_p95_us = 500_000
    graph = ServiceGraph()
    graph.nodes |= {"frontend", "currency"}
    graph.edges[("frontend", "currency")] = 300
    events = [{"caller": "frontend", "callee": "currency",
               "operation": "oteldemo.CurrencyService/Convert",
               "anomaly_windows": 6, "total_windows": 20,
               "mean_ms": 500.0, "normal_mean_ms": 50.0,
               "first_anomaly_us": t0 + 120_000_000}]
    loc = localize(graph, devs, "frontend", [], if_events=events)
    line = next(l for l in loc["evidence"] if "调用边时延检测" in l)
    assert line.startswith("[E") and "currency" in line
    pack = next(c for c in loc["candidates"] if c["service"] == "currency")
    assert line in pack["evidence"]
    # 编号连续:事件行续在既有证据编号之后
    n_before = len(loc["evidence"]) - 1
    assert f"[E{n_before + 1}]" in line


def test_localize_edge_events_capped():
    from app.diagnosis.deviation import ServiceDeviation
    devs = {"frontend": ServiceDeviation(service="frontend")}
    graph = ServiceGraph()
    graph.nodes.add("frontend")
    events = [{"caller": f"c{i}", "callee": f"svc{i}", "operation": "op",
               "anomaly_windows": i, "total_windows": 20,
               "mean_ms": 100.0, "normal_mean_ms": 10.0,
               "first_anomaly_us": None} for i in range(20)]
    loc = localize(graph, devs, "frontend", [], if_events=events)
    lines = [l for l in loc["evidence"] if "调用边时延检测" in l]
    assert len(lines) == edge_anomaly.EVENT_CAP
    # 截断保异常窗数最多的(按 -anomaly_windows 排序,前 cap 个)
    assert "svc19" in lines[0]
