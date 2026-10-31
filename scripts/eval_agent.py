"""标注样例评估:核对 glm-5.2 是否按预期选中工具,并观察 query_faq 的关键词与漏召回。
需服务运行中(docker compose/opspaw-mysql + make seed + make dev)。"""
import asyncio
import json

import httpx

BASE = "http://localhost:8000"

# (用户问法, 期望命中的工具名集合;None 表示期望不调用任何工具)
SAMPLES = [
    ("查一下 3003 的 CPU 曲线", {"query_cpu_metrics"}),
    ("告警阈值是多少", {"query_faq"}),          # 验收2:query_faq 命中
    ("服务趴窝了怎么处理", {"query_faq"}),          # 口语提「趴窝」→ 命中服务不可用 runbook,未漏(语义提参救了)
    ("3003 最近有数据库连接超时吗", {"search_topic_by_service_name", "search_log"}),
    ("2002 payment-service 是谁负责的", {"query_faq"}),
    ("api-gateway-service 最近有哪些 ERROR 日志", {"search_topic_by_service_name", "search_log"}),
    ("我要投诉,给我登记一下", {"create_ticket"}),
    ("今天天气怎么样", None),                       # 超范围,期望不调工具
]


def _faq_note(body: dict) -> str:
    """对调用了 query_faq 的样例,打印它提取的 keyword 与是否漏召回。"""
    faq_calls = [tc for tc in body["tool_calls"] if tc["name"] == "query_faq"]
    faq_results = [tr for tr in body["tool_results"] if tr["name"] == "query_faq"]
    if not faq_calls or not faq_results:
        return ""
    keyword = faq_calls[0]["args"].get("keyword", "")
    hits = json.loads(faq_results[0]["content"]).get("hits", [])
    return f"  [query_faq keyword={keyword!r} 漏召回={'是' if not hits else '否'}]"


async def main() -> None:
    passed = 0
    async with httpx.AsyncClient(timeout=60) as client:
        for msg, expect in SAMPLES:
            r = await client.post(f"{BASE}/api/agent", json={"user_id": "eval", "message": msg})
            body = r.json()
            names = {tc["name"] for tc in body["tool_calls"]}
            ok = (not names) if expect is None else bool(names & expect)
            passed += ok
            print(f"{'✅' if ok else '❌'} {msg!r} -> {names or '(未调用)'} 期望={expect}{_faq_note(body)}")
            print(f"    answer: {body['answer'][:70]}")
    print(f"\n选对工具 {passed}/{len(SAMPLES)}(glm 非确定性,抖动如实重跑记录)")


if __name__ == "__main__":
    asyncio.run(main())
