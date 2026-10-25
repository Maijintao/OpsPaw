from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages


class SessionStore:
    """内存会话存储,session_id -> 消息列表。本章不做持久化。"""

    def __init__(self) -> None:
        self._sessions: dict[str, list[BaseMessage]] = {}

    def get(self, session_id: str) -> list[BaseMessage]:
        return self._sessions.get(session_id, [])

    def append(self, session_id: str, *messages: BaseMessage) -> None:
        self._sessions.setdefault(session_id, []).extend(messages)


def trim_history(messages: list[BaseMessage], max_tokens: int) -> list[BaseMessage]:
    return trim_messages(
        messages,
        strategy="last",
        token_counter=count_tokens_approximately,
        max_tokens=max_tokens,
        start_on="human",
        allow_partial=False,
    )


# ---- ch07 锚点切窗 + 摘要注入(双层上下文的滑窗层)----

_DB_ID_PREFIX = "db-"


def _db_msg_id(m: BaseMessage) -> int | None:
    """从消息 id 解出 MySQL 消息 id(入口约定 HumanMessage.id=f"db-{msg_id}");非锚点返回 None。"""
    mid = getattr(m, "id", None)
    if isinstance(mid, str) and mid.startswith(_DB_ID_PREFIX):
        try:
            return int(mid[len(_DB_ID_PREFIX):])
        except ValueError:
            return None
    return None


def build_window(messages: list[BaseMessage], summary_upto_msg_id: int,
                 max_tokens: int) -> list[BaseMessage]:
    """滑动窗口:摘要边界锚点切窗(第一条 db-id > 边界的用户消息起)+ trim_messages token 兜底。
    锚点缺失(旧会话)/边界为 0 → 整段进 trim,退化纯 token 裁剪;裁到空宁可超预算回退原窗。
    只在调模型前现拼,不改 State。"""
    start = 0
    if summary_upto_msg_id:
        for i, m in enumerate(messages):
            if isinstance(m, HumanMessage):
                did = _db_msg_id(m)
                if did is not None and did > summary_upto_msg_id:
                    start = i
                    break
    window = messages[start:]
    trimmed = trim_history(window, max_tokens=max_tokens)
    return trimmed or window


def summary_line(summary: str | None) -> str:
    """coref/意图的历史文本前缀:单行摘要;无摘要空串。"""
    return f"(早前对话摘要:{summary})" if summary else ""


def summary_system(summary: str | None) -> SystemMessage | None:
    """main_agent 拼装用:摘要 system 消息(紧跟人设 system);无摘要 None。"""
    if not summary:
        return None
    return SystemMessage(f"## 早前对话摘要(更早轮次已压缩,其中事实可信)\n{summary}")
