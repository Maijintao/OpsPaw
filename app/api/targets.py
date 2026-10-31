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


# ---------- 实例指纹与地址自愈 ----------
# 场景:甲方线上出错后直接重建容器,docker 随机端口重新分配,targets 表里的 URL 失效,
# 数据面从此拉不到数据。自愈要解决的就是"跟着新地址走"。
#
# 为什么不能只靠协议探活认端口:协议指纹只能认出「这是一台 Prometheus」,认不出
# 「是不是我们原来那台」——认错了比拉不到更糟(诊断建立在别人的数据上)。
# 所以改写 URL 必须过三道闸,认不准就不动、如实报人工:
#   ① host 不变:只跟随端口漂移,host 变了(换机器/换域名)不自动跟;
#   ② 唯一应答者:该 host 上该协议只有一路探活通过,有多个候选就不猜;
#   ③ 实例指纹验身:候选的特征集合必须与接入时记录的"出生指纹"匹配。
# 指纹是角色级而非实例级(都是特征集合),单看不够硬,三道闸合起来才可靠——
# 这是有意取舍:宁可少自愈,不可认错源。

# 指纹采集器:type → 特征集合(容器重建、collector 重新上报后可恢复的稳定特征)。
# 返回 None = 采不到(实例刚起数据未齐),不覆盖旧指纹。
async def _fp_prometheus(url: str, headers: dict[str, str] | None = None) -> list[str] | None:
    """指标名集合(/api/v1/label/__name__/values)。不用 up——remote-write 形态下
    up 常为空,指标名才总是有的。"""
    try:
        r = await _get(url, "/api/v1/label/__name__/values", headers=headers)
        if r.status_code != 200:
            return None
        names = _json(r).get("data")
        return sorted(map(str, names)) if isinstance(names, list) and names else None
    except (httpx.HTTPError, ValueError):
        return None


async def _fp_jaeger(url: str, headers: dict[str, str] | None = None) -> list[str] | None:
    """服务名列表(口径与探活同:v1 → v3 → v3+base_path)。"""
    try:
        for path in JAEGER_SERVICE_PATHS:
            r = await _get(url, path, headers=headers)
            if r.status_code != 200:
                continue
            body = _json(r)
            names = body.get("data") if isinstance(body.get("data"), list) else body.get("services")
            if isinstance(names, list) and names:
                return sorted(map(str, names))
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def _fp_loki(url: str, headers: dict[str, str] | None = None) -> list[str] | None:
    """日志标签列表。"""
    try:
        r = await _get(url, "/loki/api/v1/labels", headers=headers)
        if r.status_code != 200:
            return None
        names = _json(r).get("data")
        return sorted(map(str, names)) if isinstance(names, list) and names else None
    except (httpx.HTTPError, ValueError):
        return None


