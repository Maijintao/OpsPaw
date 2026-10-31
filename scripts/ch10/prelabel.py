"""ch10 预标:大模型照权威术语表给问题打多标签。折中路线的前半——预标,后半人工抽审。"""
import asyncio

from pydantic import BaseModel, Field

from app.core.llm import structured
from app.core.taxonomy import LABEL2ID, terminology_table


class _Labeled(BaseModel):
    labels: list[str] = Field(description="命中的类目名数组,取术语表类目名原文")


PRELABEL_PROMPT = """你是运维值班主题标注员。对照下面这份 17 类权威归并术语表,给值班同学的问题打主题标签。

术语表(类目:边界说明(示例)):
{terminology}

标注铁律:
1. 字面提到几个诉求就打几个标签,一个不多一个不少。先数诉求再打标,多诉求句漏打就是错:\
「CPU 高想重启」→ CPU 异常+变更申请;「磁盘满了清理日志就行了吗还要注意啥」→ 磁盘异常+日志排查;\
「扩容要审批吗窗口外的话」→ 变更申请+值班制度。
2. 没提的诉求不许联想补标:「服务又趴窝了根本连不上」→ 只标服务不可用(连不上≠网络问题);\
「盘快满了告警一直在响」→ 只标磁盘异常(响的只是通知渠道);「怎么查 redis 是不是挂了」→ 只标数据库存储\
(问的是 redis 本身,不是怎么配监控)。「服务挂了」不许因为「可能要重启」就脑补变更申请。
3. 近邻边界:重启/扩容/回滚归变更申请、发布引起的问题归发布相关;CPU/内存/磁盘各归各的资源类;响应慢归响应缓慢、慢 SQL 归数据库存储\
——慢 SQL 已经拖垮连接池/拖慢接口时,数据库存储+响应缓慢两个都标;「先回滚还是先查」这类还在问处置路线的是咨询,标发布相关+日志排查,不是变更申请。
4. 方言、口语、错别字要看穿字面认诉求:「服务又趴窝了」是服务不可用;「告井」是「告警」的错字。
5. 都对不上就标「其他」;标签必须取术语表的类目名原文。

用户问题:{text}"""


async def prelabel_one(text: str) -> list[str]:
    # structured()(function calling 路)而非裸 with_structured_output:后者默认
    # json_schema/response_format,DeepSeek 官方端点等不少上游直接 400 拒收,全批静默降级「其他」。
    # temperature=0:标注是评分类任务,要可复现,不许采样噪声混进语料(项目惯例,评委链路同规)
    model = structured(_Labeled, temperature=0)
    try:
        r: _Labeled = await model.ainvoke(
            PRELABEL_PROMPT.format(terminology=terminology_table(), text=text))
    except Exception as e:
        # 失败要看得见:无声标「其他」会污染语料,还会把黄金样例闸的通过率无声拉低
        print(f"[prelabel] 调用失败,兜底标「其他」: {text[:30]}… ({type(e).__name__}: {e})")
        return ["其他"]
    labels = [lb for lb in r.labels if lb in LABEL2ID]
    return labels or ["其他"]


async def prelabel_batch(texts: list[str], concurrency: int = 8) -> list[list[str]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(t: str) -> list[str]:
        async with sem:
            return await prelabel_one(t)

    return list(await asyncio.gather(*[one(t) for t in texts]))
