"""AIOpsLab Astronomy Shop 端到端跑批(含 LLM)。

和 eval_diagnosis.py 的分工:
  eval_diagnosis.py  离线回放 fixture,只回归确定性层(拓扑/偏离/定位/门禁/证伪),零 LLM、秒级;
  本脚本             活靶子端到端跑批:注入故障 → 等故障在遥测里显形 → 调完整诊断管线
                     (含 detect 粗筛、LLM 归因、补查、开方)→ 按 AIOpsLab 口径判分 → 恢复。
                     慢(每例几分钟),但这是唯一能测到 LLM 段和 detect 段的方式。

题目从哪来:data/aiopslab/cases.json(由 scripts/gen_aiopslab_cases.py 从 AIOpsLab 源码生成),
ground truth 服务和判分口径都照它,不自造。

为什么注入走 flagd 而不是 K8s:AIOpsLab 原生跑要 K8s 集群(kubectl + Helm),而我们靶子是
docker compose。但它的 Astronomy Shop 故障**全部是 flagd feature flag**,而 flagd 配置是
bind mount 进来的普通 JSON 文件,flagd 自带 filepath sync 监听——改文件即生效,不用重启
容器。所以同一批故障在 compose 上能一比一复现,判分口径也不用改。

⚠️ 两个时间常量是拍值,按"够用且别太久"估的,可命令行覆盖:
  --settle   注入后等故障在遥测里显形。诊断的 trace 采样窗是"最小且 ≥20 条 span"的窗,
             高流量下会选 10min 窗;故障占窗比例太低会被稀释。默认 180s。
  --recover  恢复后等指标窗洗白。detect 的当前窗是 5min rate,前窗是 300~600s 前;
             不等够,上一题的残留会污染下一题的基线。默认 240s。

用法:
  uv run python scripts/aiopslab_bench.py status                 # 看靶子当前开关状态
  uv run python scripts/aiopslab_bench.py plan                   # 看跑批计划(不执行)
  uv run python scripts/aiopslab_bench.py inject <case_id>       # 只注入(手工排查用)
  uv run python scripts/aiopslab_bench.py restore                # 全部开关复位 off
  uv run python scripts/aiopslab_bench.py run [--only 关键词] [--settle 180] [--recover 240]
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from hashlib import md5
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "data" / "aiopslab" / "cases.json"
RUNS_DIR = ROOT / "data" / "aiopslab" / "runs"

SSH_HOST = "ubuntu@42.193.188.243"
FLAGD_PATH = "/home/ubuntu/opentelemetry-demo/src/flagd/demo.flagd.json"

SETTLE_S = 180
RECOVER_S = 240
# 复位≠恢复:开关关掉之后,压测机的并发用户、连接池、队列可能还在高位跑。
# 2026-10-21 实测:洪泛开关 16:12 就关了,流量到 19:15 才回落,中间 2.7 小时
# 全被算进"正常历史",把 frontend 的调用量峰值抬到 71/s,第二天同款故障涨到 40
# 都判"见过,正常"——粗筛直接失明。
# 所以复位后必须核对"流量是否真的回来了",没回来就把注入窗继续延长(登记到回落为止)。
# 这两个数是策略(可命令行覆盖):容差 1.2 = 允许比注入前高 20%,再高就算没洗干净。
RESIDUAL_TOL = 1.2
RESIDUAL_MAX_WAIT_S = 600
# 压测机内存水位上限(%):它是 Chromium,泄漏到限额就卡死、流量归零。留 20% 余量,
# 和容器驱逐的常用档位同档——不是判据阈值,是运维护栏。
LOADGEN_MEM_LIMIT_PCT = 80.0
DIAG_TIMEOUT_S = 900     # 诊断自身超时(管线内部有 300s 预算闸,这里再兜一层)
REPAIR_TIMEOUT_S = 600   # 修复 agent 从派单到回执的超时(实测一单 20-60s,余量给慢 LLM)
# 为什么从 420 抬到 900:粗筛的 24h 分位数 range 查询在靶机上要 41 秒(实测),加上
# 归因段三次 LLM 调用(慢的时候每次 1-2 分钟),420 秒会被切在收尾阶段——2026-10-17
# 实测 image_slow_load 已经跑出结论了,却在 452 秒被超时判成"无结果",整组白跑。
# 宁可多等,不要把跑完的结论丢掉。
CONTROL_GROUP = "__control__"

HOLDOUT_GROUPS = {"cart_service_failure", "loadgenerator_flood_homepage",
                  "recommendation_service_cache_failure"}
"""**留出集:只看不调。** 这三组是我从没针对性改过判据的组,用来判断改动到底是
"真的更好"还是"对着剩下 9 组调出来的"。

纪律(2026-10-18 定):
  - 任何判据改动**不许**以这三组的表现作为设计依据;
  - 每次改动后跑一次留出集,只要有 1 组变差就回退,不接受"整体更好但留出集掉一组";
  - 这三组当前基线(10-17 实测):cart ✓✓ / loadgen ✓✓ / recommendation ✓✓。

为什么需要:12 个案例被反复看三天,每条规则听起来都通用,但也是被这 12 个案例筛出来的。
留出集是唯一能低成本发现"选择性偏差"的东西。"""


# ---------- 靶子侧开关操作(全部走 ssh + 远端 python,不做字符串拼命令) ----------

_READ_TMPL = """
import json
d = json.load(open("__FLAGD__"))
for k, f in sorted((d.get("flags") or {}).items()):
    print(k + "\\t" + str(f.get("defaultVariant")) + "\\t" + ",".join((f.get("variants") or {}).keys()))
"""

_INJECT_TMPL = """
import json
p = "__FLAGD__"
flag = "__FLAG__"
variant = "__VARIANT__"
d = json.load(open(p))
f = (d.get("flags") or {}).get(flag)
if f is None:
    raise SystemExit("开关不存在: " + flag)
if variant not in (f.get("variants") or {}):
    raise SystemExit("变体不存在: " + variant)
f["defaultVariant"] = variant
# targeting 会覆盖 defaultVariant:flagd 求值时先走 targeting,走不到才看 defaultVariant。
# 实测 productCatalogFailure 的 targeting 两个分支都是 "off",改 defaultVariant 等于没改,
# 故障从来没被注入过(老记录 0/4、录制里无异常、20:00 那轮 10 秒返回"未见异常",同一根因)。
# 注入期间摘掉 targeting,还原靠 restore 从快照整文件恢复。
if "targeting" in f:
    del f["targeting"]
    print("note: targeting removed for " + flag)
json.dump(d, open(p, "w"), indent=2)
print("injected " + flag + "=" + variant)
"""

_SNAPSHOT_TMPL = """
import json, shutil, os
p = "__FLAGD__"
b = p + ".bak-opspaw"
d = json.load(open(p))
dirty = [k for k, f in (d.get("flags") or {}).items()
         if f.get("defaultVariant") != "off" and "off" in (f.get("variants") or {})]
if dirty:
    raise SystemExit("当前开关非全 off,拒绝留快照: " + ", ".join(dirty))
if os.path.exists(b):
    print("snapshot already exists: " + b)
else:
    shutil.copy2(p, b)
    print("snapshot created: " + b)
"""

_RESTORE_TMPL = """
import json, shutil, os
p = "__FLAGD__"
b = p + ".bak-opspaw"
if os.path.exists(b):
    # 快照恢复:整文件还原,连 targeting 一起——注入期间摘掉的 targeting 回得来
    shutil.copy2(b, p)
    print("restored from snapshot")
else:
    d = json.load(open(p))
    n = []
    for k, f in (d.get("flags") or {}).items():
        if f.get("defaultVariant") != "off" and "off" in (f.get("variants") or {}):
            f["defaultVariant"] = "off"
            n.append(k)
    json.dump(d, open(p, "w"), indent=2)
    print("restored: " + (", ".join(n) if n else "无(全部已是 off)"))
