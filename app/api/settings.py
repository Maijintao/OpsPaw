"""后台「API 设置」:运行时改三组上游配置(chat/embed/rerank),真测通过才落库,
保存即热生效(不改 .env、不重启)。

嵌入配置的严肃边界(不能互转问题的处理):Milvus 里的向量是某个嵌入配置产出的,
换 base_url/model 向量空间就不通用——不做任何「向量转换」,唯一的正确路径是
全量重嵌。防线有三道:
1. 维度闸:校验时实测嵌入维度,≠ 集合固定 1024 维的模型直接拒绝保存(重嵌也插不进去);
2. 指纹闸:base_url+model 的指纹持久化在 app_settings,与产出向量时的指纹不一致时
   拒绝悄悄保存,必须显式确认「drop 集合 + 全量重嵌 N 块」;
3. 重建期降级:重嵌走 drop → 全部置 pending → vectorize_pending(include_done=True)
   (幂等可重跑),期间集合为空,dense 检索自然检空、hybrid 只剩 BM25——宁可检索弱,
   不可拿旧模型的向量配新模型的查询算出一堆假相似度。"""
import asyncio
import logging
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.core import embeddings
from app.db import repository
from app.kb import milvus_client
from app.kb.dualwrite import vectorize_pending

logger = logging.getLogger(__name__)
router = APIRouter()

FP_KEY = "embed_fingerprint"
# 允许页面改的九个键:三组各 base_url/model/api_key。意图/摘要模型回落 chat_model,跟着走
# (页面不单列,减少一半配置面;真要分开是后话)。
KEYS = ("chat_base_url", "chat_model", "chat_api_key",
        "embed_base_url", "embed_model", "embed_api_key",
        "rerank_base_url", "rerank_model", "rerank_api_key")

_PROBE_TIMEOUT = 12.0


def _mask(key: str | None) -> str:
    if not key:
        return ""
    return key[:4] + "****" + key[-4:] if len(key) > 8 else "****"


def _client(trust_env: bool = False) -> httpx.AsyncClient:
    # trust_env=False:直连,防本机代理截胡 localhost
    return httpx.AsyncClient(timeout=_PROBE_TIMEOUT, trust_env=False)


def _norm_base(url: str) -> str:
    """规整 base_url 并对少版本段的补 /v1:OpenAI 兼容端点的路径都挂在 /v1 下,
    「上游地址」按字面理解很容易只填到域名——少了版本段会打成 /chat/completions,
    上游回 404,而且只在保存校验或第一次调用时冒出来,排障要绕一圈(rerank 侧同规)。"""
    url = (url or "").strip().rstrip("/")
    if url and not re.search(r"/v\d+$", url):
        url += "/v1"
    return url


async def _check_chat(base: str, key: str, model: str) -> str:
    """真发一条 1-token 补全。返回确认文本(带模型名),失败抛 HTTPException。"""
    try:
        async with _client() as c:
            r = await c.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                                   "max_tokens": 1, "stream": False})
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(400, f"对话上游连通失败:{type(e).__name__}: {e}")
    if not (body.get("choices") or []):
        raise HTTPException(400, "对话上游响应里没有 choices,不是可用的 OpenAI 兼容端点")
    return f"对话模型 {model} 可用"


async def _check_embed(base: str, key: str, model: str) -> str:
    """真嵌一条,顺手量维度:集合 dense 固定 1024 维(COSINE),维度不符的模型
    即使全量重嵌也插不进去——这是「互转」问题的硬边界,当场拒绝。"""
    try:
        async with _client() as c:
            r = await c.post(f"{base}/embeddings", headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "input": ["ping"]})
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(400, f"嵌入上游连通失败:{type(e).__name__}: {e}")
    data = body.get("data") or []
    if not data or not (data[0].get("embedding") or []):
        raise HTTPException(400, "嵌入上游响应里没有向量,不是可用的 OpenAI 兼容端点")
    dim = len(data[0]["embedding"])
    if dim != milvus_client.DIM:
        raise HTTPException(400, f"嵌入模型 {model} 输出 {dim} 维,知识库集合固定 {milvus_client.DIM} 维,"
                                 "维度不同的模型不能共用这套向量,请换 1024 维模型(如 BAAI/bge-m3)")
    return f"嵌入模型 {model} 可用(实测 {dim} 维,与集合匹配)"


async def _check_rerank(base: str, key: str, model: str) -> str:
    """真打一对 rerank。地址拼法与 app/core/rerank._rerank_url 同规:少版本段补 /v1。"""
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    try:
        async with _client() as c:
            r = await c.post(f"{base}/rerank", headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "query": "重启服务",
                                   "documents": ["如何重启服务", "今天天气不错"]})
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(400, f"重排上游连通失败:{type(e).__name__}: {e}")
    if "results" not in body:
        raise HTTPException(400, "重排上游响应里没有 results,不是 Jina/Cohere 形状的端点")
    return f"重排模型 {model} 可用"


def _effective(key: str) -> str:
    """当前生效值:进程内 settings(env + 历次热覆盖的合并结果),直接读它。"""
    return getattr(settings, key, "") or ""


