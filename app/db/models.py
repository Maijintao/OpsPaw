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



class AppSetting(Base):
    """后台「API 设置」:运行时可改的上游配置覆盖项(key-value),优先级高于 .env。

    只存与 .env 不同的覆盖值,程序自改 .env 是反模式(容器只读/git 脏),DB 才是运行时
    配置的正解。embed_fingerprint 单独一条:记录「Milvus 里的向量是哪个嵌入配置产出的」
    (base_url+model)——换嵌入模型向量空间不通用、不能互转,指纹变了必须 drop 集合
    全量重嵌;api_key 不进指纹(key 只影响连通性,不影响向量空间)。"""
    __tablename__ = "app_settings"

    skey: Mapped[str] = mapped_column(String(64), primary_key=True)
    svalue: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )



class RepairPlan(Base):
    """修复计划:动作、目标、参数定稿后算一个 hash,审批和执行都认这个 hash。

    计划一旦创建就不再改——要改就作废重开。这是"审批批的是哪一份"唯一说得清的做法。"""
    __tablename__ = "repair_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), index=True)
    connector_id: Mapped[str] = mapped_column(String(32), index=True)
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    diagnosis_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    service: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String(500), default="")
    risk: Mapped[str] = mapped_column(String(16), default="中")
    plan_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="awaiting_confirmation")
    created_by: Mapped[str] = mapped_column(String(64), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)



class RepairApproval(Base):
    """审批:绑定计划 hash、审批人、有效期。计划内容一变 hash 就变,旧审批自动失效。"""
    __tablename__ = "repair_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(32), index=True)
    plan_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/used/rejected



class RepairExecution(Base):
    """执行记录:一次计划一次执行,结果与验证分开存——执行成功不等于问题解决。"""
    __tablename__ = "repair_executions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(32), index=True)
    approval_id: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connector_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)



class RepairAuditLog(Base):
    """修复审计:谁在什么时候对哪个平台做了什么。只记 ID、状态和安全摘要,
    不放 token、密钥、完整命令。"""
    __tablename__ = "repair_audit_logs"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    target_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), index=True, nullable=True)
    connector_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="admin")
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())



class RepairRun(Base):
    """修复 agent 任务存档:同 DiagnosisRun 范式——内存只做热缓存,终态落库,
    修复卡片刷新/回放/跨天都能拿到完整回执。"""
    __tablename__ = "repair_runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), nullable=True)
    diagnosis_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    service: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16))   # done / failed(进行中不落库)
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    events: Mapped[list | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())



class Sentinel(Base):
    """哨兵:一个"谁触发、查什么"的入口。

    为什么要有它:现在诊断只能由人在对话里发起,而真实值班是**告警先响**。
    哨兵就是那根接线——外部告警系统 POST 到 /api/sentinels/hook/{token},
    我们按它配的范围发起一次诊断,把结论记回这条哨兵。

    字段说明:
    - services: 限定服务名列表;空 = 交给平台粗筛自己找
    - checks:   限定关注哪几维(error/latency/calls/cpu/memory);
                诊断仍会看全维,这里只作为"症状描述"传给归因,影响它先看什么
    - token:    webhook 路径里的凭证,泄露就换一个(重新生成)
    """
    __tablename__ = "sentinels"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"),
                                    primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    services: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    # 触发方式(可多选同时生效):
    #   schedule  定时巡检 —— 每 interval_min 分钟自动跑一遍诊断(夜莺那种告警引擎)
    #   webhook   外部告警 —— 告警平台 POST 进来就触发
    #   threshold 指标阈值 —— 自己查 Prometheus,超了才触发(不依赖外部告警平台)
    triggers: Mapped[dict] = mapped_column(JSON, default=dict)
    # 收敛策略:静默期内同一条接入只触发一次(防告警风暴);hours 是生效时段
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # 检测出来之后怎么办:diagnose=只诊断 / forward=自动转发到指定接口 / repair=自动派修复
    action: Mapped[str] = mapped_column(String(32), server_default="diagnose")
    forward_url: Mapped[str] = mapped_column(String(512), server_default="")
    enabled: Mapped[int] = mapped_column(Integer, server_default="1")
    trigger_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_diagnosis_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())



class IntakeEvent(Base):
    """一条接入的触发记录:什么时候、因为什么触发、诊断出了什么结论。

    为什么单独一张表:值班员最常问的是"它刚才为什么报警、结论是什么",
    没有这条流水,接入就只是个配置页,看不到发生过什么。
    """
    __tablename__ = "intake_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"),
                                    primary_key=True, autoincrement=True)
    intake_id: Mapped[int] = mapped_column(Integer, index=True)
    fired_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    trigger: Mapped[str] = mapped_column(String(24))   # schedule/webhook/threshold/manual
    service: Mapped[str] = mapped_column(String(128), default="")
    diagnosis_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skipped: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 被静默/停用时记原因



class Incident(Base):
    """事故:一次故障从告警到闭环的整条流水线的挂载点。

    为什么要有它:一次故障会让多条哨兵各自触发诊断,界面上就是几张孤立卡片;
    没有"一起事故"这个对象,降噪(几条告警并成一起)、复盘、认领升级都无从谈起。
    并单/拆单的判据在 app/api/incidents.py(确定性判据,零阈值):
    - 新告警指向的服务落在某未关闭事故的 scope(症状/根因/诊断子图服务集)里 → 并入;
    - 诊断完成后根因服务与另一未关闭事故相同 → 后者并入前者(同根因=同一起故障);
    - 宁可漏并不可错并:并错的每一条告警都能在详情页手动拆出成独立事故。
    等级语义(不是拍数值,是结论语义的映射,P0 只留给人工标记):
    确诊故障(root_cause/critical_failure)=P1;有异常未定位(impact/victims_only)=P2;
    其余(未确诊/clear)=P3;severity 可人工改。
    """
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"),
                                    primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(192), default="疑似故障")
    severity: Mapped[str] = mapped_column(String(4), server_default="P3")
    # open(诊断中) → diagnosed(已定位) → mitigating(修复中) → resolved(已解决) → closed(已关闭)
    status: Mapped[str] = mapped_column(String(16), server_default="open", index=True)
    platform: Mapped[str | None] = mapped_column(String(128), nullable=True)
    service: Mapped[str] = mapped_column(String(128), default="")     # 症状服务(触发时的入口)
    root_cause_service: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fault_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diagnosis_id: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 首个诊断
    scope: Mapped[list] = mapped_column(JSON, default=list)   # 并单判据用:本事故覆盖的服务集
    alert_count: Mapped[int] = mapped_column(Integer, server_default="1")  # 并入的告警条数(降噪口径)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())



class IncidentEvent(Base):
    """事故时间线:告警并入、诊断结论、修复回执、状态变更、拆单,各记一行。

    详情页按时间铺开就是事故的完整叙事;复盘(第三期)的直接原料。
    kind: alert / diagnosis / repair / status / split / note
    detail: {…} 各类事件自带字段(告警带 intake_event_id/sentinel 名,诊断带 verdict/根因)
    """
    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"),
                                    primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"),
                                             index=True)
    kind: Mapped[str] = mapped_column(String(16))
    # 指向的对象 id(诊断 id/修复 id/告警流水 id):并单、详情页反查全靠它,
    # 放独立列而不是埋在 JSON 里——跨 sqlite/MySQL 的 JSON 查询没有可移植写法
    ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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
