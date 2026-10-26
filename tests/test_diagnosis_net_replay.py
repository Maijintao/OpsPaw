"""录制/回放层对 LLM(POST)的支持:键含体哈希 + 旧录制兼容 + 回放命中语义。

阶段0 整改(见《裁判层整改方案》§4.2)的回归锚:
- 旧录制只按 URL 做键,LLM 的 POST 全撞同一个键、互相覆盖——回放对 LLM 段不可复现;
- 现在键并入体哈希,GET 键逐字节不变(老录制不受影响);
- 旧格式录制(不含 LLM 条目)回放时 LLM 退回 live,不让老录制整单失败。
"""
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from app.diagnosis import net

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def workdir():
    """项目内临时目录(pytest 自带 tmp_path 在本机沙箱下建目录会被拒)。"""
    d = Path(tempfile.mkdtemp(prefix=".wb_net_", dir=_REPO))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_get_key_unchanged():
    """GET 无请求体,键必须与旧录制逐字节一致(否则所有老录制全 miss)。"""
    r = httpx.Request("GET", "http://h/api/v1/query?query=up")
    assert net._key(r) == "http://h/api/v1/query?query=up"


def test_post_key_includes_body_hash():
    """同 URL、不同正文的 POST 必须落到不同键,否则互相覆盖。"""
    a = httpx.Request("POST", "http://h/v1/chat/completions", content=b'{"m":"a"}')
    b = httpx.Request("POST", "http://h/v1/chat/completions", content=b'{"m":"b"}')
    assert net._key(a) != net._key(b)
    assert net._key(a).startswith("http://h/v1/chat/completions#")


def test_llm_client_enabled_state_machine(workdir):
    """record 恒真;replay 看录制是否含 LLM 条目;live 恒假。"""
    new = workdir / "new.json"
    new.write_text(json.dumps({"frozen_at": 1.0, "llm_recorded": True, "entries": {}}))
    with net.replaying(new):
        assert net.llm_client_enabled() is True

    old = workdir / "old.json"           # 旧格式:无 llm_recorded、只有 GET 条目
    old.write_text(json.dumps({"frozen_at": 1.0,
                               "entries": {"http://h/api/services": {}}}))
    with net.replaying(old):
        assert net.llm_client_enabled() is False   # 退回 live,不让老录制整单失败

    assert net.llm_client_enabled() is False       # live


def test_replay_roundtrip_hit_and_miss(workdir):
    """同一正文命中录制,不同正文如实 miss(404),不静默给空数据。"""
    body = b'{"model":"m","messages":[]}'
    key = net._key(httpx.Request("POST", "http://h/v1/chat/completions", content=body))
    rec = workdir / "rec.json"
    rec.write_text(json.dumps({
        "frozen_at": 1.0, "llm_recorded": True,
        "entries": {key: {"status": 200, "headers": {}, "body": '{"ok":1}'}},
    }))

    async def call(content: bytes) -> httpx.Response:
        with net.replaying(rec):
            async with net.client(5.0) as c:
                return await c.post("http://h/v1/chat/completions", content=content)

    hit = asyncio.run(call(body))
    assert hit.status_code == 200 and hit.json() == {"ok": 1}
    miss = asyncio.run(call(b'{"other":1}'))
    assert miss.status_code == 404


def test_recording_marks_llm_recorded(workdir):
    """录制含带体条目时,落盘的 llm_recorded 为真(回放据此决定 LLM 走不走 net)。"""
    class _Echo(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b"{}", request=request)

    path = workdir / "r.json"

    async def go():
        with net.recording(path):
            transport = net._RecordingTransport(_Echo())   # 绕开真实网络
            await transport.handle_async_request(
                httpx.Request("POST", "http://h/v1/chat/completions", content=b'{"a":1}'))
            assert any("#" in k for k in net._STORE)

    asyncio.run(go())
    blob = json.loads(path.read_text())
    assert blob.get("llm_recorded") is True


def test_replaying_recording_writes_self_contained_union(workdir):
    """回放+补录写出来的文件必须**自包含**(老条目 ∪ 补录条目)。

    真缺陷:只写增量的话,单独回放这份文件会几乎全 miss——命中本来要靠老录制,
    而回放时并不加载它。实测:写增量时回放成功数从 11 掉到 1。"""
    old = workdir / "old.json"
    old.write_text(json.dumps({"frozen_at": 1.0, "entries": {
        "http://h/a": {"status": 200, "headers": {}, "body": "A"}}}))
    out = workdir / "new.json"

    class _Echo(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b"B", request=request)

    async def go():
        with net.replaying_recording(old, out):
            t = net._ReplayTransport(_Echo())
            await t.handle_async_request(httpx.Request("GET", "http://h/a"))   # 命中老录制
            await t.handle_async_request(httpx.Request("GET", "http://h/b"))   # 未命中→补录
            assert net.llm_client_enabled() is True      # 补录模式下 LLM 也走 net

    asyncio.run(go())
    blob = json.loads(out.read_text())
    assert set(blob["entries"]) == {"http://h/a", "http://h/b"}   # 并集,自包含
    assert blob["backfilled"] == 1
