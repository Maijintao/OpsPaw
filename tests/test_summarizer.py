"""ch07 后台摘要:边界计算 / 触发防抖 / 任务体(LLM 用假实现,不打真上游)。"""
import asyncio
from types import SimpleNamespace

from app.core import summarizer
from app.db import repository as repo


def _msg(mid, role, content="x"):
    return SimpleNamespace(id=mid, role=role, content=content)


def _dialog_rows(n_turns, start_id=1):
    rows, i = [], start_id
    for t in range(n_turns):
        rows.append(_msg(i, "user", f"q{t}"))
        rows.append(_msg(i + 1, "assistant", f"a{t}"))
        i += 2
    return rows


def test_compute_boundary_keeps_last_k_turns():
    rows = _dialog_rows(12)                       # user id 1,3,...,23
    b = summarizer.compute_boundary(rows, keep_turns=8)
    assert b == 8                                 # 倒数第8轮用户消息 id=9,前一条 id=8


def test_compute_boundary_not_enough_turns():
    assert summarizer.compute_boundary(_dialog_rows(8), keep_turns=8) is None
    assert summarizer.compute_boundary([], keep_turns=8) is None


async def test_run_summary_updates_fields(db_session_factory, db_clean, monkeypatch):
    cid = await repo.create_conversation("u1")
    for t in range(12):
        await repo.append_message(cid, "user", content=f"问题{t} 服务100{t}")
        await repo.append_message(cid, "assistant", content=f"回答{t}")

    async def fake_llm(old_summary, dialog):
        return f"摘要[旧:{bool(old_summary)}]:" + dialog[:20]
    monkeypatch.setattr(summarizer, "summarize_dialog", fake_llm)
    monkeypatch.setattr(summarizer.settings, "context_window_turns", 8)

    await summarizer.run_summary(cid)
    conv = await repo.get_conversation(cid)
    assert conv.summary and conv.summary.startswith("摘要[旧:False]")
    assert conv.summary_upto_msg_id is not None    # 边界=倒数第8轮用户消息前一条

    await summarizer.run_summary(cid)              # 边界未前进 → 跳过,不重写
    conv2 = await repo.get_conversation(cid)
    assert conv2.summary == conv.summary


async def test_maybe_schedule_below_threshold_noop(db_session_factory, db_clean, monkeypatch):
    cid = await repo.create_conversation("u1")
    await repo.append_message(cid, "user", content="q")
    called = []

    async def fake_run(c):
        called.append(c)
    monkeypatch.setattr(summarizer, "run_summary", fake_run)
    monkeypatch.setattr(summarizer.settings, "summary_trigger_messages", 30)

    await summarizer.maybe_schedule_summary(cid)   # 1 条 < 30 → 不起任务
    assert summarizer._running.get(cid) is None and called == []


async def test_maybe_schedule_fires_and_debounces(db_session_factory, db_clean, monkeypatch):
    cid = await repo.create_conversation("u1")
    for t in range(2):
        await repo.append_message(cid, "user", content=f"q{t}")
    monkeypatch.setattr(summarizer.settings, "summary_trigger_messages", 2)

    gate = asyncio.Event()
    ran = []

    async def slow_run(c):
        ran.append(c)
        await gate.wait()
    monkeypatch.setattr(summarizer, "run_summary", slow_run)

    await summarizer.maybe_schedule_summary(cid)
    await summarizer.maybe_schedule_summary(cid)   # 在跑 → 防抖不重复起
    await asyncio.sleep(0)                         # 让 task 起跑
    assert ran == [cid]
    gate.set()
    await summarizer._running[cid]                 # 等任务收尾
    assert cid not in summarizer._running          # done 后清防抖表


async def test_maybe_schedule_never_raises_into_reply_path(db_session_factory, db_clean, monkeypatch):
    """触发检查在用户回复路径上,DB 抖动不得把已完成的回答炸成 500(spec §8 红线)。"""
    async def boom(cid):
        raise RuntimeError("db down")
    monkeypatch.setattr(summarizer.repository, "get_conversation", boom)
    await summarizer.maybe_schedule_summary(1)     # 不抛即过


async def test_maybe_schedule_concurrent_calls_single_task(db_session_factory, db_clean, monkeypatch):
    """TOCTOU:检查与 create_task 之间有 await,同会话并发触发只允许起一个任务。"""
    cid = await repo.create_conversation("u1")
    for t in range(2):
        await repo.append_message(cid, "user", content=f"q{t}")
    monkeypatch.setattr(summarizer.settings, "summary_trigger_messages", 2)

    gate = asyncio.Event()
    ran = []

    async def slow_run(c):
        ran.append(c)
        await gate.wait()
    monkeypatch.setattr(summarizer, "run_summary", slow_run)

    await asyncio.gather(summarizer.maybe_schedule_summary(cid),
                         summarizer.maybe_schedule_summary(cid))
    await asyncio.sleep(0)
    assert ran == [cid]                            # 并发双触发只起一个
    gate.set()
    await summarizer._running[cid]
    assert cid not in summarizer._running
