"""ch08 MCP Client:MultiServerMCPClient 多 Server 接入,现问现拿(每次现拉工具清单,
adapters 每次调用新建 session——Server 侧加工具,值班系统不重启即可见)。
权限/格式化只认我们侧:Server 自报的用途描述仅供模型参考,能不能调按 registry.WRITE_TOOLS。

连接清单 = env 两台(mock 演示) ∪ targets 表启用的 mcp 数据源(真实接入,见 api/targets.py)。
真实接入的工具做命名空间化:t{id}_{原名}——两台 Server 都叫 query_cpu_metrics 时谁也不遮谁,
描述前缀服务名让模型知道工具属于哪个平台;格式化按原名查 FORMATTERS,与命名无关。
连接签名(URL 集合)变化时重建 client:后台接入/断开服务,Agent 下一轮对话即可见。"""
import asyncio
import logging
import re

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import settings
from app.tools import registry
from app.tools.registry import ToolSpec

logger = logging.getLogger(__name__)


def _fmt_cpu(d: dict) -> dict:
    """CPU 指标结果瘦身:丢过程字段,保留曲线/统计/告警三块回答要用的。"""
    return {"service_name": d.get("service_name"),
            "metric_name": d.get("metric_name"),
            "data_points": d.get("data_points"),
            "statistics": d.get("statistics"),
            "alert_info": d.get("alert_info")}


def _fmt_memory(d: dict) -> dict:
    return {"service_name": d.get("service_name"),
            "metric_name": d.get("metric_name"),
            "data_points": d.get("data_points"),
            "statistics": d.get("statistics"),
            "alert_info": d.get("alert_info")}


def _fmt_logs(d: dict) -> dict:
    """日志检索结果瘦身:丢 took_ms/query 元信息,保留日志条目与状态消息。"""
    if "error" in d:
        return {"error": d["error"], "message": d.get("message")}
    return {"total": d.get("total"), "logs": d.get("logs"), "message": d.get("message")}


# 结果格式化我们侧登记(挑回答用得上的字段 + 内部枚举码翻人话);未登记的 MCP 工具透传
FORMATTERS = {"query_cpu_metrics": _fmt_cpu, "query_memory_metrics": _fmt_memory,
              "search_log": _fmt_logs}

_client: MultiServerMCPClient | None = None
_client_sig: tuple | None = None   # 连接签名(URL 集合);变了才重建 client


async def _connections() -> tuple[dict, dict[str, dict]]:
    """返回 (MultiServerMCPClient 连接配置, 各 server 的命名空间元数据)。
    env 两台是演示底盘,targets 表的 mcp 数据源是真实接入;表读失败不连坐,
    退回纯 env(后台库故障不该把演示对话也打死)。"""
    conns: dict = {
        "monitor": {"transport": "streamable_http", "url": settings.mcp_monitor_url},
        "logs": {"transport": "streamable_http", "url": settings.mcp_logs_url},
    }
    meta: dict[str, dict] = {}
    from app.db import repository
    try:
        rows = await repository.list_targets()
    except Exception:  # noqa: BLE001
        logger.warning("targets 表不可读,本轮 Agent 只挂 env 的 MCP", exc_info=True)
        return conns, meta
    for row in rows:
        if not row.enabled:
            continue
        for i, src in enumerate(row.sources or []):
            if src.get("type") != "mcp":
                continue
            key = f"target{row.id}_{i}"
            conns[key] = {"transport": "streamable_http", "url": src["url"]}
            meta[key] = {"ns": f"t{row.id}", "label": row.name}
    return conns, meta


def _namespaced(t, ns: str, label: str):
    """接入服务的工具改名为 {ns}_{原名} 并在描述前缀服务名:原名是对方起的,
    两台 Server 撞名时按原逻辑会「先到者保留」,后接的服务就被静默吞掉——
    命名空间化后各归各;coroutine/args_schema 原样,调用仍打到原 Server。"""
    return t.model_copy(update={
        "name": f"{ns}_{t.name}",
        "description": f"【接入服务 {label}】{t.description or ''}",
    })


async def get_client() -> MultiServerMCPClient:
    """按连接签名缓存;签名变了(后台接入/断开服务)才重建。"""
    global _client, _client_sig
    conns, _ = await _connections()
    sig = tuple(sorted((k, v["url"]) for k, v in conns.items()))
    if _client is None or sig != _client_sig:
        # handle_tool_errors=False:工具错误抛 ToolException,由执行引擎统一分诊/回灌
        _client = MultiServerMCPClient(conns, handle_tool_errors=False)
        _client_sig = sig
        logger.info("MCP 连接清单更新:%s", sorted(conns))
    return _client


async def _get_tools_of(*, server_name: str):
    """薄壳:单测 monkeypatch 锚点。"""
    return await (await get_client()).get_tools(server_name=server_name)


async def fetch_mcp_specs() -> list[ToolSpec]:
    conns, meta = await _connections()
    specs: list[ToolSpec] = []
    for server in conns:
        try:
            # 我们侧超时封顶:连接拒绝会快速失败,但 Server 假死(TCP 接了不回话)只受 adapters
            # 默认超时保护——现问现拿每步都拉清单,最坏延迟必须封住
            tools = await asyncio.wait_for(_get_tools_of(server_name=server),
                                           timeout=settings.mcp_tool_timeout)
        except Exception as e:  # noqa: BLE001 单台不可达/假死:告警+跳过,不拖垮本轮对话
            logger.warning("MCP Server「%s」不可达,本轮跳过其工具:%s", server, type(e).__name__)
            continue
        m = meta.get(server)
        for t in tools:
            # 格式化按「原名」查表——改名后 FORMATTERS 的键就对不上了,必须在改名前取
            spec_fmt = FORMATTERS.get(t.name)
            if m:
                # 真实接入的 MCP:命名空间化,避免与演示端/彼此撞名被「先到者保留」静默吞掉
                t = _namespaced(t, m["ns"], m["label"])
            specs.append(registry.spec_from_langchain_tool(
                t, source="mcp", mcp_server=server, format_result=spec_fmt))
    return specs
