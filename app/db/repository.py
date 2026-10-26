from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, select, update

import app.db.base as db
from app.db.models import Conversation, Message, Ticket, ToolAuditLog, Target, DiagnosisRun

_TICKET_SEQ = 0

def _gen_ticket_no() -> str:
    global _TICKET_SEQ
    _TICKET_SEQ += 1
    return f"T{datetime.now():%Y%m%d%H%M%S}{_TICKET_SEQ:03d}"


async def create_conversation(user_id: str) -> int:
    async with db.async_session() as s:
        conv = Conversation(user_id=user_id)
        s.add(conv)
        await s.commit()
        return conv.id


async def get_conversation(conversation_id: int) -> Conversation | None:
    async with db.async_session() as s:
        return await s.get(Conversation, conversation_id)


async def append_message(
    conversation_id: int,
    role: str,
    content: str | None = None,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    meta: dict | None = None,
) -> int:
    async with db.async_session() as s:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            meta=meta,
        )
        s.add(msg)
        await s.commit()
        return msg.id


async def list_messages(conversation_id: int) -> list[Message]:
    async with db.async_session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
        )
        return list(result.scalars())


async def create_ticket(conversation_id: int, description: str, ticket_type: str) -> str:
    ticket_no = _gen_ticket_no()
    async with db.async_session() as s:
        s.add(
            Ticket(
                ticket_no=ticket_no,
                conversation_id=conversation_id,
                description=description,
                ticket_type=ticket_type,
            )
        )
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.status = "已转人工"
        await s.commit()
    return ticket_no


async def count_messages_after(conversation_id: int, after_id: int | None) -> int:
    """距上次摘要新增了多少条(after_id 空 = 从未摘要,数全量)。摘要触发判据。
    注:此处数全角色,摘要源 list_dialog_messages 只取 user/assistant——当前一致
    (tool 不落库);若未来 tool 行落库,两处口径需同步收紧。"""
    async with db.async_session() as s:
        q = (select(func.count()).select_from(Message)
             .where(Message.conversation_id == conversation_id))
        if after_id:
            q = q.where(Message.id > after_id)
        return int((await s.execute(q)).scalar_one())


async def list_dialog_messages(conversation_id: int) -> list[Message]:
    """user/assistant 消息按 id 升序(摘要任务源数据;tool 行不落库,过滤一道保险)。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id,
                   Message.role.in_(("user", "assistant")))
            .order_by(Message.id)
        )
        return list(result.scalars())


async def list_conversations(user_id: str, limit: int = 50) -> list[dict]:
    """某用户的会话列表(新在前,带首问预览 + 有无摘要标记),前端多会话切换用。"""
    async with db.async_session() as s:
        convs = list((await s.execute(
            select(Conversation).where(Conversation.user_id == user_id)
            .order_by(Conversation.id.desc()).limit(limit)
        )).scalars())
        out = []
        for c in convs:
            first = (await s.execute(
                select(Message.content)
                .where(Message.conversation_id == c.id, Message.role == "user")
                .order_by(Message.id).limit(1)
            )).scalar()
            out.append({
                "id": c.id, "status": c.status,
                "title": c.title,
                "preview": (first or "(空会话)")[:40],
                "has_summary": bool(c.summary),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            })
        return out


async def update_conversation_title(conversation_id: int, title: str) -> None:
    """会话小标题生成成功后落库(只写一次,已有值不覆盖)。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is not None and not conv.title:
            conv.title = title
            await s.commit()


async def delete_conversation(conversation_id: int) -> bool:
    """删除会话及其内容本体(messages/tickets)。两类关联行不删只摘引用:
    tool_audit_logs(审计必须留痕,无 FK 本可悬空,摘成 NULL 免得 join 出错数据)与
    low_confidence_questions(飞轮池子是资产,不随会话蒸发)。LangGraph checkpointer
    的线程状态另在 runtime.delete_thread 清,这里只管主库。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None:
            return False
        await s.execute(delete(Message).where(Message.conversation_id == conversation_id))
        await s.execute(delete(Ticket).where(Ticket.conversation_id == conversation_id))
        await s.execute(update(ToolAuditLog)
                        .where(ToolAuditLog.conversation_id == conversation_id)
                        .values(conversation_id=None))
        await s.delete(conv)
        await s.commit()
        return True


async def update_conversation_summary(conversation_id: int, summary: str, upto_msg_id: int) -> None:
    """摘要成功后原子更新两字段(一起写,不会出现摘要新边界旧)。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.summary = summary
            conv.summary_upto_msg_id = upto_msg_id
            await s.commit()


