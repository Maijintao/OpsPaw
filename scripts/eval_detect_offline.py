"""离线回放粗筛判据:用 Prometheus 历史数据在**故障发生的那些时刻**重跑粗筛,对比新旧锚点。

为什么需要它:实时跑批一轮 90 分钟(93% 是等故障显形和等指标窗洗白),而判据重构要
反复验证几十次——靠实时跑批迭代等于每次改一行代码等一个半小时。Prometheus 默认保留
1 周,而每个故障的诊断时刻都记录在跑批结果里,所以可以把**同一套判据重放到过去**:
秒级一轮,而不是 90 分钟。

口径(必须说清楚,否则数字没有意义):
  旧锚点 = 跑批当时记录下来的 anchors[0](在 data/aiopslab/runs/<轮次>/<组>.json 里)
  新锚点 = 现在这份代码在**同一个时刻**跑出来的 anchors[0]
  GT     = 题库 data/aiopslab/cases.json 里的 ground_truth_service
判分三个数:
  锚点命中(新/旧)—— 锚点是不是 GT
  对照组正确拒绝    —— 无故障的对照组,新判据有没有乱报锚点(旧实现在这里是误报)

用法:
  uv run python scripts/eval_detect_offline.py                     # 回放最新一轮
  uv run python scripts/eval_detect_offline.py --run-dir <目录>
  uv run python scripts/eval_detect_offline.py --skip-offset 15    # 诊断耗时补偿(秒)
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "data" / "aiopslab" / "cases.json"
RUNS_DIR = ROOT / "data" / "aiopslab" / "runs"
TARGET_NAME = "Otel-demo"


def _diag_moment(run_path: Path, events: list[dict], skip_offset: int) -> float | None:
    """还原这次诊断的**起点时刻**(unix 秒)。

    事件流里每条带本地 HH:MM:SS,文件 mtime 给日期。两者拼起来就是诊断开始的时刻。
    再往前推 skip_offset 秒(默认 15)对齐"粗筛跑的那一下"——粗筛是诊断的第一步,
    整个诊断通常 15~40 秒,不补这点偏移会拿诊断**结束**时刻去查,窗已经晚了。"""
    stamps = [e.get("ts") for e in events if e.get("ts")]
    if not stamps:
        return None
    mtime = run_path.stat().st_mtime
    day = datetime.fromtimestamp(mtime)
    h, m, s = (int(x) for x in stamps[0].split(":"))
    base = day.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
    return base - skip_offset


async def _prom_source() -> dict | None:
    from app.db import repository
    rows = await repository.list_targets()
    row = next((r for r in rows if r.enabled and r.name == TARGET_NAME), None)
    if row is None:
        row = next((r for r in rows if r.enabled), None)
    if row is None:
        return None
    return next((s for s in (row.sources or []) if s.get("type") == "prometheus"), None)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("用法")[0])
    ap.add_argument("--run-dir", default=None, help="跑批目录(缺省取最新一轮)")
    ap.add_argument("--skip-offset", type=int, default=15,
                    help="诊断耗时补偿:从诊断起点再往前推这么多秒(默认 15)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else (
        sorted(p for p in RUNS_DIR.iterdir() if p.is_dir())[-1] if RUNS_DIR.exists() else None)
    if run_dir is None or not run_dir.is_dir():
        sys.exit(f"找不到跑批目录:{RUNS_DIR}")

    cases = {c["case_id"]: c for c in json.loads(CASES_PATH.read_text())["cases"]}
    prom = await _prom_source()
    if prom is None:
        sys.exit("接入平台里没有可用的 Prometheus 数据源")

    from app.diagnosis import detect

    print(f"回放目录:{run_dir}")
    print(f"靶子 Prometheus:{prom['url']}\n")
    print(f"{'组':<34}{'GT':<17}{'旧锚点':<18}{'新锚点':<18}{'旧':<4}{'新':<4}")
    print("-" * 100)

    stat = {"old_hit": 0, "new_hit": 0, "n": 0,
            "old_fp": 0, "new_fp": 0, "control": 0}
    detail: list[dict] = []
    for path in sorted(run_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        blob = json.loads(path.read_text())
        run = blob.get("run") or {}
        group = blob.get("group")
        case = next((c for c in blob.get("cases") or []), None)
        if case is None:
            continue
        gt = case.get("ground_truth_service")
        is_control = bool(case.get("control"))
        at = _diag_moment(path, run.get("events") or [], args.skip_offset)
        if at is None:
            print(f"{group:<34} 事件流为空,跳过")
            continue

        old = ((run.get("result") or {}).get("detect") or {}).get("anchors") or []
        old_top = old[0]["service"] if old else None
        old_status = ((run.get("result") or {}).get("detect") or {}).get("status")

        try:
            res = await detect.scan_one(prom, platform=TARGET_NAME, now=at)
        except Exception as exc:  # noqa: BLE001  回放失败如实报,不假装跑过
            print(f"{group:<34} 回放失败:{type(exc).__name__}: {exc}")
            continue
        new = res.get("anchors") or []
        new_top = new[0]["service"] if new else None

        def hit(top):
            if is_control:
                return top is None
            return top == gt

        old_ok, new_ok = hit(old_top), hit(new_top)
        stat["n"] += 1
        stat["old_hit"] += old_ok
        stat["new_hit"] += new_ok
        if is_control:
            stat["control"] += 1
            stat["old_fp"] += (old_top is not None)
            stat["new_fp"] += (new_top is not None)
        detail.append({"group": group, "gt": gt, "control": is_control,
                       "old_top": old_top, "new_top": new_top,
                       "old_tier": (old[0].get("tier") if old else None),
                       "new_tier": (new[0].get("tier") if new else None),
                       "new_status": res.get("status", "available"),
                       "new_reasons": (new[0].get("reasons") if new else [])[:2],
                       "old_reasons": (old[0].get("reasons") if old else [])[:2],
                       "at": datetime.fromtimestamp(at).strftime("%H:%M:%S")})
        print(f"{group:<34}{str(gt or '无故障'):<17}{str(old_top):<18}{str(new_top):<18}"
              f"{'✓' if old_ok else '✗':<4}{'✓' if new_ok else '✗':<4}")

    print("\n" + "=" * 100)
    print(f"锚点命中:旧 {stat['old_hit']}/{stat['n']}  →  新 {stat['new_hit']}/{stat['n']}")
    if stat["control"]:
        print(f"对照组正确拒绝:旧 {stat['control'] - stat['old_fp']}/{stat['control']}"
              f"  →  新 {stat['control'] - stat['new_fp']}/{stat['control']}")
    changed = [d for d in detail if d["old_top"] != d["new_top"]]
    if changed:
        print(f"\n锚点发生变化的 {len(changed)} 组(逐组看新判据的理由):")
        for d in changed:
            print(f"  {d['group']}  (回放时刻 {d['at']})")
            print(f"    旧:{d['old_top']}  tier={d['old_tier']}")
            print(f"    新:{d['new_top']}  tier={d['new_tier']}  status={d['new_status']}")
            for r in d["new_reasons"]:
                print(f"      · {str(r)[:150]}")

    out = run_dir / "offline_detect_replay.json"
    out.write_text(json.dumps({"run_dir": str(run_dir), "stats": stat,
                               "detail": detail}, ensure_ascii=False, indent=1) + "\n")
    print(f"\n明细:{out}")


if __name__ == "__main__":
    asyncio.run(main())
