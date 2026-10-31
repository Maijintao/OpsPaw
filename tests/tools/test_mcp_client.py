import logging

from app.tools import mcp_client, registry
from app.tools.registry import ToolSpec


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = f"{name} 描述"
        self.args_schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


async def test_fetch_mcp_specs_marks_source_and_permission(monkeypatch):
    async def fake_get_tools(*, server_name=None):
        return [_FakeTool("query_cpu_metrics")] if server_name == "monitor" else [_FakeTool("search_log")]
    monkeypatch.setattr(mcp_client, "_get_tools_of", fake_get_tools)
    specs = await mcp_client.fetch_mcp_specs()
    by = {s.name: s for s in specs}
    assert set(by) == {"query_cpu_metrics", "search_log"}
    assert by["query_cpu_metrics"].source == "mcp" and by["query_cpu_metrics"].mcp_server == "monitor"
    assert all(s.permission == "read" for s in specs)          # 我们侧规则:MCP 不在写清单→只读
    assert by["query_cpu_metrics"].format_result is mcp_client.FORMATTERS["query_cpu_metrics"]


async def test_one_server_down_degrades_gracefully(monkeypatch, caplog):
    async def fake_get_tools(*, server_name=None):
        if server_name == "monitor":
            raise ConnectionError("拒绝连接")
        return [_FakeTool("search_log")]
    monkeypatch.setattr(mcp_client, "_get_tools_of", fake_get_tools)
    with caplog.at_level(logging.WARNING):
        specs = await mcp_client.fetch_mcp_specs()
    assert {s.name for s in specs} == {"search_log"}           # 单台挂了跳过,不拖垮
    assert any("不可达" in r.message for r in caplog.records)


async def test_get_all_specs_merges_builtin_wins(monkeypatch):
    async def fake_fetch():
        return [ToolSpec(name="query_service", description="MCP 冒名",
                         json_schema={"type": "object", "properties": {}},
                         tool=_FakeTool("query_service"), permission="read", source="mcp", mcp_server="x"),
                ToolSpec(name="query_cpu_metrics", description="CPU 指标",
                         json_schema={"type": "object", "properties": {}},
                         tool=_FakeTool("query_cpu_metrics"), permission="read", source="mcp",
                         mcp_server="monitor")]
    monkeypatch.setattr(mcp_client, "fetch_mcp_specs", fake_fetch)
    specs = await registry.get_all_specs()
    by = {s.name: s for s in specs}
    assert by["query_service"].source == "builtin"               # 重名 builtin 优先,后到 MCP 丢弃
    assert by["query_cpu_metrics"].source == "mcp"               # 指标类查询已下线内置,由 MCP 接管
    assert {"query_faq", "create_ticket", "submit_change", "query_catalog"} <= set(by)


def test_logs_formatter_keeps_answer_fields():
    out = mcp_client.FORMATTERS["search_log"](
        {"topic_id": "topic-001", "start_time": 1, "end_time": 2, "query": None,
         "total": 2, "logs": [{"level": "INFO", "message": "正在同步元数据"}],
         "message": "成功查询 2 条日志", "took_ms": 12})
    assert out == {"total": 2, "logs": [{"level": "INFO", "message": "正在同步元数据"}],
                   "message": "成功查询 2 条日志"}            # 挑字段:took_ms/query 等过程字段剔除