async def insert_tool_audit(
    conversation_id: int | None,
    tool_call_id: str | None,
    tool_name: str,
    tool_source: str,
    mcp_server: str | None,
    arguments: dict | None,
    result_summary: str | None,
    status: str,
    error_message: str | None,
    retry_count: int,
    duration_ms: int | None,
) -> None:
    """工具调用审计落一条。调用方(engine)自行 try/except——审计失败不许反拦工具执行。"""
    async with db.async_session() as s:
        s.add(ToolAuditLog(
            conversation_id=conversation_id, tool_call_id=tool_call_id,
            tool_name=tool_name, tool_source=tool_source, mcp_server=mcp_server,
            arguments=arguments, result_summary=result_summary, status=status,
            error_message=error_message, retry_count=retry_count, duration_ms=duration_ms,
        ))
        await s.commit()


async def list_targets() -> list[Target]:
    async with db.async_session() as s:
        rows = (await s.execute(select(Target).order_by(Target.id))).scalars().all()
        return list(rows)


async def ensure_targets_table() -> None:
    """项目没有迁移框架,历史建表走 sql/ 手工 DDL;这里只对新表做启动自建:
    checkfirst=True 内部先查表是否存在,只补建缺失的 targets 一张,已有表一概不碰。

    结构对不上(表里还是旧的双列 URL 版、没有 sources 列)时重建——targets 表当日成立、
    无存量数据,不值得写迁移;重建前查一遍行数,有数据就不动表只告警,宁可起不来也不删人的配置。"""
    import logging

    from sqlalchemy import inspect, text

    def _ensure(sync_conn) -> None:
        insp = inspect(sync_conn)
        if not insp.has_table("targets"):
            Target.__table__.create(sync_conn)
            return
        cols = {c["name"] for c in insp.get_columns("targets")}
        if "cluster" not in cols:      # 多云分组列:老库补一列,不重建表、不动数据
            sync_conn.execute(text(
                "ALTER TABLE targets ADD COLUMN cluster VARCHAR(64) DEFAULT ''"))
        if "sources" in cols:
            return
        n_rows = sync_conn.execute(text("SELECT COUNT(*) FROM targets")).scalar() or 0
        if n_rows:
            logging.getLogger("app").error(
                "targets 表是旧结构且有 %s 行数据,不自动重建;请手工迁移后重启", n_rows)
            return
        Target.__table__.drop(sync_conn)
        Target.__table__.create(sync_conn)

    async with db.engine.begin() as conn:
        await conn.run_sync(_ensure)


async def create_target(name: str, sources: list, cluster: str = "") -> Target:
    async with db.async_session() as s:
        row = Target(name=name, sources=sources, cluster=cluster or "")
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def update_target(target_id: int, name: str, sources: list,
                        cluster: str = "") -> Target | None:
    async with db.async_session() as s:
        row = await s.get(Target, target_id)
        if row is None:
            return None
        row.name, row.sources = name, sources
        row.cluster = cluster or ""
        await s.commit()
        await s.refresh(row)
        return row


async def get_target(target_id: int) -> Target | None:
    async with db.async_session() as s:
        return await s.get(Target, target_id)


async def delete_target(target_id: int) -> bool:
    async with db.async_session() as s:
        row = await s.get(Target, target_id)
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
        return True


async def update_target_sources(target_id: int, sources: list) -> Target | None:
    """只改 sources(指纹刷新/自愈改址专用):不碰 name——机器自动写回不许动人工起的字段。"""
    async with db.async_session() as s:
        row = await s.get(Target, target_id)
        if row is None:
            return None
        row.sources = sources
        await s.commit()
        await s.refresh(row)
        return row


async def ensure_diagnosis_table() -> None:
    """diagnosis_runs 启动自建(同 ensure_settings_table 范式:checkfirst,只补建不迁移)。
    新增的 platform / confirmation 两列用 ALTER 补——老库里已有诊断存档,不能重建。"""
    from sqlalchemy import inspect, text

    def _ensure(sync_conn) -> None:
        DiagnosisRun.__table__.create(sync_conn, checkfirst=True)
        insp = inspect(sync_conn)
        have = {c["name"] for c in insp.get_columns("diagnosis_runs")}
        add = {"platform": "VARCHAR(128)",
               "confirmation": "VARCHAR(16) DEFAULT 'pending'",
               "confirmed_at": "DATETIME",
               "confirmed_by": "VARCHAR(64)"}
        for name, ddl in add.items():
            if name not in have:
                sync_conn.execute(text(f"ALTER TABLE diagnosis_runs ADD COLUMN {name} {ddl}"))

    async with db.engine.begin() as conn:
        await conn.run_sync(_ensure)


