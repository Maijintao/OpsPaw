"""诊断层的网络出入口:一处建 HTTP 客户端,支持「实时 / 录制 / 回放」三种模式。

## 为什么要有它

实时跑批一组 7.5 分钟,其中 **93% 在空转**(等故障在遥测里显形 180s + 等数据窗洗白
240s),真正算的只有 30 秒。而"那一刻的数据"能不能再取到,取决于上游存多久:

    Prometheus   默认留 1 周        → 指标侧可以按历史时刻直接重查
    Jaeger       内存存储,只留最近若干条 trace → **历史 trace 拿不回来**
    Loki         看部署,同样不保证

所以"把时钟拨回去重跑"只能重放指标侧,traces 一取不到,采集段就断了、后面整条链
跟着断。要让**整条管线**能反复重跑,必须在采的时候把上游响应录下来。

## 三种模式

    live     直接走网络(默认,生产行为)
    record   走网络,同时把每条响应存进一个 JSON 文件
    replay   不走网络,从 JSON 文件里取响应

配合"把时钟冻在采集那一刻"(scripts/eval_batch_fast.py),回放时发出的请求与录制时
逐字节相同,所以按完整 URL 做键就能命中。

## 边界

- 录制文件是**那一刻的现场快照**,不是"永远如此"。判据改了要重跑 → 回放;数据面
  变了(接了新数据源) → 必须重新录一次。
- 回放命中不了会如实报出来(回放结束时打印未命中的请求),不静默给空数据。
"""
import hashlib
import json
import logging
from contextlib import contextmanager
from pathlib import Path

import httpx

logger = logging.getLogger("app")

_MODE = "live"           # live | record | replay | replay_record
_PATH: Path | None = None
_STORE: dict[str, dict] = {}
_FROZEN: float | None = None
_HITS = {"hit": 0, "miss": 0}
_MISSED: list[str] = []
_LLM_REPLAYABLE = False  # 当前录制里是否含 LLM 条目(决定回放时 LLM 走不走 net)
_MISS_STORE: dict[str, dict] = {}   # "回放+补录"模式里,未命中而走网络取回的响应
_MISS_PATH: Path | None = None      # 补录写盘路径;None = 普通回放(未命中返回 404)


def _key(request: httpx.Request) -> str:
    """请求的键 = 完整 URL(路径 + 查询串);带请求体时(如 LLM 的 POST)再并入体哈希。

    为什么必须并入体:GET 的参数在查询串里,URL 已经唯一;而 LLM 的 POST 全是同一个
    URL(/chat/completions)、正文不同——只按 URL 做键,所有 LLM 调用会互相覆盖,
    录制里只剩最后一次响应,回放必错。GET 的体为空,键与旧录制逐字节一致,
    不影响任何已有录制文件。"""
    base = str(request.url)
    body = request.content or b""
    if body:
        return base + "#" + hashlib.sha256(body).hexdigest()[:16]
    return base


def current_mode() -> str:
    return _MODE


def llm_client_enabled() -> bool:
    """LLM 调用是否该走 net 的客户端(从而进录制/回放)。

    - record:总是——新录制要把 LLM 也录进去,否则回放对 LLM 段不可复现;
    - replay_record:总是——回放旧录制的同时,把未命中的请求(含 LLM)走网络并补录下来;
    - replay:仅当该录制里确实有 LLM 条目。旧录制(2026-10-15 之前)没有 LLM,
      回放时如实退回 live,不让老录制因 LLM 未命中而整单失败;
    - live:不走(生产行为不变)。
    """
    if _MODE in ("record", "replay_record"):
        return True
    if _MODE == "replay":
        return _LLM_REPLAYABLE
    return False


