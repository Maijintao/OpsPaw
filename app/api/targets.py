"""检测服务(targets)接入 API:外接观测数据源的注册、编辑、断开与接入校验。

命名:表名与路由沿用 targets(Prometheus 语境里 scrape target 本就是"被采集对象"的标准叫法),
产品文案统一叫「服务检测/接入服务」,与值班业务里的"服务"(query_service 那套 demo 服务)区分。

设计口径(回应「接新平台要改源码吗」「是不是空壳」):
  - 接「另一路同类数据源」(再来一个 Prometheus、再一个 Jaeger)纯页面操作,零改码——
    类型、地址都是本表数据,探活与后续查询都按 type 分发。
  - 接「一类新系统」(如 Loki、SkyWalking)才需要在 PROBES 加一个探活+查询适配器——
    适配器模式的标准代价:新协议总得有人写第一个适配器,但只写一处。
  - 接入不是丢个网址:校验分两层,①就绪探针 ②**真发一条查询验证通路**——
    Prometheus 真执行最小 PromQL(up),Jaeger 真解析 /api/services 的 JSON 数据体。
    校验通过 = 这条连接"能查",不是"端口开着"。Agent 侧查询工具将来打的就是这同一组端点,
    所以校验通过即可信。

接入形态分两类:
  - 标准观测 API(prometheus/jaeger):直连 + 模板化查询,凭据持有、查询生成(不进模型)、
    按服务隔离都在我们侧收口;
  - MCP(通用兜底):对方把内部能力包成 MCP server 时,我们以客户端接入,健康校验=真握手,
    工具清单透传给 Agent,能力由对方声明、我们如实接——这是「小企业只给一个 MCP」场景的答案。

本路由只管「配置存在且通不通」,不管 Agent 侧消费——Agent 连接清单由 mcp_client
按 env(mock 演示)∪ 本表(真实)合并,那是接线步骤的事。两件事解耦:
配置页坏了不影响 Agent,Agent 没接上也不影响这里的探活。
"""
import asyncio
import logging
import os
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import repository

logger = logging.getLogger("app.targets")

router = APIRouter(prefix="/api/targets")

PROBE_TIMEOUT = 2.5  # 秒。探活卡后端不值得,超时就如实报离线


class SourceIn(BaseModel):
    type: str
    url: str = Field(min_length=1, max_length=512)
    # 可选补充口:鉴权/自定义请求头(云托管端点的 token、内网网关的凭据都从这里进)
    headers: dict[str, str] = Field(default_factory=dict)


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # 多云集群名:同一系统部署在多个云时几张卡共用一个名字;单云留空
    cluster: str = Field(default="", max_length=64)
    sources: list[SourceIn] = Field(default_factory=list, max_length=16)


async def _get(url: str, path: str, params: dict | None = None,
               headers: dict[str, str] | None = None):
    """直连 GET。返回 resp;网络错误抛 httpx.HTTPError。trust_env=False:不吃环境代理
    变量——本机校验走 Clash 会被代理截胡,把「连不上」误报成 502,探活必须直连。"""
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, trust_env=False) as client:
        return await client.get(f"{url}{path}", params=params, headers=headers)


def _json(resp) -> dict:
    """解析 JSON 体;非 JSON 抛 ValueError,由调用方转成可读失败原因。"""
    return resp.json()


async def _probe_prometheus(url: str, headers: dict[str, str] | None = None) -> dict:
    """两层校验:①/-/ready 就绪(纯文本,只看状态码);②真发一条最小 PromQL(up)
    并解析 JSON,验证查询通路。就绪≠可查——查询端点被反代剥掉、版本不兼容,
    只有真查一次才知道。"""
    try:
        resp = await _get(url, "/-/ready", headers=headers)
        if resp.status_code != 200:
            return {"online": False, "detail": f"就绪探针 HTTP {resp.status_code}"}
        resp = await _get(url, "/api/v1/query", params={"query": "up"}, headers=headers)
        body = _json(resp) if resp.status_code == 200 else {}
        if body.get("status") == "success":
            n = len((body.get("data") or {}).get("result") or [])
            return {"online": True, "detail": f"可查(up 返回 {n} 条序列)"}
        return {"online": False, "detail": f"查询接口异常 HTTP {resp.status_code}"}
    except httpx.TimeoutException:
        return {"online": False, "detail": f"超时({PROBE_TIMEOUT}s)"}
    except httpx.HTTPError as e:
        return {"online": False, "detail": type(e).__name__}
    except ValueError:
        return {"online": False, "detail": "响应不是 JSON(地址对?)"}


# Jaeger 口径探测路径(按序尝试,第一个通的算数):
#   v1        /api/services                — Jaeger v1 / v2 旧版(2025-10 前的通用部署)
#   v3        /api/v3/services             — Jaeger v2.19+(官方删除了 v1 搜索接口,只剩 v3 OTLP JSON)
#   v3+前缀   /jaeger/ui/api/v3/services   — otel-demo 的 query 配置了 base_path=/jaeger/ui
JAEGER_SERVICE_PATHS = ("/api/services", "/api/v3/services", "/jaeger/ui/api/v3/services")


