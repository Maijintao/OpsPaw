"""修复 agent:接手已确认根因的归因卡片,自主决策完成修复并输出修复回执。

和旧路径的区别(2026-10-19 重做):动作不再由代码拍板(resolve_auto_action 按容器
现状硬选 restart/start,那版理解错了需求),而是 **LLM 决策**——它拿着归因卡片
(开方四字段 + 故障形态 + 置信)自己思考:

  复核现场(scene) → 修复执行(fix) → 确认结果(verify) → 写回执(report)

- scene:先跑一次指标侧症状复检(服务级,同一套分位判据),LLM 再用连接器只读动作
  (inspect/logs/events/resources/list)进一步确认现场——查什么、查几轮由它决定;
- fix:LLM 只选**写动作**(restart/start/stop/recreate 四选一)和理由,执行仍走
  plans 状态机(计划 hash + 审批 + 预检查 + 冷却 + 单并发 + 独立验证),一条不动。
  根因已被用户确认 = 本次授权,审批照走流程留痕但自动通过;失败不换动作重试,
  停下来如实说(防重启循环,冷却闸也是这个精神);
- verify:LLM 再次用只读动作探测,判断原错误是否消失、服务是否恢复;
- report:像医生写回执,分四段(现场确认/修复过程/结果确认/agent 看法)输出,
  终态落库,卡片长存前端。

LLM 交互全部走 structured(同开方那条通路);每步只做一个决定,不搞自由发挥。
循环步数与并发是拍值(没有实测依据):scene/verify 各 ≤4 步是"够看清现场又不让
模型绕圈",并发 2 是防多个值班同学同时点修复把连接器排队挤爆;调整语义见各常量处。
"""
import asyncio
import logging
import time
import uuid

from pydantic import BaseModel, Field

from app.config import settings
from app.core.llm import structured
from app.db import repository
from app.repair import catalog, gateway, plans, symptom

logger = logging.getLogger("app.repair")

_RUNS: dict[str, dict] = {}
MAX_RUNS = 100   # 同诊断 runner:内存只做热缓存,终态落库

# 修复单全局并发闸。写动作本身有连接器级单并发锁(plans._LOCKS),这里是防
# 多单同时跑 scene/verify 的只读查询把连接器长轮询队列(每连接器 8 个槽)占满。
_SEM = asyncio.Semaphore(settings.max_agent_steps)

# scene / verify 两个阶段的 LLM 决策步数上限(每步一次结构化调用)。
# 4 = 最少 1 次 inspect + 最多 3 次深挖(日志/事件/资源),再多就是模型在绕圈。

# scene / verify 允许 LLM 用的只读动作:能力 = 服务端目录 ∩ 只读,写动作不在这里,
# 它有专属的决策步(FixDecision)+ 状态机执行,模型没有直接执行写动作的入口。
SCENE_TOOLS = ("inspect_service", "service_logs", "service_events",
               "service_resources", "list_services",
               "list_flags", "get_flag", "service_connections")

# 写动作白名单(FixDecision 可选的):flag 类排前面,它们是 AIOpsLab 形态的主修复路径
FIX_ACTIONS = ("set_flag", "restore_flags",
               "restart_service", "start_service", "stop_service", "recreate_service")

STAGES = ("scene", "fix", "verify", "report")


class RepairError(Exception):
    pass


def _emit(run: dict, stage: str, detail: str) -> None:
    run["events"].append({"stage": stage, "detail": detail,
                          "ts": time.strftime("%H:%M:%S")})
    run["stage"] = stage


def get(repair_id: str) -> dict | None:
    return _RUNS.get(repair_id)