class _RecordingTransport(httpx.AsyncBaseTransport):
    """走网络,顺手把响应存下来。"""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        body = await resp.aread()
        await resp.aclose()
        # 记的是**解码后**的正文,所以回放时不能再带 content-encoding——
        # 否则 httpx 会拿已经解开的正文再解一次,直接报错。
        headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in ("content-encoding", "content-length")}
        _STORE[_key(request)] = {"status": resp.status_code, "headers": headers,
                                 "body": body.decode("utf-8", "replace")}
        return httpx.Response(resp.status_code, content=body, headers=headers,
                              request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _ReplayTransport(httpx.AsyncBaseTransport):
    """从录制里取。取不到时:有内层传输就**走网络并把响应补录下来**(replay_record 模式,
    用来给老录制补上它缺的那部分——通常是 LLM 调用与新加的查询);没有内层就如实记一笔
    未命中、返回 404,不静默给空数据。"""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        entry = _STORE.get(_key(request))
        if entry is not None:
            _HITS["hit"] += 1
            return httpx.Response(entry["status"], content=entry["body"].encode(),
                                  headers=entry.get("headers") or {}, request=request)
        if self._inner is not None:
            resp = await self._inner.handle_async_request(request)
            body = await resp.aread()
            await resp.aclose()
            headers = {k: v for k, v in resp.headers.items()
                       if k.lower() not in ("content-encoding", "content-length")}
            _MISS_STORE[_key(request)] = {"status": resp.status_code, "headers": headers,
                                          "body": body.decode("utf-8", "replace")}
            _HITS["backfilled"] = _HITS.get("backfilled", 0) + 1
            logger.info("回放未命中,已补录:%s", _key(request))
            return httpx.Response(resp.status_code, content=body, headers=headers,
                                  request=request)
        _HITS["miss"] += 1
        if len(_MISSED) < 20:
            _MISSED.append(_key(request))
        # 未命中是"这段请求录制里没有"(常见于代码加了新查询、而录制是旧的),
        # 不是上游故障。显式说清楚,别让调用方把 404 误当成真实错误打整段栈。
        logger.info("回放未命中(录制里没有这条请求):%s", _key(request))
        return httpx.Response(404, content=b'{"status":"error","error":"replay miss"}',
                              headers={"x-opspaw-replay": "miss"}, request=request)


def set_mode(mode: str, path: str | Path | None = None) -> None:
    """切换模式。一般不用直接调——用下面的 recording() / replaying() 上下文管理器,
    它们会把"冻时钟"一起做掉(冻时钟是命中的前提,漏了必然全不命中)。"""
    global _MODE, _PATH, _LLM_REPLAYABLE
    if mode not in ("live", "record", "replay"):
        raise ValueError(f"未知模式:{mode!r}")
    if mode == "record":
        if path is None:
            raise ValueError("record 模式必须给输出文件路径")
        _STORE.clear()
        _PATH = Path(path)
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _LLM_REPLAYABLE = True
    elif mode == "replay":
        if path is None:
            raise ValueError("replay 模式必须给录制文件路径")
        _PATH = Path(path)
        if not _PATH.exists():
            raise FileNotFoundError(f"录制文件不存在:{_PATH}")
        _STORE.clear()
        _STORE.update(_load(_PATH))
        _HITS["hit"] = _HITS["miss"] = 0
        _MISSED.clear()
        _LLM_REPLAYABLE = _has_llm_entries()
    else:
        _LLM_REPLAYABLE = False
    _MODE = mode


def _load(path: Path) -> dict:
    blob = json.loads(path.read_text())
    return blob.get("entries") if isinstance(blob, dict) and "entries" in blob else blob


@contextmanager
def recording(path: str | Path):
    """录制:冻结时钟 + 走网络 + 存响应。**必须冻结时钟**——否则录和放两次的查询
    时刻不同(指标窗口、trace 的 start/end 都是相对"现在"算的),键对不上,回放全miss。
    LLM 的 POST 也走这里(见 llm_client_enabled),录制文件标记 llm_recorded。"""
    global _MODE, _PATH, _FROZEN, _LLM_REPLAYABLE
    import time as _time
    _FROZEN = _time.time()
    _STORE.clear()
    _PATH = Path(path)
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _MODE = "record"
    _LLM_REPLAYABLE = True     # 本次录制会把 LLM 录进去(见 llm_client_enabled)
    orig = _time.time
    _time.time = lambda: _FROZEN
    try:
        yield
    finally:
        _time.time = orig
        _MODE = "live"
        _LLM_REPLAYABLE = False
        _PATH.write_text(json.dumps(
            {"frozen_at": _FROZEN, "llm_recorded": _has_llm_entries(),
             "entries": _STORE}, ensure_ascii=False))


def _has_llm_entries() -> bool:
    """录制里是否含带请求体的条目(= LLM 这类 POST)。旧录制没有,回放时 LLM 退回 live。"""
    return any("#" in k for k in _STORE)


@contextmanager
def replaying(path: str | Path):
    """回放:从录制取响应,并把时钟冻在**录制那一刻**——这样同一段代码发出的请求
    与录制时逐字节相同,按键就能命中。未命中返回 404(不碰网络)。
    LLM 是否也走 net,取决于该录制里有没有 LLM 条目(旧录制没有 → 退回 live)。"""
    global _MODE, _PATH, _FROZEN, _LLM_REPLAYABLE
    import time as _time
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"录制文件不存在:{p}")
    blob = json.loads(p.read_text())
    _STORE.clear()
    _STORE.update(blob.get("entries") if isinstance(blob, dict) else blob)
    _FROZEN = blob.get("frozen_at") if isinstance(blob, dict) else None
    _PATH = p
    _HITS["hit"] = _HITS["miss"] = 0
    _HITS.pop("backfilled", None)
    _MISSED.clear()
    _MODE = "replay"
    # 旧录制没有 llm_recorded 字段:按是否含带体条目推断;没有就让 LLM 走 live,
    # 避免老录制因 LLM 未命中而整单失败(如实记一条告警,不静默)
    _LLM_REPLAYABLE = bool(blob.get("llm_recorded")) if isinstance(blob, dict) \
        else _has_llm_entries()
    if not _LLM_REPLAYABLE:
        logger.warning("录制 %s 不含 LLM 条目(旧格式),本次回放 LLM 段走 live、不可复现",
                       p.name)
    orig = _time.time
    if _FROZEN:
        _time.time = lambda: _FROZEN
    try:
        yield
    finally:
        _time.time = orig
        _MODE = "live"
        _LLM_REPLAYABLE = False