async def recent_incident_windows(since: datetime, until: datetime) -> list[tuple[float, float]]:
    """最近被判为真故障(root_cause/critical_failure)的诊断采样窗 [window_start, window_end]。

    用途:基线分布剔除这些时段。故障期的指标值会混进"过去 24 小时"的历史分布,把
    正常范围的估计整体抬高——同一天里第二次发生同款故障时,当前值落在被污染的
    "正常范围"内,粗筛失明(实测:早间注入的 cart 故障把历史错误率 max 抬到 0.33%,
    下午同款故障 0.14% 判成正常)。诊断存档本身就是"哪些时段出过事"的权威记录,
    拿它洗基线是零成本的。until 用来约束"只看诊断时刻之前已知的故障"(离线回放
    把时钟拨回过去时,不能拿未来才发生的故障洗过去的基线)。库不可读返回空——
    基线洗刷是增强,失败不拖垮粗筛。

    **按窗口时间筛,不按行创建时间筛**(2026-10-17 实测修正):created_at 由
    `server_default=func.now()` 落库——sqlite 上是 UTC,MySQL 上是会话时区,而窗口
    用的是本地 epoch,两者可能差整整一个时区。旧实现拿 created_at 当边界,实测在
    回放里把 8 小时**之后**才发生的故障窗也剔了(时钟拨回 14:42,却剔到 21:59 的窗),
    同时把 24 小时历史最早那 8 小时的故障窗漏掉——两个方向的错都直接改历史分布。
    现在只用一个宽松到能盖住任何时区差的粗筛把候选行捞出来,再按窗口自身的 epoch
    判"与 [since, until] 是否重叠",语义与时区都对齐。"""
    lo, hi = since.timestamp(), until.timestamp()
    margin = 86400          # 粗筛放宽一天:只用来少读行,不参与判定
    out: list[tuple[float, float]] = []
    try:
        async with db.async_session() as s:
            rows = (await s.execute(
                select(DiagnosisRun)
                .where(DiagnosisRun.verdict.in_(("root_cause", "critical_failure")))
                .where(DiagnosisRun.created_at >= since - timedelta(seconds=margin))
                .where(DiagnosisRun.created_at <= until + timedelta(seconds=margin))
            )).scalars().all()
        for r in rows:
            dq = ((r.result or {}).get("data_quality") or {}).get("trace") or {}
            ws, we = dq.get("window_start"), dq.get("window_end")
            if not (ws and we):
                continue
            ws, we = float(ws), float(we)
            if we <= ws:
                continue
            if we <= lo or ws >= hi:      # 与查询区间不重叠
                continue
            out.append((ws, we))
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("app").warning("诊断窗读取失败(%s),基线不剔除故障时段", e)
    return out


async def recent_change_signals(service: str, since: datetime, until: datetime) -> list[dict]:
    """窗内变更信号(本产品自己的库,不是外部发布系统):"有人动过/申请动过这个服务"的痕迹。
    - tickets:ticket_type='变更' 且描述里提到服务名的工单(变更表单落库,描述含服务名);
    - tool_audit_logs:submit_change 工具调用且 arguments.service_id 命中(审计表,含被拦下的)。
    服务匹配从宽:服务名是描述/参数的子串——宁可多报候选痕迹,由归因员结合证据判断,
    不在这里做精确闸。库不可读时返回空——变更维是证据增强,失败不拖垮诊断。"""
    out: list[dict] = []
    svc = (service or "").strip().lower()
    if not svc:
        return out
    try:
        async with db.async_session() as s:
            rows = (await s.execute(
                select(Ticket)
                .where(Ticket.ticket_type == "变更")
                .where(Ticket.created_at >= since)
                .where(Ticket.created_at <= until)
                .order_by(Ticket.created_at.desc())
                .limit(50))).scalars().all()
            for t in rows:
                if svc in (t.description or "").lower():
                    out.append({"kind": "change_ticket", "ts": t.created_at.isoformat(),
                                "detail": (t.description or "")[:160],
                                "status": t.status})
            audits = (await s.execute(
                select(ToolAuditLog)
                .where(ToolAuditLog.tool_name == "submit_change")
                .where(ToolAuditLog.created_at >= since)
                .where(ToolAuditLog.created_at <= until)
                .order_by(ToolAuditLog.created_at.desc())
                .limit(50))).scalars().all()
            for a in audits:
                args = a.arguments or {}
                if svc in str(args.get("service_id") or "").lower():
                    out.append({"kind": "change_request", "ts": a.created_at.isoformat(),
                                "detail": f"变更申请(status={a.status})",
                                "status": a.status})
    except Exception:  # noqa: BLE001  变更维尽力而为,失败不打断诊断
        import logging
        logging.getLogger("app").warning("变更信号查询失败 service=%s", service, exc_info=True)
        return []
    return sorted(out, key=lambda x: x["ts"], reverse=True)


