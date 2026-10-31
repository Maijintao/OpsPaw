import json
import logging
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from app.config import settings
from app.core import coref
from app.core import intent as intent_mod
from app.core import memory
from app.core import query_understanding, retrieval
from app.core.confidence import compute_evidence_confidence, snapshot_from_hits
from app.core.llm import get_chat_model
from app.core.observability import tag_intent
from app.core.prompts import (
    AGENT_SYSTEM, CHANGE_JUDGE_HINT, COMPLAINT_REPLY_TEXT,
    SCRIPT_REPLY_CHITCHAT, SCRIPT_REPLY_INTRO, SCRIPT_REPLY_OTHER,
)
from app.db import repository
from app.graph.routing import INTENT_TO_ROUTE
from app.tools import service_catalog, engine, registry

logger = logging.getLogger(__name__)

COMPLAINT_REPLY = COMPLAINT_REPLY_TEXT


def _user_text(state) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return m.content or ""
    return ""


def _history_text(state) -> str:
    """摘要行 + 完整滑窗(不含本轮最后一条 human),供 coref/意图读上下文。
    之前只取最近 6 轮,"上一轮提问、这一轮确认"的延续经常被裁出窗口,意图识别
    拿不到待确认的提议就判闲聊,对话当场掐断(真实案例:"去呀!"落零模型话术)。
    滑窗本身有 token 上限(与主 Agent 同窗),这里不再二次裁剪。"""
    msgs = memory.build_window(state.get("messages", []),
                               state.get("summary_upto_msg_id") or 0,
                               settings.context_window_max_tokens)
    prior = msgs[:-1] if msgs else []
    lines = []
    for m in prior:
        role = "用户" if isinstance(m, HumanMessage) else "值班猫"
        text = m.content if isinstance(m.content, str) else ""
        if text:
            lines.append(f"{role}:{text}")
    body = "\n".join(lines)
    head = memory.summary_line(state.get("summary"))
    return f"{head}\n{body}".strip() if head else body


def _last_assistant_text(state) -> str:
    """滑窗里值班猫的最后一条发言(判断上轮是否以提问收尾用)。"""
    msgs = memory.build_window(state.get("messages", []),
                               state.get("summary_upto_msg_id") or 0,
                               settings.context_window_max_tokens)
    for m in reversed(msgs[:-1] if msgs else []):
        if isinstance(m, AIMessage):
            return m.content if isinstance(m.content, str) else ""
    return ""


# 服务名匹配辅助:从用户话里找已发现服务名的子串(大小写不敏感),替代旧「4+位数字=服务编号」
# 的 mock 时代假设——真实服务用名字,由发现结果反查,不维护正则词表


def _match_service_name(text: str, services: list[dict]) -> str | None:
    t = (text or "").lower()
    for s in services:
        if s["service_id"].lower() in t:
            return s["service_id"]
    return None


async def fetch_service(state) -> dict:
    """变更子流程第一步:从接入平台发现真实服务,在用户话里对准服务名;对不准就 interrupt 弹
    服务选择器等前端点选(resume 回填服务名);拿到后 service_snapshot 取真实观测快照。
    interrupt 之前只做只读(resume 时本节点从头重跑)。

    平台不可达时不弹空选择器,把错误装进 service_data 如实注入,让主力 Agent 告知用户。

    存在性校验放在这里而不是只放在工具里:变更子流程是确定性节点,不经 agent_tools,
    工具那道校验够不着。用户随口报的服务名、resume 端点回传的名字,都得过同一道判断。"""
    services, err = await service_catalog.discover_services()
    if err:
        return {"service_data": {"error": err, "code": "discovery_down"},
                "trace": {"fetch_service": {"discovery_error": err}}}
    sid = state.get("service_id") or _match_service_name(
        state.get("resolved_query") or _user_text(state), services)
    # 存在性校验与 service_catalog.owns_service 同源同语义:只认发现列表里真实存在的服务名
    while not any(s["service_id"] == str(sid or "") for s in services):
        sid = interrupt({"type": "select_service", "services": services})  # resume 回填服务名
    data = await service_catalog.service_snapshot(sid, discovered=services)
    return {"service_id": sid, "service_data": data,
            "trace": {"fetch_service": {"service_id": sid}}}