@contextmanager
def replaying_recording(play_path: str | Path, record_path: str | Path):
    """**回放 + 补录**:命中就从 play_path 取;未命中(通常是老录制里没有的 LLM 调用、
    或代码新加的查询)就走网络,并把响应补录到 record_path。

    用途:老录制不含 LLM,直接回放跑不出可复现的基线。用这个模式跑一遍,就把 LLM 段
    补录下来——之后 `replaying(record_path)` 就能**全离线**重放。关键是**不用在靶子上
    注故障、也不用等 90 分钟**:观测数据照旧来自老录制(冻结在录制那一刻),只有缺的
    那部分才走网络。"""
    global _MODE, _PATH, _FROZEN, _LLM_REPLAYABLE, _MISS_PATH
    import time as _time
    p, out = Path(play_path), Path(record_path)
    if not p.exists():
        raise FileNotFoundError(f"录制文件不存在:{p}")
    blob = json.loads(p.read_text())
    _STORE.clear()
    _STORE.update(blob.get("entries") if isinstance(blob, dict) else blob)
    _FROZEN = blob.get("frozen_at") if isinstance(blob, dict) else None
    _PATH = p
    _MISS_STORE.clear()
    _MISS_PATH = out
    out.parent.mkdir(parents=True, exist_ok=True)
    _HITS["hit"] = _HITS["miss"] = 0
    _HITS.pop("backfilled", None)
    _MISSED.clear()
    _MODE = "replay_record"
    _LLM_REPLAYABLE = True
    orig = _time.time
    if _FROZEN:
        _time.time = lambda: _FROZEN
    try:
        yield
    finally:
        _time.time = orig
        _MODE = "live"
        _LLM_REPLAYABLE = False
        _MISS_PATH = None
        # 写**并集**:老录制的条目 + 这次补录的。必须自包含——只写增量的话,单独回放
        # 这份文件会几乎全 miss(命中要靠老录制,而回放时并不加载它)。
        merged = {**_STORE, **_MISS_STORE}
        out.write_text(json.dumps(
            {"frozen_at": _FROZEN,
             "llm_recorded": any("#" in k for k in merged),
             "backfilled": len(_MISS_STORE), "entries": merged},
            ensure_ascii=False))


def flush() -> dict:
    """把录制写盘,返回统计。只读模式什么都不做。"""
    if _MODE == "record" and _PATH is not None:
        _PATH.write_text(json.dumps(_STORE, ensure_ascii=False))
    return {"mode": _MODE, "recorded": len(_STORE), "hits": _HITS["hit"],
            "misses": _HITS["miss"], "backfilled": _HITS.get("backfilled", 0),
            "missed": list(_MISSED)}


def client(timeout: float) -> httpx.AsyncClient:
    """建一个诊断用的客户端。**全层只从这里建**——不然录制/回放会漏掉某条通道。"""
    # trust_env=False:直连,不吃环境代理(本机 Clash 会把远端 IP 截胡成 502)
    if _MODE == "record":
        return httpx.AsyncClient(timeout=timeout, trust_env=False,
                                 transport=_RecordingTransport(httpx.AsyncHTTPTransport(
                                     trust_env=False)))
    if _MODE == "replay":
        return httpx.AsyncClient(timeout=timeout, trust_env=False,
                                 transport=_ReplayTransport())
    if _MODE == "replay_record":
        # 命中走录制,未命中走网络并补录
        return httpx.AsyncClient(timeout=timeout, trust_env=False,
                                 transport=_ReplayTransport(
                                     httpx.AsyncHTTPTransport(trust_env=False)))
    return httpx.AsyncClient(timeout=timeout, trust_env=False)
