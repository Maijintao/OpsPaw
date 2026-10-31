"""ch10 语料流水线:捞池 → 脱敏去重 → LLM 修错别字 → LLM 预标 → 模拟补足 → 导出人工抽审。
运行:make ch10-corpus(需 mysql + 聊天上游)。产物落 data/ch10/,抽审文件给用户对话里审。"""
import asyncio
import json
import pathlib
import random

from pydantic import BaseModel

from app.core.llm import get_chat_model, structured
from app.core.taxonomy import LABEL2ID, TOPIC_CLASSES, terminology_table
from app.db import repository
from scripts.ch10.corpus_lib import dedupe, desensitize
from scripts.ch10.prelabel import prelabel_batch

OUT = pathlib.Path("data/ch10")
TARGET_PER_CLASS = 100
SIM_BATCH = 20     # 单次造数条数,小批多次控质量

CLEAN_PROMPT = """修正下面这句用户问题里的错别字和乱格式:不改语义、不改口语风格、不增删诉求,
没有错误就原样返回。只输出句子本身。

{text}"""


class _SimItem(BaseModel):
    text: str
    labels: list[str]


class _SimBatch(BaseModel):
    items: list[_SimItem]


SIMULATE_PROMPT = """你是运维值班语料造数员。为内部系统的智能值班助手「OpsPaw」(值班对象是内部微服务:\
order-service、payment-service、data-sync-service、api-gateway-service、user-service、\
notification-service、search-service、cart-service 等)生成 {n} 条模拟值班同学问题,全部命中主题类目「{name}」。

17 类权威术语表:
{terminology}

要求:
1. 每条一句独立、语义完整的口语化值班同学问题,长短语气多样,贴近真实值班提问,彼此不重复。
2. 大约 15% 用口语化表达(如「那个服务又趴窝了咋整」),10% 故意带错别字(如「告井」=告警),\
15% 是多诉求句——除「{name}」外再字面提到一个其他类目的诉求,labels 把两个类都打上。
3. 其余条目只含「{name}」一个诉求,labels 只有它。
4. 标签铁律:字面提到几个诉求打几个标签,一个不多一个不少;标签取术语表类目名原文。"""


def _dump(path: pathlib.Path, samples: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples),
                    encoding="utf-8")


async def clean_texts(texts: list[str], concurrency: int = 8) -> list[str]:
    model = get_chat_model()
    sem = asyncio.Semaphore(concurrency)

    async def one(t: str) -> str:
        async with sem:
            try:
                r = await model.ainvoke(CLEAN_PROMPT.format(text=t))
                out = r.content.strip()
                return out or t
            except Exception as e:
                print(f"[clean] 调用失败,原样保留: {t[:30]}… ({type(e).__name__})")
                return t

    return list(await asyncio.gather(*[one(t) for t in texts]))


def _parse_items(raw: str) -> list[dict]:
    """从模型输出里抠出 JSON 数组。

    造数这一步改成"纯文本 JSON"了(见 simulate 的注释),模型偶尔会带 ```json 围栏
    或前后多说一句话,所以先找最外层的 [ ... ] 再解析;实在解析不出来就当这批失败,
    交给上层重试——不猜、不硬编。"""
    text = raw.strip()
    if "```" in text:                       # 去掉代码块围栏
        text = text.replace("```json", "```").split("```")[1] if text.count("```") >= 2 else text
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict) and str(d.get("text") or "").strip()]


