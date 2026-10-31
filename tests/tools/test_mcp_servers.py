"""ch08 MCP Server 集成测试:真起子进程 → adapters client 列工具 + 真调。
连不上视为环境问题直接 fail(两台 Server 是本章交付物,不 skip)。"""
import os
import pathlib
import socket
import subprocess
import sys
import time
from datetime import datetime

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _wait_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"MCP Server 端口 {port} 在 {timeout}s 内未就绪")


@pytest.fixture(scope="module")
def mcp_procs():
    p1 = subprocess.Popen([sys.executable, str(_ROOT / "mcp_servers/monitor_server.py")],
                          env={**os.environ, "PORT": "18101"})
    p2 = subprocess.Popen([sys.executable, str(_ROOT / "mcp_servers/log_server.py")],
                          env={**os.environ, "PORT": "18102"})
    try:
        _wait_port(18101)
        _wait_port(18102)
        yield
    finally:
        p1.terminate()
        p2.terminate()
        p1.wait(timeout=5)
        p2.wait(timeout=5)


def _client() -> MultiServerMCPClient:
    return MultiServerMCPClient({
        "monitor": {"transport": "streamable_http", "url": "http://127.0.0.1:18101/mcp"},
        "logs": {"transport": "streamable_http", "url": "http://127.0.0.1:18102/mcp"},
    }, handle_tool_errors=False)


async def test_list_tools_monitor_and_logs(mcp_procs):
    tools = await _client().get_tools()
    by = {t.name: t for t in tools}
    assert {"query_cpu_metrics", "query_memory_metrics"} <= set(by)       # monitor 侧
    assert {"get_current_timestamp", "search_topic_by_service_name",
            "search_log"} <= set(by)                                      # logs 侧
    for t in by.values():
        assert t.description                                   # 用途描述
        schema = t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema()
        if t.name != "get_current_timestamp":                  # 唯一的无参工具
            assert schema.get("properties")                    # JSON Schema 参数定义


def _text_of(result) -> str:
    """adapters 返回内容块列表 [{"type":"text","text":...,"id":lc_随机}],块 id 每次不同,
    比稳定性只看文本载荷。"""
    if isinstance(result, list):
        return "\n".join(b.get("text", "") for b in result if isinstance(b, dict))
    return str(result)


async def test_invoke_monitor_stable_mock(mcp_procs):
    # 固定时间窗可复现；重叠时间点在不同查询中保持一致，窗口变化会改变序列。
    args = {"service_name": "data-sync-service",
            "start_time": "2026-07-01 10:00:00", "end_time": "2026-07-01 10:10:00"}
    tools = {t.name: t for t in await _client().get_tools(server_name="monitor")}
    r1 = await tools["query_cpu_metrics"].ainvoke(args)
    r2 = await tools["query_cpu_metrics"].ainvoke(args)
    assert _text_of(r1) == _text_of(r2)                        # 种子稳定
    assert "statistics" in _text_of(r1)                        # 统计块在(告警判定素材)
    shifted = await tools["query_cpu_metrics"].ainvoke(
        {**args, "start_time": "2026-07-01 11:00:00", "end_time": "2026-07-01 11:10:00"})
    assert _text_of(shifted) != _text_of(r1)


async def test_invoke_log_chain(mcp_procs):
    # 排障链路:按服务名定位日志主题 → 拿 topic_id → 按固定时间窗检索日志
    tools = {t.name: t for t in await _client().get_tools(server_name="logs")}
    topics = await tools["search_topic_by_service_name"].ainvoke({"service_name": "data-sync"})
    assert "topic-001" in _text_of(topics)                     # mock 主题清单里能定位到

    start_ms = int(datetime(2026, 7, 1, 10, 0).timestamp() * 1000)
    logs = await tools["search_log"].ainvoke(
        {"topic_id": "topic-001", "start_time": start_ms,
         "end_time": start_ms + 10 * 60 * 1000})
    assert "同步任务完成" in _text_of(logs)                  # mock 日志内容按分钟生成
    errors = await tools["search_log"].ainvoke(
        {"topic_id": "topic-001", "start_time": start_ms,
         "end_time": start_ms + 10 * 60 * 1000, "query": "level:ERROR"})
    assert "ERROR" in _text_of(errors) and "超时" in _text_of(errors)
    assert "INFO" not in _text_of(errors)