async def start_for_diagnosis(diag_id: str) -> dict:
    """从已确认根因的诊断单直接派出修复 agent(API 与聊天路径共用入口)。
    校验、平台/连接器定位都收在这里,失败抛 RepairError(话术直接给用户看)。"""
    from app.diagnosis import runner
    run = runner.get(diag_id) or await repository.get_diagnosis_run(diag_id)
    if run is None:
        raise RepairError("诊断任务不存在")
    if run.get("status") != "done":
        raise RepairError("诊断还没结束,不能启动修复")
    res = run.get("result") or {}
    if res.get("verdict") not in ("root_cause", "critical_failure"):
        raise RepairError("诊断结论不是根因,不能自动修复;请走人工处理")
    if (run.get("confirmation") or "pending") != "confirmed":
        raise RepairError("根因还没被用户确认,先在诊断卡片上确认根因")
    service = res.get("root_cause_service")
    if not service:
        raise RepairError("诊断结论没有根因服务,不能启动修复")

    targets = [r for r in await repository.list_targets() if r.enabled]
    platform = run.get("platform")
    target = next((r for r in targets if platform and r.name == platform), None)
    if target is None:
        from app.tools import service_catalog
        try:
            services, _ = await service_catalog.discover_services()
            hit = service_catalog.find_service(services, service)
        except Exception:  # noqa: BLE001  服务发现失败走后面明确报错,不炸派单
            hit = None
        if hit is not None:
            target = next((r for r in targets if r.name == hit["target"]), None)
    if target is None:
        raise RepairError("找不到这个服务所属的接入平台")
    conns = [c for c in await repository.list_repair_connectors(target.id)
             if c.status != "revoked" and gateway.is_online(c.id)]
    if not conns:
        raise RepairError(f"平台「{target.name}」还没有在线的修复连接器")
    if len(conns) > 1:
        raise RepairError("这个平台有多个连接器在线,请到后台指定")

    repair_id = start(run, target.id, target.name, conns[0].id)
    await repository.add_repair_audit(
        "repair_agent_started", target_id=target.id, connector_id=conns[0].id,
        actor="user", payload={"diagnosis_id": diag_id, "repair_id": repair_id,
                               "service": service})
    return {"repair_id": repair_id, "service": service, "target": target.name}


async def start_for_conversation(conversation_id: int | None) -> dict:
    """聊天路径:用会话里最近一次已确认根因的诊断派单(用户说"修复"时不用重复报服务名)。"""
    if not conversation_id:
        raise RepairError("这个会话还没有已确认根因的诊断,先完成诊断并确认根因")
    diag = await repository.find_latest_confirmed_diagnosis(conversation_id)
    if diag is None:
        raise RepairError("这个会话里还没有已确认根因的诊断,先完成诊断并确认根因")
    return await start_for_diagnosis(diag.id)


# ---------- LLM 结构化输出 ----------

class SceneStep(BaseModel):
    """现场复核的一步:查什么、为什么查;查够了就 finish 并给小结。"""
    think: str = Field(description="为什么这一步要查这个(一句大白话)")
    tool: str = Field(description=f"只读动作,只能选:{', '.join(SCENE_TOOLS)};"
                                 "现场已经看够了就填 finish")
    service: str = Field(default="", description="要查的服务名;list_services/list_flags 不需要")
    flag: str = Field(default="", description="get_flag 要查的开关名")
    target_host: str = Field(default="", description="service_connections 的目标主机名"
                                                     "(compose 网络里通常就是服务名);留空取 service")
    target_port: int = Field(default=0, description="service_connections 的目标端口")
    tail: int = Field(default=100, description="service_logs 的行数,1-200")
    limit: int = Field(default=20, description="service_events 的条数,1-100")
    summary: str = Field(default="", description="tool=finish 时必填:现场长什么样、"
                                                "和归因卡片对不对得上、症状还在不在")


class FixDecision(BaseModel):
    """写动作决策:模型只选动作、参数和理由,执行权在状态机。"""
    think: str = Field(description="为什么选这个动作:结合现场证据说明它对症在哪、"
                                   "有什么风险、为什么可接受(两三句大白话)")
    action: str = Field(description="写动作,只能选:" + " / ".join(FIX_ACTIONS) +
                                    ";症状已消失不该动手时填 none")
    flag: str = Field(default="", description="action=set_flag 时必填:要切的开关名")
    variant: str = Field(default="", description="action=set_flag 时必填:切到哪个变体"
                                                 "(通常是 off)")


class RepairReport(BaseModel):
    """修复回执:像医生写手术记录,四段式,全部大白话。"""
    scene: str = Field(description="现场确认:修复前看到了什么(指标/容器状态/日志),"
                                   "症状在不在,和归因卡片对不对得上")
    action: str = Field(description="修复过程:做了什么动作、为什么选它、执行结果如何")
    result: str = Field(description="结果确认:修复后再探测看到了什么,原错误还在不在,"
                                    "服务算不算恢复;证据不足就如实说看不准")
    opinion: str = Field(description="agent 的看法:修复顺利就写经验与注意事项;"
                                     "失败就写自己认为为什么失败、建议下一步怎么走"
                                     "(转人工/换方向/补充什么信息)")


# ---------- LLM 决策 loop(只读) ----------

