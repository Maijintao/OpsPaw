"""ch09 置信度闸:四信号(Top1 分/有效证据数/分差/关键条款)加权,纯函数可复现。"""
import pytest

from app.core.confidence import EvidenceConfidence, compute_evidence_confidence, snapshot_from_hits


def _hit(score, q="CPU 告警阈值是多少", a="CPU 持续 5 分钟超过 80% 触发告警"):
    return {"question": q, "answer": a, "rerank_score": score,
            "section_path": "监控观测 / 告警阈值", "id": 1, "content_type": "faq"}


def test_empty_hits_zero_confidence():
    r = compute_evidence_confidence([])
    assert r.score == 0.0
    assert r.signals == {"top1_score": 0.0, "valid_count": 0, "margin": 0.0, "key_clause_hit": False}


def test_strong_evidence_scores_high():
    r = compute_evidence_confidence([_hit(0.95), _hit(0.40), _hit(0.35)])
    assert r.score > 0.7
    assert r.signals["top1_score"] == 0.95
    assert r.signals["margin"] == pytest.approx(0.55)
    assert r.signals["key_clause_hit"] is True   # 「告警」「阈值」命中关键条款词表


def test_weak_flat_evidence_scores_low():
    hits = [_hit(0.22, q="助手叫什么名字", a="内部系统的智能运维助手"),
            _hit(0.21, q="助手叫什么名字", a="内部系统的智能运维助手")]
    r = compute_evidence_confidence(hits)
    assert r.score < 0.4
    assert r.signals["valid_count"] == 0          # 全部低于有效线
    assert r.signals["key_clause_hit"] is False


def test_single_hit_margin_falls_back_to_top1():
    r = compute_evidence_confidence([_hit(0.8)])
    assert r.signals["margin"] == pytest.approx(0.8)


def test_score_monotonic_in_top1():
    low = compute_evidence_confidence([_hit(0.3), _hit(0.2)])
    high = compute_evidence_confidence([_hit(0.9), _hit(0.2)])
    assert high.score > low.score


def test_score_bounded_zero_one():
    r = compute_evidence_confidence([_hit(1.0), _hit(0.0)])
    assert 0.0 <= r.score <= 1.0


def test_snapshot_from_hits_top3_shape():
    hits = [_hit(0.9), _hit(0.8), _hit(0.7), _hit(0.6)]
    snap = snapshot_from_hits(hits)
    assert len(snap) == 3
    assert snap[0] == {"question": "CPU 告警阈值是多少", "answer": "CPU 持续 5 分钟超过 80% 触发告警",
                       "rerank_score": 0.9, "section_path": "监控观测 / 告警阈值"}


def test_snapshot_empty_hits():
    assert snapshot_from_hits([]) == []
