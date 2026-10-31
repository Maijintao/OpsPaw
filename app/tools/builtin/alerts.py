"""告警接入工具:让 Agent 在对话里直接建一条"盯着某个服务"的接入。

## 为什么要有

用户在对话里说的是"帮我盯着 cart 的错误率,每 10 分钟看一眼"这种话——
让他自己去「告警接入」页填一遍表单,是把人赶出对话。这个工具把这句话直接落成配置。

## 为什么参数这么"松"

模型不该被逼着填一堆必填项。所有参数都有默认值:
- 不说频率 → 不开定时巡检(只留 webhook)
- 不说维度 → 全维度(诊断本来就全看)
- 不说服务 → 平台自动找

建完把关键信息(尤其是 webhook 地址)回给模型,它才能在对话里说清楚建了什么。
"""
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.tools import registry

_CHECK_ALIAS = {
    "错误率": "error", "报错": "error", "error": "error",
    "时延": "latency", "延迟": "latency", "耗时": "latency", "latency": "latency", "p95": "latency",
    "调用量": "calls", "流量": "calls", "calls": "calls",
    "cpu": "cpu", "处理器": "cpu",
    "内存": "memory", "memory": "memory",
}


def _norm_checks(raw: list[str]) -> list[str]:
    out: list[str] = []
    for x in raw or []:
        k = _CHECK_ALIAS.get(str(x).strip().lower()) or _CHECK_ALIAS.get(str(x).strip())
        if k and k not in out:
            out.append(k)
    return out


@tool
async def create_alert_intake(
    name: Annotated[str, Field(description="这条接入叫什么,用人话说,例如「商品页巡检」「下单链路盯梢」。用户没说就按服务名+关注点自己起一个")],
    services: Annotated[list[str], Field(description="要盯哪些服务(服务目录里的写法)。用户说「整个平台」「所有服务」就留空。用户用了代词或只说现象时,先调 query_catalog 查真实服务名,不要猜")] = [],
    checks: Annotated[list[str], Field(description="关注哪几维,可选:错误率/时延/调用量/CPU/内存。用户没说就留空(等于全看)")] = [],
    schedule_minutes: Annotated[int, Field(description="每多少分钟自动跑一遍诊断。用户说「定时看」「每隔一会查」就填 10;没提定时就填 0(不开定时)")] = 0,
    daily_at: Annotated[str, Field(description="每天定点跑一次的时间,24 小时制 HH:MM,例如「每天凌晨 12 点」填 00:00、「每天晚上 8 点」填 20:00。用户说的是具体钟点时用这个,不要用 schedule_minutes 折算")] = "",
    cron: Annotated[str, Field(description="5 段 cron(分 时 日 月 周),需要「每周一 9 点」这类复杂周期时才填,例如 0 9 * * 1。简单的每天定点用 daily_at 就够")] = "",
    threshold_metric: Annotated[str, Field(description="指标阈值:盯哪一维,可选 错误率/时延/调用量/CPU/内存。用户说「超过 X 就报警」时才填,否则留空")] = "",
    threshold_value: Annotated[float, Field(description="阈值大小(错误率填百分数,如 5 表示 5%)。不设阈值填 0")] = 0.0,
    silence_minutes: Annotated[int, Field(description="静默期:这段时间内同一条接入只触发一次,默认 15 分钟,防止一个故障刷几十遍")] = 15,
    auto_repair: Annotated[bool, Field(description="查出来之后是否自动派修复单。用户说「查出来直接修」「自动处理」才填 true,默认 false(只诊断)")] = False,
    forward_url: Annotated[str, Field(description="查出来之后把结论转发到哪个接口(完整 URL)。用户给了回调地址才填")] = "",
) -> dict:
    """创建一条告警接入(定时巡检 / 外部 webhook / 指标阈值 三种触发方式可同时开)。

    什么时候用:用户说「帮我盯着 X」「X 有异常就查一下」「每隔一会看看 Y」
    「每天 0 点跑一次巡检」「错误率超过 5% 就报警」这类话时调用。

    **直接调用,不要在对话里先问一遍再调**:系统会弹出「告警接入预览」卡让用户确认,
    那就是确认环节。再在文字里问一次等于让用户确认两遍,很难用。
    参数缺就按默认值填(不定时就不开定时、不指定服务就全平台),不要为了凑参数反复追问。

    建完把 webhook 地址原样告诉用户(他要把这个地址填进告警平台)。
    """
    from app.api import sentinels as smod
    from app.db import repository
    from app.tools import service_catalog

    await smod.ensure_tables()

    # 平台:取第一个启用的(多平台场景由用户在页面上切)
    rows = [r for r in await repository.list_targets() if r.enabled]
    if not rows:
        return {"ok": False, "error": "还没有接入任何平台,先去「接入平台」加一个"}
    target = rows[0]

    # 服务名校验:写错的名字直接指出来,别建一条盯不到东西的接入
    picked = [str(s).strip() for s in (services or []) if str(s).strip()]
    if picked:
        try:
            catalog, _err = await service_catalog.discover_services()
            valid = {x["service_id"] for x in catalog if x.get("target") == target.name}
        except Exception:  # noqa: BLE001  查不到目录就放行,不拦用户
            valid = set()
        bad = [s for s in picked if valid and s not in valid]
        if bad:
            return {"ok": False,
                    "error": f"这些服务在「{target.name}」里找不到:{'、'.join(bad)}",
                    "valid_services": sorted(valid)[:40],
                    "hint": "用 valid_services 里的写法重试,或留空让平台自动找"}

    checks_norm = _norm_checks(checks)
    thr_on = bool(threshold_metric) and threshold_value > 0

    # 定时:三种输入优先级 cron > daily_at > schedule_minutes
    expr = ""
    if cron and len(cron.split()) == 5:
        expr = cron.strip()
    elif daily_at:
        hh, _, mm = daily_at.strip().partition(":")
        try:
            expr = f"{int(mm or 0)} {int(hh)} * * *"
        except ValueError:
            expr = ""
    mode = "cron" if expr else "interval"
    triggers = {
        "schedule": {"enabled": bool(expr) or schedule_minutes > 0,
                     "mode": mode,
                     "interval_min": max(1, int(schedule_minutes or 10)),
                     "cron": expr or "0 0 * * *"},
        # 没配任何触发方式时,webhook 兜底打开,否则这条接入建了也不会动
        "webhook": {"enabled": True},
        "threshold": {"enabled": thr_on,
                      "metric": _norm_checks([threshold_metric])[0] if thr_on else "error",
                      "op": ">", "value": float(threshold_value or 0)},
    }
    policy = {"silence_min": max(0, int(silence_minutes or 0)), "hours": [0, 24]}

    import secrets
    from app.db.models import Sentinel
    from app.db import base as db_base
    row = Sentinel(name=name.strip()[:128], target_id=target.id, services=picked[:20],
                   checks=checks_norm, triggers=triggers, policy=policy,
                   action=",".join([x for x in
                                    (("forward" if forward_url else ""),
                                     ("repair" if auto_repair else "")) if x]) or "diagnose",
                   forward_url=(forward_url or "")[:512],
                   token=secrets.token_urlsafe(18), enabled=1)
    async with db_base.async_session() as sess:
        sess.add(row)
        await sess.commit()
        await sess.refresh(row)

    base = __import__("os").environ.get("OPSPAW_PUBLIC_BASE", "")
    hook = f"{base}/api/sentinels/hook/{row.token}" if base else f"/api/sentinels/hook/{row.token}"
    how = []
    if triggers["schedule"]["enabled"]:
        if mode == "cron":
            how.append(f"定时巡检(cron {expr})")
        else:
            how.append(f"每 {triggers['schedule']['interval_min']} 分钟定时巡检")
    how.append("外部告警 webhook")
    if thr_on:
        how.append(f"指标阈值 {threshold_metric}>{threshold_value}")

    return {"ok": True, "id": row.id, "name": row.name,
            "target": target.name,
            "services": picked or "(平台自动找)",
            "checks": [smod.CHECK_LABELS.get(c, c) for c in checks_norm] or "全维度",
            "triggers": how,
            "silence_minutes": policy["silence_min"],
            "webhook_url": hook,
            "note": "已建好。把 webhook_url 告诉用户,他填进告警平台就能用;"
                    "页面在「告警接入」。"}