async def retrieve_runbook(state) -> dict:
    """变更子流程强制检索 runbook:Query 扩写 3 条 → 多 query 各检索一次 → 按 chunk id 去重合并
    (保留每 id 最高分)→ 按分降序拼编号证据。产出注入 main_agent 作「能不能变更/怎么变更」的判据
    (不让模型凭记忆答)。库里知识一份,扩写只在检索侧现查现用。"""
    base = state.get("resolved_query") or _user_text(state)
    od = state.get("service_data") or {}
    seed = f"{base} {od.get('status', '')}".strip()
    queries = await query_understanding.expand_queries(seed)

    merged: dict = {}                       # chunk id -> 最高分 hit
    for q in queries:
        for h in await retrieval.search_knowledge(q, strategy="hybrid_rerank", bm25_text=q):
            cur = merged.get(h["id"])
            if cur is None or h["rerank_score"] > cur["rerank_score"]:
                merged[h["id"]] = h
    ranked = sorted(merged.values(), key=lambda h: h["rerank_score"], reverse=True)
    arranged = retrieval.arrange_head_tail(ranked)
    citations = [
        {"n": i + 1, "id": h["id"], "section_path": h["section_path"],
         "question": h["question"], "answer": h["answer"], "content_type": h["content_type"]}
        for i, h in enumerate(arranged)
    ]
    evidence = "\n".join(f"[{c['n']}] {c['question']}: {c['answer']}" for c in citations)
    return {"evidence": evidence, "citations": citations,
            "trace": {"retrieve_runbook": {"queries": queries, "hits": len(ranked)}}}


async def script_reply(state) -> dict:
    """fallback_script 出口:闲聊/其他 兜底话术(零模型),按 intent 分文案——闲聊把话题引回产品,
    其他请用户说具体些,自我介绍直接报能力清单。分流后立即命中、不进 Agent。
    (与 ch05 知识路证据弱的 fallback_reply 是两码事,名字相近但不同节点、不同触发点,勿混。)"""
    intent = state.get("intent")
    if intent == "自我介绍":
        text = SCRIPT_REPLY_INTRO
    elif intent == "其他":
        text = SCRIPT_REPLY_OTHER
    else:
        text = SCRIPT_REPLY_CHITCHAT
    return {"answer": text, "trace": {"route": "fallback_script"}}


async def complaint_reply(state) -> dict:
    """投诉:安抚话术 + 建工单(后端不执行,交前端自选)。

    只留建工单,不留"转人工值班":这套系统的使用者**就是值班的运维本人**,
    没有"转给谁"这回事——那个按钮原本也只是前端打印两句话,没有真实派单。
    建工单是真的(写库、有单号),所以留它。
    """
    actions = [
        {"type": "create_ticket",
         "draft": {"description": _user_text(state), "ticket_type": "投诉"}},
    ]
    return {"answer": COMPLAINT_REPLY, "suggested_actions": actions,
            "trace": {"route": "complaint"}}


async def resolve_reference(state) -> dict:
    """指代消解 + Query 改写(合一):吃最近几轮历史把半截话补成完整问句;已完整则透传。
    写 resolved_query,供 classify_intent 与 change_flow 检索共用(意图识别不再改写)。"""
    query = _user_text(state)
    history = _history_text(state)
    # ch07 验收可观测:每轮必经此节点,摘要行+滑窗在这一条日志里直接可见
    # (model_ctx 只在进 main_agent 的路由才有,闲聊/兜底路由靠这条看)
    logger.info("history_ctx conv=%s(摘要行+滑窗,coref/意图共用)\n%s",
                state.get("conversation_id"), history or "(无历史)")
    resolved = await coref.resolve(query, history)
    mode = "rewrite" if resolved != query else "passthrough"
    return {"resolved_query": resolved, "trace": {"coref": mode}}


