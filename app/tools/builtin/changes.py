from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool
from pydantic import Field

from app.tools import registry
from app.tools.builtin.services import NOT_FOUND
from app.tools.service_catalog import owns_service


@tool
async def submit_change(
    service_id: Annotated[str, Field(description="要执行变更的服务名,以服务目录里的写法为准")],
    user_id: Annotated[str, InjectedToolArg],
    change_type: Annotated[str | None, Field(description="变更类型(可选,最终以前端固定类目下拉为准)")] = None,
) -> dict:
    """评估确认可以对目标服务执行变更后,调用本工具发起变更申请(重启/扩容/回滚等)。
    实际提交由前端变更表单确认后落库,本工具只表示『评估通过,已把提交入口交给用户』。
    发起人身份由系统注入,你不要传 user_id。"""
    # 写操作有二次确认门,但那道门确认的是「要不要变更」,不是「服务是否真实存在」,存在性得单独校验
    # (owns_service 现为存在性语义,接 CMDB 后升级回归属校验;必须 await,协程对象恒真值会静默放行)
    if not await owns_service(user_id, service_id):
        return dict(NOT_FOUND)
    return {"status": "待用户确认", "service_id": service_id}


registry.register(registry.spec_from_langchain_tool(
    submit_change, source="builtin", inject_user_id=True))