registry.register(registry.spec_from_langchain_tool(create_alert_intake, source="builtin"))


async def _find_intake(key: str):
    """按 id 或名字找一条接入。找不到/重名都返回候选,让模型自己改,不要瞎猜。"""
    from sqlalchemy import select
    from app.db import base as db_base
    from app.db.models import Sentinel

    key = str(key or "").strip()
    async with db_base.async_session() as sess:
        if key.isdigit():
            row = await sess.get(Sentinel, int(key))
            if row is not None:
                return row, None
        rows = (await sess.execute(select(Sentinel).order_by(Sentinel.id))).scalars().all()
    exact = [r for r in rows if r.name == key]
    if len(exact) == 1:
        return exact[0], None
    cands = [{"id": r.id, "name": r.name, "enabled": bool(r.enabled)} for r in (exact or rows)]
    if len(exact) > 1:
        return None, {"error": f"有 {len(exact)} 条都叫「{key}」,请用 id 指定", "candidates": cands}
    fuzzy = [r for r in rows if key and key in r.name]
    if len(fuzzy) == 1:
        return fuzzy[0], None
    return None, {"error": f"没找到叫「{key}」的接入", "candidates": cands[:20]}


def _brief(r) -> dict:
    from app.api import sentinels as smod
    trg = smod._triggers(r)
    sc = trg["schedule"]
    when = ("每天 " + sc.get("cron", "")) if sc.get("mode") == "cron" else \
           (f"每 {sc.get('interval_min')} 分钟" if sc.get("enabled") else "未开定时")
    return {"id": r.id, "name": r.name, "enabled": bool(r.enabled),
            "services": r.services or "(平台自动找)",
            "checks": [smod.CHECK_LABELS.get(c, c) for c in (r.checks or [])] or "全维度",
            "schedule": when, "action": r.action or "diagnose",
            "trigger_count": r.trigger_count or 0}


