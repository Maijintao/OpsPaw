"""span 属性榨干(版本变更 / 错误信息)与故障形态新通道的单测。"""
from app.diagnosis.collect import Span, _spans_from_v3
from app.diagnosis import evidence, faulttype, logsig


def _sp(service: str, *, error: bool = False, start_us: int = 1_000_000,
        version: str | None = None, attrs: dict | None = None,
        status_message: str | None = None) -> Span:
    resource = {"service.name": service}
    if version:
        resource["service.version"] = version
    return Span(trace_id="t", span_id=f"s{start_us}{service}{version or ''}",
                parent_span_id=None, service=service, operation="op",
                start_us=start_us, duration_us=1000, error=error,
                status_message=status_message, attributes=attrs, resource=resource)


# ---------- version_timeline ----------

def test_version_timeline_detects_change():
    spans = [
        _sp("cart", version="v1", start_us=100),
        _sp("cart", version="v1", start_us=200),
        _sp("cart", version="v2", start_us=300),
        _sp("other", version="v1", start_us=150),   # 单版本不产生条目
    ]
    out = evidence.version_timeline(spans)
    assert set(out) == {"cart"}
    assert out["cart"]["from"] == "v1" and out["cart"]["to"] == "v2"
    assert out["cart"]["changed_us"] == 300 and out["cart"]["switches"] == 2


def test_version_timeline_empty_without_versions():
    assert evidence.version_timeline([_sp("cart"), _sp("other", version="v1")]) == {}


# ---------- error_lines(错误 span 属性榨干)----------

def test_error_lines_from_grpc_and_exception():
    spans = [
        _sp("checkout", error=True,
            attrs={"rpc.grpc.status_code": "14"}, status_message="unavailable"),
        _sp("checkout", error=True,
            attrs={"exception.message": "connect: connection refused"}),
        _sp("checkout", error=False, attrs={"exception.message": "no error"}),  # 非错误跳过
        _sp("other", error=True, attrs={"error.type": "timeout"}),              # 他人跳过
    ]
    lines = evidence.error_lines(spans, "checkout")
    # 两条(去重后),grace:第一条由状态码+状态消息合成,第二条由异常消息合成
    assert len(lines) == 2
    assert any("unavailable" in x for x in lines)
    assert any("connection refused" in x for x in lines)


def test_error_lines_capped_and_deduped():
    spans = [_sp("svc", error=True, status_message="boom", start_us=i) for i in range(50)]
    assert len(evidence.error_lines(spans, "svc")) == 1


def test_error_lines_feed_logsig_labels():
    spans = [_sp("cart", error=True, attrs={"rpc.grpc.status_code": "14"})]
    sig = logsig.classify(evidence.error_lines(spans, "cart"))
    assert sig["labels"].get("依赖不可用") == 1


# ---------- faulttype 新通道 ----------

def test_faulttype_span_sig_channel_without_loki():
    from app.diagnosis.deviation import ServiceDeviation
    d = ServiceDeviation(service="cart")
    d.abnormal_dims.add("error")
    span_sig = logsig.classify(["deadline exceeded"])
    out = faulttype.classify(d, None, None, span_sig=span_sig)
    assert out["type"] == "network_delay"
    assert "错误 span 携带的错误信息" in out["basis"][0]


def test_faulttype_restart_channel():
    from app.diagnosis.deviation import ServiceDeviation
    d = ServiceDeviation(service="cart")
    out = faulttype.classify(d, None, {"restart": {"restarts": 2.0, "oom": False}})
    assert out["type"] == "pod_failure"
    out = faulttype.classify(d, None, {"restart": {"restarts": 1.0, "oom": True}})
    assert out["type"] == "resource_stress"


def test_faulttype_span_sig_beats_generic_code_error():
    """span 属性通道优先于'机制未定'的 code_error 兜底。"""
    from app.diagnosis.deviation import ServiceDeviation
    d = ServiceDeviation(service="cart")
    d.abnormal_dims.add("error")
    out = faulttype.classify(d, {"labels": {}}, None,
                             span_sig=logsig.classify(["service unavailable"]))
    assert out["type"] == "network_loss"


# ---------- collect:v3 解析带 status_message ----------

def test_spans_from_v3_extracts_status_message():
    body = {"result": {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "cart"}},
            {"key": "service.version", "value": {"stringValue": "v9"}},
        ]},
        "scopeSpans": [{"spans": [{
            "traceId": "t", "spanId": "s", "name": "op",
            "startTimeUnixNano": "1000", "endTimeUnixNano": "2000",
            "status": {"code": 2, "message": "upstream unavailable"},
            "attributes": [{"key": "rpc.grpc.status_code",
                            "value": {"stringValue": "14"}}],
        }]}],
    }]}}
    spans = _spans_from_v3(body)
    sp = spans[0]
    assert sp.status_message == "upstream unavailable"
    assert sp.resource["service.version"] == "v9"


# ---------- condense_logs(日志压缩:去重+最早时刻)----------

def test_condense_logs_dedupes_and_counts():
    lines = [
        {"ts": "2026-10-13T10:00:05Z", "line": "connect failed at 10:00:05"},
        {"ts": "2026-10-13T10:00:02Z", "line": "connect failed at 09:59:58"},
        {"ts": "2026-10-13T10:00:01Z", "line": "oom killed process"},
    ]
    out = evidence.condense_logs(lines, cap=20)
    # 前两条数字抹平后同型 → 合并,计数 2;first_ts 取最早那条
    assert out["distinct"] == 2
    assert out["repeat"] == 1
    assert out["first_ts"] == "2026-10-13T10:00:01Z"
    assert {x["line"] for x in out["lines"]} >= {"oom killed process"}
    merged = next(x for x in out["lines"] if "connect failed" in x["line"])
    assert merged["repeat"] == 2


def test_condense_logs_cap_keeps_newest():
    lines = [{"ts": f"2026-10-13T10:{i:02d}:00Z", "line": f"err-{chr(97 + i)} happened"}
             for i in range(30)]
    out = evidence.condense_logs(lines, cap=20)
    assert len(out["lines"]) == 20
    assert out["distinct"] == 30
    assert out["repeat"] == 0
    assert out["lines"][0]["ts"] > out["lines"][-1]["ts"]


def test_condense_logs_empty():
    assert evidence.condense_logs([]) == {"lines": [], "distinct": 0,
                                          "repeat": 0, "first_ts": None}
