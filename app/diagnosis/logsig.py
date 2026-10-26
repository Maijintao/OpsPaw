"""日志信号归类(采集段):把错误日志原文按"最能定性"的类别归拢成标签。

为什么值得做:错误文本是唯一不采样、秒级的数据源,也是最能定性的证据——
"12 条连接被拒"比"错误率 30%"信息量高一个量级:前者直接指向"对端没起来/端口不通",
后者只说明"有问题"。20 行原文整段丢给 LLM 是浪费,归类成标签才是特征。

纯字符串匹配,零 LLM、零 IO——不猜,只认日志里真实出现的模式;一条都认不出就归"其他",
如实计数,不硬编类别。
"""
import re

# 顺序即优先级:一条日志可能同时命中多个模式,取最具体的那个
_RULES: tuple[tuple[str, str], ...] = (
    ("连接被拒", r"connection refused|econnrefused|connect: connection refused|拒绝连接"),
    ("连接超时", r"i/o timeout|connection timed out|context deadline exceeded|deadline exceeded|超时"),
    ("DNS/解析失败", r"no such host|name resolution|nxdomain|servfail|dns lookup|解析失败"),
    ("连接被重置", r"connection reset|econnreset|broken pipe|连接重置"),
    ("证书/TLS", r"x509|certificate|tls handshake|ssl handshake|证书"),
    ("权限/认证", r"permission denied|unauthorized|forbidden|\b40[13]\b|权限"),
    ("OOM/内存", r"out of memory|outofmemory|oomkill|oomkilled|exit code 137|"
                 r"memory limit|cannot allocate|\boom\b"),
    ("磁盘/空间", r"no space left|disk full|空间不足|磁盘"),
    ("进程崩溃", r"panic|fatal error|segmentation fault|core dumped|crashloop"),
    ("依赖不可用", r"service unavailable|upstream|\b50[234]\b|unavailable"),
    ("业务码/参数", r"invalid|validation|bad request|\b422\b|参数"),
    ("限流/资源耗尽", r"too many|throttl|rate limit|exhausted|限流|pool"),
)


def classify(lines: list) -> dict:
    """把错误日志按类别归拢,返回 {"labels": {类别: 条数}, "samples": {类别: 样例一行}}。

    lines 可以是 Loki 返回的 [{line, ts, level}, ...],也可以是纯字符串列表。
    没有日志就返回空 labels——如实空着,不编。"""
    labels: dict[str, int] = {}
    samples: dict[str, str] = {}
    for item in lines or []:
        text = (item.get("line") if isinstance(item, dict) else str(item)) or ""
        low = text.lower()
        for name, pat in _RULES:
            if re.search(pat, low):
                break
        else:
            name = "其他"
        labels[name] = labels.get(name, 0) + 1
        samples.setdefault(name, text.strip()[:160])
    return {"labels": labels, "samples": samples}


def summary(classified: dict | None) -> str:
    """一句话归拢,给证据包/事件流用:"连接被拒 12 条、连接超时 3 条"。"""
    labels = (classified or {}).get("labels") or {}
    if not labels:
        return ""
    ordered = sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))
    return "、".join(f"{k} {v} 条" for k, v in ordered)