_CONFIRM_MAX_CHARS = 6   # 短到这个字数内的回复才可能是延续确认("去呀!"/"根因分析"/"嗯,搞")


async def classify_intent(state) -> dict:
    """意图四件套:九类 + confidence + 「其他」兜底。吃 resolved_query(空则回落原话)+ 完整滑窗
    判当前意图(应对日志→变更→日志漂移)。把归属出口 route 落进 State(供 _agent_messages 判注入、
    log 留痕;route_by_intent 条件边按同一 INTENT_TO_ROUTE 分流,单一来源不漂移)。
    短回复兜底(代码级,不靠模型自觉):值班猫刚说过话、这轮就几个字却被判
    闲聊/其他时,几乎从来不是真闲聊——是"去呀!"式延续确认,或对上轮的简短回应。
    一律转 service_ops 让 Agent 带完整上下文处理,不落零模型话术把对话掐断(真实案例:
    "去呀!""去吧"被判闲聊,根因分析续不上)。兜底话术只该当开场白(会话里值班猫
    还没开过口时)。"""
    query = state.get("resolved_query") or _user_text(state)
    r = await intent_mod.classify(query, _history_text(state))
    intent, conf = r["intent"], r["confidence"]
    route = INTENT_TO_ROUTE.get(intent, "service_ops")
    override = None
    if r.get("error"):
        # 分类器挂了(重试后仍失败)——这不是"用户说不清",不能落零模型话术把人挡回去。
        # 值班场景的原则是"听不懂也比不干活强":直接交给 Agent 带上下文处理。
        route = "service_ops"
        override = f"意图分类失败,转 Agent 处理({r['error']})"
    elif route == "fallback_script" and intent != "自我介绍":
        # 打听底层模型的是边界外提问,不是"去呀!"式的延续确认,不能被短回复兜底抢走
        if (len(query.strip()) <= _CONFIRM_MAX_CHARS and _last_assistant_text(state).strip()
                and not intent_mod.asks_about_model(query)):
            intent, conf, route = "故障处理", max(conf, 0.6), "service_ops"
            override = "短回复接在上轮发言后,转 Agent 处理(原判闲聊/其他)"
    tag_intent(intent, conf)   # ch09:意图进当前 trace 的 metadata+tag,Cost Control 按意图分堆
    trace = {"intent": intent, "intent_confidence": conf, "route": route}
    if override:
        trace["confirm_override"] = override
    return {"intent": intent, "intent_confidence": conf, "route": route, "trace": trace}


async def retrieve_knowledge(state) -> dict:
    """知识类强制检索(复用 ch03/04 检索器):产出编号证据交 Agent 作答。
    ch09 的生成前置信度闸已下线:值班场景拒答=不干活,证据弱也交给 Agent,由它如实说明
    相近条目并向用户确认,不硬编(prompt 侧管住)。置信度分数只留两个用途——trace 可观测,
    以及低于阈值时把问题落低置信池喂知识飞轮(快照规则不变:只要走了检索就存 Top3)。"""
    query_raw = _user_text(state)
    u = await query_understanding.understand(query_raw)
    query = u["standard"]
    bm25_text = query + (" " + " ".join(u["expanded"]) if u["expanded"] else "")

    hits = await retrieval.search_knowledge(
        query, strategy="hybrid_rerank", bm25_text=bm25_text)
    conf = compute_evidence_confidence(hits)
    snapshot = snapshot_from_hits(hits)

    arranged = retrieval.arrange_head_tail(hits)
    citations = [
        {"n": i + 1, "id": h["id"], "section_path": h["section_path"],
         "question": h["question"], "answer": h["answer"], "content_type": h["content_type"]}
        for i, h in enumerate(arranged)
    ]
    evidence = "\n".join(f"[{c['n']}] {c['question']}: {c['answer']}" for c in citations)

    # 低置信落池(只记录、不拒答):零命中时 snapshot=[] (审核页显示「已检索,零命中」,
    # 真缺知识的强信号),区别于没走检索的 NULL
    if conf.score < settings.evidence_confidence_threshold:
        reason = (f"evidence_confidence={conf.score:.3f} "
                  f"signals={json.dumps(conf.signals, ensure_ascii=False)}")
        await repository.insert_low_confidence(
            state.get("conversation_id"), query_raw, "retrieval_low_conf", reason,
            retrieved_chunks=snapshot)

    return {"evidence_confidence": conf.score, "retrieved_snapshot": snapshot,
            "evidence": evidence, "citations": citations,
            "trace": {"forced_rag": True, "evidence_confidence": conf.score,
                      "confidence_signals": conf.signals}}


