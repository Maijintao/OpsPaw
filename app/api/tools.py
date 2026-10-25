"""Agent 工具清单 API:get_all_specs() 现拉(内置 ∪ MCP),与执行引擎同源。

单独成页的原因:工具面是 Agent 能力的实体——接了什么数据源、MCP server 起没起,
都会第一时间反映在这份清单上,值得一个不被后台首页卡片挤压的完整视图。
工具的 description 是写给模型看的「什么时候用」;MCP server 不可达时其工具如实缺席,
不凑数——这张页面展示的是「现在真的有什么」,不是「应该有什么」。
"""
from fastapi import APIRouter

from app.tools import registry

router = APIRouter(prefix="/api/tools")


@router.get("")
async def list_tools() -> dict:
    specs = await registry.get_all_specs()
    tools = []
    for s in specs:
        params = []
        for name, sch in (s.json_schema.get("properties") or {}).items():
            params.append({
                "name": name,
                "type": sch.get("type") or "any",
                "description": sch.get("description") or "",
                "required": name in (s.json_schema.get("required") or []),
            })
        tools.append({
            "name": s.name,
            "description": s.description,
            "source": s.source,
            "mcp_server": s.mcp_server,
            "permission": s.permission,
            "timeout": s.timeout,
            "params": params,
        })
    return {"tools": tools}
