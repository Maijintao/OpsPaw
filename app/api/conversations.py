"""ch07 验收配套:会话列表 + 历史消息回载 + 删除会话(前端多会话切换用)。"""
import os

from fastapi import APIRouter, HTTPException

from app.db import repository
from app.graph import runtime

router = APIRouter()


@router.get("/api/conversations")
async def list_conversations(user_id: str | None = None):
    """会话列表。user_id 可省略 —— 省了就按当前登录身份查。

    为什么要能省(2026-10-27):前端首屏原本必须**先问 /api/whoami 拿身份、再拉列表**,
    于是页面先按本机旧 id 画一次(空的)、等往返回来才画第二次。服务器在澳洲,
    一个往返一秒上下,用户看到的就是"刚进来没有会话,过一会儿才出来"——
    只读访客会以为这里压根没有历史记录,直接关掉。
    身份本来就只由登录决定(见 /api/whoami),让服务端自己认,前端一次请求就够。
    """
    if not user_id:
        user_id = os.environ.get("ACCESS_USER", "admin")
    items = await repository.list_conversations(user_id)
    return {"items": items}


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int):
    """删除会话:主库(会话+消息+工单)删本体,审计/飞轮池子摘引用保资产,
    checkpointer 线程状态尽力清。失败不静默:404/500 如实回。"""
    ok = await repository.delete_conversation(conversation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    await runtime.delete_thread(conversation_id)
    return {"deleted": True}


@router.get("/api/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: int):
    if await repository.get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = await repository.list_dialog_messages(conversation_id)
    return {"items": [
        {"role": m.role, "content": m.content or "",
         # meta.suggested_actions 回传给前端重建动作卡(诊断卡/服务卡/按钮),不回传会丢
         "actions": ((m.meta or {}).get("suggested_actions")) or None,
         # 引用角标 [n] 的原始出处:不回传的话回载后只剩纯文本 "[1]",
         # 既没有珊瑚色上标样式,也点不出来源浮层(直播时有、刷新就没)
         "citations": ((m.meta or {}).get("citations")) or None,
         "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in msgs
    ]}


@router.get("/api/whoami")
async def whoami():
    """当前登录账号。前端拿它当 user_id —— 会话要跟着账号走,不能跟着浏览器走。

    之前 user_id 是每台设备本地随机生成的,换台电脑就看不到自己的会话,
    也没法把记录给别人看:同一个人在不同设备上是两个"人"。
    """
    import os
    return {"user_id": os.environ.get("ACCESS_USER", "admin")}


@router.post("/api/conversations/claim")
async def claim_conversations(body: dict):
    """把旧的本机随机 id 名下的会话,迁到当前账号名下(一次性)。

    老用户升级后第一次打开会调这个:否则历史会话挂在那个随机 id 上,换了
    身份就再也看不见了。只迁 from 指定的那一批,不动别人的。
    """
    from sqlalchemy import update
    from app.db.base import async_session
    from app.db.models import Conversation

    old = str((body or {}).get("from") or "").strip()
    new = str((body or {}).get("to") or "").strip()
    if not old or not new or old == new:
        return {"moved": 0}
    async with async_session() as sess:
        res = await sess.execute(
            update(Conversation).where(Conversation.user_id == old).values(user_id=new))
        await sess.commit()
    return {"moved": res.rowcount or 0}
