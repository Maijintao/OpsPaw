"""OPENRCA 评测工具的最小补齐(上游仓库未分发 eval.py,接口按 judge.py 调用还原)。

judge.py 用它对答案做归一化比对:
- is_time(s):字符串是否是时间戳(比如 "2021-03-10 22:00:00" / "22:00:00");
- normalize(s):小写、去空格与标点,用于实例/错误类型比对;
- time_within_one_minute(a, b):两个时间戳是否在同一分钟内(容差判定)。
OpenRCA 的官方判分逻辑更全,这里只实现 judge 消费的三个函数。
"""
import re

_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T])\d{2}:\d{2}(:\d{2})?$|^\d{2}:\d{2}(:\d{2})?$")


def is_time(s) -> bool:
    return bool(isinstance(s, str) and _TIME_RE.match(s.strip()))


def normalize(s) -> str:
    return re.sub(r"[\s\u3000]+", "", str(s or "")).lower().strip("。.,;；、")


def _parse_ts(s: str) -> tuple[int, int, int] | None:
    m = re.findall(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        return None
    h, mi, sec = m[0]
    return int(h), int(mi), int(sec or 0)


def time_within_one_minute(a, b) -> bool:
    ta, tb = _parse_ts(str(a)), _parse_ts(str(b))
    if ta is None or tb is None:
        return normalize(a) == normalize(b)
    ha, ma, sa = ta
    hb, mb, sb = tb
    return abs((ha * 3600 + ma * 60 + sa) - (hb * 3600 + mb * 60 + sb)) <= 60
