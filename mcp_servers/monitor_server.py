"""监控指标 MCP Server(mock 数据,不接真实监控、不建表)。
独立进程:uv run python mcp_servers/monitor_server.py
提供 CPU / 内存使用率时序查询,数据按服务名+时间窗动态生成(CPU 呈爬坡曲线、内存线性上涨,
便于演示「指标异常 → 定位服务」的排障链路)。
实装 mcp==1.28.1:FastMCP(name, host=, port=) + run(transport="streamable-http"),路径默认 /mcp。"""
import logging
import random
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("monitor_mcp")

mcp = FastMCP("monitor",
              host="127.0.0.1", port=int(__import__("os").environ.get("PORT", "8101")))


def _parse_time(time_str: Optional[str], default_offset_hours: int) -> datetime:
    """解析 YYYY-MM-DD HH:MM:SS;缺省时返回当前时间+偏移。"""
    if time_str:
        try:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return datetime.now() + timedelta(hours=default_offset_hours)


def _metrics(service_name: str, start_time: Optional[str], end_time: Optional[str],
             interval: str, metric: str) -> Dict[str, Any]:
    start_dt = _parse_time(start_time, default_offset_hours=-1)
    end_dt = _parse_time(end_time, default_offset_hours=0)
    interval_minutes = {"1m": 1, "5m": 5, "1h": 60}.get(interval, 1)

    # Seed each point by its absolute timestamp so overlapping queries agree, while
    # moving the time window returns a different (but reproducible) series.
    data_points = []
    current = start_dt
    base = 10.0 if metric == "cpu" else 30.0
    cap = 96.0 if metric == "cpu" else 85.0
    total_gb = 8.0

    while current <= end_dt and len(data_points) < 500:
        rng = random.Random(f"{metric}:{service_name}:{current.strftime('%Y-%m-%d %H:%M')}")
        # A repeatable 30-minute incident window each hour for the demo service.
        # The default one-hour query therefore normally includes both baseline
        # and a rising segment, regardless of when the demo is run.
        incident = service_name == "data-sync-service" and 20 <= current.minute < 50
        if incident:
            progress = current.minute - 20
            value = min(base + progress * (2.8 if metric == "cpu" else 1.7), cap)
        else:
            value = base + (2.0 if metric == "cpu" else 3.0) * rng.random()
        value = round(value + rng.uniform(-2, 2), 1)
        value = max(0, min(100, value))
        point: Dict[str, Any] = {"timestamp": current.strftime("%H:%M"), "value": value}
        if metric == "memory":
            point["used_gb"] = round(value / 100.0 * total_gb, 2)
            point["total_gb"] = total_gb
        data_points.append(point)
        current += timedelta(minutes=interval_minutes)

    if not data_points:
        return {"service_name": service_name, "metric_name": f"{metric}_usage_percent",
                "interval": interval, "data_points": [], "statistics": {}}

    values = [p["value"] for p in data_points]
    avg = round(sum(values) / len(values), 2)
    vmax = max(values)
    threshold = 80.0 if metric == "cpu" else 70.0
    breached = vmax > threshold
    return {
        "service_name": service_name,
        "metric_name": f"{metric}_usage_percent",
        "interval": interval,
        "data_points": data_points,
        "statistics": {
            "avg": avg, "max": vmax, "min": min(values),
            "p95": round(sorted(values)[int(len(values) * 0.95)] if len(values) > 1 else vmax, 2),
        },
        "alert_info": {
            "triggered": breached,
            "threshold": threshold,
            "message": (f"{'CPU' if metric == 'cpu' else '内存'} 使用率持续超过 {threshold:.0f}% 阈值"
                        if breached else f"{'CPU' if metric == 'cpu' else '内存'} 使用率正常"),
        },
    }


@mcp.tool()
def query_cpu_metrics(
    service_name: Annotated[str, Field(description="服务名称,例如 data-sync-service")],
    start_time: Annotated[Optional[str], Field(description="开始时间 YYYY-MM-DD HH:MM:SS,缺省为 1 小时前")] = None,
    end_time: Annotated[Optional[str], Field(description="结束时间 YYYY-MM-DD HH:MM:SS,缺省为当前时间")] = None,
    interval: Annotated[str, Field(description="聚合间隔:1m/5m/1h,默认 1m")] = "1m",
) -> Dict[str, Any]:
    """查询服务的 CPU 使用率时序数据与统计(avg/max/p95),超阈值自动附带告警信息。
    用于排查服务 CPU 飙升、确认容量水位。返回值只含回答所需字段,过程字段不要引用。"""
    return _metrics(service_name, start_time, end_time, interval, "cpu")


@mcp.tool()
def query_memory_metrics(
    service_name: Annotated[str, Field(description="服务名称,例如 data-sync-service")],
    start_time: Annotated[Optional[str], Field(description="开始时间 YYYY-MM-DD HH:MM:SS,缺省为 1 小时前")] = None,
    end_time: Annotated[Optional[str], Field(description="结束时间 YYYY-MM-DD HH:MM:SS,缺省为当前时间")] = None,
    interval: Annotated[str, Field(description="聚合间隔:1m/5m/1h,默认 1m")] = "1m",
) -> Dict[str, Any]:
    """查询服务的内存使用率时序数据与统计(avg/max/p95),含已用/总内存,超阈值自动附带告警信息。
    用于排查内存泄漏、确认内存压力。"""
    return _metrics(service_name, start_time, end_time, interval, "memory")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
