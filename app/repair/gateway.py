"""连接器会话与请求路由(进程内)。

连接器是**主动出站长轮询**:它定时来问"有没有活",服务端把请求塞进它的队列,
它执行完再回结果。这样甲方不用开任何入站端口,服务端也不需要能访问甲方内网。

边界(和方案文档一致):
- 请求只能来自服务端自己的固定动作枚举,连接器不接收命令字符串;
- 每个请求带 request_id,结果按 id 回填,错配直接丢;
- 连接器超时/掉线一律回 unknown,写操作绝不自动重试。
"""
import asyncio
import logging
import secrets
import time

logger = logging.getLogger("app")

# 连接器每 25s 轮询一次;超过这个时间没来问,就认为它掉线了(3 个周期留余量)
ONLINE_WINDOW_S = 90.0
MAX_PENDING_PER_CONNECTOR = 8


class ConnectorOffline(Exception):
    pass


class _Session:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_PENDING_PER_CONNECTOR)
        self.pending: dict[str, asyncio.Future] = {}
        self.waiter: asyncio.Task | None = None   # 当前挂着的长轮询
        self.last_poll = 0.0
        self.connected_at = time.time()


_SESSIONS: dict[str, _Session] = {}


def _cancel_waiter(sess: _Session) -> None:
    """取消当前挂着的长轮询。

    为什么必须取消:连接器进程被重启时,它上一条轮询请求在服务端还挂着(连接断了,
    但协程没感知),新进程再开一条轮询,队列里就有两个等待者。任务来的时候 FIFO 先唤醒
    旧的那个,命令发给了一条死连接,新进程永远收不到(实测:重启连接器后第一次调用必超时)。
    连接器同一时刻只该有一条轮询,新来的把旧的顶掉。"""
    if sess.waiter is not None and not sess.waiter.done():
        sess.waiter.cancel()
    sess.waiter = None


def register(connector_id: str) -> None:
    """连接器完成注册/重连:旧会话(如果有)直接换掉,挂在上面的请求按掉线处理。"""
    old = _SESSIONS.pop(connector_id, None)
    if old is not None:
        _cancel_waiter(old)
        for fut in old.pending.values():
            if not fut.done():
                fut.set_exception(ConnectorOffline("连接器重连,旧请求作废"))
    _SESSIONS[connector_id] = _Session()


def unregister(connector_id: str) -> None:
    sess = _SESSIONS.pop(connector_id, None)
    if sess is None:
        return
    _cancel_waiter(sess)
    for fut in sess.pending.values():
        if not fut.done():
            fut.set_exception(ConnectorOffline("连接器已撤销或断开"))


def is_online(connector_id: str) -> bool:
    sess = _SESSIONS.get(connector_id)
    return sess is not None and (time.time() - sess.last_poll) <= ONLINE_WINDOW_S


def has_session(connector_id: str) -> bool:
    return connector_id in _SESSIONS


def online_ids() -> list[str]:
    return [cid for cid in _SESSIONS if is_online(cid)]


async def wait_command(connector_id: str, timeout: float) -> dict | None:
    """长轮询:等到一条命令或超时。连接器每次来问都算一次心跳。"""
    sess = _SESSIONS.get(connector_id)
    if sess is None:
        raise ConnectorOffline("连接器会话不存在")
    sess.last_poll = time.time()
    _cancel_waiter(sess)                       # 顶掉可能残留的旧轮询(见 _cancel_waiter)
    task = asyncio.create_task(sess.queue.get())
    sess.waiter = task
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        return None
    finally:
        if sess.waiter is task:
            sess.waiter = None


def resolve(request_id: str, payload: dict) -> bool:
    """连接器回结果。找不到对应请求(超时后迟到/重复上报)返回 False,由调用方记日志。"""
    for sess in _SESSIONS.values():
        fut = sess.pending.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)
            return True
    return False


async def call(connector_id: str, tool: str, args: dict, *, timeout: float = 20.0,
               extra: dict | None = None) -> dict:
    """服务端发起一次固定动作调用。返回连接器的结构化结果;超时回 unknown,不重试。

    extra 只放服务端生成的东西(plan_id / approval_id),模型没有机会往里塞参数。"""
    sess = _SESSIONS.get(connector_id)
    if sess is None or not is_online(connector_id):
        raise ConnectorOffline("连接器不在线")
    request_id = "req_" + secrets.token_hex(8)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    sess.pending[request_id] = fut
    command = {"request_id": request_id, "tool": tool, "args": args}
    if extra:
        command.update(extra)
    try:
        try:
            sess.queue.put_nowait(command)
        except asyncio.QueueFull:
            return {"ok": False, "status": "denied", "request_id": request_id,
                    "message": "连接器还有未完成的请求,请稍后再试", "data": {}}
        try:
            payload = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("连接器请求超时 connector=%s tool=%s", connector_id, tool)
            return {"ok": False, "status": "unknown", "request_id": request_id,
                    "message": "连接器在时限内没有回报结果,状态未知", "data": {}}
        return {**payload, "request_id": request_id}
    finally:
        sess.pending.pop(request_id, None)