async def _read_loop(run: dict, stage: str, service: str, context: str,
                     max_steps: int, collected: list[str]) -> str:
    """让 LLM 自主用只读动作看现场/验结果。每步:结构化决定 → gateway 执行 →
    结果原样(截断)喂回去。返回 LLM 的 finish 小结;步数用完没 finish 就如实说明。"""
    tool_lines = "\n".join(
        f"- {name}:{catalog.READ_ACTIONS[name]['desc']}(参数:{','.join(catalog.READ_ACTIONS[name]['params']) or '无'})"
        for name in SCENE_TOOLS)
    history = "\n".join(f"[已查{i + 1}] {ln}" for i, ln in enumerate(collected)) or "(还没查过任何东西)"
    prompt = (
        "你是修复 agent,正在用修复连接器的只读动作"
        + ("复核故障现场" if stage == "scene" else "确认修复结果") + "。\n"
        f"目标服务:{service}。\n"
        f"{context}\n"
        "可用的只读动作:\n" + tool_lines + "\n"
        "已经查到的信息:\n" + history + "\n\n"
        "决定下一步:要么选一个只读动作查一样东西(只查一步,别贪多),"
        "要么填 finish 结束并给小结。规则:\n"
        "- 每一步只回答一件事,已经查过的不要重复查;\n"
        + ("现在要做的是:确认症状还在(不能不确认就动手修)、看清容器和日志现场,"
           "并检查 feature flag——很多故障是某个开关被切到非 off 导致的,开关不查,"
           "修了也是白修;" if stage == "scene" else
           "现在要做的是:确认原来的错误还在不在、服务是不是真的恢复;"
           "重点看容器状态和最新日志,必要时对照修复前的发现;\n")
        + "- 查询失败/没数据就换个角度看或如实记录,不要编造;\n"
        # 名字靠猜 = 结论无效。2026-10-22 实测:验证阶段去探 cart→redis-cart:6379,
        # 这套部署里根本没有 redis-cart(它叫 valkey-cart),探测自然"不通",
        # 于是一次成功的修复被写成了"反而更糟"。
        + "- 探依赖连通性(service_connections)时,主机名只能取自 list_services 的清单"
          "或日志里实际出现的名字;凭印象填的名字可能根本不存在,那样得到的\"不通\"不作数;\n"
        + ("" if stage == "scene" else
           "- 说\"比修复前更糟\"之前先对照修复前的发现:修复前就已经存在的报错不算新问题,"
           "只有修复后才出现、且修复前确实没有的才算变差;名字对不上的探测结论直接作废;\n")
        + "- 信息够下判断就立刻 finish,不要为了查而查。")
    try:
        step = await structured(SceneStep, temperature=0).ainvoke(prompt)
    except Exception as exc:  # noqa: BLE001  模型不可用不炸任务,用已查到的信息继续走
        _emit(run, stage, f"决策模型不可用({type(exc).__name__}),"
                          f"以已查到的 {len(collected)} 条信息继续")
        return "决策模型不可用,未获得现场小结" if not collected else collected[-1]
    if step.tool == "finish":
        if step.summary.strip():
            collected.append("小结:" + step.summary.strip())
        return step.summary.strip() or "(未给出小结)"
    if step.tool not in SCENE_TOOLS:
        collected.append(f"模型想用「{step.tool}」,不在只读动作清单里,已拒绝")
        return await _read_loop(run, stage, service, context, max_steps - 1, collected)
    svc = (step.service or service).strip()
    params: dict = {}
    if "service" in catalog.READ_ACTIONS[step.tool]["params"]:
        import re as _re
        if not plans.SERVICE_RE.fullmatch(svc):
            collected.append(f"服务名「{svc}」不合法,这条查询没执行")
            return await _read_loop(run, stage, service, context, max_steps - 1, collected)
        params["service"] = svc
    if "flag" in catalog.READ_ACTIONS[step.tool]["params"]:
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9_]{1,64}", step.flag or ""):
            collected.append("开关名不合法,这条查询没执行")
            return await _read_loop(run, stage, service, context, max_steps - 1, collected)
        params["flag"] = step.flag
    if "target_host" in catalog.READ_ACTIONS[step.tool]["params"]:
        params["target_host"] = (step.target_host or svc).strip()
        params["target_port"] = int(step.target_port or 0)
    if "tail" in catalog.READ_ACTIONS[step.tool]["params"]:
        params["tail"] = max(1, min(int(step.tail or 100), 200))
    if "limit" in catalog.READ_ACTIONS[step.tool]["params"]:
        params["limit"] = max(1, min(int(step.limit or 20), 100))
    run["steps"] = run.get("steps", 0) + 1
    _emit(run, stage, f"{step.think.strip()} → {catalog.label(step.tool)}({svc or '本机'})")
    # 同动作同参数的查询直接复用上次结果:两单实测都出现过重复拉同一份日志,
    # 白烧一步预算还拖时间。缓存按阶段隔离(scene/verify 各自独立)——inspect/get_flag
    # 的结果会随修复变化,跨阶段复用会把修复前的旧状态带进验证,那就是错误
    sig = (stage, step.tool, tuple(sorted(params.items())))
    cache = run.setdefault("_query_cache", {})
    if sig in cache:
        line = cache[sig]
        collected.append(f"[{catalog.label(step.tool)}](与之前相同) {line}")
        if max_steps - 1 <= 0:
            _emit(run, stage, "现场查看步数已用完,按已有信息继续")
            return collected[-1] if collected else "(步数用完,无小结)"
        return await _read_loop(run, stage, service, context, max_steps - 1, collected)
    try:
        out = await gateway.call(run["connector_id"], step.tool, params, timeout=25)
    except gateway.ConnectorOffline as exc:
        out = {"ok": False, "status": "offline", "message": str(exc), "data": {}}
    # 结果压进证据流:成功给结构化要点,失败给原话,不替模型嚼碎也不给它编的空间
    if out.get("ok"):
        data = out.get("data") or {}
        if step.tool == "list_services":
            names = [s.get("name") or s.get("service") for s in data.get("services", [])
                     if isinstance(s, dict)]
            line = f"本机容器 {len(names)} 个:" + "、".join(map(str, names[:20]))
        elif step.tool == "list_flags":
            flags = data.get("flags") or {}
            non_off = data.get("non_off") or []
            detail = ";".join(f"{k}={s.get('current')}(可选:{','.join(s.get('variants') or [])})"
                              for k, s in flags.items())
            line = (f"共 {len(flags)} 个开关"
                    + (f",非 off:{'、'.join(map(str, non_off))}" if non_off else ",全部 off")
                    + "\n" + detail)
        elif step.tool == "get_flag":
            if data.get("found"):
                line = (f"开关 {data.get('flag')} 当前 = {data.get('current')}"
                        f",可选:{','.join(data.get('variants') or [])}"
                        + (",带 targeting 规则" if data.get("has_targeting") else ""))
            else:
                line = f"开关 {data.get('flag')} 不存在"
        elif step.tool == "service_connections":
            line = (f"{data.get('service')} -> {data.get('target_host')}:"
                    f"{data.get('target_port')} "
                    + ("连通" if data.get("open") else "不通"))
        elif step.tool == "inspect_service":
            line = (f"运行:{'在跑' if data.get('running') else '没在跑'}"
                    f",健康 {data.get('health') or '未知'},状态 {data.get('state') or '未知'}"
                    f",启动时间 {data.get('started_at') or '未知'}"
                    f",重启次数 {data.get('restart_count', '未知')}")
        elif step.tool == "service_logs":
            logs = data.get("logs") or data.get("lines") or ""
            text = logs if isinstance(logs, str) else "\n".join(map(str, logs))
            line = f"最近日志(截尾):\n{text[-1500:]}" if text.strip() else "日志为空"
        elif step.tool == "service_events":
            evs = data.get("events") or []
            lines = [str(e) for e in evs[:30]]
            line = "最近事件:\n" + "\n".join(lines) if lines else "事件为空"
        else:  # service_resources
            line = ("资源:CPU " + str(data.get("cpu", "未知"))
                    + ",内存 " + str(data.get("memory", "未知"))
                    + ",网络 " + str(data.get("network", "未知")))
    else:
        line = f"查询失败:{out.get('message') or out.get('status') or '未知原因'}"
    cache[sig] = line
    collected.append(f"[{catalog.label(step.tool)}] {line}")
    if max_steps - 1 <= 0:
        _emit(run, stage, "现场查看步数已用完,按已有信息继续")
        return collected[-1] if collected else "(步数用完,无小结)"
    return await _read_loop(run, stage, service, context, max_steps - 1, collected)


