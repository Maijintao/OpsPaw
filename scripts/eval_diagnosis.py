"""诊断管线确定性层回归评测(§9 校准闭环的地基,AIOpsLab oracle 模式)。

两个模式:
  snapshot  对活靶子跑确定性采集段,连同已知根因存成 fixture(JSON):
            uv run python scripts/eval_diagnosis.py snapshot <service> --root <expected_root> [--symptom 文案]
            前提:故障正在注入中/docker stop 刚做完——快照抓的就是"故障现场"。
  run       回放全部 fixture,重跑 拓扑→偏离→定位(全程零 LLM),报告三档指标:
            候选命中(期望根因进了候选集)/ Top-3 命中 / Top-1 命中。

回归范围边界(诚实声明):
- LLM 归因段不在回归内——它是生成段,单测靠引用完整性校验兜底。
- 旧的「根因信号门禁 / 对抗证伪」已在 2026-10-15 退役(只记录不改判、不参与终态),
  所以这里不再有「终态命中」这一档:确定性层的最终裁决就是定位层的 Top-1。
- 这里回归的是判据改动的确定性后果,改 FALSE_ALARM_PER_TEST(目标误报率)、LATENCY_QUANTILE
  这类参数之前先跑一遍 run,变好变坏用数字说话。判据数字的出处见
  app/diagnosis/thresholds.py 的登记表。
fixture 攒法:每次故障注入(docker stop currency 之类)后 snapshot 一次,故障恢复即失效,
一例一文件存 data/diag_fixtures/,提交进 git 当回归资产。
"""
import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import repository                      # noqa: E402
from app.diagnosis import attr_contract, collect, deviation, localize, topology  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "data" / "diag_fixtures"
LOOKBACK_MIN = 60   # 与 runner 的采样窗一致,窗边界纪律才可复现


async def _resolve_sources(service: str):
    """与 runner 同一套服务发现+数据源解析(直接复用,口径不分叉)。"""
    from app.diagnosis import runner
    return await runner._resolve_sources(service)


async def _res_ratio(prom: dict, service: str, metric: str, now: float):
    """runner._res_ratio 的快照版(嵌套闭包不可复用,逻辑保持同链:24h→2h,标 t2h)。"""
    cur, _ = await collect.metric_avg(prom["url"], prom.get("headers"), service, metric,
                                      now - 10 * 60, now)
    if cur is None:
        return None, None, None
    for start_off, end_off, tag in ((24 * 3600, 23 * 3600, "t24h"), (2 * 3600, 1 * 3600, "t2h")):
        base, _ = await collect.metric_avg(prom["url"], prom.get("headers"), service, metric,
                                           now - start_off, now - end_off)
        if base not in (None, 0):
            return round(cur, 6), round(cur / base, 3), tag
    return round(cur, 6), None, None


async def snapshot(service: str, expected_root: str, symptom: str, name: str | None) -> None:
    prom, jaeger, err = await _resolve_sources(service)
    if err:
        sys.exit(f"快照失败:{err}")
    now = time.time()
    trace = await collect.fetch_spans(jaeger["url"], jaeger.get("headers"), service,
                                      lookback_min=LOOKBACK_MIN)
    spans = trace.spans   # fetch_spans 返回 TraceFetchResult,span 列表在 .spans
    if not spans:
        sys.exit("快照失败:trace 拉空——故障没在注入中?快照必须在故障现场抓")
    cpu_cur, cpu_ratio, cpu_base = await _res_ratio(prom, service, "cpu", now)
    mem_cur, mem_ratio, mem_base = await _res_ratio(prom, service, "memory", now)
    # span 指标画像:影子准入要区分"曾经有过遥测"和"从不上报";self_latency 的
    # 下游 p95 也从画像取——回放必须保真到确定性判据所需的全部输入,否则时间/时延
    # 信号在回放里系统性失真(fixture 不存画像时下游全被判"无遥测"→全员"慢的源头")
    from app.diagnosis import detect as _detect
    _profiles = await _detect.service_profiles(prom)
    fixture = {
        "name": name or f"{service}-{expected_root}-{time.strftime('%Y%m%d-%H%M%S')}",
        "service": service, "symptom": symptom, "expected_root": expected_root,
        # 用 fetch_spans 实际选中的窗(扩窗逻辑与 runner 同一条):spans 就是这个窗的,
        # 自定义窗会跟 spans 错位,窗边界纪律在回放里整个失真
        "window_start_us": int(trace.window_start * 1e6),
        "window_end_us": int(trace.window_end * 1e6),
        "span_metrics_services": sorted(_profiles),
        "profiles": _profiles,
        "snapshot_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "spans": [asdict(s) for s in spans],
        "res_ratio": {service: {"cpu": cpu_ratio, "mem": mem_ratio,
                                "cpu_baseline": cpu_base, "mem_baseline": mem_base}},
    }
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{fixture['name']}.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"快照已存 {path}({len(spans)} 条 span,CPU 比 {cpu_ratio},内存比 {mem_ratio})")
    print("提示:故障恢复后同一现场不可复得,确认 JSON 无误后提交 git")


