import pytest

from app.diagnosis import collect


def _span(start_us: int) -> collect.Span:
    return collect.Span("t", "s", None, "currency", "op", start_us, 1_000, False)


def _span_id(start_us: int, sid: str) -> collect.Span:
    """带独立 span_id 的 span:错误优先采样并入的去重键是 (trace_id, span_id)。"""
    return collect.Span("t", sid, None, "currency", "op", start_us, 1_000, False)


@pytest.mark.asyncio
async def test_fetch_spans_stops_at_first_sufficient_window(monkeypatch):
    calls = []

    async def fetch_once(base, headers, service, minutes, limit, end):
        calls.append(minutes)
        return [_span(int((end - 30) * 1e6)) for _ in range(20)], "ok", None

    monkeypatch.setattr(collect, "_fetch_spans_once", fetch_once)
    result = await collect.fetch_spans("jaeger", None, "currency", window_end=10_000)

    assert calls == [10]
    assert result.status == "available"
    assert result.lookback_min == 10
    assert result.requested_lookbacks == [10]


@pytest.mark.asyncio
async def test_fetch_spans_expands_and_filters_out_of_window_data(monkeypatch):
    calls = []

    async def fetch_once(base, headers, service, minutes, limit, end):
        calls.append(minutes)
        if minutes == 10:
            return [_span(int((end - 11 * 60) * 1e6)) for _ in range(20)], "ok", None
        if minutes == 60:
            return [_span(int((end - 30 * 60) * 1e6)) for _ in range(20)], "ok", None
        raise AssertionError("must stop after 60-minute window is sufficient")

    monkeypatch.setattr(collect, "_fetch_spans_once", fetch_once)
    result = await collect.fetch_spans("jaeger", None, "currency", window_end=10_000)

    assert calls == [10, 60]
    assert result.status == "available"
    assert result.lookback_min == 60
    assert len(result.spans) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "expected"), [
    ("empty", "empty"),
    ("stale", "data_stale"),
    ("failure", "query_failed"),
    ("sparse", "sparse"),
])
async def test_fetch_spans_reports_final_quality_state(monkeypatch, response, expected):
    async def fetch_once(base, headers, service, minutes, limit, end):
        if response == "empty":
            return [], "empty", None
        if response == "stale":
            return [_span(int((end - 2 * 24 * 60 * 60) * 1e6))], "ok", None
        if response == "failure":
            return [], "query_failed", "HTTP 503"
        return [_span(int((end - 30) * 1e6))], "ok", None

    monkeypatch.setattr(collect, "_fetch_spans_once", fetch_once)
    result = await collect.fetch_spans("jaeger", None, "currency", window_end=10_000)

    assert result.status == expected
    assert result.requested_lookbacks == [10, 60, 360, 1440]
    assert len(result.spans) == (1 if response == "sparse" else 0)


def test_span_window_filter_uses_inclusive_bounds():
    start, end = 1_000_000, 2_000_000
    assert start <= _span(start).start_us <= end
    assert start <= _span(end).start_us <= end
    assert not start <= _span(start - 1).start_us <= end
    assert not start <= _span(end + 1).start_us <= end


@pytest.mark.asyncio
async def test_fetch_spans_merges_error_only_sampling(monkeypatch):
    """错误优先采样:普通采样 20 条无错误,错误通道带回 3 条错误 span → 并入结果。
    这是"错误被新流量挤出条数上限"那一类失明的解药。"""
    normal = [_span(int((10_000 - 30) * 1e6)) for _ in range(20)]

    async def fetch_once(base, headers, service, minutes, limit, end, error_only=False):
        if error_only:
            return [_span_id(int((10_000 - 60) * 1e6), f"e{i}") for i in range(3)], "ok", None
        return normal, "ok", None

    monkeypatch.setattr(collect, "_fetch_spans_once", fetch_once)
    result = await collect.fetch_spans("jaeger", None, "currency", window_end=10_000)
    assert result.status == "available"
    assert len(result.spans) == 23


@pytest.mark.asyncio
async def test_fetch_spans_error_channel_failure_keeps_base(monkeypatch):
    """错误通道失败(上游不认 error tag/网络抖动)原样交还普通采样,不阻断诊断。"""
    normal = [_span(int((10_000 - 30) * 1e6)) for _ in range(20)]

    async def fetch_once(base, headers, service, minutes, limit, end, error_only=False):
        if error_only:
            raise RuntimeError("upstream rejects tags param")
        return normal, "ok", None

    monkeypatch.setattr(collect, "_fetch_spans_once", fetch_once)
    result = await collect.fetch_spans("jaeger", None, "currency", window_end=10_000)
    assert result.status == "available" and len(result.spans) == 20


@pytest.mark.asyncio
async def test_fetch_spans_error_merge_dedupes_by_span_id(monkeypatch):
    """错误通道带回的 span 与普通采样重复(trace_id,span_id 相同)不重复计数。"""
    normal = [_span(int((10_000 - 30) * 1e6)) for _ in range(20)]

    async def fetch_once(base, headers, service, minutes, limit, end, error_only=False):
        if error_only:
            return [_span_id(int((10_000 - 30) * 1e6), "s") for _ in range(5)], "ok", None
        return normal, "ok", None

    monkeypatch.setattr(collect, "_fetch_spans_once", fetch_once)
    result = await collect.fetch_spans("jaeger", None, "currency", window_end=10_000)
    assert len(result.spans) == 20
