"""CLI:把 knowledge_chunks 里 pending 的块向量化写入 Milvus(幂等可重跑)。
切换 Milvus 实例后可加 --all，把原来已标 done 的块也迁移过去。
运行:PYTHONPATH=. uv run python scripts/vectorize_kb.py"""
import asyncio
import argparse

from app.kb import dualwrite, milvus_client


async def main(include_done: bool = False) -> None:
    client = milvus_client.get_client()
    milvus_client.ensure_collection(client)
    n = await dualwrite.vectorize_pending(client, include_done=include_done)
    print(f"✅ 本次向量化 {n} 块;Milvus 现有 {milvus_client.count(client)} 条")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="迁移所有知识块，包括已标记 done 的块")
    args = parser.parse_args()
    asyncio.run(main(include_done=args.all))
