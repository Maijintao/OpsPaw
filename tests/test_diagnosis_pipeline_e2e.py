"""归因管线端到端接线回归:回放一份**真实故障录制**,跑完整条管线,断言"接缝没断"。

## 为什么必须有这一条(2026-10-16 的血账)

一天里连着挖出 5 个致命问题,其中 3 个是**模块之间的约定谁都没测过**:

  ① 观测工具返回的日志时间戳是 RFC3339 字符串('2026-10-16T06:54:12Z'),
     适配层当数字 float() → **归因段必崩**;
  ② OpsAgent 的 think() 只把结果写进 message_pool,适配层却读
     agent_context.last_message()(那里只有 refine() 会写) → **必崩**;
  ③ 适配层喂 data_features["metric_features"],而 agent 读 data_features["metric"] →
     **不报错**,只是让三个 agent 全程"没有数据",靠架构常识瞎猜或弃权。

当时 90 个单测**一个都没抓到**。原因很直接:单测测的是零件,而这三个 bug 全在
零件之间的接缝上。而唯一能跑整链的验证是跑批——要共享云靶机、每组十分钟、
靶子还会互相污染,自然跑得最少。

这条测试补的就是接缝。它**不碰靶子、不走网络、秒级**:
数据来自一份真实故障录制(采集段的上游响应),LLM 打桩但**捕获 agent 真正收到的 prompt**,
然后断言四件事——跑完不崩、prompt 里真有数据、结论有证据支撑、结果结构完整。

## 边界(诚实声明)

- 回放的是**那一刻的现场**。判据改了要重新录;数据面变了(接新数据源)也要重新录。
- 录制里没有 Loki 条目(那次采集平台没返回日志),所以日志通道的**时间戳转换**由
  单测 `test_diagnosis_opsagent_adapter.py::test_log_timestamp_converts_without_crashing`
  守着,不在这条里重复。
- LLM 段是桩,不校验"判得对不对"——那条只有跑批能给。
"""
import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import base as db_base
from app.db.models import Base, DiagnosisRun, Target
from app.diagnosis import collect, net, opsagent_adapter, runner

_REPO = Path(__file__).resolve().parents[1]
# 选 cart_service_failure 这份录制的原因(实测比对过 12 份):它录到的故障现场里
# cart/checkout/payment 三个服务确实被判异常,管线会**一路走到归因段**——
# 而 ad_service_failure 那份里 16 个服务一个异常都没有,粗筛如实判"未见异常"就提前返回,
# 根本到不了归因段,接缝也就测不到。
_RECORDING = _REPO / "data" / "aiopslab" / "recordings_v4" / "cart_service_failure.json"

# 录制里的数据源地址:必须与录制时逐字节一致,否则回放按完整 URL 命中不了
_PROM = "http://42.193.188.243:9090"
_JAEGER = "http://42.193.188.243:32769"
_LOKI = "http://42.193.188.243:3100"