_KNOWLEDGE_EVIDENCE_HINT = (
    "\n\n## 已检索到的知识证据(请据此作答,每个关键结论后标注来源编号如[1];"
    "证据已给,不要再调用 query_faq;仍可按需调用服务查询/监控/日志等工具)\n"
    "服务名、编号这类标识串逐字复制证据里的写法,证据里没有的服务不要写;带条件的结论要连条件一起说。\n"
)


def _turn_context(state) -> str:
    """本轮才有的材料:检索证据 + 变更路的服务数据。没有就返回空串。

    顺序不能反:CHANGE_JUDGE_HINT 的措辞是「下面给出该服务数据与检索到的 runbook 证据」,
    它假设证据已在前文给过。"""
    parts = []
    ss = memory.summary_system(state.get("summary"))
    if ss is not None:
        parts.append("\n\n" + ss.content)     # 摘要排最前:它讲的是更早发生的事
    if state.get("evidence"):
        parts.append(_KNOWLEDGE_EVIDENCE_HINT + state["evidence"])
    if state.get("route") == "change_flow":
        parts.append(CHANGE_JUDGE_HINT + json.dumps(state.get("service_data", {}), ensure_ascii=False))
    return "".join(parts)     # 三段文本逐字沿用原来的常量,一个字都不改:这次只挪位置,
                              # 措辞一起动的话出了问题分不清是缓存改动还是提示词改动


TURN_CTX_ID = "turn-ctx"   # 哨兵:日志里据此把注入块跟真实用户消息分开。只能用 id 不能用 name
                           # ——name 会被序列化发给上游,改变请求字节;id 不会。


def _with_turn_context(window: list, turn_ctx: str) -> list:
    """把本轮材料插在滑窗里最后一条用户消息之后。

    插在这个位置而不是整个列表末尾,是为了 ReAct 轮内也能命中前缀缓存:第 2 步的
    [Sys, 用户问, 材料, AI, Tool] 完整包含第 1 步的 [Sys, 用户问, 材料] 作前缀;
    追加到末尾则第 2 步变成 [Sys, 用户问, AI, Tool, 材料],公共前缀只到用户问那条。
    窗口里没有用户消息(极端裁剪回退分支)时退化成追加到末尾,不抛异常。"""
    msg = HumanMessage(turn_ctx, id=TURN_CTX_ID)
    for i in range(len(window) - 1, -1, -1):
        if isinstance(window[i], HumanMessage):
            return [*window[:i + 1], msg, *window[i + 1:]]
    return [*window, msg]


def _agent_messages(state) -> list:
    """拼装顺序固定(ch07):人设+红线 system → 滑窗原文 → 摘要与本轮材料(紧跟用户那句)。

    整条消息列表里**只有一条 SystemMessage**,内容恒为 AGENT_SYSTEM。这不是洁癖:
    上游的 chat template 会把列表里所有 system 消息上提、合并成一个头部块渲染,所以
    「摘要单独放第二条 system」等于把它拼在了 AGENT_SYSTEM 后面,工具 schema 被挤到
    可变内容之后 —— 而 prompt caching 按渲染后的前缀精确匹配,于是整段前缀全 miss。
    实测:无摘要的会话 cache_read=2048,摘要一进 system 就掉到 0。
    摘要因此跟证据一起走用户侧那条消息。

    滑窗=摘要边界锚点切 + trim_messages token 兜底;State 全量历史不动,这里只现拼精简版。"""
    window = memory.build_window(state.get("messages", []),
                                 state.get("summary_upto_msg_id") or 0,
                                 settings.context_window_max_tokens)
    turn_ctx = _turn_context(state)
    if turn_ctx:
        window = _with_turn_context(window, turn_ctx)
    return [SystemMessage(AGENT_SYSTEM), *window]


