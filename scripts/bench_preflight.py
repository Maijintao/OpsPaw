#!/usr/bin/env python
"""跑批前置自检:三小时之前,先把"会让整轮白跑"的条件全验一遍。

为什么要有这个东西(2026-10-20 夜跑的真实教训):
  12 组跑完、退出码 0、分数看着正常,结果 9 组的 LLM 归因根本没跑起来——
  服务器缺 pytz,归因抛异常后被兜底逻辑接住,退回"确定性排序第一名"。
  从外面完全看不出来,只有翻每组 JSON 的 confidence_note 才发现。
  所以:依赖、模型通道、数据深度、靶子状态,必须在开跑前一条条验。

用法(在服务器上,项目根目录):
    set -a && source .env && set +a
    .venv/bin/python scripts/bench_preflight.py          # 全检,有任何一条不过退出码 1
    .venv/bin/python scripts/bench_preflight.py --quick  # 跳过最慢的两条(模型/数据深度)

退出码:0 全过;1 有硬性条件不满足。
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OPSAGENT = ROOT / "app" / "diagnosis" / "opsagent"

SERVER = "http://127.0.0.1:8000"
PROM = "http://127.0.0.1:19090"
JAEGER = "http://127.0.0.1:32769"
LOKI = "http://127.0.0.1:3100"

OK, BAD, WARN = "✓", "✗", "!"


def line(mark: str, name: str, detail: str) -> None:
    print(f"  {mark} {name:<16}{detail}", flush=True)


async def check_server(client) -> bool:
    try:
        r = await client.get(f"{SERVER}/")
        ok = r.status_code == 200
        line(OK if ok else BAD, "服务端", f"HTTP {r.status_code}")
        return ok
    except Exception as exc:  # noqa: BLE001
        line(BAD, "服务端", f"{type(exc).__name__}: {exc}")
        return False


async def check_tunnel(client) -> bool:
    """三个观测端口:数据面全走隧道,断一个这轮数据就不可信。"""
    probes = [("Prometheus", f"{PROM}/-/ready", {200}),
              ("Jaeger", f"{JAEGER}/api/services", {200, 404}),
              ("Loki", f"{LOKI}/ready", {200})]
    all_ok = True
    for name, url, codes in probes:
        try:
            r = await client.get(url, timeout=6)
            ok = r.status_code in codes
            line(OK if ok else BAD, f"隧道·{name}", f"HTTP {r.status_code}")
            all_ok = all_ok and ok
        except Exception as exc:  # noqa: BLE001
            line(BAD, f"隧道·{name}", f"{type(exc).__name__}")
            all_ok = False
    return all_ok


def check_concurrent() -> bool:
    """两个跑批同时动靶子 = 整轮数据作废(2026-10-20 实测踩过)。"""
    out = subprocess.run(["pgrep", "-af", "aiopslab_bench.py run"],
                         capture_output=True, text=True)
    pids = [ln for ln in out.stdout.splitlines() if ln.strip()]
    if pids:
        line(BAD, "并发跑批", f"已有 {len(pids)} 个在跑:{pids[0][:80]}")
        return False
    line(OK, "并发跑批", "没有别的跑批在跑")
    return True


def check_attribution_imports() -> bool:
    """归因三 agent 的导入链:缺一个包,归因会静默退化成排序兜底,分数失真。

    这里刻意导入适配层真正要的那 9 个模块(不是全部文件)——和运行期同一条路,
    多导几个反而会把用不到的调参脚本拉进来误报。
    """
    mods = ["agents.orchestrator", "agents.ad_agent", "agents.ft_agent", "agents.rcl_agent",
            "utils.MessagePool", "utils.ReviewPool", "utils.blackboard",
            "utils.data_processer", "reflection.ReflectionVector"]
    prev_cwd = os.getcwd()
    prev_path = list(sys.path)
    missing: list[str] = []
    try:
        os.chdir(OPSAGENT)
        sys.path.insert(0, str(OPSAGENT))
        for m in mods:
            try:
                __import__(m)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{m}: {type(exc).__name__} {str(exc)[:60]}")
    finally:
        os.chdir(prev_cwd)
        sys.path[:] = prev_path
    if missing:
        line(BAD, "归因依赖", f"{len(missing)} 个模块导不进来(跑批会静默降级)")
        for m in missing[:3]:
            print(f"      {m}")
        return False
    line(OK, "归因依赖", f"{len(mods)} 个模块全部可导入")
    return True


async def check_llm(client) -> bool:
    """模型通道:端点和 key 失效同样会让归因降级(和缺包一个后果)。"""
    from app.config import settings
    base = (settings.chat_base_url or "").rstrip("/")
    if not base or not settings.chat_api_key:
        line(BAD, "模型通道", "没配 CHAT_BASE_URL / CHAT_API_KEY")
        return False
    try:
        r = await client.post(f"{base}/chat/completions", timeout=25,
                              headers={"Authorization": f"Bearer {settings.chat_api_key}"},
                              json={"model": settings.chat_model, "max_tokens": 4,
                                    "messages": [{"role": "user", "content": "hi"}]})
        if r.status_code == 200:
            line(OK, "模型通道", f"{settings.chat_model} 可用")
            return True
        line(BAD, "模型通道", f"HTTP {r.status_code}: {r.text[:80]}")
        return False
    except Exception as exc:  # noqa: BLE001
        line(BAD, "模型通道", f"{type(exc).__name__}: {str(exc)[:80]}")
        return False


async def check_history_depth(client) -> bool:
    """历史样本够不够:粗筛判据是"当前值在自身历史分布里的分位",
    样本不足时整轮只会 inconclusive(2026-10-20 白天:保留期 1h,8 小时全白跑)。
    """
    from app.diagnosis import detect
    from app.diagnosis import stats

    try:
        r = await client.get(f"{PROM}/api/v1/label/__name__/values", timeout=10)
        names = (r.json() or {}).get("data") or []
    except Exception as exc:  # noqa: BLE001
        line(BAD, "历史深度", f"取指标族失败:{type(exc).__name__}")
        return False
    fam = next((n for n in names if "cpu" in n and n.endswith("_total")), None)
    if not fam:
        line(WARN, "历史深度", "Prometheus 里找不到 CPU 计数类指标族,跳过深度检查")
        return True
    # 用子查询问"过去 24h 里最早的一个样本在哪",一步拿到数据起点
    expr = f"min_over_time((timestamp({fam}))[24h:5m])"
    try:
        r = await client.get(f"{PROM}/api/v1/query", params={"query": expr}, timeout=25)
        res = ((r.json() or {}).get("data") or {}).get("result") or []
    except Exception as exc:  # noqa: BLE001
        line(WARN, "历史深度", f"深度查询失败({type(exc).__name__}),跳过")
        return True
    if not res:
        line(BAD, "历史深度", "Prometheus 里没有任何历史样本")
        return False
    oldest = float(res[0]["value"][1])
    span = time.time() - oldest
    need_s = stats.MIN_HISTORY_SAMPLES * detect.SCAN_WINDOW_S
    samples = int(span / detect.SCAN_WINDOW_S)
    ok = span >= need_s
    detail = (f"数据跨度 {span / 3600:.1f}h,可出 {samples} 个 {detect.SCAN_WINDOW_S}s 窗"
              f"(判据要 {stats.MIN_HISTORY_SAMPLES} 个 ≈ {need_s / 3600:.1f}h)")
    line(OK if ok else BAD, "历史深度", detail)
    return ok


async def check_flags(client) -> bool:
    """靶子开关干净:带病开跑,第一组的基线就被上一个故障污染。"""
    try:
        rows = (await client.get(f"{SERVER}/api/targets", timeout=15)).json().get("targets") or []
    except Exception as exc:  # noqa: BLE001
        line(WARN, "靶子开关", f"取接入平台失败({type(exc).__name__}),跳过")
        return True
    target = next((t for t in rows if t.get("cluster") is not None), None)
    if not target:
        line(BAD, "靶子开关", "没有接入平台")
        return False
    try:
        r = await client.post(f"{SERVER}/api/targets/{target['id']}/repair/read",
                              json={"action": "list_flags"}, timeout=30)
        if r.status_code != 200:
            line(WARN, "靶子开关", f"连接器读不到开关(HTTP {r.status_code}),跳过")
            return True
        data = (r.json() or {}).get("data") or {}
        dirty = data.get("non_off") or []
        if dirty:
            line(BAD, "靶子开关", f"{len(dirty)} 个非 off:{'、'.join(dirty[:5])}")
            return False
        line(OK, "靶子开关", f"{len(data.get('flags') or {})} 个开关全部 off")
        return True
    except Exception as exc:  # noqa: BLE001
        line(WARN, "靶子开关", f"读取失败({type(exc).__name__}),跳过")
        return True


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过模型通道与历史深度(慢的两条)")
    args = ap.parse_args()

    import httpx
    print("跑批前置自检")
    results = []
    async with httpx.AsyncClient(trust_env=False) as client:
        results.append(await check_server(client))
        results.append(await check_tunnel(client))
        results.append(check_concurrent())
        results.append(check_attribution_imports())
        if not args.quick:
            results.append(await check_llm(client))
            results.append(await check_history_depth(client))
        results.append(await check_flags(client))

    bad = results.count(False)
    print("-" * 60)
    if bad:
        print(f"{BAD} {bad} 条硬性条件不满足,不要开跑(跑了也是废数据)")
        return 1
    print(f"{OK} 全部通过,可以开跑")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
