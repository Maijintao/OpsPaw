"""指代消解标注评估:多轮带指代补全 + 已完整透传。需聊天上游可调通。
用法:.venv/bin/python -m scripts.eval_coref"""
import asyncio

from app.core.coref import resolve

# (history, query, 期望:'rewrite' 补出实体 / 'passthrough' 原样)
CASES = [
    ("用户:3003 昨晚有没有报错\n值班猫:查到 3 条 ERROR 日志", "帮我把它重启了", "rewrite"),
    ("用户:服务 1001 现在啥状态\n值班猫:运行中,CPU 65%", "那内存呢", "rewrite"),
    ("", "告警阈值是多少", "passthrough"),
    ("", "变更窗口是几点", "passthrough"),
]


async def main():
    for hist, q, kind in CASES:
        got = await resolve(q, hist)
        changed = got != q
        ok = (changed and kind == "rewrite") or (not changed and kind == "passthrough")
        print(f"{'✅' if ok else '❌'} {q!r} -> {got!r} 期望={kind}")
    print("\n(glm 非确定性;透传类必须不改,补全类应含上文实体。抖动如实重跑记录)")


if __name__ == "__main__":
    asyncio.run(main())