def _log_model_context(state, msgs) -> None:
    """ch07 验收可观测:把实际发给模型的摘要与滑窗打进日志(log/app.log)。
    摘要本就几十到两百字,打全文;滑窗每条打角色+前 40 字。"""
    from langchain_core.messages.utils import count_tokens_approximately
    # 本轮材料块虽然也是 HumanMessage,但它不是用户说的话,从滑窗里摘出来单独报,
    # 否则「window=N条」会凭空多一条,验收时读不准
    ctx_block = next((m for m in msgs if getattr(m, "id", None) == TURN_CTX_ID), None)
    window = [m for m in msgs
              if not isinstance(m, SystemMessage) and getattr(m, "id", None) != TURN_CTX_ID]
    lines = []
    for m in window:
        text = m.content if isinstance(m.content, str) else ""
        lines.append(f"  [{m.type}] {text[:40]!r}")
    logger.info(
        "model_ctx conv=%s step=%s summary=%r window=%s条 本轮材料=%s tokens≈%s\n%s",
        state.get("conversation_id"), state.get("steps", 0),
        state.get("summary") or "(无)", len(window),
        f"{len(ctx_block.content)}字" if ctx_block else "无",
        count_tokens_approximately(msgs), "\n".join(lines),
    )


async def main_agent(state, config=None) -> dict:
    """ReAct 推理步:调模型(带工具),累加 steps 与 token 消耗。
    注:streaming=True 下 ChatOpenAI 默认不回传 usage_metadata;usage 回传已在
    get_chat_model 统一收口(ch09 stream_usage=True),这里不再单独 bind。"""
    specs = await registry.get_all_specs()   # ch08 现问现拿:内置+MCP 合并清单,每轮现拉
    model = get_chat_model(streaming=True).bind_tools([s.tool for s in specs])
    msgs = _agent_messages(state)
    _log_model_context(state, msgs)   # ch07:摘要+滑窗进日志,验收 tail -f 可见
    ai: AIMessage = await model.ainvoke(msgs, config)
    usage = ai.usage_metadata or {}
    used = usage.get("total_tokens", 0)
    # 前缀缓存命中数:system + tools 那段稳定前缀有没有真被复用,只能靠上游报的这个数看。
    # 上游不报就是 0,不代表没命中(得去服务商控制台看),别据此下结论。
    cached = (usage.get("input_token_details") or {}).get("cache_read", 0)
    logger.info("agent_step conv=%s step=%s input=%s cache_read=%s total=%s",
                state.get("conversation_id"), state.get("steps", 0) + 1,
                usage.get("input_tokens", 0), cached, used)
    return {"messages": [ai],
            "steps": state.get("steps", 0) + 1,
            "tokens_used": state.get("tokens_used", 0) + used}


