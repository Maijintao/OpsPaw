"""ch10 地基:topic_classifications ORM 与 repository 归类方法。"""
from app.db import repository


async def test_insert_and_exclude_classified(db_session_factory):
    qid = await repository.insert_low_confidence(None, "服务又宕机了太坑了", "retrieval_low_conf", None)
    rid = await repository.insert_review_item("服务宕机后能否申请重启恢复", None)
    await repository.set_matched_review(qid, rid)
    rows = await repository.list_unclassified_questions()
    assert any(r["question_id"] == qid for r in rows)

    n = await repository.insert_topic_classifications(
        [{"question_id": qid, "labels": ["服务不可用", "变更申请"]}])
    assert n == 1
    rows = await repository.list_unclassified_questions()
    assert not any(r["question_id"] == qid for r in rows)


async def test_unmerged_excluded_from_classify(db_session_factory):
    """未归并(无 matched_review_id)的问题不进分类旁路——只归归并过的。"""
    qid = await repository.insert_low_confidence(None, "日志保留几天", "retrieval_low_conf", None)
    rows = await repository.list_unclassified_questions()
    assert not any(r["question_id"] == qid for r in rows)


async def test_text_prefers_normalized(db_session_factory):
    qid = await repository.insert_low_confidence(None, "这个服务能重启吗", "retrieval_low_conf", None)
    rid = await repository.insert_review_item("order-service 能否直接重启恢复", None)
    await repository.set_matched_review(qid, rid)
    rows = await repository.list_pool_texts()
    row = next(r for r in rows if r["question_id"] == qid)
    assert row["text"] == "order-service 能否直接重启恢复"


async def test_topic_distribution_counts_all_17(db_session_factory):
    qid = await repository.insert_low_confidence(None, "服务又宕机了太坑了", "retrieval_low_conf", None)
    await repository.insert_topic_classifications(
        [{"question_id": qid, "labels": ["服务不可用", "变更申请"]}])
    dist = await repository.topic_distribution()
    assert dist["total"] == 1
    assert len(dist["classes"]) == 17
    by_label = {c["label"]: c for c in dist["classes"]}
    assert by_label["服务不可用"]["count"] == 1 and by_label["变更申请"]["count"] == 1
    assert by_label["服务状态"]["count"] == 0
    assert by_label["服务不可用"]["samples"] == ["服务又宕机了太坑了"]
    assert dist["latest"] is not None


async def test_topic_distribution_ignores_legacy_labels(db_session_factory):
    """旧版电商分类留在库中时，不计入当前 on-call 主题总量。"""
    qid = await repository.insert_low_confidence(None, "商品什么时候发货", "retrieval_low_conf", None)
    await repository.insert_topic_classifications([{"question_id": qid, "labels": ["物流", "售后"]}])
    dist = await repository.topic_distribution()
    assert dist["total"] == 0
    assert all(row["count"] == 0 for row in dist["classes"])