# ---------- 主流程 ----------

def start(diag_run: dict, target_id: int, target_name: str, connector_id: str,
          conversation_id: int | None = None) -> str:
    repair_id = uuid.uuid4().hex[:12]
    service = (diag_run.get("result") or {}).get("root_cause_service") \
        or diag_run.get("service") or ""
    run = {"id": repair_id, "diagnosis_id": diag_run.get("id"),
           "conversation_id": conversation_id or diag_run.get("conversation_id"),
           "service": service, "target_id": target_id, "platform": target_name,
           "connector_id": connector_id, "status": "running", "stage": "scene",
           "events": [], "result": None,
           "created_at": time.strftime("%H:%M:%S")}
    _RUNS[repair_id] = run
    if len(_RUNS) > MAX_RUNS:
        terminal = [k for k, v in _RUNS.items() if v.get("status") in ("done", "failed")]
        for k in terminal[:len(_RUNS) - MAX_RUNS]:
            _RUNS.pop(k, None)
    asyncio.create_task(_safe_run(run, diag_run))
    return repair_id


async def _safe_run(run: dict, diag_run: dict) -> None:
    try:
        async with _SEM:
            run["result"] = await _run(run, diag_run)
        run["status"] = "done"
    except Exception as e:  # noqa: BLE001  修复任务不许静默死,错误如实落终态
        logger.exception("修复任务失败 service=%s", run.get("service"))
        run["status"] = "failed"
        run["result"] = run.get("result") or {}
        run["result"].setdefault("verdict", "failed")
        _emit(run, "failed", f"修复 agent 异常:{type(e).__name__}: {e}")
    finally:
        try:
            await repository.save_repair_run(run)
        except Exception:  # noqa: BLE001  落库失败已在 save 内告警
            logger.warning("修复任务 %s 存档异常", run.get("id"), exc_info=True)
        # 事故中心联动:修复回执推进所属事故(派单→修复中,成功→已解决)。
        # 增强信号,失败只记日志;懒加载避免导入环。
        try:
            from app.api import incidents as _incidents
            await _incidents.on_repair(run["id"], run.get("diagnosis_id"),
                                       (run.get("result") or {}).get("verdict"),
                                       run.get("status"),
                                       (run.get("result") or {}).get("action"))
        except Exception:  # noqa: BLE001
            logger.warning("事故联动(修复)失败 repair=%s", run.get("id"), exc_info=True)