async def agent_tools(state) -> dict:
    """ReAct 行动步(ch08):一切工具经统一执行引擎。create_ticket(唯一写操作)走确认流——
    参数齐则节点顶部 interrupt 推工单预览(interrupt 前只做纯计算,resume 重跑安全,照 fetch_service
    范式);参数缺则交引擎按「校验拦下」回灌,模型自然向用户追问,不弹卡。
    submit_change 仍拦成前端变更表单(ch06 语义不变)。"""
    last = state["messages"][-1]
    cid = state.get("conversation_id", 0)
    uid = state.get("user_id", "")
    specs = {s.name: s for s in await registry.get_all_specs()}   # ch08:执行侧同样现拉

    # 建工单确认:先纯校验(无副作用),参数齐才 interrupt;resume 回 {"confirmed": bool}
    ticket_calls = [tc for tc in last.tool_calls if tc["name"] == "create_ticket"]
    tspec = specs.get("create_ticket")
    # 告警接入的三个写工具:建 / 改 / 删。都要弹卡确认,否则会被权限门直接拒掉
    INTAKE_WRITE = {"create_alert_intake", "update_alert_intake", "delete_alert_intake"}
    intake_calls = [tc for tc in last.tool_calls if tc["name"] in INTAKE_WRITE]
    ispec = specs.get(intake_calls[0]["name"]) if intake_calls else None
    decision = None
    if ticket_calls and tspec is not None \
            and engine.validate_args(tspec, dict(ticket_calls[0].get("args") or {})) is None:
        first_args = ticket_calls[0]["args"]
        decision = interrupt({"type": "confirm_ticket",
                              "preview": {"ticket_type": first_args.get("ticket_type", "咨询"),
                                          "description": first_args.get("description", "")}})
    elif intake_calls and ispec is not None \
            and engine.validate_args(ispec, dict(intake_calls[0].get("args") or {})) is None:
        # 建/改/删告警接入都是写操作:不弹卡确认的话,引擎会按"权限拒绝"挡掉,
        # Agent 只能干说"被系统拦下了"——用户没有任何地方可以点确认。
        tc0 = intake_calls[0]
        a = dict(tc0.get("args") or {})
        if tc0["name"] == "create_alert_intake":
            pv = {"op": "create",
                  "name": a.get("name", ""),
                  "services": a.get("services") or [],
                  "checks": a.get("checks") or [],
                  "schedule_minutes": a.get("schedule_minutes") or 0,
                  "daily_at": a.get("daily_at") or "",
                  "cron": a.get("cron") or "",
                  "threshold_metric": a.get("threshold_metric") or "",
                  "threshold_value": a.get("threshold_value") or 0,
                  "silence_minutes": a.get("silence_minutes") or 15,
                  "auto_repair": bool(a.get("auto_repair")),
                  "forward_url": a.get("forward_url") or ""}
        elif tc0["name"] == "update_alert_intake":
            pv = {"op": "update", "name": a.get("name", ""),
                  "enabled": a.get("enabled"), "daily_at": a.get("daily_at") or "",
                  "interval_min": a.get("interval_min") or 0,
                  "silence_minutes": a.get("silence_minutes") or 0}
        else:
            pv = {"op": "delete", "name": a.get("name", "")}
        decision = interrupt({"type": "confirm_intake", "preview": pv})

    tool_msgs = []
    actions = list(state.get("suggested_actions", []))
    # 本轮服务存在性校验的数据源:发现一次全轮复用(submit_change/query_service 可能各判一次)
    platform_services, discovery_err = await service_catalog.discover_services()

    def _exists(sid) -> bool:
        # 与 service_catalog.owns_service 同源同语义:只认接入平台里真实存在的服务。
        # 服务身份是"平台+服务名":裸名跨平台歧义时 find_service 返回 None → 视为不存在,
        # 让调用方走"扫描"或"让用户指明平台",绝不靠"先发现的平台胜出"去猜。
        return not discovery_err and service_catalog.find_service(
            platform_services, str(sid or "")) is not None

    def _not_found_note() -> str:
        return ("接入平台暂时不可达,无法核对服务,本次不发起变更。请如实告知平台不可达,"
                "建议用户到后台「服务检测」检查数据源后再试,不要编造服务。" if discovery_err else
                "接入平台中没有发现这个服务,本次不发起变更。请如实告知没查到,"
                "并让用户从下面列出的服务里选一个,不要再调用任何工具。")

    def _is_platform_name(sid) -> bool:
        """给的是平台名而不是服务名(如「诊断下 otel-demo」)。平台名对用户来说就是"那一摊东西",
        不该逼他再报服务名。判据:与某个接入平台的 name 同名(不区分大小写)。"""
        want = str(sid or "").strip().lower()
        return bool(want) and any(want == str(s.get("target") or "").strip().lower()
                                  for s in platform_services)

    def _diagnosis_not_found_note() -> str:
        """诊断专用的 not-found 提示。关键是**别让模型回头追问用户要服务名**——
        用户说不清服务是常态,系统有能力自己全平台扫;追问等于把系统的活推回给用户
        (真实事故:用户说「诊断下 otel-demo」,模型回「没查到 otel-demo 这个服务名,
        麻烦你确认下具体是哪个服务」,用户直接炸了)。"""
        if discovery_err:
            return ("接入平台暂时不可达,无法核对服务,本次不发起诊断。请如实告知平台不可达,"
                    "不要编造服务。")
        return ("接入平台里没有这个服务名,本次不发起诊断。**不要追问用户要服务名**——"
                "直接改用 start_diagnosis 并把 service 留空重新调用一次,系统会自动全平台"
                "扫描找可疑服务再归因;如果用户报的确实是个错名,回复里顺带提一句即可。")

    not_found = False        # 这一轮有没有人报了接入平台里不存在的服务
    for tc in last.tool_calls:
        if tc["name"] == "start_diagnosis":
            # 诊断发起同样在节点侧拦截:服务名给了才做存在性校验(防线语义同 submit_change);
            # 留空=用户说不清服务,直接放行——管线会先扫全平台找异常锚点再归因
            sid = str((tc.get("args") or {}).get("service") or "")
            if sid and _is_platform_name(sid):
                sid = ""      # 给的是平台名 → 当"说不清服务"处理,走全平台扫描,不追问他
            # 服务身份是"平台+服务名":用户/模型可以用「平台/服务名」指明(多平台同名时必须)。
            # 裸名跨平台歧义时 _exists 为 False → 走下面的 not-found 提示(留空重调=全平台扫描)。
            _plat = sid.split("/", 1)[0] if "/" in sid else None
            if sid and not _exists(sid):
                not_found = True
                tool_msgs.append(ToolMessage(
                    content=_diagnosis_not_found_note(),
                    tool_call_id=tc["id"], name="start_diagnosis", status="error"))
                continue
            from app.diagnosis import runner
            diag_id = runner.start(sid, str((tc.get("args") or {}).get("symptom") or ""),
                                   conversation_id=state.get("conversation_id"), platform=_plat)
            actions.append({"type": "diagnosis_card", "diagnosis_id": diag_id,
                            "service": sid})
            tool_msgs.append(ToolMessage(
                content=f"根因分析任务 {diag_id} 已发起,结果由前端诊断卡片展示(含异常拓扑与证据链)。"
                        "请告知用户分析已在进行、完成后看卡片,不要编造或预告结论,不要再调用任何工具。",
                tool_call_id=tc["id"], name="start_diagnosis"))
        elif tc["name"] == "submit_change":
            # submit_change 在这里就被拦成前端表单、不进执行引擎,所以工具内那道存在性校验
            # 对它不生效,得在拦截之前判一次。不然变更入口就成了绕过校验的后门。
            if not _exists(tc["args"].get("service_id")):
                not_found = True
                tool_msgs.append(ToolMessage(
                    content=_not_found_note(),
                    tool_call_id=tc["id"], name="submit_change", status="error"))
                continue
            actions.append({"type": "change_form",
                            "draft": {"service_id": tc["args"].get("service_id", ""),
                                      "change_type": tc["args"].get("change_type")}})
            tool_msgs.append(ToolMessage(
                content="已把『提交变更申请』选项交给用户确认。请用一句话说明评估结论并停止,不要再调用任何工具。",
                tool_call_id=tc["id"], name="submit_change"))
        elif tc["name"] in INTAKE_WRITE and decision is not None:
            if tc is intake_calls[0]:
                if isinstance(decision, dict) and decision.get("confirmed"):
                    run = await engine.execute_tool_call(tc, cid, specs, confirmed=True,
                                                        user_id=uid)
                else:
                    run = await engine.execute_tool_call(
                        tc, cid, specs, confirmed=False, user_id=uid,
                        deny_note="用户在告警接入预览卡上点了取消,本次不改动。请勿再发起,"
                                  "除非用户再次明确要求。")
                tool_msgs.append(run.tool_message)
            else:
                tool_msgs.append(ToolMessage(content="一次只处理一个建接入请求,本次调用已忽略。",
                                             tool_call_id=tc["id"], name="create_alert_intake",
                                             status="error"))
        elif tc["name"] == "create_ticket" and decision is not None:
            if tc is ticket_calls[0]:
                # resume 值形状守卫:同一 resume 端点服务两种中断,错配(如传了 service_id 字符串)
                # 一律按未确认处理——宁可让用户重来,不让节点炸
                if isinstance(decision, dict) and decision.get("confirmed"):
                    run = await engine.execute_tool_call(tc, cid, specs, confirmed=True,
                                                        user_id=uid)
                else:
                    run = await engine.execute_tool_call(
                        tc, cid, specs, confirmed=False, user_id=uid,
                        deny_note="用户在工单预览卡片上点了取消,本次不建单。请勿再发起,除非用户再次明确要求。")
                tool_msgs.append(run.tool_message)
            else:
                tool_msgs.append(ToolMessage(content="一次只处理一个建工单请求,本次调用已忽略。",
                                             tool_call_id=tc["id"], name="create_ticket",
                                             status="error"))
        else:
            # 含参数缺失的 create_ticket(decision is None):引擎校验拦下,错误说明回灌
            run = await engine.execute_tool_call(tc, cid, specs, user_id=uid)
            if tc["name"] == "query_service" and not _exists(
                    (tc.get("args") or {}).get("service_name")):
                not_found = True
            # 修复 agent:工具已派单,这里把修复卡动作发出去(进度/回执全在修复卡上滚动)
            if tc["name"] == "start_repair" and run.ok:
                try:
                    payload = json.loads(run.tool_message.content)
                    if payload.get("status") == "running" and payload.get("repair_id"):
                        actions.append({"type": "repair_card",
                                        "repair_id": payload["repair_id"],
                                        "service": payload.get("service")})
                except (ValueError, TypeError):
                    logger.warning("start_repair 返回无法解析,修复卡未生成")
            tool_msgs.append(run.tool_message)
    # 查无此服务之后给一条出路:把接入平台发现的服务亮出来点选,否则用户既查不到,
    # 也不知道标准服务名是什么写法
    if not_found:
        actions.append({"type": "select_service", "services": platform_services})
    out = {"messages": tool_msgs}
    if actions:
        out["suggested_actions"] = actions
    return out


