"""诊断任务编排:顺序 async 函数,事件流进内存 store,前端卡片轮询 GET /api/diagnosis/{id}。

阶段:检测(没给服务名时先扫平台)→ 采集 → 拓扑+挂死 → 定位 → LLM 归因 → 信号门禁
→ 代码证伪 → 故障形态 → 置信 → 开方。终态落库;单次诊断受查询次数/总时长预算约束,
且全局限并发(诊断工具不能把客户的监控打挂)。
"""
import asyncio
import logging
import time
import uuid
from dataclasses import replace

from app.db import repository
from app.diagnosis import (collect, confidence, detect, deviation, edge_anomaly, edgeview,
                           evidence, faulttype, flagchange, localize, logsig, stats, topology)
from app.diagnosis import opsagent_adapter
from app.diagnosis import narrative as narrative_mod

logger = logging.getLogger("app")

_RUNS: dict[str, dict] = {}
MAX_RUNS = 100   # 内存 store 上限:终态已有 DB 存档,内存只留最近的,防长期运行泄漏

# 单次诊断的资源闸(商用底线:诊断工具不能把客户的监控打挂)。
# 查询次数与总时长谁先到算谁;并发闸保证几个人同时点诊断不会变成 N 倍流量。
# ⚠️ 三个都是**拍值**(按"够用且不伤上游"的工程余量估的,没有实测依据)。
# 调整语义:调大 → 结论更全但更慢更重;调小 → 更快更轻但可能证据不全(会如实记进缺口清单)。
# 90 的由来:候选不再截断(全部异常服务都给证据预取,每候选约 3 条查询)+ 画像复用
# 省下的 ~12 条——上限跟着候选规模走,否则预算会在证据采集半路烧干(实测 3 个候选
# 就触顶 61 次,后半程证据全标"无数据")。
DIAG_MAX_QUERIES = 90
DIAG_MAX_SECONDS = 300
MAX_CONCURRENT_DIAGNOSES = 3
_SEM = asyncio.Semaphore(MAX_CONCURRENT_DIAGNOSES)

# 判挂死的静默门槛**不写死**。界线 = max(该服务自己观测到的最长间隔, 典型间隔 × ln(1/α)):
#   前一项是"比我见过的任何一次间隔都长"(数据自证);
#   后一项是"间隔近似指数分布时,这么长的静默出现概率 < α"(由目标误报率反解,见 stats)。
# 样本下限 = 估中位数所需的最小观测数(见 stats.min_samples_for_median),低于它间隔的
# 典型值估不准,宁缺勿错(不判)。
HANG_MIN_HISTORY = stats.min_samples_for_median()


def _target_silence(spans: list, service: str, window_end: float) -> dict | None:
    """目标服务是否已停止出遥测(疑似挂死):自身最新 span 距今超过
    max(自身最长间隔, 间隔中位数 × ln(1/α))。
    停机服务的旧 span 仍躺在采样窗里,四维偏离看着正常——单看旧数据会对
    "整个服务挂死"永久失明(真实案例:payment 已停,旧 span 照常过健康检查)。"""
    import statistics
    own = sorted(sp.start_us for sp in spans if sp.service == service)
    if len(own) < HANG_MIN_HISTORY:
        return None
    silence_s = window_end - own[-1] / 1e6
    gaps = [b - a for a, b in zip(own, own[1:])]
    med_gap_s = (statistics.median(gaps) / 1e6) if gaps else 0.0
    max_gap_s = (max(gaps) / 1e6) if gaps else 0.0
    fence_s = max(max_gap_s, med_gap_s * stats.tail_multiple())
    if silence_s <= fence_s:
        return None
    return {"silence_s": round(silence_s), "median_gap_s": round(med_gap_s, 1),
            "fence_s": round(fence_s, 1), "last_own_us": own[-1]}


def _points_to(sp, service: str) -> bool:
    """失败 span 是否指向目标服务:peer 属性或操作名段级匹配(与影子指认同一口径)。"""
    from app.diagnosis.localize import _op_points_to
    return sp.peer == service or _op_points_to(sp.operation, service)


def _plain_narrative(desc: str, fault: str, reason: str, other: str) -> dict:
    """没结论时的开方卡内容,字段与成功路径一致。

    为什么要给失败也写这段:成功时用户能看到四段话(描述/结论/依据/下一步),
    失败时却只剩一句「证据不足,未定位根因」——**跑了两分钟,什么交付物都没有**。
    而失败恰恰是最需要"你查了什么、为什么没查出来"的时候。
    """
    return {"description": desc, "fault": fault, "reason": reason, "other": other}


def _emit(run: dict, stage: str, detail: str) -> None:
    run["events"].append({"stage": stage, "detail": detail,
                          "ts": time.strftime("%H:%M:%S")})
    run["stage"] = stage


def _profile_line(svc: str, p: dict) -> str:
    """把一条指标画像翻成人话证据(trace 是抽样,指标是连续量,这条补前者)。"""
    parts = [f"{svc} 指标窗调用 {p['calls']:.3f} 次/秒"]
    if p.get("baseline"):
        parts.append(f"基线({p['baseline']}) {p['base_calls']:.3f} 次/秒")
    if p.get("err_rate") is not None:
        parts.append(f"错误率 {p['err_rate']:.1%}")
        if p.get("base_err_rate") is not None:
            parts.append(f"基线错误率 {p['base_err_rate']:.1%}")
    if p.get("p95_ms") is not None:
        parts.append(f"p95 {p['p95_ms']:.0f}ms")
        if p.get("base_p95_ms") is not None:
            parts.append(f"基线 p95 {p['base_p95_ms']:.0f}ms")
    if p.get("traffic_collapse"):
        parts.append("调用量相对基线塌陷(服务失联/挂死的实锤)")
    return "、".join(parts) + "(Prometheus span 指标,连续量,非 trace 抽样)"


async def _cluster_of(platform: str | None) -> str:
    """平台名 → 所属多云集群名(单云返回空串)。用户直接点名服务时,平台是现查的,
    集群得跟着补上,否则归因/修复拿不到"这是哪套系统的哪一朵云"。"""
    if not platform:
        return ""
    try:
        for row in await repository.list_targets():
            if row.name == platform:
                return getattr(row, "cluster", "") or ""
    except Exception:  # noqa: BLE001 取不到就当单云,不影响诊断
        return ""
    return ""