def _diag_context(diag_run: dict) -> str:
    """归因卡片 → 修复 agent 的交接材料:开方四字段 + 形态 + 置信,原样转述不加工。"""
    res = diag_run.get("result") or {}
    nar = res.get("narrative") or {}
    ft = res.get("fault_type") or {}
    conf = res.get("confidence") or {}
    parts = [
        f"症状:{diag_run.get('symptom') or '系统自主巡检发现异常'}",
        # 多云:同一集群里的其他平台是同一套系统的另外几朵云,这台机器上的连接器只管本平台
        (f"部署位置:多云集群「{diag_run.get('cluster')}」中的平台「{diag_run.get('platform')}」,"
         f"只在这台机器上修;其他云上的同名服务不归这次修"
         if diag_run.get("cluster") else
         (f"部署位置:平台「{diag_run.get('platform')}」" if diag_run.get("platform") else "")),
        "诊断开方(上一棒诊断 agent 写给你的):",
        f"- 发生了什么:{nar.get('description') or '(缺)'}",
        f"- 病根:{nar.get('fault') or '(缺)'}",
        f"- 依据:{nar.get('reason') or '(缺)'}",
        f"- 交接注意:{nar.get('other') or '无'}",
        f"故障形态:{ft.get('text') or '未定性'}({', '.join(ft.get('basis') or []) or '无判据'})",
        f"证据充分度:{conf.get('tier') or 'unknown'}({conf.get('summary') or ''})",
        "建议动作:" + (";".join(res.get("suggested_actions") or []) or "无"),
    ]
    return "\n".join(parts)


async def _runtime_of(run: dict) -> tuple[str, str]:
    """这台机器是 docker 还是 k8s——动作语义完全不同(重启在 k8s 是滚动重启 Deployment,
    停止是副本数缩到 0,根本没有"容器"这个对象)。模型不知道这件事就会按容器口径
    描述现场、给错动作。连接器注册时把运行时写在 host_info.runtime 里。"""
    rt = "docker"
    try:
        rows = await repository.list_repair_connectors(run.get("target_id"))
        row = next((r for r in rows if r.id == run.get("connector_id")), None)
        info = (row.host_info or {}) if row else {}
        rt = str(info.get("runtime") or "docker")
    except Exception:  # noqa: BLE001  取不到就按 docker,不阻断修复
        rt = "docker"
    note = {
        "docker": "运行环境:docker compose。动作语义 = 重启/启动/停止容器,日志看 docker logs。",
        "k8s": ("运行环境:kubernetes(kubectl)。动作语义 = 重启是滚动重启 Deployment、"
                "启动/停止是副本数 1/0、日志看 kubectl logs;没有 docker 容器这个概念,"
                "描述现场时按 pod/Deployment 说,不要用容器口径。"),
    }.get(rt, "")
    return rt, note