"""


def _ssh_py(code: str, timeout_s: int = 60) -> str:
    """在靶机上跑一段 python(走 stdin,不拼命令行)。**硬超时,绝不允许无限阻塞。**

    为什么不用 subprocess.run(timeout=...):实测它在"远端命令挂住 + 管道被子进程持有"
    的形态下会无限期阻塞,timeout 形同虚设。真实代价:整轮跑批被钉死 93 分钟,期间
    loadGeneratorFloodHomepage 一直开着,靶子白泡在洪峰里。所以改成显式 Popen + kill,
    ssh 侧再加 BatchMode / ServerAlive 三重保底——跑批卡死比跑批失败糟糕得多。"""
    proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
         SSH_HOST, "python3", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(input=code, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:                      # 收尸也设上限:再卡住就直接放弃管道,不再阻塞
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise RuntimeError(f"靶机操作超时({timeout_s}s),已强杀 ssh") from None
    if proc.returncode != 0:
        raise RuntimeError(f"靶机操作失败: {(err or out).strip()[:300]}")
    return out.strip()


def read_flags() -> list[dict]:
    out = []
    for line in _ssh_py(_READ_TMPL.replace("__FLAGD__", FLAGD_PATH)).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out.append({"flag": parts[0], "current": parts[1], "variants": parts[2].split(",")})
    return out


def inject(flag: str, variant: str) -> None:
    """改 flagd 的 defaultVariant。flagd 的文件监听会热加载,不重启容器。"""
    # 值来自我们自己的题库,仍然过一遍白名单——这条命令最终会写靶机的文件
    if not re.fullmatch(r"[A-Za-z0-9_]+", flag or ""):
        raise ValueError(f"非法开关名:{flag!r}")
    if not re.fullmatch(r"[A-Za-z0-9_%]+", variant or ""):
        raise ValueError(f"非法变体:{variant!r}")
    code = (_INJECT_TMPL.replace("__FLAGD__", FLAGD_PATH)
            .replace("__FLAG__", flag).replace("__VARIANT__", variant))
    _ssh_py(code)


def snapshot_flags() -> None:
    """开跑前给 flagd 文件留一份干净快照,restore 从它整文件还原。

    为什么:注入要摘 targeting(productCatalogFailure 的 targeting 恒为 off,不摘注入
    等于没注),而 restore 若只复位 defaultVariant,targeting 就永远丢了——靶子是被
    别人共享的,改了人家文件不还原是挖坑。快照只在没有且当前干净时创建。"""
    _ssh_py(_SNAPSHOT_TMPL.replace("__FLAGD__", FLAGD_PATH))


def restore(tries: int = 3) -> None:
    """把靶子全部开关复位为 off。**重试到成功为止,不允许静默跳过**——
    带着上一题的开关进下一题,整轮数据就废了,比直接中止跑批更糟(上一轮就是这么
    让靶子白泡了 93 分钟洪峰)。"""
    last: Exception | None = None
    for _ in range(tries):
        try:
            _ssh_py(_RESTORE_TMPL.replace("__FLAGD__", FLAGD_PATH))
            return
        except Exception as exc:  # noqa: BLE001  重试,最后一次仍失败才抛
            last = exc
            time.sleep(2)
    raise RuntimeError(f"复位靶子失败(已重试 {tries} 次):{last}")


# ---------- 组间清场:重启靶场应用容器 ----------
# 为什么必须做(2026-10-16 实测):故障结束后**资源不回落**。ad 被 ad* 故障顶到 287 MB
# (自身历史中位 148 MB)之后就一直在那儿,而基线洗刷只洗历史、洗不掉"现在的水平",
# 于是 ad 被永久顶成锚点,抢走了后面 4 组里 3 组的锚点(其中 3 组真因根本不是 ad)。
# 故障注入基准必须每题从干净靶场开始,否则量的是"上一题的余波"。
# 只重启应用容器:不动观测栈(jaeger/prometheus/loki/collector),也不动 load-generator
# ——它是流量源,重启有把浏览器搞卡的风险(实测 loadGeneratorFloodHomepage 那组就把
# 全站流量打到 0.8/s,自己恢复要几分钟)。
# 靶机上没有重启类指标(container_start_time_seconds 等都不存在),所以重启不会
# 误触发"窗内重启"那条判据——这一点实测确认过。
RESET_CONTAINERS = ("ad", "cart", "checkout", "currency", "email", "flagd", "frontend",
                    "frontend-proxy", "image-provider", "payment", "product-catalog",
                    "quote", "recommendation", "shipping")
_RESET_TMPL = """
import subprocess
r = subprocess.run(["sudo", "-n", "docker", "restart", *"__NAMES__".split()],
                   capture_output=True, text=True, timeout=240)