async def _probe_jaeger(url: str, headers: dict[str, str] | None = None) -> dict:
    """真解析服务列表的 JSON 数据体——200 但解析不出服务列表也算不可查。
    口径按 JAEGER_SERVICE_PATHS 顺序自动适配:v1 响应 {"data":[...]}、v3 响应
    {"services":[...]}。探活通过用的路径与 Agent 查询工具是同一组,校验通过即可信。"""
    try:
        resp = None
        for path in JAEGER_SERVICE_PATHS:
            resp = await _get(url, path, headers=headers)
            if resp.status_code != 200:
                continue
            body = _json(resp)
            names = body.get("data") if isinstance(body.get("data"), list) else body.get("services")
            if isinstance(names, list):
                api = "v1" if path == "/api/services" else "v3"
                return {"online": True, "detail": f"可查({api} 口径,发现 {len(names)} 个服务)"}
        return {"online": False, "detail": f"查询接口异常 HTTP {getattr(resp, 'status_code', '?')}"
                                          "(v1/v3 服务列表端点都不通,确认地址与版本)"}
    except httpx.TimeoutException:
        return {"online": False, "detail": f"超时({PROBE_TIMEOUT}s)"}
    except httpx.HTTPError as e:
        return {"online": False, "detail": type(e).__name__}
    except ValueError:
        return {"online": False, "detail": "响应不是 JSON(地址对?)"}


async def _probe_loki(url: str, headers: dict[str, str] | None = None) -> dict:
    """两层校验(对齐 prometheus 口径):①/ready 就绪(纯文本);②真查一次标签列表并解析
    JSON,验证查询通路。Loki 兼容 Grafana 系(Cloud CLS 有自家 API 不在此列)。"""
    try:
        resp = await _get(url, "/ready", headers=headers)
        if resp.status_code != 200:
            return {"online": False, "detail": f"就绪探针 HTTP {resp.status_code}"}
        resp = await _get(url, "/loki/api/v1/labels", headers=headers)
        body = _json(resp) if resp.status_code == 200 else {}
        names = body.get("data") if isinstance(body.get("data"), list) else None
        if body.get("status") == "success" and names is not None:
            return {"online": True, "detail": f"可查(发现 {len(names)} 个日志标签)"}
        return {"online": False, "detail": f"查询接口异常 HTTP {resp.status_code}"}
    except httpx.TimeoutException:
        return {"online": False, "detail": f"超时({PROBE_TIMEOUT}s)"}
    except httpx.HTTPError as e:
        return {"online": False, "detail": type(e).__name__}
    except ValueError:
        return {"online": False, "detail": "响应不是 JSON(地址对?)"}


async def _probe_mcp(url: str, headers: dict[str, str] | None = None) -> dict:
    """MCP 数据源(通用兜底):对方没暴露标准观测 API 时,只要把能力包成 MCP 就接得住。
    校验=真握手:initialize + tools/list,工具即能力——发现到几个工具,Agent 接线后就
    有几个工具。Streamable HTTP 形态(url 指到 /mcp 这类端点)。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _handshake():
        async with streamablehttp_client(url, headers=headers or None) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                return init, tools

    try:
        init, tools = await asyncio.wait_for(_handshake(), PROBE_TIMEOUT * 2)
        name = getattr(getattr(init, "serverInfo", None), "name", None) or "mcp"
        return {"online": True, "detail": f"可查({len(tools.tools)} 个工具,server={name})"}
    except TimeoutError:  # 3.11+ 内建 TimeoutError(asyncio.wait_for 抛的就是它)
        return {"online": False, "detail": f"超时({PROBE_TIMEOUT * 2}s)"}
    except Exception as e:  # noqa: BLE001 MCP 传输层异常类型杂,且经 anyio 任务组会包成 ExceptionGroup
        while isinstance(e, BaseExceptionGroup):   # 剥开包装,报最里层真实原因
            e = e.exceptions[0]
        return {"online": False, "detail": type(e).__name__}


# 数据源类型注册表:type → 探活+校验器。新类型在这里加一行,前端下拉与校验自动跟上。
# prometheus/jaeger/loki=标准观测 API,模板化查询; mcp=通用兜底,对方把能力包成 MCP 就接得住,
# 工具清单透传给 Agent(能力由对方声明,我们如实接)。
PROBES: dict[str, callable] = {
    "prometheus": _probe_prometheus,
    "jaeger": _probe_jaeger,
    "loki": _probe_loki,
    "mcp": _probe_mcp,
}


def _norm_url(url: str) -> str:
    """去首尾空白与尾部斜杠;没写 scheme 补 http://(别让低级格式把人拦在门外)。"""
    url = url.strip().rstrip("/")
    return url if "://" in url else f"http://{url}"