async def _fp_mcp(url: str, headers: dict[str, str] | None = None) -> list[str] | None:
    """server 名 + 工具名清单(能力由对方声明,重建后声明不变)。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _collect() -> list[str]:
        async with streamablehttp_client(url, headers=headers or None) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                names = [getattr(getattr(init, "serverInfo", None), "name", None) or "mcp"]
                return names + sorted(t.name for t in tools.tools)

    try:
        names = await asyncio.wait_for(_collect(), PROBE_TIMEOUT * 2)
        return names or None
    except Exception:  # noqa: BLE001 同 _probe_mcp:传输层异常类型杂,采不到就当没有
        return None


FINGERPRINTS: dict[str, callable] = {
    "prometheus": _fp_prometheus,
    "jaeger": _fp_jaeger,
    "loki": _fp_loki,
    "mcp": _fp_mcp,
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _fp_match(recorded: list[str], fresh: list[str]) -> bool:
    """指纹匹配:相等,或任一方向为子集且交集非空。子集语义是特性不是漏洞:
    新 ⊆ 记录 = 刚重建、collector 数据还没收齐;记录 ⊆ 新 = 业务扩容。"""
    if not recorded:
        return True   # 从没采到过出生指纹:②的唯一应答者闸已兜底,退化为信任
    if not fresh:
        return False  # 候选采不到指纹:不认,等数据收齐的下轮再说
    rs, fs = set(recorded), set(fresh)
    return bool(rs & fs) and (fs <= rs or rs <= fs)


# 候选端口表(克制红线:对公网 host 做端口段扫描=云安全眼里的攻击行为,绝不段扫。
# 候选只来自三处:本表约定端口 / 同靶其他 source 已知端口 / docker API 精确映射,上限 12 个)
DEFAULT_CANDIDATE_PORTS: dict[str, tuple[int, ...]] = {
    "prometheus": (9090, 9091),
    "jaeger": (16686, 16685, 3200, 3201),
    "loki": (3100, 3101, 3110),
    "mcp": (8000, 8080, 9000, 3000),
}
# docker 容器内的约定端口:API 发现通道按"容器内端口映射到宿主"反查
CONTAINER_PORTS: dict[str, set[int]] = {
    "prometheus": {9090},
    "jaeger": {16686, 16685, 3200},
    "loki": {3100},
    "mcp": set(),  # mcp 端口无约定,只按容器名匹配
}


async def _docker_ports(host: str, stype: str) -> set[int]:
    """docker API 发现通道(有权限时最准,零扫描):/containers/json 拿端口映射,
    按容器内约定端口或容器名含类型关键字匹配宿主端口。
    docker host 取环境变量 OPSPAW_DOCKER_HOST(如 tcp://1.2.3.4:2375),缺省猜 host:2375;
    2375 没开是常态(不安全默认),静默返回空集退到候选端口表。"""
    if host in ("localhost", "127.0.0.1") or ":" in host:
        base = os.getenv("OPSPAW_DOCKER_HOST", "")
    else:
        base = os.getenv("OPSPAW_DOCKER_HOST") or f"tcp://{host}:2375"
    if not base or base.startswith("unix://"):
        return set()
    try:
        async with httpx.AsyncClient(timeout=2.5, trust_env=False) as c:
            r = await c.get(f"{base.replace('tcp://', 'http://')}/containers/json")
            if r.status_code != 200:
                return set()
            containers = r.json()
    except Exception:  # noqa: BLE001 没权限/不通是常态,发现通道失败不算事
        return set()
    want = CONTAINER_PORTS.get(stype, set())
    out: set[int] = set()
    for cont in containers or []:
        names = " ".join(cont.get("Names") or []).lower()
        for p in cont.get("Ports") or []:
            pub = p.get("PublicPort")
            if pub and (p.get("PrivatePort") in want or (stype in names)):
                out.add(int(pub))
    return out


async def _heal_source(src: dict, extra_ports: set[int] = frozenset()) -> tuple[dict | None, str]:
    """单路离线数据源的自愈尝试。返回 (新 source dict|None, 说明文本)。
    只生成候选、验身后返回,不落库——落库决定权在 maybe_heal_all。"""
    parsed = urlparse(src["url"])
    host, scheme, old_port = parsed.hostname, parsed.scheme, parsed.port
    if not host:
        return None, "URL 解析不出主机,不自动处理"
    cands: set[int] = set(DEFAULT_CANDIDATE_PORTS.get(src["type"], ())) | set(extra_ports)
    cands |= await _docker_ports(host, src["type"])
    cands.discard(old_port)
    ports = sorted(cands)[:12]
    if not ports:
        return None, "无候选端口可试"

    async def _try(port: int):
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        u = f"{scheme}://{netloc}"
        ok = (await PROBES[src["type"]](u, src.get("headers")))["online"]
        fp = await FINGERPRINTS[src["type"]](u, src.get("headers")) if ok else None
        return u, ok, fp

    results = await asyncio.gather(*[_try(p) for p in ports])
    alive = [(u, fp) for u, ok, fp in results if ok]
    if len(alive) != 1:
        return None, f"候选端口 {ports} 中 {len(alive)} 路应答,唯一性不满足,不自动改写"
    new_url, fp = alive[0]
    recorded = (src.get("fp") or {}).get("set") or []
    if not _fp_match(recorded, fp or []):
        return None, f"疑似新地址 {new_url},但指纹与记录不符,不自动改写(需人工确认)"
    new_src = {k: v for k, v in src.items() if k not in ("url", "fp")}
    new_src["url"] = new_url
    new_src["fp"] = {"set": fp, "at": _now(), "healed_from": src["url"]}
    return new_src, f"地址已跟随:{src['url']} -> {new_url}"


async def _capture_fp(sources: list[dict]) -> list[dict]:
    """给在线的 source 采一份指纹存进 source dict(接入/编辑时=出生证明,自愈比对的基准)。"""
    out = []
    for s in sources:
        fp = None
        try:
            h = await PROBES[s["type"]](s["url"], s.get("headers"))
            if h.get("online"):
                fp = await FINGERPRINTS[s["type"]](s["url"], s.get("headers"))
        except Exception:  # noqa: BLE001 指纹是尽力而为,采不到不影响接入
            fp = None
        out.append({**s, **({"fp": {"set": fp, "at": _now()}} if fp else {})})
    return out


_HEAL_LOCK = asyncio.Lock()
HEAL_INTERVAL = 60  # 秒。甲方重建容器到自愈跟随的窗口,一分钟内数据面恢复


async def maybe_heal_all() -> list[str]:
    """自愈一轮:所有启用靶子探活 → 在线路顺带刷新指纹(防业务变动导致指纹过期失配)→
    离线路尝试自愈 → 有改写才落库。返回动作日志。全局锁防重入(与周期任务/手动触发并发)。"""
    if _HEAL_LOCK.locked():
        return []
    async with _HEAL_LOCK:
        notes: list[str] = []
        for row in await repository.list_targets():
            if not row.enabled:
                continue
            health = await probe_target(row)
            sources, changed = list(row.sources or []), False
            for i, (s, h) in enumerate(zip(sources, health)):
                if h.get("online"):
                    fp = await FINGERPRINTS[s["type"]](s["url"], s.get("headers"))
                    if fp and fp != (s.get("fp") or {}).get("set"):
                        sources[i] = {**s, "fp": {"set": fp, "at": _now()}}
                        changed = True
                        notes.append(f"[{row.name}] {s['type']} 指纹已刷新")
                else:
                    # 同靶其他 source 的宿主端口也进候选池(同宿主机漂移常成对出现)
                    extra = {u.port for u in map(urlparse, [x["url"] for x in sources]) if u.port}
                    new_s, note = await _heal_source(s, extra)
                    notes.append(f"[{row.name}] {s['type']} 离线:{note}")
                    if new_s:
                        sources[i] = new_s
                        changed = True
            if changed:
                await repository.update_target_sources(row.id, sources)
        return notes


async def heal_loop(interval: int = HEAL_INTERVAL) -> None:
    """后台自愈循环:lifespan 启动,应用关闭时取消。"""
    while True:
        await asyncio.sleep(interval)
        try:
            for note in await maybe_heal_all():
                logger.info("数据源自愈:%s", note)
        except Exception:  # noqa: BLE001 循环不许死,任何异常只记日志
            logger.warning("自愈轮异常", exc_info=True)


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
    from app.repair import gateway

    rows = await repository.list_targets()
    if fresh:
        _PROBE_CACHE.clear()
    # 探活并发跑:多个平台时不再一个等一个(缓存命中时这里几乎不耗时)
    healths = await asyncio.gather(*[_health_of(r) for r in rows]) if rows else []
    out = []
    for row, health in zip(rows, healths):
        conns = [c for c in await repository.list_repair_connectors(row.id)
                 if c.status != "revoked"]
        repair = {"total": len(conns),
                  "online": sum(1 for c in conns if gateway.is_online(c.id)),
                  "names": [c.name for c in conns][:5]}
        out.append(_dump(row, health, repair))
    return out


@router.get("")
async def list_targets(fresh: bool = False) -> dict:
    """fresh=1 强制重新探活(后台「刷新」按钮用);默认吃十几秒内的缓存,点了就出。"""
    return {"targets": await _dump_all(fresh)}


@router.post("")
async def create_target(body: TargetIn) -> dict:
    # 接入即采出生指纹(自愈验身的基准);探活两次(采指纹+出报告)是后台操作,可接受
    row = await repository.create_target(body.name, await _capture_fp(_norm_sources(body.sources)),
                                        (body.cluster or "").strip())
    health = await probe_target(row)
    _PROBE_CACHE[row.id] = (time.time(), health)   # 刚探过,顺手热上缓存
    return _dump(row, health)


@router.put("/{target_id}")
async def update_target(target_id: int, body: TargetIn) -> dict:
    row = await repository.update_target(
        target_id, body.name, await _capture_fp(_norm_sources(body.sources)),
        (body.cluster or "").strip())
    if row is None:
        raise HTTPException(404, "靶子不存在")
    _forget_health(target_id)                      # 数据源可能改了,旧探活结果作废
    health = await probe_target(row)
    _PROBE_CACHE[row.id] = (time.time(), health)
    return _dump(row, health)


@router.delete("/{target_id}")
async def delete_target(target_id: int) -> dict:
    from app.repair import gateway
    for conn in await repository.list_repair_connectors(target_id):
        gateway.unregister(conn.id)
    await repository.delete_repair_connectors_of_target(target_id)
    if not await repository.delete_target(target_id):
        raise HTTPException(404, "靶子不存在")
    _forget_health(target_id)
    return {"ok": True, "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
