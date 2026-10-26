"""ch10 权威归并术语表:全系统唯一一份,17 类主题类目。
数据处理、预标、训练、推理、评测、前端全部 import 这里,不许各自抄一份。
类目名与边界说明采用课程 README 归并术语表原文;元组顺序即 label id,训练/推理共用。
severity 是容错档位:归错会带偏补知识优先级的类目从严。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class TopicClass:
    name: str
    boundary: str                 # 一句边界说明:什么算这一类,近邻类目靠它划开
    examples: tuple[str, ...]     # 用户的说法示例
    severity: str                 # 容错档位:严 / 中 / 宽


TOPIC_CLASSES: tuple[TopicClass, ...] = (
    TopicClass("告警阈值", "各告警的触发条件与阈值;处置方法归对应故障类",
               ("告警阈值", "CPU 多少算高", "什么级别会告警", "内存告警线"), "严"),
    TopicClass("CPU 异常", "CPU 使用率高、负载高类问题",
               ("CPU 飙高", "处理器打满", "load 高", "CPU 告警了"), "严"),
    TopicClass("内存异常", "内存使用率高、OOM、GC 频繁",
               ("内存爆了", "OOM 了", "频繁 full GC", "内存泄漏"), "严"),
    TopicClass("服务不可用", "服务挂了、健康检查失败、全实例下线",
               ("服务挂了", "不可用", "健康检查不过", "全实例宕机"), "严"),
    TopicClass("响应缓慢", "接口慢、P99 超标、超时增多;资源高归资源类",
               ("接口好慢", "P99 超了", "响应超时", "变卡了"), "严"),
    TopicClass("磁盘异常", "磁盘使用率、inode、日志占满磁盘",
               ("磁盘满了", "日志太大", "inode 用完", "盘快满了"), "严"),
    TopicClass("变更申请", "重启、扩容、回滚等变更怎么办、怎么审批",
               ("重启服务", "扩容", "回滚版本", "变更怎么审批", "变更窗口几点"), "严"),
    TopicClass("日志排查", "怎么查日志、日志主题、过滤语法",
               ("查日志", "ERROR 日志", "日志主题在哪", "怎么过滤"), "中"),
    TopicClass("监控观测", "监控曲线、指标含义、怎么查监控",
               ("CPU 曲线", "怎么看监控", "内存水位", "p95 是什么"), "中"),
    TopicClass("值班制度", "值班安排、响应时效、升级机制、交接班",
               ("几点值班", "多久要响应", "升级给谁", "怎么交接班"), "中"),
    TopicClass("服务信息", "服务目录、归属团队、依赖关系、服务等级",
               ("这个服务谁负责", "有哪些服务", "依赖哪个服务", "服务编号多少"), "中"),
    TopicClass("网络问题", "连通性、DNS、负载均衡、安全组",
               ("连不上", "DNS 解析失败", "负载均衡异常", "网络超时"), "中"),
    TopicClass("数据库存储", "慢 SQL、连接池、缓存、Redis/MQ 依赖故障",
               ("慢查询", "连接池耗尽", "Redis 挂了", "缓存穿透"), "中"),
    TopicClass("发布相关", "发布流程、灰度、发布引起的问题;回滚处置归变更申请",
               ("发布窗口", "灰度怎么发", "刚发完版就告警", "发布卡住了"), "中"),
    TopicClass("通知公告", "故障通告、值班群同步、恢复通告",
               ("发恢复通告", "群里同步啥", "公告模板", "要通知用户吗"), "宽"),
    TopicClass("工具使用", "值班猫助手能力边界、工具怎么用",
               ("能帮我重启吗", "怎么查监控", "你能干什么", "建单怎么建"), "宽"),
    TopicClass("其他", "上面都对不上的,先兜底",
               ("闲聊", "转人工", "值班猫是啥"), "宽"),
)

TOPIC_NAMES: tuple[str, ...] = tuple(c.name for c in TOPIC_CLASSES)
LABEL2ID: dict[str, int] = {name: i for i, name in enumerate(TOPIC_NAMES)}
ID2LABEL: dict[int, str] = {i: name for i, name in enumerate(TOPIC_NAMES)}
NUM_CLASSES = len(TOPIC_CLASSES)
SEVERITY: dict[str, str] = {c.name: c.severity for c in TOPIC_CLASSES}


def terminology_table() -> str:
    """预标/造数 prompt 用的术语表文本:类目:边界说明(示例)。"""
    return "\n".join(
        f"- {c.name}:{c.boundary}(示例:{'、'.join(c.examples)})" for c in TOPIC_CLASSES
    )
