"""直连 SiliconFlow /rerank 冒烟。不通即红线停。"""
import httpx

from app.config import settings
from app.core.rerank import _rerank_url


def main() -> None:
    url = _rerank_url()   # 跟应用同一份拼法,别在这里各拼各的
    payload = {
        "model": settings.rerank_model,
        "query": "重启服务要不要审批",
        "documents": [
            "值班期间收到告警先认领再排查,5 分钟内响应。",
            "紧急止血场景(服务已宕机、单实例异常、内存耗尽)重启任何时间免审批。",
            "数据同步服务负责跨机房元数据同步与校验。",
        ],
        # 字段跟 app/core/rerank.py 发的那份保持一致:冒烟要验的是应用真会发的请求形状,
        # 多带一个 return_documents 就成了验另一条路。只读 index 和 relevance_score,不要原文。
        "top_n": 3,
    }
    r = httpx.post(url, json=payload,
                   headers={"Authorization": f"Bearer {settings.rerank_api_key}"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    print("原始返回:", data)
    results = data["results"]
    assert results, "rerank 返回空"
    top = max(results, key=lambda x: x["relevance_score"])
    print(f"最相关 index={top['index']} score={top['relevance_score']:.4f}")
    assert top["index"] == 1, "重启审批问题应命中第 2 条(免审批场景)"
    print("GO: rerank 链路通")


if __name__ == "__main__":
    main()
