"""挂死检测(目标遥测静默)的单测:纯函数判定 + 信号门槛对 hang 维的接受度。"""
import time

from app.diagnosis.attr_contract import support_signals
from app.diagnosis.collect import Span
from app.diagnosis.deviation import ServiceDeviation
from app.diagnosis.runner import _target_silence


def _span(service: str, start_us: int, error: bool = False, op: str = "op") -> Span:
    return Span(trace_id="t", span_id=f"s{start_us}", parent_span_id=None,
                service=service, operation=op, start_us=start_us,
                duration_us=1000, error=error)


def test_active_service_not_silent():
    """正常出遥测的服务(最新 span 就在眼前)不判静默。"""
    now = time.time()
    end_us = int(now * 1e6)
    spans = [_span("payment", end_us - i * 1_000_000) for i in range(10)]
    assert _target_silence(spans, "payment", now) is None


def test_stopped_service_detected():
    """停机服务:旧 span 间隔密、静默远超阈值 → 判挂死。"""
    now = time.time()
    # 最后一条自身 span 在 200s 前,之前每 2s 一条
    base_us = int((now - 200) * 1e6)
    spans = [_span("payment", base_us - i * 2_000_000) for i in range(10)]
    info = _target_silence(spans, "payment", now)
    assert info is not None
    assert 198 <= info["silence_s"] <= 202
    assert info["median_gap_s"] == 2.0


def test_low_traffic_service_flagged_only_beyond_tail():
    """低频服务(间隔 60s)的界线由数据与目标误报率反解,不是"10× 中位数"拍值:
    界线 = max(自身最长间隔 60s, 60×ln(1/α)≈276s)。静默 200s 未超 → 不判;
    静默 300s 已超 → 判可疑(指数尾部概率 e^-5 ≈ 0.7% < α)。"""
    now = time.time()
    for silence, flagged in ((200, False), (300, True)):
        base_us = int((now - silence) * 1e6)
        spans = [_span("rare", base_us - i * 60_000_000) for i in range(10)]
        assert (_target_silence(spans, "rare", now) is not None) is flagged


def test_silence_fence_is_reported():
    """界线要如实报出来,证据里能看出"为什么判它静默"。"""
    now = time.time()
    base_us = int((now - 200) * 1e6)
    spans = [_span("payment", base_us - i * 2_000_000) for i in range(10)]
    info = _target_silence(spans, "payment", now)
    assert info is not None and info["fence_s"] > 0


def test_too_few_spans_not_flagged():
    """窗内自身 span 不足,谈不上"此前正常",不判。"""
    now = time.time()
    spans = [_span("payment", int((now - 500) * 1e6)) for _ in range(3)]
    assert _target_silence(spans, "payment", now) is None