def _action_menu(runtime: str) -> str:
    """动作清单按运行时给:同一批动作,在 k8s 下说的是另一套东西。"""
    if runtime == "k8s":
        return (
            "- set_flag(切开关):现场发现某个 feature flag 被切到非 off/错误变体时,"
            "这就是正解——把它切回 off(写的是 flagd 的 ConfigMap,容器内生效有几十秒延迟);\n"
            "- restore_flags(复位全部开关):多个开关都被动过时用它一次清干净;\n"
            "- start_service(启动):Deployment 被缩到 0 副本时把它扩回 1;\n"
            "- restart_service(重启):pod 在跑但进程级异常(崩溃循环、僵死)→ 滚动重启;\n"
            "- recreate_service(重建):怀疑配置/镜像被改坏,需要重新拉起;\n"
            "- stop_service(停止):主动下线才用(副本数缩到 0),修复场景基本不该选;\n")
    return (
        "- set_flag(切开关):现场发现某个 feature flag 被切到非 off/错误变体时,"
        "这就是正解——把它切回正常值(通常是 off)。flag 类故障重启服务是治不好的"
        "(配置在宿主机文件里,重启容器照旧读故障值),**不要**对 flag 故障用重启;\n"
        "- restore_flags(复位全部开关):多个开关都被动过时用它一次清干净;\n"
        "- start_service(启动):容器没在跑(exited/stopped);\n"
        "- restart_service(重启):容器在跑但进程级异常(崩溃循环、僵死);\n"
        "- recreate_service(重建):怀疑容器可写层被改坏,需要按 Compose 配置重建;\n"
        "- stop_service(停止):主动下线才用,修复场景基本不该选;\n")


