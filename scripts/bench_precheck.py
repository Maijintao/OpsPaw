"""跑批前置检查:靶子开关/残留错误活动/Jaeger 残留/画像新鲜度。

默认只读。发现靶子开关未复位时只报告；需要恢复时显式传入 ``--restore``。
恢复会通过 SSH 修改靶子，并可能从快照还原整个 flagd 文件。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.diagnosis import collect
from app.diagnosis import runner as _runner
from scripts.aiopslab_bench import read_flags, restore


async def main(restore_dirty: bool = False) -> None:
    # 1. 靶子开关:必须全部复位
    flags = read_flags()
    dirty = [f for f in flags if f["current"] not in ("off", "default", "false")]
    print(f"[1] 靶子开关 {len(flags)} 个,非默认值 {len(dirty)} 个")
    for f in dirty:
        print(f"    残留:{f['flag']} = {f['current']}")
    if dirty and restore_dirty:
        print("    → 已显式请求 restore，将修改靶子并可能还原整个 flagd 文件…")
        restore()
        flags = read_flags()
        dirty = [f for f in flags if f["current"] not in ("off", "default", "false")]
        print(f"    复位后非默认值 {len(dirty)} 个{'(仍有残留!)' if dirty else ''}")
    elif dirty:
        print("    ⚠️ 只读检查未修改靶子；如需恢复，请重新运行并加 --restore")

    # 2. 数据源可达 + 当前错误活动(近 10 分钟错误 span)
    prom, jaeger, err, _service = await _runner._resolve_sources("frontend")
    if err:
        sys.exit(f"[2] 数据源解析失败:{err}")
    print(f"[2] 数据源可达 prom={prom['url'][:40]}… jaeger={jaeger['url'][:40]}…")
    services = await collect.list_services(jaeger["url"], jaeger.get("headers"))
    print(f"    Jaeger 服务清单 {len(services)} 个: {', '.join(sorted(services))}")

    now = time.time()
    total_err = 0
    per_svc = {}
    for svc in sorted(services):
        spans, state, _ = await collect._fetch_spans_once(
            jaeger["url"], jaeger.get("headers"), svc, 10, 50, now, error_only=True)
        # error 通道会混入 envoy 路由 span(它们没有真错误 tag),按 span.error 过滤——
        # 不过滤的话健康系统也会"报"两万条假错误(precheck 首跑的真实教训)
        real = [sp for sp in (spans or []) if sp.error]
        if state == "ok" and real:
            per_svc[svc] = len(real)
            total_err += len(real)
    print(f"[3] 近 10 分钟错误 span:合计 {total_err}")
    for svc, n in sorted(per_svc.items(), key=lambda x: -x[1]):
        print(f"    {svc}: {n}")
    if total_err > 20:
        print("    ⚠️ 残留错误活动偏高——前一轮跑批的故障残留还没洗掉,先等洗窗再跑")
    else:
        print("    ✓ 残留错误可忽略,窗口干净")

    # 3. 画像新鲜度
    p = Path("data/iforest/stats.pkl")
    if p.exists():
        age_min = (time.time() - p.stat().st_mtime) / 60
        print(f"[4] 调用边画像 age {age_min:.0f} 分钟({p})")
        if age_min > 60:
            print("    ⚠️ 画像偏旧,建议跑批前重训")
        else:
            print("    ✓ 画像新鲜")
    else:
        print("[4] ⚠️ 无画像,跑批前必须重训")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true",
                        help="发现非默认开关时恢复靶子(会修改远端 flagd 文件)")
    args = parser.parse_args()
    asyncio.run(main(restore_dirty=args.restore))
