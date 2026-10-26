"""训练调用边时延画像(edge_anomaly 模块的正常期建模入口)。

前提:靶子当前**健康**(没有故障注入在跑)——画像学的是"这条边的常态",
拿故障期数据训练等于教模型把故障当正常。

用法:
    uv run python scripts/train_edge_stats.py --minutes 40 [--target 平台名] [--limit 200]

流程:取启用平台的 Jaeger → 列服务清单 → 逐服务拉正常期 trace → 按调用边分组
建 MAD 画像 → 存 data/iforest/stats.pkl。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import repository              # noqa: E402
from app.diagnosis import collect, edge_anomaly  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description="训练调用边时延画像(靶子须处于健康态)")
    ap.add_argument("--minutes", type=int, default=40,
                    help="正常期采样窗长(分钟),默认 40(对齐原方案的 MINUTES_AFTER)")
    ap.add_argument("--target", default=None, help="平台名,默认取第一个启用的平台")
    ap.add_argument("--limit", type=int, default=200,
                    help="每服务拉取的 trace 条数上限(searchDepth),默认 200")
    args = ap.parse_args()

    rows = [r for r in await repository.list_targets() if r.enabled]
    if not rows:
        sys.exit("没有启用的平台,先在产品里接入数据源")
    row = next((r for r in rows if r.name == args.target), rows[0]) \
        if args.target else rows[0]
    src = {s["type"]: s for s in (row.sources or [])}
    jaeger = src.get("jaeger")
    if not jaeger:
        sys.exit(f"平台「{row.name}」未接入 Jaeger")
    print(f"平台:{row.name}  Jaeger:{jaeger['url']}  采样窗:最近 {args.minutes} 分钟")

    services = await collect.list_services(jaeger["url"], jaeger.get("headers"))
    if not services:
        sys.exit("Jaeger 服务清单为空——靶子可能没有流量,先确认 otel-demo 在跑")
    print(f"服务清单({len(services)}):{', '.join(sorted(services))}")

    now = time.time()
    all_spans: list[collect.Span] = []
    for svc in sorted(services):
        spans, state, _ = await collect._fetch_spans_once(
            jaeger["url"], jaeger.get("headers"), svc, args.minutes, args.limit, now)
        print(f"  {svc}: {len(spans)} span({state})")
        all_spans.extend(spans)
    if not all_spans:
        sys.exit("采样窗内一条 span 都没有,无法训练")

    stats = edge_anomaly.train(all_spans)
    print(f"\n训练完成:{len(stats)} 条调用边有画像"
          f"(窗长 {edge_anomaly.WIN_SIZE_S}s,最少 {edge_anomaly.MIN_TRAIN_WINDOWS} 窗/边,"
          f"判线由误报率 {0.01} 反解)")
    for key, st in sorted(stats.items()):
        mad_note = f"MAD {st['mad'] / 1000:.1f}ms" if st["mad"] else "MAD=0(零方差,推理时跳过)"
        print(f"  {key}: {st['windows']} 窗,中位 {st['median'] / 1000:.0f}ms,{mad_note}")
    print(f"画像已存:{edge_anomaly.MODEL_DIR / 'stats.pkl'}")


if __name__ == "__main__":
    asyncio.run(main())