async def _resolve_sources(service: str,
                           platform: str | None = None) -> tuple[dict | None, dict | None, str | None, str]:
    """复用服务发现确认存在性,并取该服务所属平台的 prometheus/jaeger 数据源。

    platform 给定时按"平台+服务名"定位——多平台可以有同名服务,靠"先发现的胜出"会诊错平台。
    不给 platform 且裸名跨平台歧义时**不猜**,如实报错让调用方指明。

    第 4 个返回值是**归一化后的服务名**:调用方可能传"平台/服务名"(前端目录里就是这么标的),
    而 Jaeger/Prometheus 里的 service_name 是裸名。拿带前缀的串去查,上游不报错、只回空
    (2026-10-22 实测:诊断存了 service='Otel-demo/product-catalog',四档窗口全 0 条 span,
    整条诊断停在"未找到 trace")。所以解析成功后一律用 hit 里的裸名往下走。"""
    from app.tools import service_catalog
    services, err = await service_catalog.discover_services()
    if err:
        return None, None, f"服务发现不可达:{err}", service
    hit = service_catalog.find_service(services, service, platform)
    if hit is None:
        dup = [s["key"] for s in services if s["service_id"] == service]
        if len(dup) > 1 and not platform:
            return None, None, f"「{service}」在多个平台上都有(" + "、".join(dup) + "),需要指明平台", service
        return None, None, f"接入平台中未发现服务「{service}」", service
    resolved = str(hit.get("service_id") or service)
    rows = await repository.list_targets()
    row = next((r for r in rows if r.enabled and r.name == hit.get("target")), None)
    if row is None:
        return None, None, f"平台「{hit.get('target')}」不存在或已停用", resolved
    src = {s["type"]: s for s in (row.sources or [])}
    prom = src.get("prometheus")
    jaeger = src.get("jaeger")
    if not prom or not jaeger:
        return None, None, "该平台未同时接入 Prometheus 与 Jaeger,证据不全", resolved
    return ({"url": prom["url"], "headers": prom.get("headers")},
            {"url": jaeger["url"], "headers": jaeger.get("headers")}, None, resolved)


async def _loki_for(service: str, platform: str | None = None) -> tuple[dict | None, str | None]:
    """补查用的 Loki 解析:返回 (loki 源, 平台名)。发现逻辑与日志证据段同一套,
    不复用那段内联块是因为两处对「服务没发现」的处置不同(那里要保持 not_needed 语义)。"""
    from app.tools import service_catalog
    services, err = await service_catalog.discover_services()
    if err:
        return None, None
    hit = service_catalog.find_service(services, service, platform)
    if hit is None:
        return None, None
    rows = await repository.list_targets()
    target = next((r for r in rows if r.enabled and r.name == hit.get("target")), None)
    if target is None:
        return None, None
    loki = next((s for s in (target.sources or []) if s.get("type") == "loki"), None)
    return loki, target.name


def start(service: str, symptom: str, conversation_id: int | None = None,
          platform: str | None = None) -> str:
    diag_id = uuid.uuid4().hex[:12]
    run = {"id": diag_id, "service": service, "symptom": symptom,
           "conversation_id": conversation_id, "platform": platform,
           "status": "running", "stage": "queued", "events": [], "result": None,
           "confirmation": "pending",
           "created_at": time.strftime("%H:%M:%S")}
    _RUNS[diag_id] = run
    if len(_RUNS) > MAX_RUNS:   # 先淘汰终态(已落库),不够再淘汰最旧——内存只做热缓存
        terminal = [k for k, v in _RUNS.items() if v.get("status") in ("done", "failed")]
        for k in terminal[:len(_RUNS) - MAX_RUNS]:
            _RUNS.pop(k, None)
    asyncio.create_task(_safe_run(run, service, symptom, platform))
    return diag_id


async def _safe_run(run: dict, service: str, symptom: str,
                    platform: str | None = None) -> None:
    try:
        # 并发闸:同时最多放 N 单,超出的在这里排队——几个值班同学一起点诊断,
        # 不该变成 N 倍流量一起砸到客户的 Prometheus/Jaeger 上。
        async with _SEM:
            collect.budget_start(DIAG_MAX_QUERIES, DIAG_MAX_SECONDS)
            try:
                run["result"] = await _run(run, service, symptom, platform)
            finally:
                budget = collect.budget_report()
                collect.budget_reset()
        if isinstance(run["result"], dict):
            run["result"]["budget"] = budget
            if budget["exhausted"]:
                # 预算用完了要如实说——结论偏保守的原因得看得见,不能悄悄降级
                run["events"].append({
                    "stage": run.get("stage", "done"), "ts": time.strftime("%H:%M:%S"),
                    "detail": f"查询预算已用完({budget['used']}/{budget['max_queries']} 次),"
                              "已就现有数据出结论,可能偏保守"})
                run["result"].setdefault("missing", []).append(
                    f"本次诊断触及查询预算上限({budget['used']} 次),部分证据未采集完整,结论偏保守")
        run["status"] = "done"
    except Exception as e:  # noqa: BLE001  诊断任务不许静默死,错误如实落终态
        logger.exception("诊断任务失败 service=%s", service)
        run["status"] = "failed"
        _emit(run, "failed", f"诊断管线异常:{type(e).__name__}: {e}")
    finally:
        # 终态落库:内存 store 重启即丢,存档后卡片刷新/跨进程回放都拿得到结论
        try:
            await repository.save_diagnosis_run(run)
        except Exception:  # noqa: BLE001  落库失败已在 save 内告警,这里再兜一层
            logger.warning("诊断任务 %s 存档异常", run.get("id"), exc_info=True)
        try:
            from app.api import incidents
            await incidents.on_diagnosed(run.get("id", ""), run)
        except Exception:  # noqa: BLE001  事故联动失败不影响诊断结果
            logger.warning("事故联动(诊断)失败 diag=%s", run.get("id"), exc_info=True)


