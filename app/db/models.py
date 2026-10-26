from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        Enum("进行中", "已转人工", "已结束"), server_default="进行中"
    )
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 会话小标题(满2轮后摘要模型异步生成;空=未生成,前端回退首问截断)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)               # ch07 早期轮次滚动摘要
    summary_upto_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 摘要覆盖到的消息 id
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id")
    )
    role: Mapped[str] = mapped_column(Enum("user", "assistant", "tool"))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 前端回放数据(suggested_actions 等),不进 LLM 上下文
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id")
    )
    description: Mapped[str] = mapped_column(Text)
    ticket_type: Mapped[str] = mapped_column(Enum("故障", "变更", "咨询", "投诉"))
    status: Mapped[str] = mapped_column(
        Enum("待处理", "已处理"), server_default="待处理"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ToolAuditLog(Base):
    __tablename__ = "tool_audit_logs"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 不挂 FK:审计不能被引用约束拦
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_source: Mapped[str] = mapped_column(Enum("builtin", "mcp"))
    mcp_server: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Enum("成功", "失败", "超时", "校验拦下", "权限拒绝"))
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Target(Base):
    """检测靶子:接入的真实被测服务 + 若干观测数据源(sources JSON 数组,元素 {"type","url"})。

    数据源类型看 app/api/targets.py 的 PROBES 注册表(Prometheus / Jaeger),同类型可配多路。
    本表是「外接数据源」的唯一事实源:Agent 侧连接清单 = env 的 mock MCP(演示) ∪ 本表(真实)。
    未接入时表为空,Agent 保持纯演示形态,主链路零感知——靶子是可选增强,不是主链路依赖。
    接「另一路同类数据源」纯页面操作零改码;接「一类新系统」才需要在 PROBES 加一个探活器。"""
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    # 多云集群名:同一个系统部署在多个云时,几张卡共用一个集群名(单云留空)。
    # 只做分组与上下文,不改变"一张卡=一台机器"的既有模型。
    cluster: Mapped[str] = mapped_column(String(64), default="", server_default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)  # [{"type": "prometheus", "url": "http://..."}]
    enabled: Mapped[int] = mapped_column(Integer, server_default="1")  # 0=保留配置但暂不接入(为后续 Agent 侧接线预留)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )



class RepairEnrollmentToken(Base):
    """修复连接器的一次性注册令牌:只存 hash,明文只在生成命令那一刻出现一次。

    绑定平台(target_id)、带有效期、用完即废——命令泄漏了也只能换一次连接,
    而且平台管理员随时能撤销。"""
    __tablename__ = "repair_enrollment_tokens"

    token_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())



class RepairConnector(Base):
    """修复连接器:甲方机器上跑的那一个,一个平台可以挂多个(多宿主机/多控制面)。

    身份是"平台 + 连接器":写操作必须指明是哪一个,不能靠"这个平台随便挑一台"。
    secret_hash 与注册令牌分开存——连接器上线后用自己的密钥说话,令牌不再复用。"""
    __tablename__ = "repair_connectors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default="online")  # online/offline/revoked
    secret_hash: Mapped[str] = mapped_column(String(64))
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    host_info: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DiagnosisRun(Base):
    """根因分析任务存档:诊断层 v0 的 run 只存内存,进程重启卡片就查无此单。
    落库后聊天卡片历史回放/刷新/跨天都能拿到完整结论;终态才写库(进行中的轮询走内存)。"""
    __tablename__ = "diagnosis_runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    service: Mapped[str] = mapped_column(String(128))
    symptom: Mapped[str | None] = mapped_column(String(512), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16))   # done / failed(queued/running 不落库)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    root_cause_service: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 用户对根因的确认:诊断结论在确认前只是系统分析,不能拿它启动修复
    confirmation: Mapped[str] = mapped_column(String(16), server_default="pending")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    events: Mapped[list | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