async def simulate(name: str, need: int) -> list[dict]:
    """造数走纯文本 JSON,不走 function calling。

    原因:嵌套数组(text + labels 列表)的 tool_call 参数在 DeepSeek 端点上经常拼坏,
    实测 17 个类目连续 5 批全挂(OutputParserException),一条都造不出来;让它直接吐
    JSON 数组、我们自己容错解析,反而稳。schema 只用来校验,不用来约束模型输出。"""
    model = get_chat_model(streaming=False, temperature=0.8)
    out: list[dict] = []
    misses = 0
    while len(out) < need and misses < 5:
        n = min(SIM_BATCH, need - len(out))
        prompt = SIMULATE_PROMPT.format(n=n, name=name, terminology=terminology_table()) + (
            "\n\n只输出一个 JSON 数组,形如 "
            '[{"text": "问题原句", "labels": ["类目名"]}, ...],'
            "不要解释、不要代码块、不要多余字段。")
        try:
            r = await model.ainvoke(prompt)
            content = r.content if isinstance(r.content, str) else str(r.content)
            items = _parse_items(content)
            if not items:
                raise ValueError("没解析出 JSON 数组")
        except Exception as e:
            print(f"[simulate] {name} 一批失败({type(e).__name__}),重试(misses={misses + 1})")
            misses += 1
            continue
        got = 0
        for it in items:
            labels = [lb for lb in (it.get("labels") or []) if lb in LABEL2ID]
            if name not in labels:
                labels = [name] + labels
            text = str(it.get("text") or "").strip()
            if text:
                out.append({"text": text, "labels": labels, "origin": "simulated"})
                got += 1
        misses = misses + 1 if got == 0 else 0
    return out[:need]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 1) 捞池(标准化问法优先)
    pool = await repository.list_pool_texts()
    raw = [{"text": p["text"], "labels": [], "origin": "pool"} for p in pool]
    _dump(OUT / "corpus_raw.jsonl", raw)
    print(f"捞池 {len(raw)} 条")
    # 2) 清洗:脱敏 → 去重 → LLM 修错别字 → 再去重
    cleaned = dedupe([{**s, "text": desensitize(s["text"])} for s in raw])
    fixed = await clean_texts([s["text"] for s in cleaned])
    cleaned = dedupe([{**s, "text": t} for s, t in zip(cleaned, fixed)])
    _dump(OUT / "corpus_clean.jsonl", cleaned)
    print(f"清洗后 {len(cleaned)} 条")
    # 3) 预标真实问题
    labels = await prelabel_batch([s["text"] for s in cleaned])
    labeled = [{**s, "labels": lb} for s, lb in zip(cleaned, labels)]
    # 4) 模拟补足:按标签计数补到每类 TARGET_PER_CLASS(多诉求句给命中的每类都记数)
    counts = {c.name: 0 for c in TOPIC_CLASSES}
    for s in labeled:
        for lb in s["labels"]:
            counts[lb] += 1
    for c in TOPIC_CLASSES:
        need = TARGET_PER_CLASS - counts[c.name]
        if need <= 0:
            continue
        sims = await simulate(c.name, need)
        labeled.extend(sims)
        for s in sims:
            for lb in s["labels"]:
                counts[lb] += 1
        print(f"{c.name}: 补造 {len(sims)} 条(当前 {counts[c.name]})")
    labeled = dedupe(labeled)
    _dump(OUT / "corpus_labeled.jsonl", labeled)
    print(f"语料总量 {len(labeled)} 条;各类:{counts}")
    # 5) 抽审导出:真实池全量 + 每类模拟抽 5
    rng = random.Random(42)
    lines = ["# ch10 语料人工抽审(预标 + 模拟)", "",
             "> 格式:问题 → 标签。看到错标直接指出原句。", "",
             "## 真实池问题(全量)", ""]
    for s in labeled:
        if s["origin"] == "pool":
            lines.append(f"- {s['text']} → {'、'.join(s['labels'])}")
    lines += ["", "## 模拟问题(每类抽 5)", ""]
    for c in TOPIC_CLASSES:
        sims = [s for s in labeled if s["origin"] == "simulated" and c.name in s["labels"]]
        lines.append(f"### {c.name}")
        lines += [f"- {s['text']} → {'、'.join(s['labels'])}"
                  for s in rng.sample(sims, min(5, len(sims)))]
        lines.append("")
    (OUT / "sample_review.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"抽审文件已导出:{OUT / 'sample_review.md'}")


if __name__ == "__main__":
    asyncio.run(main())
