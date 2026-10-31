from dataclasses import dataclass

from app.kb import dedup


@dataclass
class Item:
    question: str


def test_normalize_strips_punct_keeps_chinese():
    assert dedup.normalize_question(" 内存告警阈值,是多少? ") == "内存告警阈值是多少"


def test_dedupe_within_batch_and_against_existing():
    items = [Item("内存告警阈值是多少"), Item("内存告警阈值是多少?"), Item("怎么回滚"), Item("内存持续上涨怎么排查")]
    kept, discarded = dedup.dedupe(items, existing_questions=["内存持续上涨怎么排查"])
    kept_q = [i.question for i in kept]
    assert "内存告警阈值是多少" in kept_q          # 首次出现保留
    assert "怎么回滚" in kept_q
    assert "内存持续上涨怎么排查" not in kept_q       # 与库内已有重复 → 丢弃
    assert len(kept) == 2 and len(discarded) == 2