def _jaeger_base(url: str) -> str:
    """Jaeger 地址只留基础部分。用户极易把完整接口(/api/traces、/jaeger/ui/api/v3/...)
    整段粘进 URL 栏,而探活与查询都会在其后拼路径,叠层必 404——在写入时就截到 /api/ 之前。"""
    i = url.find("/api/", url.find("://") + 3)
    return url[:i] if i != -1 else url


def _norm_sources(raw: list[SourceIn]) -> list[dict]:
    out, seen = [], set()
    for src in raw:
        if src.type not in PROBES:
            raise HTTPException(400, f"不支持的数据源类型:{src.type}(支持:{'、'.join(PROBES)})")
        url = _norm_url(src.url)
        if src.type == "jaeger":
            url = _jaeger_base(url)
        hdrs = {str(k).strip(): str(v) for k, v in src.headers.items()
                if str(k).strip() and str(v).strip()}
        if len(hdrs) > 8:
            raise HTTPException(400, "请求头最多 8 条,别把配置当仓库")
        key = (src.type, url)
        if key in seen:  # 同类型同地址去重,重复配置没有意义
            continue
        seen.add(key)
        out.append({"type": src.type, "url": url, **({"headers": hdrs} if hdrs else {})})
    if not out:
        raise HTTPException(400, "至少要配一路观测数据源,不然接进来干什么")
    return out


async def probe_target(row) -> list[dict]:
    """一个检测服务的完整校验:各路并发,互不拖累(某一侧不通不让另一侧多等一个超时)。"""
    tasks = [PROBES[src["type"]](src["url"], src.get("headers")) for src in row.sources]
    results = await asyncio.gather(*tasks) if tasks else []
    return [{**src, **result} for src, result in zip(row.sources, results)]


# ---------- 探活结果短缓存 ----------
# 每次打开/操作后台都真探活一遍:经隧道到靶机单次往返 0.5~1 秒,三路加起来 2 秒多,
# 表现为"点任何按钮都要顿一下"。健康状态本来就是分钟级的信息,十几秒的陈旧不影响判断,
# 所以这里按平台缓存:页面操作立刻返回,点「刷新」传 fresh=1 强制重探。
PROBE_TTL_S = 15.0
_PROBE_CACHE: dict[int, tuple[float, list[dict]]] = {}


async def _health_of(row) -> list[dict]:
    hit = _PROBE_CACHE.get(row.id)
    if hit and time.time() - hit[0] < PROBE_TTL_S:
        return hit[1]
    health = await probe_target(row)
    _PROBE_CACHE[row.id] = (time.time(), health)
    return health


def _forget_health(target_id: int) -> None:
    _PROBE_CACHE.pop(target_id, None)


def _dump(row, health: list[dict], repair: dict | None = None) -> dict:
    online = [h["online"] for h in health]
    # 状态判定与后台首页四档口径一致:全在线 ok;有在线有离线 attention;
    # 配了但全离线 error;一路都没配 attention(还不会探活,先催配置)
    status = ("attention" if not health else "ok" if all(online)
              else "attention" if any(online) else "error")
    return {
        "id": row.id, "name": row.name, "cluster": getattr(row, "cluster", "") or "",
        "sources": row.sources,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "health": health, "status": status,
        "repair": repair or {"total": 0, "online": 0, "names": []},
    }


async def _dump_all(fresh: bool = False) -> list[dict]:
    rows = await repository.list_targets()
    if fresh:
        _PROBE_CACHE.clear()
    healths = await asyncio.gather(*[_health_of(r) for r in rows]) if rows else []
    return [_dump(row, health) for row, health in zip(rows, healths)]


@router.get("")
async def list_targets(fresh: bool = False) -> dict:
    """fresh=1 强制重新探活(后台「刷新」按钮用);默认吃十几秒内的缓存,点了就出。"""
    return {"targets": await _dump_all(fresh)}

@router.post("")
async def create_target(body: TargetIn) -> dict:
    # 接入即采出生指纹(自愈验身的基准);探活两次(采指纹+出报告)是后台操作,可接受
    row = await repository.create_target(body.name, _norm_sources(body.sources),
                                        (body.cluster or "").strip())
    health = await probe_target(row)
    _PROBE_CACHE[row.id] = (time.time(), health)   # 刚探过,顺手热上缓存
    return _dump(row, health)

@router.put("/{target_id}")
async def update_target(target_id: int, body: TargetIn) -> dict:
    row = await repository.update_target(
        target_id, body.name, _norm_sources(body.sources),
        (body.cluster or "").strip())
    if row is None:
        raise HTTPException(404, "靶子不存在")
    _forget_health(target_id)                      # 数据源可能改了,旧探活结果作废
    health = await probe_target(row)
    _PROBE_CACHE[row.id] = (time.time(), health)
    return _dump(row, health)

@router.delete("/{target_id}")
async def delete_target(target_id: int) -> dict:
    if not await repository.delete_target(target_id):
        raise HTTPException(404, "平台不存在")
    _forget_health(target_id)
    return {"ok": True, "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
