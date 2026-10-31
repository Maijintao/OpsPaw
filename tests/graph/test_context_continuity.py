"""对话延续性(上下文断裂修复)的单测:完整滑窗喂意图 + 短确认语代码级兜底。"""
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import nodes
from app.graph.nodes import _history_text, _last_assistant_text, classify_intent


def _state(pairs: list[tuple[str, str]], last_user: str) -> dict:
    """pairs=(用户,值班猫) 历史轮,末尾追加本轮用户消息(窗口约定:最后一条是本轮 human)。"""
    msgs = []
    for u, a in pairs:
        msgs.append(HumanMessage(u))
        msgs.append(AIMessage(a))
    msgs.append(HumanMessage(last_user))
    return {"messages": msgs, "summary": "", "summary_upto_msg_id": 0}


def test_history_text_keeps_early_turns():
    """超过 6 轮的早期发言也要进意图/指代的历史文本——以前裁到只剩 6 轮,
    待确认的提议被裁掉,"去呀!"就断了。"""
    pairs = [(f"第{i}轮问题", f"第{i}轮回答,结论{i}") for i in range(1, 9)]
    text = _history_text(_state(pairs, "去呀!"))
    assert "第1轮问题" in text   # 第 1 轮(8 轮之前)仍在
    assert "第7轮回答" in text


def test_last_assistant_text_finds_question():
    st = _state([("checkout 报错", "疑似 payment 异常,要发起根因分析吗?")], "去呀!")
    assert _last_assistant_text(st).endswith("要发起根因分析吗?")


async def test_short_confirmation_overrides_fallback(monkeypatch):
    """上轮以问句收尾、这轮几个字被判闲聊 → 代码级转 service_ops,不落话术。"""
    async def fake_classify(query, history):
        return {"intent": "闲聊", "confidence": 0.9}
    monkeypatch.setattr(nodes.intent_mod, "classify", fake_classify)
    st = _state([("checkout 下单报错", "疑似 payment 异常。要发起根因分析吗?")], "去呀!")
    out = await classify_intent(st)
    assert out["route"] == "service_ops"
    assert out["intent"] == "故障处理"
    assert "confirm_override" in out["trace"]


async def test_short_reply_after_assistant_speech_also_overridden(monkeypatch):
    """上轮值班猫发言(哪怕是陈述句"我发起一次根因分析"),这轮短句"去吧"被判闲聊
    → 同样转 Agent。兜底话术只该当开场白,不当中途掐断对话(真实案例)。"""
    async def fake_classify(query, history):
        return {"intent": "其他", "confidence": 0.8}
    monkeypatch.setattr(nodes.intent_mod, "classify", fake_classify)
    st = _state([("checkout 下单报错", "我发起一次根因分析,让归因管线把下单失败链路串起来。")], "去吧")
    out = await classify_intent(st)
    assert out["route"] == "service_ops"
    assert "confirm_override" in out["trace"]


async def test_short_chitchat_with_no_history_stays_fallback(monkeypatch):
    """会话里值班猫还没开过口(开场白场景),短句判闲聊照常落话术。"""
    async def fake_classify(query, history):
        return {"intent": "闲聊", "confidence": 0.9}
    monkeypatch.setattr(nodes.intent_mod, "classify", fake_classify)
    st = _state([], "在吗")
    out = await classify_intent(st)
    assert out["route"] == "fallback_script"
    assert "confirm_override" not in out["trace"]


async def test_normal_chitchat_still_fallback(monkeypatch):
    """正常长度的闲聊不受兜底影响。"""
    async def fake_classify(query, history):
        return {"intent": "闲聊", "confidence": 0.95}
    monkeypatch.setattr(nodes.intent_mod, "classify", fake_classify)
    st = _state([("checkout 报错", "要发起根因分析吗?")], "今天天气不错适合摸鱼")
    out = await classify_intent(st)
    assert out["route"] == "fallback_script"