print((r.stdout or "").strip() or (r.stderr or "").strip()[:200])
"""


def _settle_seconds(settle: int) -> int:
    """显形等待:必须盖住判据读取的当前窗口(粗筛用的是 rate(...[300s]))。

    故障刚起 3 分钟时,5 分钟窗口里只有 3/5 是故障期,测出来的错误率被稀释到六成,
    刚好卡在门槛下——实测 2026-10-17 21:39:payment 注入 3 分钟后测到 5%,而 13 分钟
    后同样的判据测到 85%,门槛是 13.8%。这不是判据不稳,是**采样窗口还没被故障填满**。
    与洗窗同一条道理:等待时间必须覆盖判据真正读取的窗口长度。
    """
    from app.diagnosis.detect import SCAN_WINDOW_S
    return max(settle, SCAN_WINDOW_S)


def _wash_seconds(recover: int, settle: int) -> int:
    """洗窗时长:必须盖住诊断**真正读取**的最近窗口。

    采集段的第一档回看就是 collect.DEFAULT_LOOKBACKS[0] 分钟,而重启靶子发生在这段
    等待的开头。如果 recover+settle 短于这个窗口,重启瞬间的 503/连接拒绝会留在诊断
    窗口里,被 agent 当成故障证据(实测 2026-10-17:adHighCpu 那轮 agent 的推理整段在讲
    frontend-web 的 connection refused,与本题毫无关系——那是开跑时全量重启留下的)。

    这里取"窗口长度 - 显形等待",宁可每组多等两三分钟,也不给诊断喂自己制造的噪音。
    """
    from app.diagnosis.collect import DEFAULT_LOOKBACKS
    need = DEFAULT_LOOKBACKS[0] * 60 - settle
    return max(recover, need)


def reset_platform(services: tuple[str, ...] | None = None) -> None:
    """重启靶场应用容器,清掉"故障结束后资源不回落"的漂移。

    services 给定时只重启这些(组间清场只重启本题的真因服务——全量重启 14 个容器
    要 1-3 分钟,12 组白等几十分钟;而漂移的源头就是被注入的那个服务)。
    清场失败**不中止跑批**(清场是增强),但会如实打印——带着脏靶子跑出来的分数
    自己心里要有数。"""
    names = RESET_CONTAINERS if services is None else services
    try:
        _ssh_py(_RESET_TMPL.replace("__NAMES__", " ".join(names)), timeout_s=300)
        print(f"  [清场] 已重启 {'、'.join(names)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 清场失败(继续跑,但分数可能被漂移污染):{exc}", flush=True)


# ---------- 诊断侧 ----------

async def diagnose(timeout_s: int = DIAG_TIMEOUT_S) -> dict:
    """跑一次完整诊断。service 留空 = 走"平台粗筛找锚点"那条路——这是 AIOpsLab
    localization 题的忠实对应(题目不给症状服务,只要求"找出坏在哪")。"""
    from app.diagnosis import runner
    diag_id = runner.start("", "", platform=None)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        run = runner.get(diag_id)
        if run and run.get("status") in ("done", "failed"):
            return run
        await asyncio.sleep(2)
    return runner.get(diag_id) or {"id": diag_id, "status": "timeout", "result": None}


# ---------- mitigation:诊断后自动确认根因并派修复 agent ----------

SERVICE_BASE_URL = os.environ.get("OPSPAW_BASE_URL", "http://127.0.0.1:8000")


PROM_URL = os.environ.get("BENCH_PROM", "http://127.0.0.1:19090")


async def _prom_scalar(expr: str, at: float | None = None) -> float | None:
    """查一个标量。拿不到就 None,不抛——校验是增强,不能挡跑批。"""
    import httpx
    params = {"query": expr}
    if at is not None:
        params["time"] = at
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{PROM_URL}/api/v1/query", params=params)
            rows = ((r.json() or {}).get("data") or {}).get("result") or []
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    try:
        return float(rows[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


async def measure_injection(gt: str) -> dict:
    """注入之后,目标服务在遥测里到底动了没有。

    为什么要有:2026-10-24 的 cartFailure,整个 300s 窗口里只有 2 笔调用 cart 失败,
    与基线无差异——题目本身没造出可观测故障,却算我们答错。判分前先把这件事记下来,
    数字摆在那,不替谁开脱,也不让"看不见的故障"混进能力评估。

    只报数字,不判好坏:错误数、时延各给"窗内 / 注入前等长窗"两个值。
    """
    now = time.time()
    base = now - 600
    err = ('sum(increase(traces_span_metrics_calls_total'
           '{service_name="%s",status_code="STATUS_CODE_ERROR"}[300s]))' % gt)
    p95 = ('histogram_quantile(0.95, sum by (le) (rate('
           'traces_span_metrics_duration_milliseconds_bucket'
           '{service_name="%s"}[300s])))' % gt)
    out = {
        "gt_error_win": await _prom_scalar(err),
        "gt_error_base": await _prom_scalar(err, at=base),
        "gt_p95_win": await _prom_scalar(p95),
        "gt_p95_base": await _prom_scalar(p95, at=base),
    }
    ew, eb = out["gt_error_win"], out["gt_error_base"]
    pw, pb = out["gt_p95_win"], out["gt_p95_base"]
    # "没有可见变化"只认两件事同时成立:错误数与基线一样多,时延没升高。
    # 不设倍数阈值——"完全没变"是事实,"涨了 1.2 倍算不算有效"才是判断题,不在这做。
    _err_same = ew is not None and eb is not None and abs(ew - eb) < 1.0
    _lat_flat = (pw is None or pb is None) or pw <= pb * 1.05
    # 错误数必须拿得到;时延拿不到就只按错误判(缺一维不等于"有效")
    out["no_change"] = bool(_err_same and _lat_flat)
    return out


def _http(method: str, path: str, payload: dict | None = None,
          timeout: float = 30.0) -> dict:
    import urllib.request
    req = urllib.request.Request(
        SERVICE_BASE_URL + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def service_reachable() -> bool:
    """修复 agent 在服务端进程里跑(连接器连的是它),跑批进程只做编排。
    服务端不在线时修复环节跳过,检测/定位两项照常出分。"""
    try:
        _http("GET", "/api/repair/approval-mode", timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


async def mitigate(diag_id: str, timeout_s: int = REPAIR_TIMEOUT_S) -> dict:
    """脚本模拟用户确认根因 → 服务端派修复 agent → 轮询到终态。

    为什么走 HTTP 而不是进程内直调:连接器的长轮询会话挂在**服务端进程**的内存里,
    跑批进程里调 gateway.call 永远是"连接器不在线"。诊断结果已落库,服务端能读到,
    派单门槛(确认/根因/在线连接器)全部在服务端侧照常校验。"""
    from app.db import repository
    from app.diagnosis import runner
    # 确认根因:跑批进程内改内存 runner(它是诊断的权威),同时落库给服务端读
    saved = await repository.confirm_diagnosis(diag_id, True, by="bench")
    if saved is None:
        raise RuntimeError("诊断单还没落库,无法确认根因")
    live = runner.get(diag_id)
    if live is not None:
        live["confirmation"] = "confirmed"
    out = _http("POST", f"/api/diagnosis/{diag_id}/repair/run", {})
    repair_id = out["repair_id"]
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        run = _http("GET", f"/api/repair/runs/{repair_id}")
        if run.get("status") in ("done", "failed"):
            return run
        await asyncio.sleep(3)
    return {"id": repair_id, "status": "timeout", "result": {}}


# ---------- 判分(AIOpsLab 口径) ----------

def score_case(case: dict, run: dict) -> dict:
    """AIOpsLab 的判分口径,原样搬:
      detection    —— 它期望一个 "Yes"/"No" 字符串。我们映射:verdict 不是 inconclusive
                      就算"检出故障";对照题(no_op)期望 "No"。
      localization —— 提交服务名列表:完全一致 100%;真根因在列表里记 (1/len)*100%;
                      否则 0。success = 完全一致 或 (是子集且列表长度=1)。
    另记几项我们自己的观察量(故障形态对不对、置信档位、LLM 轮次、耗时),
    它们不进 AIOpsLab 的分,但决定下一步往哪优化。"""
    res = (run or {}).get("result") or {}
    verdict = res.get("verdict")
    root = res.get("root_cause_service")
    gt = case["ground_truth_service"]
    pred = [root] if root else []
    # "检出" = 明确说了有故障。clear(平台正常)/no_data(看不到)/inconclusive(证据不足)
    # 都不算——尤其 no_data 不能算,否则"数据源挂了"会被记成"成功检出故障"。
    detected = verdict in ("root_cause", "critical_failure", "impact", "victims_only")

    rec = {"case_id": case["case_id"], "task": case["task"], "group": case["group"],
           "control": case["control"], "fault_flag": case["fault_flag"],
           "flag_variant": case["flag_variant"],
           "ground_truth": gt, "verdict": verdict, "root_cause": root,
           "fault_type": (res.get("fault_type") or {}).get("type"),
           "fault_type_text": (res.get("fault_type") or {}).get("text"),
           "confidence_tier": (res.get("confidence") or {}).get("tier"),
           "candidates": [c.get("service") for c in (res.get("candidates") or [])],
           "detect_anchor": ((res.get("detect") or {}).get("anchors") or [{}])[0].get("service")
           if (res.get("detect") or {}).get("anchors") else None,
           "refill_rounds": (res.get("refill") or {}).get("count"),
           "gate_passed": (res.get("gate") or {}).get("passed"),
           "falsify_refuted": (res.get("falsify") or {}).get("refuted"),
           "status": (run or {}).get("status"),
           "budget": res.get("budget"),
           "events": len((run or {}).get("events") or []),
           }

    if case["task"] == "detection":
        expected_yes = not case["control"]
        rec["detection_correct"] = (detected == expected_yes)
        rec["detection_accuracy"] = "Correct" if rec["detection_correct"] else "Incorrect"
        rec["success"] = rec["detection_correct"]
    elif case["task"] == "localization":
        if case["control"]:
            # 对照题没有真根因:报出任何根因都算误报
            rec["localization_accuracy"] = 0.0 if root else 100.0
            rec["success"] = not root
            rec["is_subset"] = False
        else:
            exact = pred == [gt]
            subset = gt in pred
            rec["localization_accuracy"] = (100.0 if exact
                                            else (100.0 / len(pred) if subset else 0.0))
            rec["success"] = bool(exact or (subset and len(pred) == 1))
            rec["is_subset"] = bool(subset)
    else:
        rec["localization_accuracy"] = None      # mitigation:我们没有修复 agent,记为 N/A
        rec["success"] = None
    return rec


def _fmt_run_duration(run: dict) -> str:
    evs = (run or {}).get("events") or []
    ts = [e.get("ts") for e in evs if e.get("ts")]
    if len(ts) < 2:
        return "?"
    def _s(x):
        h, m, sec = (int(v) for v in x.split(":"))
        return h * 3600 + m * 60 + sec
    return f"{_s(ts[-1]) - _s(ts[0])}s"


# ---------- 跑批 ----------

def load_cases(only: str | None = None) -> list[dict]:
    if not CASES_PATH.exists():
        sys.exit(f"题库不存在:{CASES_PATH}——先跑 scripts/gen_aiopslab_cases.py")
    cases = json.loads(CASES_PATH.read_text())["cases"]
    if only:
        # 逗号分隔多关键词:补录场景只跑锚点翻车的几组,不必全量
        keys = [k.strip() for k in only.split(",") if k.strip()]
        cases = [c for c in cases
                 if any(k in c["case_id"] or k in (c["group"] or "") for k in keys)]
        if not cases:
            sys.exit(f"--only {only!r} 没匹配到任何题")
    return cases


def group_cases(cases: list[dict]) -> list[tuple[str, list[dict]]]:
    """同一故障开关的 detection / localization 共用一次注入——省一半注入和等待时间。"""
    groups: dict[str, list[dict]] = {}
    for c in cases:
        groups.setdefault(c["fault_flag"] or CONTROL_GROUP, []).append(c)
    return sorted(groups.items(), key=lambda kv: (kv[0] == CONTROL_GROUP, kv[0]))


def cmd_plan(args) -> None:
    groups = group_cases(load_cases(args.only))
    print(f"题库 {CASES_PATH.name}:{len(groups)} 个注入组(共 "
          f"{sum(len(g) for _, g in groups)} 题)\n")
    total = 0.0
    for flag, cs in groups:
        c0 = cs[0]
        if flag == CONTROL_GROUP:
            cost, act = 30.0, "不注入(对照题)"
        else:
            cost = args.settle + args.recover + 30.0
            act = f"注入 {flag}={c0['flag_variant']}"
            if not c0["flag_variant_ok"]:
                act += "  ⚠️ 变体上游不合法,当前题库值可能注入失败"
        total += cost
        print(f"  {flag:<28} {act:<52} GT={c0['ground_truth_service'] or '无故障'} "
              f"| {'/'.join(c['task'] for c in cs)} | ~{cost / 60:.1f}min")
    print(f"\n预计总耗时 ~{total / 60:.0f} 分钟(settle={args.settle}s recover={args.recover}s)")


async def _record_superset() -> None:
    """超集录制:把全平台**所有服务**的 trace 一次性录进当前录制文件。

    为什么必须(真实代价:两轮 90 分钟录制先后报废)——录制原本只存"当时那套判据
    恰好查过的请求",判据一改、锚点一翻,新锚点的 trace 没录过,回放 404 断粮,
    只能重注故障重录一遍。超集录制把"任何锚点都可能查到的 URL"提前录齐
    (全服务 × 与诊断管线同一套 fetch_spans 扩窗参数),之后改判据不再需要重录。
    必须在 net.recording 上下文内调用:冻结时钟保证 URL 与回放逐字节一致;
    此时故障还开着,录到的正是故障现场。"""
    from app.db import repository
    from app.diagnosis import collect
    try:
        rows = await repository.list_targets()
    except Exception as exc:  # noqa: BLE001
        print(f"  超集录制跳过:数据源注册表不可读({type(exc).__name__})", flush=True)
        return
    row = next((r for r in rows if r.enabled), None)
    jaeger = next((s for s in (row.sources or []) if s.get("type") == "jaeger"), None) \
        if row else None
    if jaeger is None:
        print("  超集录制跳过:没有 jaeger 数据源", flush=True)
        return
    services = await collect.list_services(jaeger["url"], jaeger.get("headers"))
    done = 0
    for svc in sorted(services):
        try:
            await collect.fetch_spans(jaeger["url"], jaeger.get("headers"), svc)
            done += 1
        except Exception:  # noqa: BLE001  单服务失败不拖垮录制
            continue
    print(f"  超集录制:已录 {done}/{len(services)} 个服务的 trace", flush=True)


async def _register_injection_window(group: str, t_inject: float, t_restore: float) -> None:
    """把本轮注入窗写进诊断存档,让基线剔除**必然**覆盖它。

    为什么不能依赖诊断自己落窗:基线剔除只认 verdict=root_cause/critical_failure 的
    诊断窗,判失的组(粗筛漏报)不落窗——它的故障期留在 24h 历史分布里,污染同批
    后面所有组的基线(实测:cart 判失一下午,kafka 的窗也没登记,checkout 的基线
    整个晚上都在吃污染)。跑批自己就是注入的权威记录,复位时无条件登记,
    判没判对都不影响下一组的基线干净。"""
    from app.db import base as appdb
    from app.db.models import DiagnosisRun
    rid = "inj" + md5(f"{group}/{t_inject}".encode()).hexdigest()[:13]
    try:
        async with appdb.async_session() as s:
            if await s.get(DiagnosisRun, rid) is not None:
                return
            s.add(DiagnosisRun(
                id=rid, service=group[:128], symptom=f"跑批注入窗({group})",
                status="done", verdict="root_cause", root_cause_service=None,
                events=[], result={"data_quality": {"trace": {
                    "window_start": t_inject - 30, "window_end": t_restore + 30}}}))
            await s.commit()
    except Exception as exc:  # noqa: BLE001  登记失败如实说,不假装排干净了
        print(f"  ⚠️ 注入窗登记失败:{exc}", flush=True)


async def _extend_injection_window(group: str, t_inject: float, t_restore: float) -> None:
    """把已登记的注入窗往后延(复位后流量没回落时用)。

    _register_injection_window 认 group+t_inject 做 id,已存在就早返回——所以延长
    必须单独走更新,不能指望再调一次登记。"""
    from app.db import base as appdb
    from app.db.models import DiagnosisRun
    rid = "inj" + md5(f"{group}/{t_inject}".encode()).hexdigest()[:13]
    try:
        async with appdb.async_session() as s:
            row = await s.get(DiagnosisRun, rid)
            if row is None:
                return
            result = dict(row.result or {})
            dq = dict(result.get("data_quality") or {})
            trace = dict(dq.get("trace") or {})
            trace["window_end"] = max(float(trace.get("window_end") or 0), t_restore + 30)
            dq["trace"] = trace
            result["data_quality"] = dq
            row.result = result
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 注入窗延长失败:{exc}", flush=True)


async def _total_calls() -> float | None:
    """全平台总调用量(次/秒),用来判断"复位之后流量到底回没回来"。
    直接查靶机 Prometheus,不经过应用层——这是跑批自己的观测,不依赖被测系统。"""
    import urllib.parse
    import urllib.request
    host = SSH_HOST.split("@")[-1]
    q = "sum(rate(traces_span_metrics_calls_total[5m]))"
    url = f"http://{host}:9090/api/v1/query?" + urllib.parse.urlencode({"query": q})
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.load(r)["data"]["result"]
        return float(data[0]["value"][1]) if data else None
    except Exception:  # noqa: BLE001  查不到就返回 None,由调用方如实说
        return None


def _loadgen_mem_pct() -> float | None:
    """load-generator 的内存占用百分比(拿不到返回 None)。

    它跑的是 Chromium(浏览器流量),内存会一路涨到限额然后卡死:2026-10-22 夜跑
    实测 1.457GiB/1.465GiB 时,全平台流量从 80 次/秒掉到 0.6,一晚上掉了七次;
    诊断正好落进这些空窗,六道题判"未见异常"——数据里确实什么都没有。
    这是整轮数据不可用的根源,不是判据的问题。"""
    code = ('import subprocess\n'
            'r = subprocess.run(["sudo","-n","docker","stats","--no-stream",'
            '"--format","{{.MemPerc}}","load-generator"],'
            'capture_output=True, text=True, timeout=30)\n'
            'print((r.stdout or "").strip())\n')
    try:
        out = _ssh_py(code, timeout_s=45).strip().rstrip("%")
        return float(out) if out else None
    except Exception:  # noqa: BLE001  量不到就不管,别把跑批搞挂
        return None


def _reset_loadgen() -> None:
    """重启压测机。只清它泄漏的内存,不动配置——重启后浏览器会话重建,流量自然回来。"""
    code = ('import subprocess\n'
            'r = subprocess.run(["sudo","-n","docker","restart","load-generator"],'
            'capture_output=True, text=True, timeout=180)\n'
            'print((r.stdout or "").strip() or (r.stderr or "").strip()[:200])\n')
    _ssh_py(code, timeout_s=200)


async def _stable_calls(minutes: int = 10) -> float | None:
    """注入前流量的**中位数**(最近 N 分钟),不是单点采样。

    单点会被空窗骗。2026-10-22 夜跑里有一题注入前刚好撞上容器重启后的空窗,
    采到 0.5 次/秒(正常是 20),于是复位后的残留检查一直等"回落到 0.65 以下",
    而真实流量 65 —— 空等 73 次,把整轮从 3 小时拖到 3 小时 53 分。
    取中位数后,一两个空窗点影响不到基准。"""
    import statistics
    import urllib.parse
    import urllib.request
    host = SSH_HOST.split("@")[-1]
    q = "sum(rate(traces_span_metrics_calls_total[5m]))"
    end = time.time()
    url = f"http://{host}:9090/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": q, "start": end - minutes * 60, "end": end, "step": 60})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)["data"]["result"]
        vals = [float(v) for _, v in (data[0]["values"] if data else [])]
        return statistics.median(vals) if vals else None
    except Exception:  # noqa: BLE001  查不到就返回 None,由调用方如实说
        return None


async def cmd_run(args) -> None:
    groups = group_cases(load_cases(args.only))
    resume_dir = Path(args.resume_from) if args.resume_from else None
    if resume_dir is not None:
        if not resume_dir.is_dir():
            sys.exit(f"--resume-from 不是目录:{resume_dir}")
        done = _done_groups(resume_dir)
        skipped = [g for g, _ in groups if g in done]
        groups = [(g, cs) for g, cs in groups if g not in done]
        print(f"续跑 {resume_dir.name}:跳过已完成 {len(skipped)} 组"
              f"({'、'.join(skipped) if skipped else '无'}),本次跑 {len(groups)} 组")
        if not groups:
            sys.exit("没有需要续跑的组")
    stamp = resume_dir.name if resume_dir else time.strftime("%Y%m%d-%H%M%S")
    out_dir = resume_dir or (RUNS_DIR / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    print(f"跑批开始 {stamp} | {len(groups)} 组 | settle={args.settle}s recover={args.recover}s")
    print(f"结果目录:{out_dir}\n")

    # 开跑前先把靶子恢复到干净状态:上一轮残留的开关会让第一题直接失真
    restore()
    snapshot_flags()          # 留干净快照:restore 从它整文件还原(含 targeting)
    reset_platform()          # 组间清场:清掉上一轮累积的资源漂移
    _sess_start = time.time()  # 会话起点:收尾时整段登记为已知故障窗(见 finally)
    _settle = _settle_seconds(args.settle)
    _wash = _wash_seconds(args.recover, _settle)
    if not args.skip_recover:
        print(f"[准备] 靶子已复位,等 {_wash}s 让指标窗洗白(含重启留下的 503 余波)…")
        await asyncio.sleep(_wash)

    try:
        # 本轮已注入的 (组名, 注入时刻) 清单:收尾时统一把注入窗延到当前时刻。
        # 为什么:恢复期的流量回落数据同样污染基线,而正常流程的"复位登记+回落延长"
        # 只有跑完才有——跑批被中断(Ctrl-C/杀进程/卡死)时走不到那两步,故障期和
        # 恢复期就留在 24h 历史分布里(实测 2026-10-22 凌晨:loadgen 组被杀,恢复期
        # 40 分钟的高流量漏登记,把下一场的"正常范围"抬到故障水位,粗筛失明)。
        _injected: list[tuple[str, float]] = []
        _plan = [(rep, idx, flag, cs)
                 for rep in range(1, args.repeat + 1)
                 for idx, (flag, cs) in enumerate(groups, 1)]
        for n, (rep, idx, flag, cs) in enumerate(_plan, 1):
            c0 = cs[0]
            head = (f"[{n}/{len(_plan)}] {flag}" if args.repeat == 1
                    else f"[{rep}/{args.repeat}] [{idx}/{len(groups)}] {flag}")
            if flag in HOLDOUT_GROUPS:
                head += "  ← 留出集(只看不调)"
            is_control = flag == CONTROL_GROUP
            t_inject: float | None = None
            base_calls: float | None = None
            residual_dirty = False     # 上一题收尾时靶子没洗干净 → 本题数据不可信
            if is_control:
                print(f"{head} | 对照题(不注入)")
            else:
                # 注入前的流量基准:取最近 10 分钟的中位数,不用单点(单点会撞上空窗)
                base_calls = await _stable_calls()
                t_inject = time.time()
                print(f"{head} | 注入 {flag}={c0['flag_variant']} …", flush=True)
                try:
                    inject(flag, c0["flag_variant"])
                except Exception as exc:  # noqa: BLE001  注入失败如实记账,不假装跑过
                    print(f"  注入失败:{exc}")
                    for c in cs:
                        records.append({"case_id": c["case_id"], "task": c["task"],
                                        "group": c["group"], "control": c["control"],
                                        "ground_truth": c["ground_truth_service"],
                                        "status": "inject_failed", "error": str(exc),
                                        "success": None, "localization_accuracy": None,
                                        "repeat": rep})
                    continue
                # 注入窗**当场登记**,窗尾给保守估计(显形等待 + 诊断超时上限)。
                # 为什么不能等本组跑完再登记(旧做法):跑批被中断(Ctrl-C/杀进程/卡死)
                # 时永远走不到登记那一步,故障期就留在 24h 历史分布里——下一轮同款故障
                # 的"正常范围"被它抬高,判成"见过,正常"(实测 2026-10-17:payment 组
                # 第 2 次注入因上一轮被杀未登记,35% 错误率被判"正常",候选直接为空)。
                # 估计值宁大勿小:多洗几分钟健康数据无害,漏洗故障数据才有害。
                try:
                    await _register_injection_window(
                        c0["group"], t_inject,
                        t_inject + _settle + args.diag_timeout + 60)
                    _injected.append((c0["group"], t_inject))
                except Exception as exc:  # noqa: BLE001  登记是增强,失败不挡跑批
                    print(f"  ⚠️ 注入窗登记失败(下一轮基线可能被污染):{exc}")
                print(f"  等 {_settle}s 让故障在遥测里显形(需盖住 300s 判据窗)…",
                      flush=True)
                await asyncio.sleep(_settle)

            # 判分前先量一下"这题到底有没有造出可观测故障"
            _inj = None
            if not is_control and c0.get("ground_truth_service"):
                _inj = await measure_injection(c0["ground_truth_service"])
                print("  注入效果:错误 %s(基线 %s) | p95 %sms(基线 %sms)%s" % (
                    _inj["gt_error_win"], _inj["gt_error_base"],
                    None if _inj["gt_p95_win"] is None else round(_inj["gt_p95_win"]),
                    None if _inj["gt_p95_base"] is None else round(_inj["gt_p95_base"]),
                    "  ⚠️ 无明显变化" if _inj.get("no_change") else ""), flush=True)

            print("  跑诊断管线…", flush=True)
            t0 = time.time()
            # --record:把这一组诊断发出的**所有上游响应**存下来,之后就能离线反复重跑
            # 整条管线(见 scripts/eval_batch_fast.py)。录制必须发生在故障现场,
            # 因为 Jaeger 是内存存储、历史 trace 拿不回来——只有当场录下来才留得住。
            if args.record:
                from app.diagnosis import net
                with net.recording(Path(args.record) / f"{c0['group']}.json"):
                    run = await diagnose(args.diag_timeout)
                    await _record_superset()
                print(f"  已录制 {net.flush()['recorded']} 条上游响应", flush=True)
            else:
                run = await diagnose(args.diag_timeout)
            wall = round(time.time() - t0, 1)
            for c in cs:
                rec = score_case(c, run)
                rec["wall_s"] = wall
                rec["pipeline_span"] = _fmt_run_duration(run)
                rec["repeat"] = rep
                rec["injection"] = _inj
                rec["residual_dirty"] = residual_dirty
                records.append(rec)
                mark = ("✓" if rec.get("success") else
                        ("—" if rec.get("success") is None else "✗"))
                print(f"  {mark} {c['task']:<13} GT={rec['ground_truth'] or '无故障':<16} "
                      f"→ verdict={rec['verdict']} root={rec['root_cause']} "
                      f"形态={rec['fault_type']} 置信={rec['confidence_tier']} "
                      f"({wall}s)", flush=True)

            # mitigation:诊断定位对 + 用户(此处为脚本)确认后,修复 agent 自主修复。
            # 判分是双条件的:agent 自报 succeeded **且** 靶机 flag 实查复位——
            # 修复执行方自证不算数,靶子现状才是客观判据(与产品验收同一立场)。
            #
            # 2026-10-21:**每组都必须有一条修复记录**。以前只在"定位判对"时跑修复,
            # 定位错的组干脆不记账,于是修复项看起来像"没有题"——修复 agent 的能力
            # 就没被考核。现在定位错的组也记一条 success=False(写明"未派单"的原因),
            # 分数才完整:修复分 = 定位对 **且** 真修好了。
            mits: list[dict] = []
            if not args.no_mitigation and not is_control:
                gt = c0.get("ground_truth_service")
                loc_ok = any(r.get("task") == "localization" and r.get("success")
                             for r in records[-len(cs):])
                mit_base = {"case_id": c0["case_id"] + "_mit", "task": "mitigation",
                            "group": c0["group"], "control": False,
                            "fault_flag": c0["fault_flag"], "ground_truth": gt,
                            "repeat": rep}
                if not loc_ok:
                    print("  ⊘ 修复跳过:localization 未判对(根因不对,修复无从谈起)",
                          flush=True)
                    mits.append({**mit_base, "success": False,
                                 "skip_reason": "定位未判对,没有确认过的根因,未派修复 agent"})
                elif not service_reachable():
                    print("  ⊘ 修复跳过:OpsPaw 服务端不在线(修复 agent 挂在服务端进程)",
                          flush=True)
                    mits.append({**mit_base, "success": None,
                                 "skip_reason": "服务端不在线,未派修复 agent"})
                else:
                    print("  走修复 agent(自动确认根因 → 服务端派单)…", flush=True)
                    t1 = time.time()
                    try:
                        repair_run = await mitigate(run["id"], args.repair_timeout)
                        res = repair_run.get("result") or {}
                        dirty = [f["flag"] for f in read_flags()
                                 if f["current"] != "off" and "off" in f["variants"]]
                        mit = {**mit_base,
                               "repair_id": repair_run.get("id"),
                               "repair_verdict": res.get("verdict"),
                               "repair_action": res.get("action"),
                               "flags_dirty_after": dirty,
                               "success": (res.get("verdict") == "succeeded"
                                           and not dirty),
                               "localization_ok": loc_ok}
                    except Exception as exc:  # noqa: BLE001  修复环节失败如实记账
                        mit = {**mit_base, "error": str(exc)[:200], "success": None}
                    wall_r = round(time.time() - t1, 1)
                    mit["wall_s"] = wall_r
                    mits.append(mit)
                    mark = ("✓" if mit.get("success") else
                            ("—" if mit.get("success") is None else "✗"))
                    print(f"  {mark} {'mitigation':<13} GT={gt or '-':<16} "
                          f"→ repair={mit.get('repair_verdict')} "
                          f"action={mit.get('repair_action')} "
                          f"靶机残留={mit.get('flags_dirty_after', 'n/a')} "
                          f"({wall_r}s)", flush=True)
            records.extend(mits)
            # 修复记录必须落进这一组的 JSON:总表是从各组 JSON 重新汇总的,
            # 只留在内存里就等于没记(2026-10-20 夜跑:6 单修复成功,总表却是 0/0)
            (out_dir / f"{c0['group']}.json").write_text(
                json.dumps({"group": flag, "cases": cs, "run": run,
                            "mitigations": mits}, ensure_ascii=False,
                           indent=1, default=str) + "\n")

            if not is_control:
                restore()
                if t_inject is not None:
                    await _register_injection_window(c0["group"], t_inject, time.time())
                    # 复位 ≠ 恢复:开关关了,流量可能还在高位(压测用户不退、连接池不缩)。
                    # 没回落就把注入窗继续延长,否则这段故障数据会被算进"正常历史",
                    # 下一轮同款故障涨不到那么高,粗筛直接判"见过,正常"。
                    if base_calls:
                        waited = 0
                        settled = False
                        while waited < args.residual_wait:
                            cur = await _total_calls()
                            if cur is None or cur <= base_calls * args.residual_tol:
                                settled = True
                                break
                            print(f"  ⏳ 流量未回落({cur:.1f} vs 注入前 {base_calls:.1f} 次/秒),"
                                  f"继续等并延长注入窗…", flush=True)
                            await asyncio.sleep(60)
                            waited += 60
                        if waited:
                            await _extend_injection_window(c0["group"], t_inject, time.time())
                            print(f"  ✓ 注入窗已延长 {waited}s(这段数据不进基线)", flush=True)
                        if not settled:
                            # 等满上限还没回落:记一笔"残留未清"就往下走。
                            # 2026-10-26 实测:一轮 11 组里 73 次没回落,每轮撞满 1800s,
                            # 把一轮从 2 小时拖到 6 小时;而"总流量"本来就随压测器爬升漂移,
                            # 拿几小时前的固定值当回落标准,永远等不到。
                            residual_dirty = True
                            print(f"  ⚠️ 等满 {waited}s 流量仍未回落,记为「残留未清」,继续下一题", flush=True)
                # 组间清场:只重启本题的真因服务,清掉它的资源漂移(否则它会顶成下一题的锚点)
                _gt = c0.get("ground_truth_service")
                if _gt in RESET_CONTAINERS:
                    reset_platform((_gt,))
                # 压测机守卫:它跑 Chromium,内存会一路涨到限额然后卡死,流量直接归零。
                # 留 20% 余量(和容器 eviction 的常用档位同档),超了就重启它,
                # 下面的洗窗+流量恢复等待会等它重新把流量带起来。
                pct = _loadgen_mem_pct()
                if pct is not None and pct >= LOADGEN_MEM_LIMIT_PCT:
                    print(f"  ⚠️ 压测机内存 {pct:.0f}%(会卡死,流量归零),重启它…", flush=True)
                    _reset_loadgen()
                if not args.skip_recover:
                    print(f"  已复位,等 {_wash}s 洗窗…", flush=True)
                    await asyncio.sleep(_wash)
                    # 洗窗只是**下限**,不是"已经恢复"的证明:重启过的服务要一两分钟才
                    # 把流量带回来,固定等待经常在流量还没回的时候就注入下一题,诊断于是
                    # 跑在空窗里(2026-10-22 夜跑:product-catalog 那题诊断时流量 0.7 次/秒、
                    # 错误率 0,判"未见异常"——数据里确实什么都没有,错的是诊断跑早了)。
                    # 这里再用流量本身确认一次:回到基准的 1/容差 以上才算真恢复。
                    if base_calls:
                        need = base_calls / args.residual_tol
                        waited = 0
                        while waited < args.residual_wait:
                            cur = await _total_calls()
                            if cur is None or cur >= need:
                                break
                            print(f"  ⏳ 流量未恢复({cur:.1f} < {need:.1f} 次/秒),继续等…",
                                  flush=True)
                            await asyncio.sleep(60)
                            waited += 60
                        if waited:
                            print(f"  ✓ 多等了 {waited}s 才恢复", flush=True)
                        if waited >= args.residual_wait:
                            residual_dirty = True
                            print(f"  ⚠️ 等满 {waited}s 流量仍未恢复,记为「残留未清」", flush=True)
    finally:
        # 无论跑批怎么结束,靶子必须回到干净状态——留着开关开着就是给下一个使用者挖坑
        try:
            restore()
            print("\n[收尾] 靶子开关已全部复位")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[收尾] ⚠️ 复位失败,请手工检查靶机 flagd:{exc}")
        # 中断兜底:把本轮所有注入窗延到收尾时刻再下账——中断发生时走不到正常流程的
        # "复位登记+回落延长",恢复期的污染数据就留在基线里了(2026-10-22 实测教训)。
        for _g, _t in _injected:
            try:
                await _extend_injection_window(_g, _t, time.time())
            except Exception as exc:  # noqa: BLE001
                print(f"[收尾] ⚠️ {_g} 注入窗收尾延长失败:{exc}")
        if _injected:
            print(f"[收尾] 已把 {len(_injected)} 个注入窗延到当前时刻(不进基线)")
        # 会话整段登记:从起跑到收尾的**全部时间**(含组间洗窗、流量回落余波、诊断间隔)
        # 都不进基线。为什么光有每组一小段的注入窗不够(2026-10-22 实测):注入窗是
        # "注入时刻+固定估计"的小段,组与组之间的洗窗期和余波期漏在外面——凌晨洪泛组
        # 的余波把全平台 CPU 抬到 3~4 倍,这些窗留在基线里,把 recommendation 的历史
        # max 抬到 0.093,中午真故障(0.044,中位 2.1 倍)反而够不着 max,判"正常"。
        # 跑批期间系统本来就不在稳态,整段剔除没有误伤——这就是等价的"环境重建"。
        try:
            from app.diagnosis import known_incidents
            if known_incidents.register_window(_sess_start, time.time()):
                print(f"[收尾] 会话窗已整段登记({_sess_start:.0f} → {time.time():.0f}),"
                      f"跑批期间的数据不进基线")
        except Exception as exc:  # noqa: BLE001  登记是增强,失败不挡收尾
            print(f"[收尾] ⚠️ 会话窗登记失败:{exc}")

    all_records = _collect_records(out_dir)     # 续跑时把上一轮的记录一并纳入汇总
    (out_dir / "summary.json").write_text(
        json.dumps({"stamp": stamp, "settle_s": args.settle, "recover_s": args.recover,
                    "records": all_records}, ensure_ascii=False, indent=1, default=str) + "\n")
    _print_summary(all_records)
    print(f"\n明细:{out_dir}")


def _trivial_baselines(cases: list[dict]) -> dict:
    """平凡基线:不做任何分析就能拿到的分数。**没有基线的准确率不许上报。**

    为什么必须算(2026-10-14 调研,arXiv 2609.27069 审计 RCAEval 的结论):
    故障注入基准的正负比是倾斜的、注入目标是小集合,所以"永远说有故障""永远猜
    最高频注入服务"这类**不看遥测的排序器**就能拿到不低的分数。论文原话:
    "A model that reports a single high score is therefore uninformative about its own
    behaviour: it may be reading the prior rather than the incident."
    实测:我们的 detection 6/12 看着是 50%,但"永远说有故障"是 11/12 = 91.7%——
    **比平凡基线低 42 个点**。不摆出这个数,6/12 会被误读成"及格"。

    基线定义:
      detection   —— 恒答"有故障"(只有对照组是负例,所以基线 = 正例占比)
      localization—— 恒答出现次数最多的那个 ground truth 服务
    """
    det = [c for c in cases if c["task"] == "detection"]
    loc = [c for c in cases if c["task"] == "localization" and not c.get("control")]
    out: dict = {}
    if det:
        positives = sum(1 for c in det if not c.get("control"))
        out["detection"] = {"baseline": positives / len(det), "rule": "恒答「有故障」",
                            "n": len(det)}
    if loc:
        counts: dict[str, int] = {}
        for c in loc:
            gt = c.get("ground_truth_service")
            counts[gt] = counts.get(gt, 0) + 1
        top, n_top = max(counts.items(), key=lambda kv: kv[1])
        out["localization"] = {"baseline": n_top / len(loc),
                               "rule": f"恒答出现最多的「{top}」({n_top}/{len(loc)} 题)",
                               "n": len(loc)}
    return out


def _print_summary(records: list[dict]) -> None:
    scored = [r for r in records if r.get("success") is not None]
    print("\n" + "=" * 78)
    print("跑批汇总(AIOpsLab 口径)")
    print("=" * 78)
    # 平凡基线:同一次跑批的题量算出来,和实际成绩并排显示
    try:
        base = _trivial_baselines(load_cases())
    except SystemExit:
        base = {}
    for task in ("detection", "localization", "mitigation"):
        rows = [r for r in scored if r["task"] == task]
        if not rows:
            continue
        ok = sum(1 for r in rows if r.get("success"))
        accs = [r["localization_accuracy"] for r in rows
                if r.get("localization_accuracy") is not None]
        avg = f",平均准确率 {sum(accs) / len(accs):.1f}%" if accs else ""
        line = f"{task:<14} 成功 {ok}/{len(rows)}{avg}"
        b = base.get(task)
        if b:
            b_ok = round(b["baseline"] * len(rows))
            line += (f"   | 平凡基线 {b_ok}/{len(rows)}({b['baseline']:.1%})"
                     f"「{b['rule']}」")
        print(line)
        if task == "mitigation":
            # 修复分要分清两件事:分母是"每组都要有修复成绩",分子里还得看出
            # "真正派了单的有几单、成了几单"——定位没对的组压根没派单,不该混着看
            sent = [r for r in rows if r.get("repair_id")]
            if sent:
                s_ok = sum(1 for r in sent if r.get("success"))
                print(f"{'':<14} 其中实际派单 {len(sent)} 单,成功 {s_ok}/{len(sent)}")
    # 注入没造出可观测故障的题:目标服务错误数与基线一样、时延没升。
    # 单列出来并给一个剔除后的定位分——"题目没造出故障"不该算成"我们答错",
    # 但也不替谁开脱,两个数都摆出来。
    # 系统自己判"平台正常"的题:一个异常维都没越过判据线。这类题的定位分说明不了
    # 能力——要么故障太弱,要么我们全瞎;两种情况都不该混进分母装成"答错"。
    blind = [r for r in scored if r.get("verdict") == "clear"]
    if blind:
        print(f"其中 {len(blind)} 题系统判「平台正常」(一个异常维都没越线):"
              + "、".join(r["group"] for r in blind))
    # 上一题没洗干净就开跑的题:基线里带着别人的故障,结论不可信
    dirty = [r for r in scored if r.get("residual_dirty")]
    if dirty:
        print(f"其中 {len(dirty)} 题开跑时靶子未洗干净(上一题残留):"
              + "、".join(r["group"] for r in dirty))
    no_sig = [r for r in scored if (r.get("injection") or {}).get("no_change")]
    if no_sig:
        print(f"其中 {len(no_sig)} 题注入后 GT 服务无可见变化(错误数与基线相同、时延未升):"
              + "、".join(r["group"] for r in no_sig))
        loc = [r for r in scored if r["task"] == "localization"]
        adj = [r for r in loc if r not in no_sig]
        if adj:
            print(f"{'':<14} 剔除这些题后 localization "
                  f"{sum(1 for r in adj if r.get('success'))}/{len(adj)}")

    failed = [r for r in records if r.get("success") is None]
    if failed:
        print(f"未计分 {len(failed)} 题:" + "、".join(r["case_id"] for r in failed))
    # 重复跑:每组成功率(单次结果说明不了能力,波动必须摆出来)
    reps = [r.get("repeat") for r in scored]
    if any(x not in (None, 1) for x in reps):
        print("\n每组成功率(重复跑):")
        by_group: dict[str, list[dict]] = {}
        for r in scored:
            by_group.setdefault(r.get("group") or "?", []).append(r)
        for g, rs in sorted(by_group.items()):
            ok = sum(1 for r in rs if r.get("success"))
            tag = "  [留出集]" if g in HOLDOUT_GROUPS else ""
            print(f"  {g:<38s} {ok}/{len(rs)} = {ok/len(rs):.0%}{tag}")
    # 错例清单——校准就是对着这张表改拍值
    miss = [r for r in scored if not r.get("success")]
    if miss:
        print("\n错例(下一步校准的输入):")
        for r in miss:
            gt = r.get("ground_truth") or "无故障"
            if r.get("task") == "mitigation":
                # 修复记录没有 verdict/root_cause 字段,单独一版(不单独处理会 KeyError)
                print(f"  {r['case_id']:<58} GT={gt:<16}"
                      f" 修复={r.get('repair_verdict') or r.get('skip_reason') or '未派单':<24}"
                      f" 动作={r.get('repair_action') or '-'}")
                continue
            print(f"  {r['case_id']:<58} GT={gt:<16}"
                  f" 实得={r.get('root_cause') or 'None':<18}"
                  f" verdict={r.get('verdict') or 'None':<17}"
                  f" 粗筛锚点={r.get('detect_anchor')}")


def _collect_records(run_dir: Path) -> list[dict]:
    """把一轮跑批目录里的所有组结果重新判分。判分口径改了不用重跑,跑批中断也能出部分结果。"""
    records: list[dict] = []
    for path in sorted(run_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        blob = json.loads(path.read_text())
        for case in blob.get("cases") or []:
            records.append(score_case(case, blob.get("run") or {}))
        # 修复记录单列一份(不在 cases 里):漏了这段,总表就只剩检测/定位
        records.extend(blob.get("mitigations") or [])
    return records


def _done_groups(run_dir: Path) -> set[str]:
    """跑批目录里已经完成的注入组(文件名即组键)。续跑靠它跳过已完成的组。"""
    return {json.loads(p.read_text()).get("group")
            for p in run_dir.glob("*.json") if p.name != "summary.json"}


def cmd_report(args) -> None:
    """离线重算一轮跑批的判分——跑批中断也能出部分结果,改判分口径不用重跑。"""
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        cands = sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()) if RUNS_DIR.exists() else []
        if not cands:
            sys.exit(f"{RUNS_DIR} 下没有跑批记录")
        run_dir = cands[-1]
    if not run_dir.is_dir():
        sys.exit(f"不是目录:{run_dir}")

    done = _done_groups(run_dir)
    records = _collect_records(run_dir)
    total = len(group_cases(load_cases()))
    print(f"跑批目录:{run_dir}")
    print(f"已完成 {len(done)} 组 / 题库共 {total} 组"
          + ("(未跑完,以下为部分结果)" if len(done) < total else ""))
    if not records:
        sys.exit("该目录下没有可判分的记录")
    print()
    print(f"{'题组':<34}{'任务':<13}{'GT':<17}{'实得根因':<18}{'形态':<17}{'置信':<6}结果")
    print("-" * 118)
    for r in sorted(records, key=lambda x: (x["group"], x["task"])):
        if r["task"] == "mitigation":
            continue
        print(f"{r['group']:<34}{r['task']:<13}{str(r['ground_truth']):<17}"
              f"{str(r['root_cause']):<18}{str(r['fault_type']):<17}"
              f"{str(r['confidence_tier']):<6}{'✓' if r.get('success') else '✗'}")
    print()
    _print_summary(records)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("用法")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="看靶子当前开关状态")
    sub.add_parser("restore", help="把靶子全部开关复位为 off")

    p_rep = sub.add_parser("report", help="离线重算某轮跑批的判分(中断也能出部分结果)")
    p_rep.add_argument("run_dir", nargs="?", default=None,
                       help="跑批目录(缺省取 data/aiopslab/runs 下最新一轮)")

    p_plan = sub.add_parser("plan", help="看跑批计划(不执行)")
    p_inj = sub.add_parser("inject", help="只注入一道题(手工排查用)")
    p_inj.add_argument("case_id", help="题库里的 case_id 或故障组名")

    for p in (p_plan,):
        p.add_argument("--only", default=None, help="只跑匹配该关键词的题")
        p.add_argument("--settle", type=int, default=SETTLE_S)
        p.add_argument("--recover", type=int, default=RECOVER_S)

    p_run = sub.add_parser("run", help="跑批")
    p_run.add_argument("--only", default=None, help="只跑匹配该关键词的题")
    p_run.add_argument("--resume-from", default=None, metavar="RUN_DIR",
                       help="续跑:跳过该目录里已完成的组,结果并回同一目录")
    p_run.add_argument("--record", default=None, metavar="DIR",
                       help="把每组诊断的上游响应录到该目录(供离线快速重跑整条管线)")
    p_run.add_argument("--settle", type=int, default=SETTLE_S,
                       help=f"注入后等故障显形(默认 {SETTLE_S}s)")
    p_run.add_argument("--recover", type=int, default=RECOVER_S,
                       help=f"恢复后等洗窗(默认 {RECOVER_S}s)")
    p_run.add_argument("--repeat", type=int, default=1,
                       help="每组重复跑几次(默认 1)。>1 时汇总会报每组的成功率,"
                            "用来判断波动——单次结果不足以说明能力")
    p_run.add_argument("--diag-timeout", type=int, default=DIAG_TIMEOUT_S)
    p_run.add_argument("--residual-tol", type=float, default=RESIDUAL_TOL,
                       help="复位后流量回落到注入前的多少倍以内才算洗干净(默认 1.2)")
    p_run.add_argument("--residual-wait", type=int, default=RESIDUAL_MAX_WAIT_S,
                       help="流量没回落时最多等多久(秒,默认 600);等满就记「残留未清」继续下一题")
    p_run.add_argument("--no-mitigation", action="store_true",
                       help="跳过修复环节(只考检测/定位;默认考三项)")
    p_run.add_argument("--repair-timeout", type=int, default=REPAIR_TIMEOUT_S,
                       help=f"修复 agent 派单到回执的超时(默认 {REPAIR_TIMEOUT_S}s)")
    p_run.add_argument("--skip-recover", action="store_true",
                       help="跳过等待(调试用;会引入跨题污染,结果仅供参考)")

    args = ap.parse_args()
    if args.cmd == "status":
        print(f"{'开关':<30}{'当前':<10}可选档位")
        for f in read_flags():
            mark = "  ← 正在注入" if f["current"] != "off" else ""
            print(f"{f['flag']:<30}{f['current']:<10}{','.join(f['variants'])}{mark}")
    elif args.cmd == "restore":
        restore()
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "inject":
        cases = load_cases(args.case_id)
        c = cases[0]
        if not c["fault_flag"]:
            sys.exit(f"{c['case_id']} 是对照题,没有开关可注入")
        inject(c["fault_flag"], c["flag_variant"])
        print(f"已注入 {c['fault_flag']}={c['flag_variant']}(GT={c['ground_truth_service']})")
    elif args.cmd == "plan":
        cmd_plan(args)
    else:
        asyncio.run(cmd_run(args))


if __name__ == "__main__":
    main()
