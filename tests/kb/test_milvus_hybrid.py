import os
import uuid

import pytest

from app.kb import milvus_client as mc

URI = "http://localhost:19530"
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MILVUS_TESTS") != "1",
    reason="需要 Milvus Standalone；设置 RUN_MILVUS_TESTS=1 后运行",
)


@pytest.fixture()
def coll():
    client = mc.get_client(uri=URI)
    name = f"test_kb_{uuid.uuid4().hex[:8]}"
    mc.ensure_collection(client, collection=name)
    rows = [
        {"id": 1, "dense": [0.1] * 1024, "text": "告警 CPU 持续 5 分钟超过 80% 触发",
         "question": "告警阈值是多少", "answer": "CPU 持续 5 分钟超过 80% 触发",
         "section_path": "值班规范", "content_type": "faq", "category": "值班规范"},
        {"id": 2, "dense": [0.2] * 1024, "text": "order-service 网关 自动扩容",
         "question": "Pro 型号功能", "answer": "自动清理除臭",
         "section_path": "服务手册 / order-service", "content_type": "manual", "category": "服务手册"},
    ]
    mc.upsert_vectors(client, rows, collection=name)
    mc.flush(client, collection=name)          # 新数据须 flush 才可被检索
    client.load_collection(name)
    import time; time.sleep(2)                  # 等 sparse 索引对新段生效
    yield client, name
    mc.drop(client, name)


def test_bm25_hits_model_number(coll):
    client, name = coll
    hits = mc.bm25_search(client, "order-service 网关", top_k=2, collection=name)
    assert hits[0]["id"] == 2
    assert hits[0]["section_path"] == "服务手册 / order-service"


def test_hybrid_returns_scored_hits(coll):
    client, name = coll
    hits = mc.hybrid_search(client, [0.1] * 1024, "告警", top_k=2, recall=10, collection=name)
    assert hits
    assert {"id", "score", "question", "answer", "section_path", "content_type", "category"} <= hits[0].keys()


def test_category_filter(coll):
    client, name = coll
    hits = mc.bm25_search(client, "网关 告警", top_k=5, category="值班规范", collection=name)
    assert all(h["category"] == "值班规范" for h in hits)
    assert all(h["id"] == 1 for h in hits)
