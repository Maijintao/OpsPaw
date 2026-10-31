from langchain_core.messages import AIMessage

from app.config import settings

# 九类意图 → 五出口(写死的分流规则,spec §3.1 / README route_by_intent;
# 返回值 = build.py 条件边映射键,单一来源不漂移)
INTENT_TO_ROUTE: dict[str, str] = {
    "投诉": "escalate",
    "闲聊": "fallback_script",
    "其他": "fallback_script",
    # 自我介绍:代码级短路判出来的,固定文案报能力清单(见 intent.is_self_intro)
    "自我介绍": "fallback_script",
    "运维知识": "knowledge",
    "变更申请": "change_flow",
    "故障处理": "service_ops",
    "建单跟进": "service_ops",   # ch08:明确要求建工单/人工跟进 → 主力 Agent 走 create_ticket 确认流
    # 配置管理:建/改告警接入、定时巡检、阈值规则这类"配置平台自己"的诉求,
    # 交给 Agent 用 create_alert_intake 落配置,不能落到 fallback 说"听不懂"
    "配置管理": "service_ops",
    "日志指标": "service_ops",
    "服务状态": "service_ops",
}


def route_by_intent(state) -> str:
    """按意图分流;未知意图保守归 service_ops(让 Agent 自己应对)。"""
    return INTENT_TO_ROUTE.get(state.get("intent", ""), "service_ops")


def should_continue(state) -> str:
    """ReAct 停止条件:无 tool_calls 就收敛,步数封顶则强停,否则继续。

    只用步数封顶,不在这里卡 token。防打转靠的是步数:一步一次模型调用,数得清、好解释,
    各家 Agent SDK 给的也是 max_turns / max_iterations 这类步数上限。
    token 花销是另一件事——它管的是烧钱和撑爆上下文,不是死循环,阈值要按真实用量分布标定,
    归第 9 章的成本控制一起讲。混在这里卡,一次估偏就把工具链整条掐断,而且掐在模型
    已经决定调工具之后,用户只看到半句「我这就去查」。"""
    last = state["messages"][-1]
    has_tool_calls = isinstance(last, AIMessage) and bool(last.tool_calls)
    if not has_tool_calls:
        return "stop"
    if state.get("steps", 0) >= settings.max_agent_steps:
        return "stop"
    return "continue"