def replay_case(path: Path) -> dict:
    """回放单个 fixture 的确定性层,返回判定报告。"""
    fx = json.loads(path.read_text(encoding="utf-8"))
    spans = [collect.Span(**s) for s in fx["spans"]]
    symptom = fx["service"]
    graph = topology.build(spans)
    profiles = fx.get("profiles")
    # edges 必须与真实 runner 同口径传入:self_latency 判定的"下游 p95"从调用图取,
    # 缺了它下游集合为空 → down_p95=0 → 全员误判"下游解释不了、自己是慢的源头"
    devs = deviation.compute(deviation.span_stats(spans), fx["res_ratio"], spans=spans,
                             profiles=profiles, edges=set(graph.edges))
    # 挂死检测(runner ③.5 的 trace 内部核心,与真实管线同一口径):停机服务的旧 span
    # 躺在采样窗里四维全正常,没有这一段它们永远进不了候选。live 管线的"拉调用方
    # 独立采样补证"那段需要 Jaeger 查询,回放不涵盖——那部分属于采集段,不是判据段。
    from app.diagnosis.runner import _points_to, _target_silence
    window_end_us = fx.get("window_end_us") or fx["window_start_us"] + LOOKBACK_MIN * 60 * 1_000_000
    window_end_s = window_end_us / 1e6
    _up1, _down1 = graph.neighbors(symptom)
    for svc in {symptom} | _up1 | _down1:
        dev = devs.get(svc)
        if dev is None or dev.abnormal:
            continue
        sil = _target_silence(spans, svc, window_end_s)
        if not sil:
            continue
        blamed = [sp for sp in spans if sp.error and sp.service != svc
                  and sp.start_us > sil["last_own_us"] and _points_to(sp, svc)]
        if blamed:
            dev.abnormal_dims.add("hang")
            dev.hang_since_us = sil["last_own_us"]
    loc = localize.localize(graph, devs, symptom, spans, window_start_us=fx["window_start_us"],
                            span_profiles=sorted(profiles) if profiles
                            else fx.get("span_metrics_services"))
    cands = loc["candidates"]
    names = [c["service"] for c in cands]
    expected = fx["expected_root"]
    top1 = names[0] if names else None
    # 支撑信号盘点(attr_contract 保留的纯代码能力):给出 Top-1 是靠哪几类信号撑起来的。
    # 纯审计用,不参与判分——旧的门禁/证伪已退役,这里只如实陈述,不裁决。
    edges = set(graph.edges) | set(loc.get("shadow_edges") or {})
    signals = sorted(attr_contract.support_signals(
        top1 or "", symptom, cands, devs, edges, fx["window_start_us"])) if top1 else []
    return {"name": fx["name"], "expected": expected,
            "candidates": names,
            "candidate_hit": expected in names,
            "top3_hit": expected in names[:3],
            "top1": top1,
            "top1_hit": top1 == expected,
            "signals": signals}


def run() -> None:
    files = sorted(FIXTURE_DIR.glob("*.json"))
    if not files:
        sys.exit(f"{FIXTURE_DIR} 下没有 fixture——先在故障注入现场 snapshot")
    reps = [replay_case(p) for p in files]
    for r in reps:
        print(f"[{r['name']}] 期望根因={r['expected']} 候选={','.join(r['candidates']) or '无'}"
              f" → 候选{'✓' if r['candidate_hit'] else '✗'}"
              f" Top3={'✓' if r['top3_hit'] else '✗'}"
              f" Top1={'✓' if r['top1_hit'] else '✗'}({r['top1']})"
              f" 支撑信号={','.join(r['signals']) or '无'}")
    n = len(reps)
    print(f"\n合计 {n} 例:候选命中 {sum(r['candidate_hit'] for r in reps)}/{n},"
          f"Top-3 命中 {sum(r['top3_hit'] for r in reps)}/{n},"
          f"Top-1 命中 {sum(r['top1_hit'] for r in reps)}/{n}")
    print("口径:候选命中=定位层没把期望根因漏出候选集;Top-3/Top-1=排序质量。"
          "LLM 归因段与已退役的门禁/证伪不在回归内。")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("(")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot", help="对活靶子抓故障现场存 fixture")
    snap.add_argument("service", help="症状服务(与对话里诊断的对象一致)")
    snap.add_argument("--root", required=True, help="已知根因(注入了什么就写什么)")
    snap.add_argument("--symptom", default="", help="症状描述文案")
    snap.add_argument("--name", default=None, help="fixture 名(缺省自动生成)")
    sub.add_parser("run", help="回放全部 fixture 出回归指标")
    args = ap.parse_args()
    if args.cmd == "snapshot":
        asyncio.run(snapshot(args.service, args.root, args.symptom, args.name))
    else:
        run()


if __name__ == "__main__":
    main()