@router.get("/api/settings")
async def get_settings() -> dict:
    """回显当前生效配置(key 掩码)。首次访问把当前 env 配置的指纹落库,作为向量产出的基线——
    从这一刻起,「Milvus 里的向量是谁产的」有了明确记录。"""
    fp_now = f"{_norm_base(settings.embed_base_url)}|{settings.embed_model}"
    stored = await repository.get_settings_map()
    fp_stored = stored.get(FP_KEY)
    if fp_stored is None:
        await repository.upsert_settings({FP_KEY: fp_now})
        fp_stored = fp_now
    stats = await repository.knowledge_stats()
    return {
        "groups": {
            "chat": {"base_url": _effective("chat_base_url"), "model": _effective("chat_model"),
                     "api_key": _mask(_effective("chat_api_key"))},
            "embed": {"base_url": _norm_base(_effective("embed_base_url")),
                      "model": _effective("embed_model"),
                      "api_key": _mask(_effective("embed_api_key"))},
            "rerank": {"base_url": _norm_base(_effective("rerank_base_url")),
                       "model": _effective("rerank_model"),
                       "api_key": _mask(_effective("rerank_api_key"))},
        },
        "embed_fingerprint": {"stored": fp_stored, "current": fp_now,
                              "in_sync": fp_stored == fp_now},
        "chunk_total": stats.get("total"),
    }


class GroupBody(BaseModel):
    """一组上游配置。api_key 空 = 沿用现值(掩码回显的设计里,改别的字段不该被迫重填密钥)。"""
    base_url: str
    model: str
    api_key: str = ""


class SettingsBody(BaseModel):
    """按组提交:三组相互独立,传哪组存哪组(页面三张卡各自保存)。"""
    chat: GroupBody | None = None
    embed: GroupBody | None = None
    rerank: GroupBody | None = None
    rebuild: bool = False       # 嵌入指纹变更时的显式重建确认


_PROBES = {
    "chat": _check_chat,
    "embed": _check_embed,
    "rerank": _check_rerank,
}


@router.post("/api/settings")
async def save_settings(body: SettingsBody) -> dict:
    groups = {name: g for name, g in
              (("chat", body.chat), ("embed", body.embed), ("rerank", body.rerank)) if g is not None}
    if not groups:
        raise HTTPException(400, "没有提交任何一组配置")

    # ① 展开成扁平键值(api_key 空 = 沿用现值),并真测连通——测不通不许存,
    #    配置面不允许「存了才发现全挂」
    vals: dict[str, str] = {}
    checks: list[str] = []
    for name, g in groups.items():
        base, model = _norm_base(g.base_url), g.model.strip()
        if not base or not model:
            raise HTTPException(400, f"{name} 组的 base_url 与模型名都不能为空")
        key = g.api_key.strip() or _effective(f"{name}_api_key")
        if not key:
            raise HTTPException(400, f"{name} 组的 api_key 为空且没有存量值,请填一个")
        vals[f"{name}_base_url"] = base
        vals[f"{name}_model"] = model
        vals[f"{name}_api_key"] = key
        checks.append(await _PROBES[name](base, key, model))

    # ② 嵌入指纹闸:与产出当前向量的配置不一致 → 必须显式确认重建,不许悄悄生效
    if "embed" in groups:
        fp_new = f"{vals['embed_base_url']}|{vals['embed_model']}"
        stored = await repository.get_settings_map()
        fp_old = stored.get(FP_KEY) or f"{_norm_base(settings.embed_base_url)}|{settings.embed_model}"
        if fp_new != fp_old:
            chunk_total = (await repository.knowledge_stats()).get("total")
            if not body.rebuild:
                raise HTTPException(409, detail={
                    "requires_rebuild": True, "chunks": chunk_total,
                    "old_fingerprint": fp_old, "new_fingerprint": fp_new,
                    "message": f"嵌入配置变了({fp_old} → {fp_new}),已有 {chunk_total} 块向量作废,"
                               "需要 drop 集合并全量重嵌。确认后才会保存新配置并开始重建。"})
            # 确认过了:先写配置+热生效(重嵌任务要用新配置嵌入),再 drop + 全量置 pending + 后台重嵌
            await repository.upsert_settings({**vals, FP_KEY: fp_new})
            _apply_overrides(vals)
            await repository.mark_all_chunks_pending()
            await milvus_client.acall(lambda: milvus_client.drop(
                milvus_client.get_client(), milvus_client.COLLECTION))
            asyncio.create_task(_rebuild_task())
            return {"saved": True, "checks": checks, "rebuilding": True,
                    "chunks": chunk_total,
                    "message": f"配置已生效,向量重建已开始({chunk_total} 块),期间检索只剩 BM25 关键词路"}

    # ③ 常规保存:落库 + 热生效,即刻可用
    await repository.upsert_settings(vals)
    _apply_overrides(vals)
    return {"saved": True, "checks": checks, "rebuilding": False}


def _apply_overrides(vals: dict[str, str]) -> None:
    """热生效:直接写进程内 settings(pydantic BaseSettings 可变属性),
    所有读 settings 的代码零改动;embeddings 的单例 client 必须重置,
    否则旧 base_url/key 会一直用到进程重启。"""
    for k, v in vals.items():
        setattr(settings, k, v)
    embeddings.reset_client()
    logger.info("API 设置已热生效:%s",
                " ".join(f"{k}={v if not k.endswith('api_key') else _mask(v)}"
                         for k, v in sorted(vals.items())))


async def _rebuild_task() -> None:
    """全量重嵌后台任务:幂等可重跑,中断了在 kb 页按「向量化待补块」能捡起来。
    期间集合为空,dense 检空、hybrid 只剩 BM25,检索面宁可弱不可错。"""
    try:
        n = await vectorize_pending(include_done=True)
        logger.info("嵌入配置变更后的向量重建完成,共重嵌 %s 块", n)
    except Exception:  # noqa: BLE001
        logger.exception("向量重建后台任务失败;chunk 已全部置 pending,修复上游后重跑 vectorize 即可补齐")