@pytest_asyncio.fixture()
async def _repo_with_target(monkeypatch):
    """sqlite 内存库 + 一条 Otel-demo 数据源配置。

    用 sqlite 而不是 MySQL 测试库:照 tests/test_diagnosis_change_signals.py 的路子,
    让这条回归在**本机无 MySQL 时也能跑**——一条跑不起来的测试等于没有测试。
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Target.__table__, DiagnosisRun.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_base, "async_session", factory)
    async with factory() as s:
        s.add(Target(name="Otel-demo", enabled=1, sources=[
            {"type": "prometheus", "url": _PROM},
            {"type": "jaeger", "url": _JAEGER},
            {"type": "loki", "url": _LOKI},
        ]))
        await s.commit()
    yield factory
    await engine.dispose()


@pytest.fixture()
def _stub_llm(monkeypatch):
    """打桩 LLM,并**捕获 agent 真正收到的 prompt**。

    为什么要捕获 prompt:今晚最贵的一个 bug 就是键名不匹配——agent 全程收不到数据,
    而且**不报错**,只是输出一大段像模像样的"没有数据"的推理。唯一能守住它的断言,
    就是"prompt 里真的带上了数据段"。

    narrative(开方)那条 LLM 直接让它失败,走确定性兜底——回归不该依赖模型可用性。
    """
    prompts: list[str] = []

    def _fake_invoke(model, tokenizer, system_prompt, user_prompt,
                     max_token=512, temperature=1, verbose=True):
        prompts.append(user_prompt)
        return ("<thinking>桩:按证据判断</thinking>\n"
                "<answer>['ad']</answer>"), user_prompt

    # OpsAgent 的模块顶层用相对路径读 configs,import 前必须进它的工作目录
    with opsagent_adapter._opsagent_cwd():
        import agents.base_agent as base_agent
        monkeypatch.setattr(base_agent, "llm_invoke", _fake_invoke)

    def _no_llm(*_a, **_k):
        raise RuntimeError("端到端回归不调真实模型")

    monkeypatch.setattr("app.diagnosis.narrative.structured", _no_llm)
    return prompts


@pytest.mark.asyncio
async def test_pipeline_replays_recorded_fault_without_seam_breaks(
        _repo_with_target, _stub_llm):
    if not _RECORDING.exists():
        pytest.skip(f"缺录制文件 {_RECORDING.name}——采集段的真实快照不进仓库时这条回归无法运行,"
                    "请用 scripts/aiopslab_bench.py run --record 重新录一份")

    run = {"id": "e2e", "service": "", "symptom": "", "conversation_id": None,
           "platform": None, "status": "running", "stage": "queued",
           "events": [], "result": None, "created_at": ""}

    # service 留空 = 走"平台粗筛找锚点"那条路,与跑批的调用方式一致
    with net.replaying(_RECORDING):
        collect.budget_start(500, 180)
        try:
            result = await runner._run(run, "", "", None)
        finally:
            collect.budget_reset()

    # ① 跑完:整条链没有崩。这条同时守住"归因段必崩"的两个 bug
    #    (日志时间戳 float()、agent_context.last_message() 恒为 None)。
    assert isinstance(result, dict), "管线没有返回结论字典"
    assert result.get("verdict") in ("root_cause", "critical_failure",
                                     "impact", "inconclusive"), \
        f"结论形态非法:{result.get('verdict')!r}"

    # ② 最要紧的一条:LLM 真的拿到了数据。
    #    键名对不上时这三段**一个都不会出现**,而且不会报错——所以必须显式断言。
    assert _stub_llm, "三个 agent 一次 LLM 都没调:管线没走到归因段"
    assert any("derived from metric data" in p or "derived from trace data" in p
               for p in _stub_llm), (
        "agent 的 prompt 里没有任何数据段——data_features 的键名又和 agent 读的对不上了"
        "(agent 读 metric/log/trace,extract_* 返回的是 *_features)")

    # ③ 结论必须有证据支撑:指向的服务要么在候选集里,要么自身有异常维。
    #    桩故意答了 ['ad']——候选集外、且自身没有异常维,所以结论**必须不采纳它**。
    #    这条守住"LLM 凭空指认":它曾经答过一个整张调用图上毫无异常的服务。
    root = result.get("root_cause_service")
    cands = {c["service"] for c in (result.get("candidates") or [])}
    abnormal = {n["service"] for n in (result.get("full_graph") or {}).get("nodes", [])
                if n.get("abnormal")}
    assert root != "ad", "桩答的 ad 是候选集外且无异常信号的服务,结论不该采纳它"
    if root:
        assert root in cands or root in abnormal, \
            f"结论指向 {root},但它既不在候选集里、自身也没有异常信号——凭空指认"

    # ④ 结果结构完整:后续的卡片/修复 agent 全靠这几个字段,缺一个就是静默断链。
    for key in ("fault_type", "confidence", "narrative", "full_graph",
                "data_quality", "chain"):
        assert key in result, f"结论字典缺字段:{key}"

    # 原始模型输出单独保存，供复盘使用，不混入最终结论判据。
    raw = result.get("opsagent")
    assert raw == {
        "status": "ok",
        "answer_text": "ad",
        "answers": ["ad"],
        "thinkings": ["桩:按证据判断"] * 3,
        "error": None,
    }
