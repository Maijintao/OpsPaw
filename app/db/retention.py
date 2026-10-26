"""诊断/告警流水数据的保留策略:按运维在后台(admin「数据保留」)配置执行清理。

为什么需要:diagnosis_runs 的 result 是几十上百 KB 的大 JSON(完整证据链+候选+
全图),只增不减——半年后"翻最近 12 条找调用图""事故联动全表扫"这些路径会被
自己的历史拖慢。策略(默认全关,删不删、删多狠由运维拍板,天数后台可改):

- 结论为 clear/no_data 的诊断:超过 clear_days(默认 30)整行删——它们只在
  基线剔除里有短期价值,过期即无;
- 其余诊断(确诊/有异常/证据不足):超过 slim_days(默认 90)裁剪大字段——
  result 只留结论摘要(结论/根因/形态/开方/置信/预算),events 清空。事故复盘
  和"相似事故"匹配要的是结论,不是全量证据。**events 为空 = 已裁剪的标记**,
  幂等,重复执行不重复处理;
- intake_events 告警流水:超过 intake_days(默认 90)整行删,统计价值随时间
  衰减到零;
- incidents / incident_events 不删:事故量天然小,是核心资产。

执行方式:每批限量删除(避免长事务锁表影响在线诊断);admin 的「立即清理」
按钮随时可手动执行;后台循环只按 enabled 开关每日自动跑——开关只管自动,
手动是运维的显式意志,永远可用。
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update

from app.db import base as db
from app.db.models import DiagnosisRun, Incident, IncidentEvent, IntakeEvent

logger = logging.getLogger("app")

# 每批删除的行数:工程常量(避免长事务),不是校准出来的判据阈值
BATCH = 500
# 天数缺省:流水价值半衰期的工程估计——clear 类结论 30 天后对基线剔除无贡献,
# 摘要化 90 天足够覆盖"上月出过什么事故"的复盘窗口;均可后台改
DEFAULTS = {"clear_days": 30, "slim_days": 90, "intake_days": 90}
_SLIM_KEYS = ("verdict", "root_cause_service", "fault_type", "narrative",
              "confidence", "missing", "budget")


async def get_policy() -> dict:
    """读保留策略(app_settings 覆盖项,缺省全关 + 默认天数)。"""
    from app.db import repository
    try:
        m = await repository.get_settings_map()
    except Exception:  # noqa: BLE001  库不可读时按全关处理,不猜
        return {"enabled": False, **DEFAULTS}
    out = {"enabled": m.get("retention.enabled") == "1"}
    for k in DEFAULTS:
        try:
            out[k] = max(1, min(int(m.get(f"retention.{k}") or DEFAULTS[k]), 3650))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    return out


async def save_policy(enabled: bool, clear_days: int, slim_days: int,
                      intake_days: int) -> dict:
    """保存策略。天数夹在 1~3650(防手滑填 0 把库清空/填天文数字没意义)。"""
    from app.db import repository
    clamp = lambda n: max(1, min(int(n), 3650))  # noqa: E731
    await repository.upsert_settings({
        "retention.enabled": "1" if enabled else "0",
        "retention.clear_days": str(clamp(clear_days)),
        "retention.slim_days": str(clamp(slim_days)),
        "retention.intake_days": str(clamp(intake_days)),
    })
    return await get_policy()


async def _delete_batched(sess, model, id_col, where) -> int:
    """分批限量删除:先取一批 id 再删,避免长事务;返回总删除行数。"""
    total = 0
    while True:
        ids = (await sess.execute(
            select(id_col).where(where).limit(BATCH))).scalars().all()
        if not ids:
            return total
        await sess.execute(delete(model).where(id_col.in_(ids)))
        total += len(ids)


def _slim_result(raw) -> dict:
    """大 result → 结论摘要(保留键白名单 + 裁剪标记)。str/dict 都兼容。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    out = {k: raw[k] for k in _SLIM_KEYS if raw.get(k) is not None}
    out["slimmed"] = True
    out["slimmed_at"] = datetime.now().isoformat(timespec="seconds")
    return out


async def run_once(manual: bool = False) -> dict:
    """按当前策略执行一轮清理。manual=True 是 admin 手动按钮,无视 enabled;
    后台循环走 manual=False,只在开关打开时执行。返回各类清理计数。"""
    p = await get_policy()
    if not manual and not p["enabled"]:
        return {"skipped": "retention disabled"}
    now = datetime.now()
    out: dict = {}
    async with db.async_session() as sess:
        out["cleared_runs"] = await _delete_batched(
            sess, DiagnosisRun, DiagnosisRun.id,
            DiagnosisRun.verdict.in_(("clear", "no_data"))
            & (DiagnosisRun.created_at < now - timedelta(days=p["clear_days"])))
        # 裁剪:events 非空 = 还没裁过(裁完置空作幂等标记)。
        # 直接取 (id, result) 算摘要,再用带条件的单条 UPDATE 落库——不走 ORM
        # 逐行 get/commit(实测在 aiosqlite 上会卡死),条件里再押一遍
        # events 非空,天然幂等。一批 200 行:200×100KB 的峰值内存是可控上限
        n = 0
        while True:
            rows = (await sess.execute(
                select(DiagnosisRun.id, DiagnosisRun.result)
                .where(DiagnosisRun.created_at < now - timedelta(days=p["slim_days"]),
                       DiagnosisRun.events.isnot(None))
                .limit(200))).all()
            if not rows:
                break
            for did, raw in rows:
                await sess.execute(
                    update(DiagnosisRun)
                    .where(DiagnosisRun.id == did,
                           DiagnosisRun.events.isnot(None))
                    .values(result=_slim_result(raw), events=None))
                n += 1
            await sess.commit()
        out["slimmed_runs"] = n
        out["intake_events"] = await _delete_batched(
            sess, IntakeEvent, IntakeEvent.id,
            IntakeEvent.fired_at < now - timedelta(days=p["intake_days"]))
    if any(v for k, v in out.items() if k != "skipped"):
        logger.info("数据保留清理完成:%s", out)
    return out


async def data_stats() -> dict:
    """数据体检:各表行数 + 诊断结论分布 + 最老记录,给 admin 卡看增长趋势。"""
    async with db.async_session() as sess:
        async def count(model) -> int:
            return (await sess.execute(
                select(func.count()).select_from(model))).scalar() or 0
        runs_total = await count(DiagnosisRun)
        slimmed = (await sess.execute(
            select(func.count()).select_from(DiagnosisRun)
            .where(DiagnosisRun.events.is_(None)))).scalar() or 0
        oldest = (await sess.execute(
            select(func.min(DiagnosisRun.created_at)))).scalar()
        return {
            "diagnosis_runs": {"total": runs_total, "slimmed": slimmed,
                               "oldest": oldest.isoformat() if oldest else None},
            "incidents": await count(Incident),
            "incident_events": await count(IncidentEvent),
            "intake_events": await count(IntakeEvent),
        }


async def retention_loop() -> None:
    """后台每日循环:开关开着就按策略清一轮。睡 6 小时醒一次——清理幂等,
    多跑无害,漏跑一天也不致命。"""
    while True:
        try:
            await asyncio.sleep(6 * 3600)
            p = await get_policy()
            if p["enabled"]:
                await run_once(manual=False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  后台循环永不因单次失败退出
            logger.warning("保留清理循环异常", exc_info=True)