async def _run(run: dict, service: str, symptom: str,
               platform: str | None = None) -> dict:
    now = time.time()
    detect_info: dict | None = None
    _scan_profiles_map: dict[str, dict] = {}   # 粗筛扫描的画像(平台名→画像);直接指定服务时为空

    # ①.5 检测层:用户说不清服务时(service 为空)先便宜地扫全平台找异常锚点,挑最可疑的
    # 起手诊断。运维不该被要求先自己定位到服务——"我要是知道,要你干什么"。
    if not service:
        _emit(run, "detect", "未指定服务,先扫描平台找异常…")
        detect_info = await detect.scan_platforms(platform=platform)
        # 扫描时的全平台画像就地取走(含历史分布内部对象,只在本进程内传):
        # 同一次诊断里"谁异常"只算一次——之前扫描算一遍、进诊断又重算一遍,两次隔
        # 几分钟,临界时候说翻脸就翻脸(实测:recommendationCacheFailure 扫描时
        # recommendation 时延超历史 p99,诊断时重算没超,真因从异常名单上掉了)。
        # 不 pop 的话画像里的 Distribution 对象会跟着 detect 进序列化路径炸掉存档。
        _scan_profiles_map = detect_info.pop("profiles", None) or {}
        if detect_info["status"] == "no_data":
            note = detect_info["note"]
            _emit(run, "failed", note)
            return {"verdict": "no_data", "root_cause_service": None, "chain": [],
                    "confidence_note": note, "missing": [note],
                    "suggested_actions": ["检查接入平台与 Prometheus 数据源是否可达"],
                    "narrative": _plain_narrative(
                        "这一轮没取到数据——不是「没发现故障」,是「看不到」。",
                        "结论:数据不可用,无法判断平台有没有故障。",
                        note,
                        "先确认 Prometheus / Jaeger 是否可达、指标名对不对得上;"
                        "数据源修好后重跑一次。"),
                    "detect": detect_info}
        if detect_info["status"] == "clear":
            note = detect_info["note"]
            _gaps = detect_info.get("gaps") or []
            if _gaps:
                # 有维度查询失败时不能说成纯健康——缺的那个维对应的故障类型本轮看不见
                _emit(run, "done", f"诊断完成:平台粗筛未见异常,但有维度查询失败({note})")
                _conf = ("粗筛部分维度查询失败,其余维度未见异常;缺失维度对应的故障类型"
                         f"本轮可能漏报({note})")
            else:
                _emit(run, "done", f"诊断完成:平台粗筛未见异常({note})")
                _conf = "全平台粗筛未见异常:各服务与自身历史分布比都在正常范围"
            return {"verdict": "clear", "root_cause_service": None, "chain": [],
                    "confidence_note": _conf,
                    "missing": [note], "suggested_actions": [],
                    "narrative": _plain_narrative(
                        "全平台每个服务的调用量、错误率、时延、资源四个维度,"
                        "都与它自己的历史比在正常范围内。",
                        "结论:这一轮没发现异常。",
                        _conf,
                        "如果用户侧确实有症状,说明症状还没进遥测(流量空窗或采集延迟),"
                        "过几分钟再跑一次;反复正常但用户仍在报障,就需要人工核对采集链路。"),
                    "detect": detect_info}
        if detect_info["status"] == "victims_only":
            # 平台有异常,但最高只到"纯时延中继"层——异常都能被下游解释,证据指向的是
            # 受害者而不是源头。**不能硬从受害者里挑最响的当锚点**(那正是"零故障却报出
            # high 置信根因"的成因),也不能当"健康"放行——如实说"看到了但指不出源头"。
            note = detect_info["note"]
            _emit(run, "done", f"诊断完成:发现异常但只指向受害者({note})")
            return {"verdict": "victims_only", "root_cause_service": None, "chain": [],
                    "confidence_note": "平台存在异常,但当前证据只指向被下游带慢的受害者,"
                                       "未定位到源头",
                    "missing": [note],
                    "suggested_actions": ["核对上下游调用链,确认被指服务的下游是否有"
                                          "未接入遥测的组件(无 span 的中间件是已知盲区)"],
                    "narrative": _plain_narrative(
                        "看到了异常,但这些异常都能被调用链上的邻居解释——它们是受害者。",
                        "结论:平台确实有故障,但当前证据指不出源头。",
                        note,
                        "顺着调用链往被指服务的下游查,重点看有没有没接遥测的组件"
                        "(没有 span 的中间件是已知盲区)。"),
                    "detect": detect_info}
        if detect_info.get("gaps"):
            # 单维查询失败不拦诊断,但必须浮到事件流——锚点是"其余维"选出来的,
            # 缺失维对应的故障类型这单可能看不见(2026-10-18:时延维超时整单失明的教训)
            _emit(run, "detect", "粗筛部分维度查询失败:"
                  + "；".join(g.split(":", 1)[-1] for g in detect_info["gaps"])
                  + "——对应故障类型本单可能漏报")
        top = detect_info["anchors"][0]
        service = top["service"]
        # 锚点带平台:多平台同名服务靠它定位,不能退回"裸名先发现的胜出"
        platform = top.get("platform") or platform
        run["service"] = service
        run["platform"] = platform
        run["cluster"] = top.get("cluster") or ""
        symptom = symptom or "平台粗筛发现的异常"
        _emit(run, "detect", f"粗筛锚点:{service}({top['reasons'][0]})"
              + (f";另有可疑:{'、'.join(a['service'] for a in detect_info['anchors'][1:])}"
                 if len(detect_info["anchors"]) > 1 else ""))

    prom, jaeger, err, resolved_service = await _resolve_sources(service, platform)
    if err:
        _emit(run, "failed", err)
        return {"verdict": "no_data", "root_cause_service": None, "chain": [],
                "confidence_note": err, "missing": [err], "suggested_actions": [],
                "narrative": _plain_narrative(
                    "还没开始查就停了:找不到这个服务,或者它所在的平台没把数据源接全。",
                    "结论:无法诊断,原因见下。",
                    err,
                    "先确认服务名和平台名对不对、该平台是否同时接了 Prometheus 与 Jaeger。"),
                "detect": detect_info}
    if resolved_service and resolved_service != service:
        # 传进来的是"平台/服务名"(前端目录的标法),上游只认裸名。后续所有查询、
        # 日志检索、候选比对统一用裸名;run["service"] 保留原写法给界面看。
        _emit(run, "collect", f"服务名归一:{service} → {resolved_service}"
                              "(上游 service_name 用裸名)")
        service = resolved_service
    # 直接点名服务的路径没有粗筛锚点,集群要从平台现查补上
    run["cluster"] = run.get("cluster") or await _cluster_of(platform)

    # ②.0 全平台指标画像(span 指标:连续量,不是 trace 抽样)。**一次取齐、全链共用**:
    # 它同时是 ① 错误率/时延的纵向基线来源、② 指标侧异常服务的召回来源、③ 故障形态的输入。
    # 之前它只喂粗筛和证据行、没进判据——"进证据"不等于"进判据",这就是那个缺口。
    # 扫描路径下直接复用粗筛的画像(见上文 _scan_profiles_map),不再重查重算:
    # 省约 12 条查询的预算,更重要的是判定快照唯一——同一个服务不会"扫描时异常、
    # 诊断时不异常"(实测发生过,真因从名单上掉)。直接指定服务时没有扫描,现取。
    _scan_profiles = _scan_profiles_map.get(platform or "", {})
    if _scan_profiles:
        _profiles = _scan_profiles
        _emit(run, "collect", f"指标画像:复用粗筛扫描结果({len(_profiles)} 个服务,"
                              "判定快照与粗筛一致)")
    else:
        _profiles = await detect.service_profiles(prom)
        if _profiles:
            _emit(run, "collect", f"指标画像:{len(_profiles)} 个服务的调用量/错误率/时延"
                                  "(span 指标,连续量;作纵向基线 + 召回来源)")

    # ②采集：固定窗口终点，按 10m→1h→6h→24h 扩展，最多一天。
    _emit(run, "collect", f"查询 {service} 的 Jaeger trace(10m→1h→6h→24h)…")
    trace = await collect.fetch_spans(jaeger["url"], jaeger.get("headers"), service,
                                      window_end=now)
    if trace.status in {"empty", "data_stale"}:
        # 影子救援:目标自身在 Jaeger 已无任何 trace(从未上报/记忆环淘汰——停机够久,
        # 旧 trace 会被环挤出去)。挂死服务的痕迹只在调用方身上:按平台服务清单逐个
        # 拉短窗采样,凑够指向目标的失败调用现场就提前收工,拿到后走正常影子归因路。
        def _enough(cur: list) -> bool:
            return sum(1 for sp in cur if sp.error and _points_to(sp, service)) >= 3
        rescued, queried = await collect.fetch_platform_spans(
            jaeger["url"], jaeger.get("headers"), service, window_end=now, enough=_enough)
        blamed_n = sum(1 for sp in rescued if sp.error and _points_to(sp, service))
        if blamed_n >= 3:
            # 状态标 available 不标 sparse:现场 span 有 1000+(统计有效),sparse 会
            # 触发"样本不足不调 LLM"的确定性短路,影子归因就永远开不了口;
            # 目标自身零样本这件事由 shadow 维和证据行如实陈述,不靠这个标签
            trace = replace(trace, spans=rescued, status="available",
                            lookback_min=10, window_start=now - 600, window_end=now)
            _emit(run, "collect",
                  f"目标自身在 Jaeger 已查不到 trace(记忆环淘汰或从未上报),"
                  f"改按平台服务({','.join(queried) or '无'})拉调用方现场:"
                  f"找到 {blamed_n} 条失败调用指向 {service},走影子归因")
    spans = trace.spans
    sparse = trace.status == "sparse"
    # ②.1 span 资源属性榨干(不接任何新采集器):版本变更检测——窗内出现 ≥2 个
    # service.version = 发生过发版/回滚,生产环境最常见根因来源之一,数据本来在手
    _versions = evidence.version_timeline(spans)
    data_quality = {
        "trace": {"status": trace.status, "spans": len(spans),
                  "requested_lookbacks_min": trace.requested_lookbacks,
                  "selected_lookback_min": trace.lookback_min,
                  "window_start": trace.window_start, "window_end": trace.window_end,
                  "error": trace.error},
    }
    if trace.status in {"empty", "data_stale", "query_failed", "budget_exhausted"}:
        reasons = {
            "empty": "最近24小时内未找到该服务的 Jaeger trace",
            "data_stale": "Jaeger 返回的 trace 全部不在请求时间窗内",
            "query_failed": f"Jaeger 查询失败:{trace.error or '未知错误'}",
            "budget_exhausted": "本次诊断触及上游查询预算上限,未能完成 trace 采集",
        }
        reason = reasons[trace.status]
        _emit(run, "done", f"诊断停止:{reason};未执行根因归因")
        return {"verdict": "no_data", "root_cause_service": None, "chain": [],
                "confidence_note": reason, "missing": [reason],
                "suggested_actions": ["检查 Jaeger 查询时间范围、服务名映射及 trace 写入状态"],
                "narrative": _plain_narrative(
                    "指标侧能看,但链路(trace)这一路取不到;归因要靠链路,所以停在采集段。",
                    "结论:证据不足,没有进入根因归因。",
                    reason,
                    "检查 Jaeger 是否可达、服务名对不对得上、这段时间有没有 trace 写入。"),
                "insufficient_trace": True, "data_quality": data_quality,
                "falsify": {"refuted": False, "checked_by": "code_rules",
                            "reason": "trace 数据质量不满足要求,未进入归因与证伪流程"},
                "gate": None}

    # ②.2 重启/OOM 指标(标准 K8s/cAdvisor 族,平台采了才查,≤3 条查询):
    # pod_failure/资源耗尽的机制级实锤,比"遥测静默"更具体
    _restarts = await detect.restart_signals(prom, set(_profiles) | {service},
                                             trace.window_start, trace.window_end)
    for _svc, _r in _restarts.items():
        if _svc in _profiles:
            _profiles[_svc]["restart"] = _r
    if _versions:
        _emit(run, "collect", "版本变更:" + ";".join(
            f"{s} {v['from']} → {v['to']}(窗内)" for s, v in sorted(_versions.items())))
    if _restarts:
        _emit(run, "collect", "重启信号:" + ";".join(
            f"{s}{' OOMKilled' if r['oom'] else ''} 重启 {r['restarts']:.0f} 次"
            for s, r in sorted(_restarts.items())))

    async def _res_ratio(metric: str,
                         svc: str | None = None) -> tuple[float | None, float | None, str | None, str, str]:
        """当前窗均值 + 相对基线比值 + 基线来源标签。基线链:T-24h 同时段窗 → T-2h~T-1h 窗
        (靶机重部署未满 24h 时的回退,代价是可能含故障期——标签 t2h 一路带进证据包,
        让归因员知情,比值仅供参考)。基线缺失不连坐:当前值照常返回、比值为 None——
        真实教训:昨晚 CPU 数据一直在,被"基线空"整对丢弃。
        svc 缺省=症状服务;候选证据预取时传候选服务名,口径与症状服务完全一致。"""
        s = svc or service
        cur, cur_status = await collect.metric_avg(prom["url"], prom.get("headers"), s, metric,
                                                   trace.window_start, trace.window_end)
        if cur is None:
            return None, None, None, cur_status, "not_checked"
        baseline_status = "no_data"
        for start_off, end_off, tag in ((24 * 3600, 23 * 3600, "t24h"), (2 * 3600, 1 * 3600, "t2h")):
            base, baseline_status = await collect.metric_avg(
                prom["url"], prom.get("headers"), s, metric,
                now - start_off, now - end_off)
            if base not in (None, 0):
                return round(cur, 6), round(cur / base, 3), tag, cur_status, baseline_status
        return round(cur, 6), None, None, cur_status, baseline_status

    cpu_cur, cpu_ratio, cpu_base, cpu_status, cpu_baseline_status = await _res_ratio("cpu")
    mem_cur, mem_ratio, mem_base, mem_status, mem_baseline_status = await _res_ratio("memory")
    _base_note = (";基线为 T-2h 回退窗(可能含故障期,资源比值仅供参考)"
                  if "t2h" in (cpu_base, mem_base) else "")
    _emit(run, "collect", f"链路 span {len(spans)} 条,选中 {trace.lookback_min}min 窗口"
                          f"{'(样本偏少,归因将保守)' if sparse else ''};"
                          f"CPU 当前 {cpu_cur if cpu_cur is not None else '无数据'}"
                          f"{'(无基线比值)' if cpu_cur is not None and cpu_ratio is None else ''},"
                          f"内存当前 {mem_cur if mem_cur is not None else '无数据'}"
                          f"{'(无基线比值)' if mem_cur is not None and mem_ratio is None else ''}"
                          + _base_note)

    # ③拓扑+偏离
    _emit(run, "topology", "从 span parent-child 恢复服务调用图,计算四维偏离度…")
    window_start_us = int(trace.window_start * 1e6)
    graph = topology.build(spans)
    # 指标推的全平台调用边并入 trace 图:只用 trace 建图只覆盖"穿过症状服务的链路",
    # 不在链路上的根因天然看不见(召回被锁死)。指标边只增不减,不会让本来可达的变不可达。
    for _a, _b in await detect.metric_edges(prom, set(graph.nodes)):
        graph.nodes.add(_a)
        graph.nodes.add(_b)
        if graph.edges[(_a, _b)] == 0:
            graph.edges[(_a, _b)] = 1        # 指标边只作结构,次数不参与判据
    stats = deviation.span_stats(spans)
    res_ratio = {service: {"cpu": cpu_ratio, "mem": mem_ratio,
                           "cpu_baseline": cpu_base, "mem_baseline": mem_base}}
    # 指标侧发病时刻(分钟粒度):trace 锚点有父子偏置——同一个 trace 里父 span 必然早于子 span,
    # 级联里上游会假性显得更早,时间信号系统性偏袒上游。用指标的连续曲线定时刻,同刻异常会并列。
    _onsets = await detect.metric_onsets(prom, window_start=trace.window_start,
                                         window_end=trace.window_end, baselines=_profiles)
    if _onsets:
        _emit(run, "topology", f"指标侧发病时刻:{len(_onsets)} 个服务"
                               "(分钟粒度,替代 trace 的 span 时刻)")
    # 纵向基线:错误率/时延也跟服务自己的历史比(之前四维里只有 CPU/内存是纵向的)
    devs = deviation.compute(stats, res_ratio, spans=spans, profiles=_profiles,
                             window_s=max(trace.window_end - trace.window_start, 1.0),
                             edges=set(graph.edges), onsets=_onsets)
    operation_evidence = evidence.operation_stats(spans)
    dependency_evidence = evidence.dependency_stats(spans)
    _emit(run, "topology", f"调用图:{len(graph.nodes)} 个服务节点,{len(graph.edges)} 条边;"
                           f"异常节点:" + ("、".join(s for s, d in devs.items() if d.abnormal) or "无"))

    # ③.5 挂死检测:停机服务的旧 span 仍躺在采样窗里,四维偏离看着正常——单看旧数据
    # 会对"整个服务挂死"失明,两种翻车形态都真实发生过:①目标自身停机被判"健康"
    # (空症状放行);②症状服务的一跳邻居停机,邻居进不了候选,症状服务被自查成根因
    # (checkout 报错、payment 停机,结论却是 checkout)。对症状服务及其一跳邻居逐个
    # 查静默:静默=最新自身 span 距今超过 max(90s, 10×自身间隔中位数);
    # 实锤=停声之后仍有别的服务的失败调用指向它(调用发生却无响应,证据就在窗内)。
    # 症状服务自己的采样窗里没有别人的新调用记录,拉最大调用方近 3 分钟独立采样补证。
    hang_findings: dict[str, dict] = {}
    hang_note: str | None = None
    target_dev = devs.get(service)
    _up, _down = graph.neighbors(service)
    for svc in {service} | _up | _down:
        dev = devs.get(svc)
        if dev is None or dev.abnormal:
            continue   # 已有异常维的另有信号,不重复定性
        sil = _target_silence(spans, svc, trace.window_end)
        if not sil:
            continue
        blamed = [sp for sp in spans if sp.error and sp.service != svc
                  and sp.start_us > sil["last_own_us"] and _points_to(sp, svc)]
        if not blamed and svc == service:
            # 目标服务的采样窗里只有它自己的旧链路,拉最大调用方近 3 分钟独立采样
            caller = max((a for (a, b) in graph.edges if b == svc),
                         key=lambda a: graph.edges.get((a, svc), 0),
                         default=None)
            if caller is None:
                hang_note = (f"目标遥测已静默 {sil['silence_s']}s,但调用图上找不到它的"
                             "调用方,挂死无法实证")
                continue
            fresh, _fstate = await collect.fetch_recent(
                jaeger["url"], jaeger.get("headers"), caller,
                minutes=3, limit=30, window_end=trace.window_end)
            if not fresh:
                hang_note = (f"目标静默 {sil['silence_s']}s,但调用方 {caller} 近 3 分钟"
                             "也无遥测——整站安静,不判挂死")
                continue
            blamed = [sp for sp in fresh if sp.error and _points_to(sp, svc)]
            if not blamed:
                hang_note = (f"目标静默 {sil['silence_s']}s,但调用方 {caller} 近 3 分钟"
                             "遥测正常、无指向目标的失败调用——不判挂死")
                continue
            sil["blamer"] = caller
        if not blamed:
            continue
        sil.setdefault("blamer", blamed[0].service)
        sil["blamed"] = len(blamed)
        hang_findings[svc] = sil
    for svc, h in hang_findings.items():
        d = devs.get(svc)
        if d is not None:
            d.abnormal_dims.add("hang")
            d.hang_since_us = h["last_own_us"]
    if hang_findings:
        _emit(run, "topology", "疑似挂死:" + ";".join(
            f"{s} 已静默 {h['silence_s']}s(此前间隔中位数 {h['median_gap_s']}s),"
            f"停声后 {h['blamer']} 有 {h['blamed']} 条失败调用指向它"
            for s, h in hang_findings.items()))
    elif hang_note:
        _emit(run, "topology", hang_note)

    # ③.6 用已经取好的全平台画像补挂死判定:_target_silence 要求"窗内至少 5 条自身 span"
    # 才谈得上静默,自身几乎不出遥测的服务(整体失联)反而判不出来;指标能判。
    # 零 span 的服务不在这里造记录(那会顶掉 localize 的指认准入),它们走影子/错误指认那条路。
    _metric_hangs: list[str] = []
    for _svc, _p in sorted(_profiles.items()):
        _d = devs.get(_svc)
        if not _p["traffic_collapse"] or _d is None or "hang" in _d.abnormal_dims:
            continue
        # 必须"调用方正在它身上报错"才敢按挂死处理:流量塌陷本身说明不了问题——整条链被上游
        # 堵住时,下游一样没量(被带的沉默,真实事故:product-catalog 变慢后 cart / currency
        # 的调用量也塌了,却被当成挂死拿到高分,把真凶挤出了候选集)。指认从 trace 采样里找。
        _blamed = [sp for sp in spans if sp.error and sp.service != _svc
                   and _points_to(sp, _svc)]
        if not _blamed:
            continue
        _d.abnormal_dims.add("hang")
        _metric_hangs.append(_svc)
    if _metric_hangs:
        _emit(run, "topology",
              "指标窗调用量塌陷**且被失败调用指认**,按挂死形态处理:" + "、".join(_metric_hangs))

    # ④传播定位
    _emit(run, "localize", "提取异常子图,按严重层级排序候选…")
    # 粗筛的其他锚点强制入候选:平台级异常实锤不因"选了第一锚点"而丢弃(双重故障场景,
    # 第二锚点可能就是另一单故障的真凶)。带上粗筛给的层级,localize 换算后用作候选层级
    # (锚点的"被点名/塌陷"证据在定位层的维度字段里没有承载,不能按维度滤掉)。
    # emit 如实说明并入,不悄悄。
    _extra_anchors = [(a["service"], a.get("tier"), i)
                      for i, a in enumerate((detect_info or {}).get("anchors", [])[1:], 1)
                      if a["service"] != service]
    if _extra_anchors:
        _emit(run, "localize",
              f"粗筛同时发现的其他异常锚点并入候选:"
              + "、".join(f"{s}(层级 {t})" for s, t, _ in _extra_anchors))
    # 调用边时延检测(架构移植自 MicroRCA-Agent,检测核为 MAD 稳健判线):服务级
    # p95 看不出"哪条边慢",边级异常窗是被调方时延异常的独立证据。只进证据包不改判;
    # 画像未训练时如实说明原因,不假装检测过(缺画像 ≠ 没异常,也 ≠ 有异常)。
    _edge_stats, _edge_err = edge_anomaly.load()
    if_events, _edge_note = edge_anomaly.detect(spans, _edge_stats)
    _edge_note = _edge_note or _edge_err
    if if_events:
        _emit(run, "localize", f"调用边时延检测:{len(if_events)} 条调用边报异常窗"
                               f"(按异常窗数取前 {edge_anomaly.EVENT_CAP} 条进证据包)")
    elif _edge_note:
        _emit(run, "localize", f"调用边时延检测不可用:{_edge_note}")
    # ④.0 两条"因果实锤"通道(指标侧,共约 4 条查询,预算闸照记):
    # 边级错误归属——调用方的客户端错误集中在哪条边,被调方是失败承载方;
    # 开关变更探测——flag 求值遥测里哪个服务的开关取值翻了。两者都由 localize
    # 消费成候选准入/层级兑换/排序证据,不可用时如实说明、不影响主线。
    _known = set(graph.nodes) | set(devs) | set(_profiles)
    _edge_blames, _edgeview_note = await edgeview.client_edge_errors(
        prom, _known, trace.window_start, trace.window_end)
    if _edge_blames:
        _emit(run, "localize", "边级错误归属:" + ";".join(
            f"{e.caller} → {e.callee}({e.count:.0f} 笔失败调用)" for e in _edge_blames))
    elif _edgeview_note:
        _emit(run, "localize", f"边级错误归属不可用:{_edgeview_note}")
    _flag_changes, _flag_note = await flagchange.detect(
        prom, trace.window_start, trace.window_end, now=now)
    if _flag_changes:
        _emit(run, "localize", "功能开关变更:" + ";".join(
            f"{c.service} 的 {c.flag} → {c.variant}" for c in _flag_changes))
    elif _flag_note:
        _emit(run, "localize", f"开关变更探测不可用:{_flag_note}")
    loc = localize.localize(graph, devs, service, spans, window_start_us=window_start_us,
                            span_profiles=_profiles, forced_extra=_extra_anchors,
                            if_events=if_events,
                            symptom_tier=((detect_info or {}).get("anchors") or [{}])[0].get("tier"),
                            edge_blames=_edge_blames, flag_changes=_flag_changes)
    cands = loc["candidates"]
    target_has_confirmed_issue = bool(target_dev and target_dev.abnormal)
    if hang_findings:
        # 挂死证据以 [E#] 并入全局证据表与对应候选的证据包——归因引用完整性校验
        # 按同一套编号算,不并入候选包的话 LLM 引用不了这几条
        eid = len(loc["evidence"]) + 1
        for svc, h in hang_findings.items():
            hang_lines = [
                f"[E{eid}] {svc} 自身遥测已静默 {h['silence_s']} 秒"
                f"(此前 span 间隔中位数 {h['median_gap_s']} 秒),"
                "最后一条自身 span 之后没有再产生任何新遥测",
                f"[E{eid + 1}] 调用方 {h['blamer']} 在其停声之后有 {h['blamed']} 条失败调用"
                f"指向 {svc}(调用持续发生,目标无响应)",
            ]
            eid += 2
            loc["evidence"].extend(hang_lines)
            pack = next((c for c in cands if c["service"] == svc), None)
            if pack:
                pack["evidence"].extend(hang_lines)
    # 指标画像作为证据写进症状与候选的证据包:调用量/错误率的连续序列(带基线),补上
    # "trace 只有几十条抽样"的粗估。走同一套 [E#] 编号,归因引用完整性校验照常算。
    # 版本变更/重启/变更记录是同一批"免费数据"里的另外三条通道,一并进证据包。
    # 变更记录来自产品自己的库(变更工单+变更申请审计),症状与候选逐个查(本地库,零预算)。
    from datetime import datetime as _dt, timezone as _tz
    _window_since = _dt.fromtimestamp(trace.window_start, _tz.utc).replace(tzinfo=None)
    _window_until = _dt.fromtimestamp(trace.window_end, _tz.utc).replace(tzinfo=None)
    _change_signals: dict[str, list[dict]] = {}
    for _svc in [service, *[c["service"] for c in cands]]:
        _rows = await repository.recent_change_signals(_svc, _window_since, _window_until)
        if _rows:
            _change_signals[_svc] = _rows
    if _change_signals:
        _emit(run, "collect", "变更记录:" + ";".join(
            f"{s} {len(v)} 条({','.join(sorted({x['kind'] for x in v}))})"
            for s, v in _change_signals.items()))
    if _profiles:
        eid = len(loc["evidence"]) + 1
        # 只给症状 + 候选写画像证据(全平台画像有几十条,全写进证据包会淹掉归因员)
        for svc in [service, *[c["service"] for c in cands]]:
            p = _profiles.get(svc)
            if p:
                line = f"[E{eid}] {_profile_line(svc, p)}"
                loc["evidence"].append(line)
                pack = next((c for c in cands if c["service"] == svc), None)
                if pack:
                    pack["evidence"].append(line)
                eid += 1
            v = _versions.get(svc)
            if v:
                line = (f"[E{eid}] {svc} 在症状窗内发生版本变更:{v['from']} → {v['to']}"
                        f"(版本切换于 {time.strftime('%H:%M:%S', time.gmtime(v['changed_us'] / 1e6))},"
                        "发版引入故障是生产环境最常见根因来源之一)")
                loc["evidence"].append(line)
                pack = next((c for c in cands if c["service"] == svc), None)
                if pack:
                    pack["evidence"].append(line)
                eid += 1
            r = _restarts.get(svc)
            if r:
                line = (f"[E{eid}] {svc} 在症状窗内容器重启 {r['restarts']:.0f} 次"
                        + ("(OOMKilled,内存耗尽被指标直接点名)" if r["oom"] else "")
                        + "(重启计数窗内增量,机制级实锤)")
                loc["evidence"].append(line)
                pack = next((c for c in cands if c["service"] == svc), None)
                if pack:
                    pack["evidence"].append(line)
                eid += 1
            cs = _change_signals.get(svc)
            if cs:
                line = (f"[E{eid}] {svc} 在症状窗内有 {len(cs)} 条变更记录:"
                        + ";".join(f"{x['ts'][:19]} {x['kind']}({x['detail'][:60]})"
                                   for x in cs[:3])
                        + "——变更临近发病是发版引入故障的强信号")
                loc["evidence"].append(line)
                pack = next((c for c in cands if c["service"] == svc), None)
                if pack:
                    pack["evidence"].append(line)
                eid += 1
    # 候选证据预取:refill 的注释自己写着"候选的资源维度常年是 None""候选的日志证据常年
    # 缺席"——这是确定性的洞,不该花 LLM 的补查名额去声明它。候选集一出来就无条件补齐
    # (≤3 个 × 几次查询,预算闸兜着),LLM 的补查名额留给真正非结构化的缺口。
    # 症状服务的资源/日志主线已经查过,不重复。
    _prefetch = [c for c in cands if c["service"] != service]
    _cand_logsig: dict[str, dict] = {}      # 候选的日志归类结果:故障形态判定要用
    if _prefetch:
        _loki, _loki_target = await _loki_for(service, platform)   # 一次发现,候选共用(单平台诊断)
        _log_minutes = min(trace.lookback_min, 120)
        _eid = len(loc["evidence"]) + 1
        for _c in _prefetch:
            _svc = _c["service"]
            _lines: list[str] = []
            for _metric, _label in (("cpu", "CPU"), ("memory", "内存")):
                _cur, _ratio, _tag, _cur_st, _bs = await _res_ratio(_metric, _svc)
                if _cur is None:
                    _lines.append(f"{_svc} {_label}当前窗状态 {_cur_st}(无数据不编数)")
                    continue
                _txt = f"{_svc} {_label}当前窗均值 {_cur}"
                if _ratio is None:
                    _txt += "(无基线比值,偏离幅度只能看个大概)"
                else:
                    _txt += f",为基线({_tag})的 {_ratio} 倍"
                    if _tag == "t2h":
                        _txt += "(回退基线可能含故障期,比值仅供参考)"
                    # 是否异常看画像里的统计判据(与其余维度同一套),不拿比值当阈值
                    _jd = ((_profiles.get(_svc) or {}).get("dev") or {}).get(_metric)
                    if _jd is not None and _jd.abnormal:
                        _txt += "——超出该服务自身历史范围,资源维异常"
                _lines.append(_txt)
            if _loki is None:
                _lines.append(f"{_svc} 所在平台未接入 Loki,错误日志这一维是盲的")
            else:
                from app.tools.builtin.observability import query_platform_logs
                _raw = await query_platform_logs.ainvoke({
                    "service": _svc, "minutes": _log_minutes,
                    "target_name": _loki_target, "filter_text": "error"})
                if isinstance(_raw, dict) and _raw.get("error"):
                    _lines.append(f"{_svc} 错误日志查询失败({_raw.get('code', 'query_failed')})"
                                  "——查询失败不等于无故障")
                else:
                    _lg = (_raw or {}).get("lines") or []
                    if _lg:
                        _cand_logsig[_svc] = logsig.classify(_lg)
                        _sig = logsig.summary(_cand_logsig[_svc])
                        _lines.append(f"{_svc} 近 {_log_minutes} 分钟错误日志 "
                                      f"{(_raw or {}).get('total', len(_lg))} 条,"
                                      f"归类:{_sig or '未识别出常见故障特征'}")
                    else:
                        _lines.append(f"{_svc} 近 {_log_minutes} 分钟错误日志 0 条"
                                      "(日志缺失不等于无故障,如实记录)")
            for _ln in _lines:
                _ev = f"[E{_eid}] {_ln}"
                loc["evidence"].append(_ev)
                _c["evidence"].append(_ev)
                _eid += 1
        _emit(run, "collect",
              "候选证据预取:已为 " + "、".join(c["service"] for c in _prefetch)
              + " 补齐资源比值与错误日志")
    target_hang = hang_findings.get(service)
    symptom_text = symptom or (
        f"目标服务已静默 {target_hang['silence_s']}s 无遥测,调用方失败调用指认,疑似挂死"
        if target_hang else "")
    # 多云上下文:同一集群里的其他平台是同一套系统的另外几朵云。归因模型必须知道这件事,
    # 否则会把跨云同名服务的指标/日志混着看,或者在错的云上收敛。
    _cluster = run.get("cluster") or ""
    if _cluster:
        symptom_text = (symptom_text + "\n" if symptom_text else "") + (
            f"【部署形态】这是多云集群「{_cluster}」中的一朵云(平台「{platform}」)。"
            f"同一套系统的其他云也有同名服务,它们是不同实例;证据只取本平台的,"
            f"结论也要落在本平台,不要把跨云数据混在一起判断。")
        _emit(run, "collect", f"多云集群「{_cluster}」:本次只在本平台「{platform}」取证与定位")
    if not symptom and not target_has_confirmed_issue and not cands:
        # 健康检查放行的完整条件:没症状、目标自身无异常维、且连影子候选都没有——
        # 目标零样本但被调用方失败指认时(影子形态),cands 里会有它,不能当健康放行
        _emit(run, "done", "诊断完成:当前未发现可确认的服务异常")
        _health_dq = {**data_quality, "health_check": "no_confirmed_anomaly"}
        _health_missing = ["未提供明确故障症状,仅完成健康检查"]
        if hang_note:
            _health_dq["hang_probe"] = hang_note
            _health_missing.append(hang_note)
        return {"verdict": "clear", "root_cause_service": None, "chain": [],
                "confidence_note": "当前目标服务错误率、资源和绝对时延均未达到异常准入条件",
                "missing": _health_missing,
                "suggested_actions": [], "insufficient_trace": False,
                "narrative": _plain_narrative(
                    "指定服务自身的错误率、资源用量、时延都没到异常线,也没有被调用方指认。",
                    "结论:这个服务当前是健康的。",
                    "当前目标服务错误率、资源和绝对时延均未达到异常准入条件",
                    "如果用户仍在报障,换一个服务、或者补上具体症状再查一次。"),
                "operation_evidence": [item.__dict__ for item in operation_evidence
                                        if item.service == service],
                "dependency_evidence": [item for item in dependency_evidence
                                         if service in (item["from"], item["to"])],
                "data_quality": _health_dq, "detect": detect_info}

    # Loki 只在错误、样本异常或用户说明日志类现象时查;查询结果与缺数状态分开。
    log_result = {"status": "not_needed", "lines": []}
    should_query_logs = bool(symptom or any(span.error for span in spans)
                             or (target_dev and target_dev.abnormal))
    if should_query_logs:
        from app.tools import service_catalog
        services, discovery_error = await service_catalog.discover_services()
        hit = service_catalog.find_service(services, service, platform)
        if hit:
            rows = await repository.list_targets()
            target = next((row for row in rows if row.enabled and row.name == hit.get("target")), None)
            loki = next((source for source in (target.sources or [])
                         if source.get("type") == "loki"), None) if target else None
            if loki:
                from app.tools.builtin.observability import query_platform_logs
                raw_logs = await query_platform_logs.ainvoke({
                    "service": service, "minutes": min(trace.lookback_min, 120),
                    "target_name": target.name, "filter_text": "error"})
                if isinstance(raw_logs, dict) and raw_logs.get("error"):
                    log_result = {"status": raw_logs.get("code", "query_failed"), "lines": []}
                else:
                    # 日志压缩:同型错误刷屏只留代表行(带重复计数),最早出现时刻
                    # 单独给——它是时间锚的日志侧信号源,比"20 行全是同一句"有用
                    _cond = evidence.condense_logs(raw_logs.get("lines", []), cap=20)
                    log_result = {"status": "available" if _cond["lines"] else "no_data",
                                  "lines": _cond["lines"], "label": raw_logs.get("label"),
                                  "distinct": _cond["distinct"], "repeat": _cond["repeat"],
                                  "first_ts": _cond["first_ts"]}
            elif not discovery_error:
                log_result = {"status": "no_loki_source", "lines": []}
    # 日志归类:20 行原文当佐证是浪费——按"最能定性"的类别归拢成标签("12 条连接被拒"
    # 比"错误率 30%"信息量高一个量级)。归拢结果随 log_result 一起进证据包。
    if log_result.get("lines"):
        log_result["signals"] = logsig.classify(log_result["lines"])
        _sig = logsig.summary(log_result["signals"])
        if _sig:
            _emit(run, "collect", f"日志信号归类:{_sig}")
    _emit(run, "collect", f"Loki 日志证据:{log_result['status']}"
                          + (f"(最早错误 {log_result['first_ts']},"
                             f"去重后 {log_result['distinct']} 条)"
                             if log_result.get("first_ts") else ""))
    _emit(run, "localize", "Top 候选:" + ("、".join(
        f"{c['service']}(得分 {c['score']},方向 {c['direction']})" for c in cands) or "无异常候选"))

    # ⑤归因 = OpsAgent 三 agent 协作(ASE Industry 2026,AIOpsLab 排行榜第一的方案;
    # 源码原样保留在 opsagent/,本层喂活数据并桥接 LLM)。编排序列:异常检测/分诊/
    # 根因定位 think → 两两交叉评审 → refine → Judge 终裁。旧的单次归因/白名单补查/
    # 门禁/证伪随本次替换退役(备份:~/Desktop/OpsPaw_归因板块备份_20261015/)。
    _emit(run, "attribute", "证据交 OpsAgent 三 agent 协作归因(异常检测→分诊→根因定位,交叉评审)…")
    _loki_src, _loki_target = await _loki_for(service, platform)
    _attr_err: str | None = None
    try:
        answer_text, answers, thinkings = await opsagent_adapter.run_opsagent(
            symptom_text, service, cands, spans,
            prom=prom, jaeger=jaeger, loki=_loki_src, target_name=_loki_target,
            window_start=trace.window_start, window_end=trace.window_end,
            log_minutes=min(trace.lookback_min, 120))
    except Exception as exc:  # noqa: BLE001  模型/网络故障不该整单失败
        # 归因段是唯一依赖外部模型的地方:端点抖动、限流、超时都可能发生。以前这里
        # 没有兜底,一次 60 秒的端点抖动就能把整条诊断拖死(2026-10-17 实测卡死 15 分钟,
        # 连预算闸都没机会触发)。现在退回**确定性排序的首选候选**并如实写明原因——
        # 定位本来就是确定性层做的,模型只是解读;模型不可用不该等于诊断不可用。
        logger.warning("OpsAgent 归因失败,退回确定性首选候选", exc_info=True)
        answer_text, answers, thinkings = "", [], []
        _attr_err = f"{type(exc).__name__}: {str(exc)[:160]}"
    if _attr_err:
        from app.diagnosis.attr_contract import Attribution
        _fb = cands[0]["service"] if cands else None
        attr = Attribution(
            verdict="root_cause" if _fb else "inconclusive", root_cause_service=_fb,
            chain=[], confidence_note=f"归因模型不可用({_attr_err}),已退回确定性排序的首选候选",
            missing=["归因模型未返回结论,本次结论只依据确定性定位"])
    else:
        attr = opsagent_adapter.attr_from_answers(
            answer_text, answers, cands, service,
            services_all={sp.service for sp in spans} | set(graph.nodes),
            abnormal={s for s, d in devs.items() if d.abnormal})
    passed = bool(attr.root_cause_service or attr.verdict == "inconclusive")
    _emit(run, "attribute", f"归因结论:{attr.verdict}"
                            f"{(' → ' + attr.root_cause_service) if attr.root_cause_service else ''}")
    gate: dict | None = None
    falsify_info: dict | None = None
    refill_summary: dict | None = None
    edges_all = set(graph.edges) | set(loc.get("shadow_edges") or {})

    # ⑥.5 故障形态判定(代码,LLM 不碰):结论从"某服务坏了"变成"某服务 + 怎么坏的"。
    # 判据全部来自已有证据(偏离维 / 指标画像 / 日志归类 / 错误 span 属性),认不出就归"未定性"。
    _fault_svc = attr.root_cause_service or service
    _fault_logs = (log_result.get("signals") if _fault_svc == service
                   else _cand_logsig.get(_fault_svc))
    # 错误 span 属性通道(OTel 标准埋点自带,不依赖 Loki):错误 span 携带的
    # error.type/grpc 状态码/异常消息,是平台没接日志时唯一的机制定性来源
    _fault_span_sig = logsig.classify(evidence.error_lines(spans, _fault_svc))
    fault_info = faulttype.classify(devs.get(_fault_svc), _fault_logs,
                                    _profiles.get(_fault_svc), span_sig=_fault_span_sig)
    _emit(run, "done", f"故障形态:{fault_info['text']}({fault_info['basis'][0]})")

    # ⑦置信表达(纯代码,LLM 不碰):三档标签 + 按类归拢的证据清单 + 采集盲点。
    # 数字置信度不给——LLM 自报数字全线失准(社区调研定稿),分数只用于候选排序
    confidence_info = confidence.assess(
        attr=attr, service=service, devs=devs, candidates=cands,
        edges=edges_all, window_start_us=window_start_us,
        data_quality=data_quality, log_result=log_result, sparse=sparse)
    _emit(run, "done", f"证据充分度:{confidence.TIER_TEXT.get(confidence_info['tier'], confidence_info['tier'])}"
                       f"({confidence_info['summary']})")

    # ⑧开方:最终结论定稿后(门禁可能降级,所以必须在它之后),
    # 诊断 agent 用大白话把"病根是什么、为什么、要注意什么"分四个字段讲清楚——
    # 像医生开方。这份输出同时是后续修复 agent 的任务输入,失败走确定性兜底。
    _emit(run, "done", "诊断开方:生成面向人的根因说明…")

    # 全图快照(节点含四维偏离):详情页画整条链路用,健康度分级着色在前端做。
    # 影子服务(挂死零 span)不在 devs 里,从 shadow_edges 的被指认方补进节点;
    # 影子边也并入全图边,否则前端又是孤立框。
    _shadow_edges = loc.get("shadow_edges") or {}
    _shadow_svcs = [b for (_, b) in _shadow_edges if b not in devs]
    result = {
        "verdict": attr.verdict, "root_cause_service": attr.root_cause_service,
        "amplifiers": attr.amplifiers, "unrelated": attr.unrelated,
        "fault_type": fault_info,
        "chain": attr.chain, "confidence_note": attr.confidence_note,
        "opsagent": {
            "status": "error" if _attr_err else "ok",
            "answer_text": answer_text,
            "answers": answers,
            "thinkings": thinkings,
            "error": _attr_err,
        },
        "missing": attr.missing, "suggested_actions": attr.suggested_actions,
        "subgraph": loc["subgraph"], "candidates": cands,
        "edge_blames": [{"callee": e.callee, "caller": e.caller, "count": e.count,
                         "operations": e.operations} for e in _edge_blames],
        "flag_changes": [{"service": c.service, "flag": c.flag, "variant": c.variant,
                          "prev_variants": list(c.prev_variants),
                          "onset_us": c.onset_us} for c in _flag_changes],
        "operation_evidence": [],
        "dependency_evidence": [],
        "logs": log_result,
        "full_graph": {
            "nodes": [{"service": s, "abnormal": d.abnormal, "dims": d.dims_view(),
                       "calls": d.calls} for s, d in devs.items()]
                      + [{"service": s, "abnormal": False, "shadow": True,
                          "dims": {"error_rate": 0, "error_dev": 0, "latency_dev": 0,
                                   "cpu_ratio": None, "mem_ratio": None,
                                   "abnormal_dims": []},
                          "calls": 0} for s in _shadow_svcs],
            "edges": [{"from": a, "to": b, "calls": n} for (a, b), n in graph.edges.items()]
                     + [{"from": a, "to": b, "calls": n, "shadow": True}
                        for (a, b), n in _shadow_edges.items()],
        },
        "insufficient_trace": sparse, "falsify": falsify_info, "gate": gate,
        "refill": refill_summary, "confidence": confidence_info, "detect": detect_info,
        "data_quality": {
            **data_quality,
            **({"hang_probe": hang_findings} if hang_findings else {}),
            "cpu": {"current": cpu_status, "baseline": cpu_baseline_status,
                    "baseline_window": cpu_base},
            "memory": {"current": mem_status, "baseline": mem_baseline_status,
                       "baseline_window": mem_base},
        },
    }
    # 开方放在 result 组装后:它消费的就是这份终稿(含置信/缺口),写回 narrative
    result["narrative"] = await narrative_mod.prescribe(service, symptom_text, result)
    _VERDICT_TEXT = {"root_cause": "定位到根因 {s}", "critical_failure": "关键故障 {s}(根因服务无法进一步解释)",
                     "impact": "症状服务仅被波及,证据里没有根因", "inconclusive": "证据不足,未定位根因(缺口见卡片详情)",
                     "clear": "平台正常,未发现异常", "victims_only": "有异常但只指向受害者,指不出源头",
                     "no_data": "数据不可用,无法判断"}
    _done_text = _VERDICT_TEXT.get(attr.verdict, attr.verdict).format(s=attr.root_cause_service or "")
    _emit(run, "done", "诊断完成:" + _done_text
                       + ("" if attr.verdict == "inconclusive" or not attr.missing
                          else ";首要缺口:" + attr.missing[0]))
    return result


def get(diag_id: str) -> dict | None:
    return _RUNS.get(diag_id)