@tool
async def list_alert_intakes() -> dict:
    """列出当前所有告警接入(名称、id、是否启用、盯哪些服务、什么节奏、触发过几次)。

    用户问「我有哪些告警」「那条巡检还在跑吗」「把每天的巡检找出来」时先调它,
    拿到 id/名字再决定下一步,不要凭记忆编名字。
    """
    from sqlalchemy import select
    from app.db import base as db_base
    from app.db.models import Sentinel

    async with db_base.async_session() as sess:
        rows = (await sess.execute(select(Sentinel).order_by(Sentinel.id))).scalars().all()
    if not rows:
        return {"ok": True, "count": 0, "items": [],
                "note": "目前一条告警接入都没有,可以问用户要不要建一条"}
    return {"ok": True, "count": len(rows), "items": [_brief(r) for r in rows]}


@tool
async def update_alert_intake(
    name: Annotated[str, Field(description="要改哪一条:接入名称或 id。不确定就先调 list_alert_intakes")],
    enabled: Annotated[bool | None, Field(description="true 启用 / false 停用;不改就留空")] = None,
    daily_at: Annotated[str, Field(description="改成每天定点跑,HH:MM(如 00:00);不想改定时留空")] = "",
    interval_min: Annotated[int, Field(description="改成每 N 分钟跑一次;不想改留 0")] = 0,
    silence_minutes: Annotated[int, Field(description="改静默期分钟数;不改留 0")] = 0,
) -> dict:
    """修改已有告警接入:启停、改定时、改静默期。这是**写操作**,系统会弹确认卡。

    用户说「把那条巡检停了」「改成每 30 分钟」「静默期太长了改短点」时用。
    直接调用,不要先问一遍——弹出来的确认卡就是确认环节。
    """
    from app.api import sentinels as smod
    await smod.ensure_tables()
    row, err = await _find_intake(name)
    if err:
        return {"ok": False, **err}

    trg = smod._triggers(row)
    pol = smod._policy(row)
    changes = []
    if enabled is not None:
        row.enabled = 1 if enabled else 0
        changes.append("启用" if enabled else "停用")
    if daily_at:
        hh, _, mm = daily_at.strip().partition(":")
        try:
            trg["schedule"].update({"enabled": True, "mode": "cron",
                                    "cron": f"{int(mm or 0)} {int(hh)} * * *"})
            changes.append(f"改成每天 {daily_at}")
        except ValueError:
            return {"ok": False, "error": f"时间写法不对:{daily_at},要 HH:MM"}
    if interval_min > 0:
        trg["schedule"].update({"enabled": True, "mode": "interval",
                                "interval_min": int(interval_min)})
        changes.append(f"改成每 {interval_min} 分钟")
    if silence_minutes > 0:
        pol["silence_min"] = int(silence_minutes)
        changes.append(f"静默期 {silence_minutes} 分钟")
    if not changes:
        return {"ok": False, "error": "没说要改什么。可以改:启停 / 定点时间 / 间隔 / 静默期"}

    from app.db import base as db_base
    async with db_base.async_session() as sess:
        db_row = await sess.get(type(row), row.id)
        db_row.enabled = row.enabled
        db_row.triggers = trg
        db_row.policy = pol
        await sess.commit()
        await sess.refresh(db_row)
        row = db_row
    return {"ok": True, "id": row.id, "name": row.name,
            "changes": changes, "now": _brief(row)}


@tool
async def delete_alert_intake(
    name: Annotated[str, Field(description="要删哪一条:接入名称或 id。不确定就先调 list_alert_intakes")],
) -> dict:
    """删除一条告警接入(不可恢复,webhook 地址同时失效)。这是**写操作**,系统会弹确认卡。

    用户说「删掉那条」「不要了」时用。**只是不想让它跑的话优先用 update 停用**,
    删了就找不回来了。直接调用,不要先问一遍。
    """
    from sqlalchemy import delete
    from app.api import sentinels as smod
    from app.db import base as db_base
    from app.db.models import IntakeEvent

    await smod.ensure_tables()
    row, err = await _find_intake(name)
    if err:
        return {"ok": False, **err}
    name_saved, id_saved = row.name, row.id
    async with db_base.async_session() as sess:
        await sess.execute(delete(IntakeEvent).where(IntakeEvent.intake_id == id_saved))
        await sess.execute(delete(type(row)).where(type(row).id == id_saved))
        await sess.commit()
    return {"ok": True, "id": id_saved, "name": name_saved,
            "note": "已删除。如果只是暂时不想跑,下次建议用 update 停用而不是删。"}


registry.register(registry.spec_from_langchain_tool(list_alert_intakes, source="builtin"))
registry.register(registry.spec_from_langchain_tool(update_alert_intake, source="builtin"))
registry.register(registry.spec_from_langchain_tool(delete_alert_intake, source="builtin"))
