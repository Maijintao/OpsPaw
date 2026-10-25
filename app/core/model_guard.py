"""服务名机械闸:答案里出现的服务标识必须逐字来自本轮证据,否则这句话是编的。

服务名是有限且可机械校验的实体:答案里每个 service/gateway 标识
要么能在证据里逐字找到,要么就是模型自己拼的。判它不需要再调一次大模型,正则就够,
所以别把这件事交给忠实度裁判(裁判贵、还会漏)。

和 ch09 读图小注里那道数字校验是同一手法:**能机械校验的,就不要用模型校验。**

捕获 api-gateway-service、data-sync-service 一类带连字符的服务标识。大小写不敏感,
但比对时按原样比——API-Gateway-Service 这种大小写不一致也当没命中。
"""
import re

SERVICE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-service\b", re.IGNORECASE)


def models_in(text: str) -> list[str]:
    """按出现顺序去重列出文本里的服务标识。"""
    seen: dict[str, None] = {}
    for m in SERVICE_RE.findall(text or ""):
        seen.setdefault(m, None)
    return list(seen)


def unsupported_models(answer: str, evidence: str) -> list[str]:
    """答案里有、证据里没有的服务名。空列表 = 这一关过了。"""
    allowed = set(models_in(evidence))
    return [m for m in models_in(answer) if m not in allowed]


def repair_hint(bad: list[str]) -> str:
    """重写提示:只告诉它哪几个服务名没依据,不替它猜正确服务名。"""
    return (
        "上一版回答里这些服务名在给你的证据里找不到:" + "、".join(bad) + "。"
        "请重写回答:服务名必须逐字复制证据里出现过的标识,证据里没有的服务名一个都不要写,"
        "拿不准就不提服务名。其余内容与引用编号保持不变。"
    )