async def _run(run: dict, diag_run: dict) -> dict:
    service = run["service"]
    runtime, runtime_note = await _runtime_of(run)
    run["runtime"] = runtime
    diag_ctx = _diag_context(diag_run)
    if runtime_note:
        diag_ctx = diag_ctx + "\n" + runtime_note
        _emit(run, "scene", f"运行环境:{runtime}")

    # ---- scene:复核现场 ----
    _emit(run, "scene", f"复核现场中:{service} 的指标侧症状复检…")
    sym = await symptom.check(run["target_id"], service)
    scene_notes: list[str] = [f"指标侧复检:{sym.get('reason') or sym.get('status')}"]
    _emit(run, "scene", f"指标侧复检结果:{sym.get('reason') or sym.get('status')}")
    # 固定动作:开关状态必查(不依赖 LLM 自觉)。flag 注入形态的故障在日志和指标上
    # 可能都不显形(两单实测),开关值是唯一的决定性证据——它必须出现在现场材料里,
    # 否则 LLM 没查到就可能误判"症状已消失"。这是确定性保障,不是替模型做决策。
    try:
        fl = await gateway.call(run["connector_id"], "list_flags", {}, timeout=25)
        if fl.get("ok"):
            data = fl.get("data") or {}
            non_off = data.get("non_off") or []
            flags = data.get("flags") or {}
            detail = ";".join(f"{k}={s.get('current')}(可选:{','.join(s.get('variants') or [])})"
                              for k, s in flags.items())
            line = (f"全平台开关({len(flags)} 个):"
                    + (f"非 off:{'、'.join(map(str, non_off))}" if non_off else "全部 off")
                    + "\n" + detail)
            _emit(run, "scene", "开关状态已入现场材料"
                  + (f"(非 off:{'、'.join(map(str, non_off))})" if non_off else "(全部 off)"))
        else:
            line = f"开关查询失败:{fl.get('message') or '未知原因'}"
            _emit(run, "scene", "开关状态查询失败(不影响后续判断,已如实记录)")
    except gateway.ConnectorOffline as exc:
        line = f"开关查询失败:{exc}"
        _emit(run, "scene", "开关状态查询失败(连接器失联)")
    scene_notes.append("功能开关现场:" + line)
    scene_summary = await _read_loop(
        run, "scene", service, diag_ctx + "\n\n指标侧复检:" + (sym.get("reason") or ""),
        settings.max_agent_steps, scene_notes)

    # ---- fix:修复执行(决策在 LLM,执行在状态机) ----
    _emit(run, "fix", "修复执行中:agent 决策修复动作…")
    decision = await structured(FixDecision, temperature=0).ainvoke(
        "你是修复 agent,现场已复核完,现在决定修复动作。\n"
        f"目标服务:{service}\n{diag_ctx}\n\n"
        "现场复核材料:\n" + "\n".join(scene_notes) + f"\n\n小结:{scene_summary}\n\n"
        "可选动作及对症关系:\n" + _action_menu(runtime) +
        "- none:不执行任何动作。**只有两个证据都安静才允许选 none**:指标复检 resolved"
        " 且现场(日志/事件/容器状态)也没有异常实锤。\n"
        "规则:\n"
        "- **现场实锤优先于指标**:指标窗口可能还没被故障填满(故障刚发生几分钟时,"
        "错误率被稀释成『正常』是常见假象),也可能对某些故障形态不显形(如开关注入"
        "的失败根本不在指标里)。『功能开关现场』里有非 off 开关时,它就是最硬的"
        "现场实锤,必须作为决策的第一依据;\n"
        "- **有非 off 开关且与故障服务/形态相关时,正解就是 set_flag 切回 off**,"
        "此时禁止选 none、禁止用重启代替;\n"
        "- 选 none 的门槛是三件事同时成立:指标复检 resolved + 现场日志/事件安静 + "
        "所有开关都是 off。缺一不可;\n"
        "- set_flag 必须填 flag 和 variant(恢复通常切回 off,变体名从复核材料里找);\n"
        "- 评估风险,选最保守的对症动作,理由里写清不确定性。")
    action = (decision.action or "").strip()
    if action == "none":
        _emit(run, "fix", "agent 判断症状已消失,不执行修复动作")
        # verify_notes 还没走到确认阶段,传空列表——别把 None 喂给 join
        report = await _report(run, diag_ctx, scene_notes, scene_summary,
                               None, None, [])
        return {"verdict": "symptom_resolved", "action": None, "plan_id": None,
                "report": report, "scene_notes": scene_notes}
    if action not in catalog.ACTIONS:
        raise RepairError(f"模型选了不可执行的动作「{action}」,修复中止")
    fix_params: dict = {}
    if action == "set_flag":
        fix_params = {"flag": (decision.flag or "").strip(),
                      "variant": (decision.variant or "").strip()}
        if not fix_params["flag"] or not fix_params["variant"]:
            raise RepairError("选了切换开关但没给开关名/目标变体,修复中止")

    _emit(run, "fix", f"决策:{catalog.label(action)}——{decision.think.strip()[:120]}")
    plan = None
    execution = None
    plan_error: str | None = None
    try:
        plan = await plans.create(
            run["target_id"], run["connector_id"], action, service,
            reason=f"[修复 agent] {decision.think.strip()}",
            diagnosis_id=run["diagnosis_id"], conversation_id=run.get("conversation_id"),
            created_by="repair-agent", params=fix_params or None)
        ap = await plans.approve(plan["id"], approved_by="repair-agent:根因已确认")
        _emit(run, "fix", f"计划 {plan['id']} 已按「根因已确认」授权自动审批,执行中…")
        execution = await plans.execute(plan["id"], ap["approval_id"])
    except plans.PlanError as exc:
        # 状态机拒了(没声明该动作/冷却中/预检查不过/连接器掉线…):不换动作硬试,
        # 停下来让 agent 在回执里说看法——"为什么失败"本身就是交付的一部分
        plan_error = str(exc)
        _emit(run, "fix", f"状态机拒绝执行:{plan_error};不重试,进入回执")
    final_status = (execution or {}).get("status") or "execution_failed"
    verification = ((execution or {}).get("verification") or {}) if execution else {}
    post = verification.get("post_symptom") or {}
    if execution is not None:
        _emit(run, "fix", f"执行结束:{final_status};"
                          f"容器验证:{verification.get('status') or '无'}"
                          + (f";指标复检:{post.get('status')}" if post else ""))
        if final_status not in ("succeeded", "verification_failed",
                                "verification_unknown", "symptom_resolved"):
            # execution_failed / execution_unknown:不换动作重试,停下来如实说
            _emit(run, "fix", "执行未成功,不再换动作重试(防重启循环),进入回执")

    # ---- verify:确认结果 ----
    _emit(run, "verify", "确认结果中:agent 再探测原错误是否消失…")
    if plan_error:
        verify_ctx = (f"你选了 {catalog.label(action)},但状态机拒绝执行:{plan_error}。"
                      "修复没有真正执行,你再查一遍现场,为回执和下一步建议攒依据。")
    else:
        verify_ctx = (f"你刚执行了 {catalog.label(action)},状态机回报:"
                      f"执行结果 {final_status},容器验证 {verification.get('status') or '无'}"
                      f"({verification.get('reason') or '无理由'}),"
                      f"指标复检 {post.get('status') or '无'}({post.get('reason') or ''})。"
                      "注意:这些是机器快照,你要亲自再查一遍才算数。")
    verify_notes: list[str] = []
    await _read_loop(run, "verify", service, diag_ctx + "\n\n" + verify_ctx,
                     settings.max_agent_steps, verify_notes)

    # ---- report:写回执 ----
    _emit(run, "report", "写回执中:agent 整理修复回执…")
    report = await _report(run, diag_ctx, scene_notes, scene_summary,
                           decision, execution, verify_notes, plan_error)
    # 可回滚信息:set_flag 改前的旧值在连接器回包里,留着给"一键回滚"用。
    # 容器类动作没有"撤销重启"这种逆操作,不生成 rollback,卡片上明确回滚=人工。
    rollback = None
    if plan_error is None and execution is not None and decision.action == "set_flag":
        prev = ((execution.get("result") or {}).get("data") or {}).get("previous")
        if prev:
            rollback = {"action": "set_flag",
                        "params": {"flag": fix_params["flag"], "variant": prev}}
    return {"verdict": final_status, "action": action,
            "plan_id": plan["id"] if plan else None,
            "execution_id": (execution or {}).get("execution_id"),
            "report": report, "scene_notes": scene_notes,
            "verify_notes": verify_notes, "rollback": rollback,
            "verification": {k: verification.get(k) for k in
                             ("status", "mode", "reason", "before", "after")},
            "post_symptom": post}


