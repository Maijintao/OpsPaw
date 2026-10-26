"""快速跑批:把时钟拨回每个故障发生的时刻,重跑**完整**诊断管线。

## 为什么能快

实时跑批一组 7.5 分钟,拆开看:

    等故障在遥测里显形      180s   ← 纯等
    真正跑诊断              ~30s   ← 只有这一段在算
    等数据窗洗白(给下一组)  240s   ← 纯等

**93% 的时间在空转。** 而故障"那一刻的数据"其实还在:Prometheus 默认留 1 周,
Jaeger 里的 trace 也留着。所以不需要重新注入故障、也不需要等——**把进程的时钟
拨回当时的时刻,整条管线查到的就是当时的数据**。

一轮 12 组 ≈ 6 分钟,对比实时跑批的 90 分钟。

## 为什么只要拨时钟就够

诊断层所有"现在几点"都走 `time.time()`(Prometheus 的查询时刻、Jaeger/Loki 的
start/end 窗口、指标基线偏移都是相对它算的)。所以冻住 `time.time()`,整条管线
自动落在那个时刻上,不用挨个模块传参数。

**`time.monotonic()` 不冻**——它是事件循环的时钟,冻住 `asyncio.sleep` 就永远醒不来;
而诊断的时长预算是拿它算的,一次快跑 30 秒,远没到 300 秒上限,不冻也不会误触发。

## 边界(必须说清楚)

- 数据只留 1 周。**超过一周的故障时刻没法再跑**,得重新注入采一次。
- 快跑复现的是"当时的**采集**结果",不是"当时的**结论**"——判据改了,结论就该变,
  这正是我们要的。
- 靶子的告警/自愈等**副作用不会重放**(快跑只读不写)。

用法:
  uv run python scripts/eval_batch_fast.py                    # 跑最新一轮的全部时刻
  uv run python scripts/eval_batch_fast.py --run-dir <目录>
  uv run python scripts/eval_batch_fast.py --only adFailure   # 只跑匹配关键词的
  uv run python scripts/eval_batch_fast.py --record-llm data/aiopslab/recordings_v2
      # 回放 + 补录:老录制缺的(主要是 LLM 调用)走网络补录到新目录。
      # 跑一遍就得到一份**可全离线重放**的录制 —— 不用在靶子上注故障、不用等 90 分钟。
      # 之后用 --recordings <新目录> 回放,LLM 段也是离线的、结论可复现。
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.aiopslab_bench import (CASES_PATH, RUNS_DIR, _print_summary,  # noqa: E402
                                    load_cases, score_case)
from scripts.eval_detect_offline import _diag_moment  # noqa: E402
from app.diagnosis import net  # noqa: E402

RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "data" / "aiopslab" / "recordings"
DIAG_TIMEOUT_S = 240        # 快跑一轮应该 ~30s;给足余量,超了说明卡住了


class frozen_at:
    """把进程的 `time.time()` 冻在某个时刻(只冻它,不动 monotonic)。"""

    def __init__(self, at: float):
        self.at = at
        self._orig = time.time

    def __enter__(self):
        time.time = lambda: self.at          # noqa: E731  重放到那一刻
        return self

    def __exit__(self, *exc):
        time.time = self._orig
        return False


async def _diagnose() -> dict:
    from app.diagnosis import runner
    diag_id = runner.start("", "", platform=None)
    t0 = time.monotonic()
    while time.monotonic() - t0 < DIAG_TIMEOUT_S:
        run = runner.get(diag_id)
        if run and run.get("status") in ("done", "failed"):
            return run
        await asyncio.sleep(1)
    return runner.get(diag_id) or {"status": "timeout", "result": None}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("用法")[0])
    ap.add_argument("--run-dir", default=None, help="取时刻的跑批目录(缺省最新一轮)")
    ap.add_argument("--recordings", default=str(RECORDINGS_DIR),
                    help="录制目录(每组一个 <组名>.json);有录制就能重跑整条管线,"
                         "没有就只拨时钟(只有指标侧能重查)")
    ap.add_argument("--no-recordings", action="store_true", help="忽略录制,只拨时钟")
    ap.add_argument("--record-llm", default=None, metavar="DIR",
                    help="回放的同时把未命中的请求(含 LLM)补录到该目录——跑一遍就得到"
                         "一份可全离线重放的录制,不用注故障、不用等 90 分钟")
    ap.add_argument("--only", default=None, help="只跑匹配该关键词的题")
    ap.add_argument("--skip-offset", type=int, default=15,
                    help="诊断耗时补偿(秒),默认 15")
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else (
        sorted(p for p in RUNS_DIR.iterdir() if p.is_dir())[-1] if RUNS_DIR.exists() else None)
    if run_dir is None or not run_dir.is_dir():
        sys.exit(f"找不到跑批目录:{RUNS_DIR}")

    wanted = {c["case_id"] for c in load_cases(args.only)} if args.only else None
    rec_dir = None if args.no_recordings else Path(args.recordings)

    # 从上一轮跑批结果里还原每个故障的时刻(文件 mtime 给日期,事件流首行给时分秒)
    jobs: list[tuple[str, float, list[dict]]] = []
    for path in sorted(run_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        blob = json.loads(path.read_text())
        # 只认"组结果文件"(形如 {"group":…, "cases":[…]})。跑批目录里可能还躺着
        # 别的辅助产物,格式不同,不能当成组来解析。
        if not isinstance(blob, dict) or "cases" not in blob:
            continue
        group_cases = blob.get("cases") or []
        if not group_cases:
            continue
        if wanted and not any(c["case_id"] in wanted for c in group_cases):
            continue
        at = _diag_moment(path, (blob.get("run") or {}).get("events") or [],
                          args.skip_offset)
        if at is not None:
            jobs.append((blob.get("group"), at, group_cases))

    print(f"快跑 {len(jobs)} 组(时钟拨回故障时刻)")
    print(f"时刻来源:{run_dir}\n")
    records: list[dict] = []
    t_all = time.monotonic()
    for i, (group, at, group_cases) in enumerate(jobs, 1):
        stamp = datetime.fromtimestamp(at).strftime("%m-%d %H:%M:%S")
        # 有录制就用录制(整条管线都能重跑);没有录制就只拨时钟——那样只有指标侧
        # 能重查(Prometheus 留 1 周),traces 早没了,采集段会断。
        # 录制文件名用的是题库 case["group"](如 ad_service_failure),而跑批目录
        # JSON 的 "group" 字段是注入开关名(如 adFailure)——两者不是一个命名,
        # 拿开关名拼录制路径永远 miss(真实事故:12 组录制全齐,回放却全走 live)。
        rec_name = next((c.get("group") for c in group_cases if c.get("group")), None)
        rec_path = rec_dir / f"{rec_name}.json" if (rec_dir and rec_name) else None
        mode = "replay" if (rec_path and rec_path.exists()) else "live"
        print(f"[{i}/{len(jobs)}] {group:<30} 拨回 {stamp} | {mode}"
              + ("" if mode == "replay" else "  ← 无录制,只有指标侧可重查"), flush=True)
        t0 = time.monotonic()
        if mode == "replay":
            if args.record_llm:
                # 回放 + 补录:观测数据仍来自老录制(冻在录制那一刻),只有老录制里没有的
                # 请求(主要是 LLM 调用)才走网络并补录。跑一遍之后,那份新录制就能
                # **全离线**重放——不用在靶子上注故障,也不用等 90 分钟。
                out_rec = Path(args.record_llm) / f"{rec_name}.json"
                with net.replaying_recording(rec_path, out_rec):
                    run = await _diagnose()
                net_info = net.flush()
                if net_info.get("backfilled"):
                    print(f"    补录 {net_info['backfilled']} 条未命中(含 LLM)→ {out_rec}",
                          flush=True)
            else:
                # 回放:响应全来自录制,时钟也被 net 冻在录制那一刻,全程不碰网络
                with net.replaying(rec_path):
                    run = await _diagnose()
                net_info = net.flush()
        else:
            with frozen_at(at):
                run = await _diagnose()
            net_info = {"hits": 0, "misses": 0}
        wall = round(time.monotonic() - t0, 1)
        if mode == "replay":
            print(f"    录制命中 {net_info['hits']} 条"
                  + (f",补录 {net_info['backfilled']} 条(含 LLM)"
                     if net_info.get("backfilled") else "")
                  + (f",**未命中 {net_info['misses']} 条**" if net_info.get("misses") else ""),
                  flush=True)
        for c in group_cases:
            rec = score_case(c, run)
            rec["wall_s"] = wall
            rec["replay_mode"] = mode
            records.append(rec)
            print(f"    {'✓' if rec.get('success') else '✗'} {c['task']:<13}"
                  f" GT={rec['ground_truth'] or '无故障':<16} → {rec['verdict']}"
                  f" root={rec['root_cause']}  ({wall}s)", flush=True)

    print(f"\n快跑完成:{len(jobs)} 组共 {round(time.monotonic() - t_all)} 秒"
          f"(实时跑批同量约 {len(jobs) * 7.5:.0f} 分钟)")
    _print_summary(records)
    out = run_dir / "_aux" / "fast_batch.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=1, default=str) + "\n")
    print(f"\n明细:{out}")


if __name__ == "__main__":
    asyncio.run(main())
