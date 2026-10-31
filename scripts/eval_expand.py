"""Query 扩写标注评估:核心场景出 3 条 + JSON 稳定 + 保持关键实体。需聊天上游可调通。
用法:.venv/bin/python -m scripts.eval_expand"""
import asyncio

from app.core.query_understanding import expand_queries

CASES = ["CPU 飙高了还能申请重启吗", "服务3003内存爆了是不是要扩容", "网关一直报错怎么查日志"]


async def main():
    for q in CASES:
        qs = await expand_queries(q)
        print(f"{'✅' if len(qs) == 3 else '⚠️'} {q!r} -> {qs}(条数={len(qs)})")
    print("\n核心场景应出 3 条、彼此侧重点不同、含原问题关键实体;JSON 不稳时条数会 <3。抖动如实重跑记录")


if __name__ == "__main__":
    asyncio.run(main())
