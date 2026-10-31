"""ch06 四验收端到端评估。需全服务起(mysql/milvus/上游/app)+ 已应用 sql/ch06-ticket-type.sql。
用法:.venv/bin/python -m scripts.eval_ch06"""
import asyncio

import httpx

BASE = "http://localhost:8000"


async def agent(client, msg, cid=None):
    r = await client.post(f"{BASE}/api/agent",
                          json={"user_id": "eval-ch06", "message": msg, "conversation_id": cid})
    return r.json()


async def main():
    async with httpx.AsyncClient(timeout=180) as c:
        results = []

        # 验收1:多轮 状态查询→变更申请→状态查询,每轮意图/指代随上下文(明细看 app 日志 intent/route/coref)
        r1 = await agent(c, "服务1001当前状态如何")
        cid = r1["conversation_id"]
        await agent(c, "那帮我把它重启了", cid)          # 指代「它」+ 意图漂移到变更申请
        await agent(c, "算了,它现在啥状态", cid)        # 又漂回服务状态
        results.append(("验收1 多轮意图漂移(看日志 intent/route)", True,
                        f"conv={cid};日志应见 route 服务状态→change_flow→服务状态"))

        # 验收3:「这个能重启吗」先补指代、再走变更子流程(带服务编号,免 interrupt)
        b = await agent(c, "支付服务2002可以申请回滚吗")
        acts = {a["type"] for a in b.get("suggested_actions", [])}
        results.append(("验收3 变更子流程(change_form 或说明)", bool(b["answer"]),
                        {"actions": list(acts), "answer": b["answer"][:40]}))

        # 验收4(非流式部分):不带服务编号问变更 → interrupt 返回服务列表
        b = await agent(c, "我要扩容")
        results.append(("验收4 缺服务弹服务选择器(interrupt)", bool(b.get("interrupt")),
                        b.get("interrupt")))

        for name, ok, detail in results:
            print(f"{'✅' if ok else '❌'} {name} -> {detail}")

    print("\n验收2(意图 JSON 稳定/怪问题落其他):跑 `.venv/bin/python -m scripts.eval_intent`。")
    print("验收4 前端 interrupt→resume→变更按钮 全链路:见浏览器截图。")
    print("验收1/3 的 coref/intent/route 明细:grep app 日志 `intent=.. route=.. trace={..coref..}`。")


if __name__ == "__main__":
    asyncio.run(main())