async def _report(run: dict, diag_ctx: str, scene_notes: list[str],
                  scene_summary: str, decision, execution, verify_notes,
                  plan_error: str | None = None) -> dict:
    """回执是 agent 的交付口:成功写经验,失败写看法,都像医生开方那样分四段。"""
    fix_line = (f"执行了 {catalog.label(decision.action)},理由:{decision.think}"
                if decision else "未执行修复动作(agent 判断症状已消失)")
    if plan_error:
        fix_line += f";状态机拒绝执行:{plan_error}"
    exec_line = ""
    if execution is not None:
        v = execution.get("verification") or {}
        exec_line = (f"状态机回报:{execution.get('status')};"
                     f"容器验证:{v.get('status') or '无'}"
                     f"({v.get('reason') or '无'});"
                     f"修复后观测:{(v.get('post_symptom') or {}).get('status') or '无'}")
    prompt = (
        "你是修复 agent,修复流程跑完了,现在写修复回执——像医生写手术记录,"
        "给值班同学和后续复盘的人看。全部大白话,不堆术语,不编造没发生过的事。\n"
        f"目标服务:{run['service']}\n{diag_ctx}\n\n"
        "现场复核材料:\n" + "\n".join(scene_notes) +
        f"\n现场小结:{scene_summary}\n\n"
        f"修复过程:{fix_line}\n" + (exec_line + "\n" if exec_line else "") +
        "修复后再探测:\n" + ("\n".join(verify_notes) or "(只读探测没有留下记录)") + "\n\n"
        "按四个字段写:\n"
        "1. scene:修复前现场什么样,症状在不在,和归因卡片对不对得上;\n"
        "2. action:做了什么、为什么、结果如何;"
        + ("状态机拒了这次执行的话,把拒绝原因如实写进来;\n"
           if plan_error else "\n") +
        "3. result:修复后再探测看到了什么,原错误还在不在,算不算恢复;"
        "没真正执行就如实说没有执行后的对照;"
        "证据不足就如实说看不准;"
        "**报错要分清是修复后才冒出来的、还是修复前就有的**——修复前已经存在的报错"
        "属于既有问题,不能算到这次修复头上;名字对不上的探测(比如探了一个本机根本"
        "不存在的容器名)结论作废,别当成新故障;\n"
        "4. opinion:你的看法——顺利就写经验和注意点;没修好就认真说你认为"
        "为什么失败(动作不对症?被状态机拦下?数据不足?故障自己变了?),"
        "建议下一步怎么走。\n"
        "每段 1-4 句,不要分点编号,不要客套。")
    try:
        out = await structured(RepairReport, temperature=0).ainvoke(prompt)
        return {"scene": (out.scene or "").strip(), "action": (out.action or "").strip(),
                "result": (out.result or "").strip(), "opinion": (out.opinion or "").strip()}
    except Exception as exc:  # noqa: BLE001  回执是增强,失败用确定性兜底,事实一个不编
        logger.warning("修复回执 LLM 失败(%s),用确定性兜底", type(exc).__name__)
        verdict = (execution or {}).get("status") if execution else None
        return {
            "scene": ";".join(scene_notes) or "(无现场记录)",
            "action": fix_line + (f";{exec_line}" if exec_line else ""),
            "result": ";".join(verify_notes) or "(无修复后探测记录)",
            "opinion": f"回执生成模型不可用({type(exc).__name__}),以上为流程原始记录;"
                       f"最终状态 {verdict or run.get('status')}",
        }