async def save_diagnosis_run(run: dict) -> None:
    """诊断终态落库(整单覆盖式 upsert,同一 id 只有一份终态)。失败不抛——存档是增强,
    不能反过来弄死诊断任务本身,调用方已自行兜底。"""
    import logging

    row = {
        "id": run["id"], "conversation_id": run.get("conversation_id"),
        "service": run.get("service") or "", "symptom": run.get("symptom"),
        "platform": run.get("platform"),
        "status": run.get("status") or "done",
        "verdict": (run.get("result") or {}).get("verdict"),
        "root_cause_service": (run.get("result") or {}).get("root_cause_service"),
        "confirmation": run.get("confirmation") or "pending",
        "events": run.get("events") or [], "result": run.get("result"),
    }
    try:
        async with db.async_session() as s:
            await s.merge(DiagnosisRun(**row))
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("app").warning("诊断任务 %s 落库失败(%s),卡片仅本次进程内可见",
                                         run.get("id"), type(e).__name__)


async def get_diagnosis_run(diag_id: str) -> dict | None:
    """按 id 取存档,重建成 API 同形的 run 载荷(内存没有时兜底用)。"""
    async with db.async_session() as s:
        row = await s.get(DiagnosisRun, diag_id)
    if row is None:
        return None
    return {"id": row.id, "service": row.service, "symptom": row.symptom or "",
            "platform": row.platform,
            "status": row.status, "stage": "done" if row.status == "done" else "failed",
            "confirmation": row.confirmation or "pending",
            "events": row.events or [], "result": row.result or {},
            "conversation_id": row.conversation_id,
            "created_at": row.created_at.isoformat() if row.created_at else None}


async def confirm_diagnosis(diag_id: str, confirmed: bool, by: str = "admin") -> dict | None:
    """记录用户对根因的确认/否认。未确认的结论不得用来启动修复(计划创建会校验)。"""
    async with db.async_session() as s:
        row = await s.get(DiagnosisRun, diag_id)
        if row is None:
            return None
        row.confirmation = "confirmed" if confirmed else "rejected"
        row.confirmed_at = datetime.now()
        row.confirmed_by = by
        await s.commit()
        return {"id": row.id, "confirmation": row.confirmation,
                "confirmed_at": row.confirmed_at.isoformat(timespec="seconds")}


async def find_confirmed_diagnosis(conversation_id: int,
                                   service: str) -> DiagnosisRun | None:
    """这个会话里、针对这个服务、用户已确认的最近一次诊断——计划创建的门槛。"""
    async with db.async_session() as s:
        stmt = (select(DiagnosisRun)
                .where(DiagnosisRun.conversation_id == conversation_id,
                       DiagnosisRun.root_cause_service == service,
                       DiagnosisRun.confirmation == "confirmed",
                       DiagnosisRun.status == "done")
                .order_by(DiagnosisRun.created_at.desc()).limit(1))
        return (await s.execute(stmt)).scalars().first()


async def find_latest_confirmed_diagnosis(conversation_id: int) -> DiagnosisRun | None:
    """会话内最近一次已确认根因的诊断(修复 agent 派单用,不限服务——用户说"修复"
    时不该逼他再报一遍服务名,最近确认的那单就是要修的)。"""
    async with db.async_session() as s:
        stmt = (select(DiagnosisRun)
                .where(DiagnosisRun.conversation_id == conversation_id,
                       DiagnosisRun.confirmation == "confirmed",
                       DiagnosisRun.status == "done",
                       DiagnosisRun.verdict.in_(("root_cause", "critical_failure")))
                .order_by(DiagnosisRun.created_at.desc()).limit(1))
        return (await s.execute(stmt)).scalars().first()