def resolve_answer(state) -> str:
    """最终答复:确定性节点写在 state['answer'];Agent 答复取最后一条 AIMessage 文本。"""
    if state.get("answer"):
        return state["answer"]
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, str):
                return content
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


async def log_node(state) -> dict:
    """日志记录:留痕 intent/route/trace(可观测地基),并落 MySQL 一条 assistant 消息(审计)。"""
    logger.info(
        "turn conv=%s intent=%s route=%s resolved=%r trace=%s",
        state.get("conversation_id"), state.get("intent"), state.get("route"),
        state.get("resolved_query"), state.get("trace", {}),
    )
    answer = resolve_answer(state)
    if state.get("conversation_id"):
        # suggested_actions 随消息落库(meta 列):前端历史回载要重建诊断卡/服务卡,
        # 之前不落库靠文案精确匹配,卡片刷新即丢。meta 只喂前端,不进 LLM 上下文。
        #
        # citations 同理:引用角标 [n] 在直播时由前端 decorateCitations 渲染成可点的
        # 珊瑚色上标,但回载时这份数据没有 → 只剩普通正文的 "[1]",颜色和浮层全丢。
        # 引用本身是模型输出的一部分,和正文一起落库才自洽。
        actions = state.get("suggested_actions") or None
        cites = state.get("citations") or None
        meta: dict = {}
        if actions:
            meta["suggested_actions"] = actions
        if cites:
            meta["citations"] = cites
        await repository.append_message(
            state["conversation_id"], "assistant",
            content=answer or None,
            meta=meta or None)
    return {}
