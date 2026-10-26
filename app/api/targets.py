"""平台与观测数据源配置管理。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import repository
router = APIRouter(prefix="/api/targets")
SOURCE_TYPES = ("prometheus", "jaeger", "loki", "mcp")

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
        if src.type not in SOURCE_TYPES:
            raise HTTPException(400, f"不支持的数据源类型:{src.type}(支持:{'、'.join(SOURCE_TYPES)})")
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

def _dump(row):
    return {"id": row.id, "name": row.name, "cluster": row.cluster,
            "sources": row.sources, "enabled": row.enabled}

@router.get("")
async def list_targets():
    return {"targets": [_dump(r) for r in await repository.list_targets()]}

@router.post("")
async def create_target(body: TargetIn):
    return _dump(await repository.create_target(body.name.strip(), _norm_sources(body.sources), body.cluster.strip()))

@router.put("/{target_id}")
async def update_target(target_id: int, body: TargetIn):
    row = await repository.update_target(target_id, body.name.strip(), _norm_sources(body.sources), body.cluster.strip())
    if row is None:
        raise HTTPException(404, "平台不存在")
    return _dump(row)

@router.delete("/{target_id}")
async def delete_target(target_id: int):
    if not await repository.delete_target(target_id):
        raise HTTPException(404, "平台不存在")
    return {"ok": True}
