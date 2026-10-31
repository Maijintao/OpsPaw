from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core import query_understanding, retrieval
from app.tools import registry


class FaqInput(BaseModel):
    keyword: str = Field(description="值班同学咨询的规范/流程/操作类问题(可用原话)")
    category: str | None = Field(default=None, description="可选:按主题过滤,如『告警』『变更』『值班手册』")


@tool(args_schema=FaqInput)
async def query_faq(keyword: str, category: str | None = None) -> dict:
    """查询常见问题/runbook 知识库(混合检索+重排)。用于规范、流程、阈值、处置步骤等通用问题。
    返回带编号证据供作答引用;evidence 为空表示没检索到条目,请如实告知并给通用建议,不要硬编规范条款。"""
    u = await query_understanding.understand(keyword)
    query = u["standard"]
    # 同义词扩展只拼进 BM25 检索文本(检索侧),不改 dense query、不改标准问法语义(ch04 spec §2)
    bm25_text = query + (" " + " ".join(u["expanded"]) if u["expanded"] else "")

    hits = await retrieval.search_knowledge(
        query, strategy="hybrid_rerank", category=category, bm25_text=bm25_text)

    # 主题过滤稳健性:category 由模型自由填写,常猜错入库值(如「值班手册」≠ 实存「值班制度」),
    # 精确过滤会把结果清空。过滤后为空则回退到不过滤重试(过滤是优化、非硬约束)。
    if category and not hits:
        hits = await retrieval.search_knowledge(
            query, strategy="hybrid_rerank", category=None, bm25_text=bm25_text)

    # 首尾组装 + 编号。拒答闸已下线:检索到什么就交什么,由 Agent 决定
    # 怎么用(答/说明相近/向用户确认),检索层不再替生成层做「答不答」的决定。
    arranged = retrieval.arrange_head_tail(hits)
    citations = [
        {"n": i + 1, "id": h["id"], "section_path": h["section_path"],
         "question": h["question"], "answer": h["answer"], "content_type": h["content_type"]}
        for i, h in enumerate(arranged)
    ]
    evidence = "\n".join(f"[{c['n']}] {c['question']}: {c['answer']}" for c in citations)
    return {"evidence": evidence, "citations": citations}


# query_faq 走 RAG 管线(改写+混合检索+重排,多次上游往返):远慢于默认 5s 给 30s;
# 超时多为上游慢而非瞬时抖动,重试大概率仍超时纯烧时间 → max_retries=0(沿 ch05 NO_RETRY 的理由)
registry.register(registry.spec_from_langchain_tool(
    query_faq, source="builtin", timeout=30.0, max_retries=0))
