"""意图分类器的失败语义:模型挂了不能伪装成"用户说不清"。"""
import asyncio

from langchain_core.runnables import RunnableLambda

from app.core import intent as intent_mod


def test_classifier_failure_returns_error_not_other(monkeypatch):
    """模型调用抛异常时必须带 error 返回,由调用方转 Agent。

    回归:旧实现 except 里直接 return 其他/0.0,上游抖动会变成一句
    「抱歉,我不太确定您的意思」——把系统的错算在用户头上。
    """
    def boom(_):
        raise RuntimeError("upstream 502")

    monkeypatch.setattr(intent_mod.llm, "structured",
                        lambda *a, **k: RunnableLambda(boom))
    r = asyncio.run(intent_mod.classify("商品无法加载了,你去看看什么原因"))
    assert r["intent"] == "其他"
    assert r["confidence"] == 0.0
    assert "error" in r and "502" in r["error"]


def test_classifier_retries_once_before_giving_up(monkeypatch):
    """失败先重试一次:瞬时抖动占多数,多给一次机会比直接降级划算。"""
    calls = {"n": 0}

    def flaky(_):
        calls["n"] += 1
        raise RuntimeError("blip")

    monkeypatch.setattr(intent_mod.llm, "structured",
                        lambda *a, **k: RunnableLambda(flaky))
    asyncio.run(intent_mod.classify("服务挂了帮我看看"))
    assert calls["n"] == 2
