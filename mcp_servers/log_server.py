"""日志检索 MCP Server(仿腾讯云 CLS,mock 数据,不接真实日志系统、不建表)。
独立进程:uv run python mcp_servers/log_server.py
工具链:按服务名找日志主题(search_topic_by_service_name)→ 拿 topic_id → 按时间窗+过滤条件
检索日志(search_log);get_current_timestamp 辅助拼毫秒时间戳。
数据按 topic 动态生成:应用日志持续输出 INFO,便于演示「服务报错 → 查日志定位」的排障链路。
实装 mcp==1.28.1:FastMCP(name, host=, port=) + run(transport="streamable-http"),路径默认 /mcp。"""
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("log_mcp")

mcp = FastMCP("logs",
              host="127.0.0.1", port=int(os.environ.get("PORT", "8102")))

# mock 主题清单:服务名 → 日志主题。真实系统里这一步是按服务元数据查。
_MOCK_TOPICS = [
    {"topic_id": "topic-001", "topic_name": "数据同步服务日志",
     "service_name": "data-sync-service", "region_code": "ap-guangzhou",
     "description": "数据同步服务的应用日志,包含同步任务执行情况"},
    {"topic_id": "topic-002", "topic_name": "数据同步服务错误日志",
     "service_name": "data-sync-service", "region_code": "ap-guangzhou",
     "description": "数据同步服务的错误日志"},
    {"topic_id": "topic-003", "topic_name": "API网关服务日志",
     "service_name": "api-gateway-service", "region_code": "ap-shanghai",
     "description": "API 网关服务的访问日志"},
]


@mcp.tool()
def get_current_timestamp() -> int:
    """获取当前毫秒时间戳,用于构造 search_log 的 start_time/end_time。
    查最近 N 分钟:start_time = 当前时间戳 - N*60*1000,end_time = 当前时间戳。"""
    return int(datetime.now().timestamp() * 1000)


@mcp.tool()
def search_topic_by_service_name(
    service_name: Annotated[str, Field(description="服务名称,支持模糊匹配,例如 data-sync")],
    region_code: Annotated[Optional[str], Field(description="地域代码(可选),例如 ap-guangzhou")] = None,
) -> Dict[str, Any]:
    """按服务名检索日志主题(topic),支持模糊匹配。返回主题列表(含 topic_id),
    拿到 topic_id 后再用 search_log 查日志。查日志前必须先调本工具定位主题。"""
    matched = []
    for t in _MOCK_TOPICS:
        if region_code and t["region_code"] != region_code:
            continue
        sn = t["service_name"].lower()
        q = service_name.lower()
        if q in sn or sn in q:
            matched.append(dict(t, create_time="2026-07-01 10:00:00", log_count=0))
    return {
        "total": len(matched),
        "topics": matched,
        "message": f"找到 {len(matched)} 个匹配的日志主题" if matched
                   else f"未找到服务 '{service_name}' 的日志主题",
    }


@mcp.tool()
def search_log(
    topic_id: Annotated[str, Field(description="日志主题 ID,需先用 search_topic_by_service_name 查到,例如 topic-001")],
    start_time: Annotated[int, Field(description="开始毫秒时间戳,可用 get_current_timestamp 减去偏移计算")],
    end_time: Annotated[int, Field(description="结束毫秒时间戳,通常传当前时间戳")],
    query: Annotated[Optional[str], Field(description="过滤条件(可选),例如 level:ERROR 或 message:超时")] = None,
    limit: Annotated[int, Field(description="返回条数上限,默认 100")] = 100,
) -> Dict[str, Any]:
    """按主题+时间窗检索日志,可带过滤条件。返回日志列表(时间/级别/内容)。
    时间戳必须是毫秒整数;不知道当前时间就先调 get_current_timestamp。"""
    if topic_id not in {t["topic_id"] for t in _MOCK_TOPICS}:
        return {"topic_id": topic_id, "total": 0, "logs": [],
                "error": f"主题不存在: {topic_id}",
                "message": f"未找到主题 {topic_id},请先用 search_topic_by_service_name 确认"}
    logs = []
    current_ms = start_time
    max_by_time = int((end_time - start_time) / (60 * 1000)) + 1
    actual_limit = min(max(1, limit), 500, max_by_time)
    while current_ms <= end_time and len(logs) < actual_limit:
        dt = datetime.fromtimestamp(current_ms / 1000)
        # Stable synthetic events: ERROR every fifth minute, WARN every third,
        # otherwise INFO. This makes level/message filters useful in demos.
        minute = dt.minute
        level = "ERROR" if minute % 5 == 0 else "WARN" if minute % 3 == 0 else "INFO"
        if level == "ERROR":
            message = "数据库连接超时，重试次数达到上限"
        elif level == "WARN":
            message = "同步延迟升高，队列积压增加"
        else:
            message = "同步任务完成，处理记录 128 条"
        entry = {
            "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": message,
        }
        if query:
            level_match = re.search(r"(?:^|\s)level\s*:\s*(ERROR|WARN|INFO)(?:$|\s)", query, re.I)
            text_match = re.search(r"(?:^|\s)message\s*:\s*(.+)$", query, re.I)
            if level_match and level != level_match.group(1).upper():
                current_ms += 60 * 1000
                continue
            if text_match and text_match.group(1).strip().lower() not in message.lower():
                current_ms += 60 * 1000
                continue
            if not level_match and not text_match and query.lower() not in message.lower():
                current_ms += 60 * 1000
                continue
        logs.append(entry)
        current_ms += 60 * 1000
    return {"topic_id": topic_id, "start_time": start_time, "end_time": end_time,
            "query": query, "total": len(logs), "logs": logs,
            "message": f"成功查询 {len(logs)} 条日志"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
