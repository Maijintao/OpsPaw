from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool
from pydantic import Field

from app.tools import registry
from app.tools.service_catalog import discover_services, owns_service, service_snapshot

# 查不到和不可达回同一句话。分开回就成了枚举 oracle:攻击者靠回答差异
# 就能挨个试出哪些服务真实存在。
NOT_FOUND = {"error": "没有在接入平台中找到这个服务", "code": "service_not_found"}


@tool
async def query_service(
    service_name: Annotated[str, Field(description="服务名,以服务目录里查到的写法为准,例如 frontend、cartservice")],
    user_id: Annotated[str, InjectedToolArg],
) -> dict:
    """查询已接入平台中某个真实服务的运行快照:运行状态及判定依据、CPU/内存水位、
    近 1 小时链路抽样与错误数。用于用户询问某个服务运行状况时。
    服务名先用 query_catalog 对准,查无此名会如实报错,不要编造快照数据。
    发起人身份由系统注入,你不要传 user_id。"""
    if not await owns_service(user_id, service_name):
        return dict(NOT_FOUND)
    return await service_snapshot(service_name)


@tool
async def query_catalog(
    keyword: Annotated[str, Field(description="服务名或关键词,例如 frontend、cart、网关、otel")],
) -> dict:
    """查询服务目录:列出后台「服务检测」接入的平台中真实发现的服务(服务名+所属平台)。
    用于把用户口中的服务对准到标准服务名、或查看接入了哪些平台和服务时。
    记录带 cluster 字段的属于多云集群:同一集群下的多个平台是同一套系统部署在不同云,
    同名服务是不同实例,查询和变更都要指明是哪个平台,别当成一个。
    **服务身份是「平台/服务名」**:同名服务可能在多个平台上都有(那条记录 ambiguous=true),
    这种时候必须用「平台/服务名」全名去查快照或提变更,裸名会被拒绝——绝不靠"先发现的平台胜出"
    去猜,改错平台是真事故。没匹配到关键词时返回全量目录;平台不可达会如实报错,不要编造清单。"""
    services, err = await discover_services()
    if err:
        return {"error": err, "code": "discovery_down"}
    kw = keyword.lower().strip()
    matched = [s for s in services
               if kw in s["name"].lower() or kw in s["name"].lower().replace("-", "")
               or kw in str(s.get("target", "")).lower()]
    if not matched:          # 没匹配就亮全量目录,让模型自己认(真实服务清单规模有限,全给不超载)
        matched = services
    for s in matched:
        s.pop("status", None)   # 目录只管名字对准,状态让 query_service 现查,避免过期
    return {"keyword": keyword, "matched": len(matched), "services": matched}


registry.register(registry.spec_from_langchain_tool(
    query_service, source="builtin", inject_user_id=True))
registry.register(registry.spec_from_langchain_tool(query_catalog, source="builtin"))
