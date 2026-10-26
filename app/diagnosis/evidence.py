"""Trace-driven service, operation and dependency evidence helpers."""
from collections import defaultdict
from dataclasses import dataclass
import statistics

from app.diagnosis.collect import Span

# 错误 span 里值得看的属性键(OTel 语义约定 + 常见库名):错误定性比错误率高一个量级,
# "/grpc.Unavailable"比"错误率 30%"更能定性。数据本来就在 Span.attributes 里,只是
# 之前从来没人看过——这是不接任何新采集器就能榨出的最大一块数据。
_ERROR_ATTR_KEYS = ("error.type", "exception.type", "exception.message", "error.message")
_GRPC_STATUS_KEY = "rpc.grpc.status_code"
_HTTP_STATUS_KEYS = ("http.response.status_code", "http.status_code")
# gRPC 状态码 → logsig 类别名(与日志归类同一套标签,faulttype 无缝消费):
# 14 UNAVAILABLE→依赖不可用 / 4 DEADLINE_EXCEEDED→连接超时 / 7,16 权限认证 /
# 8 RESOURCE_EXHAUSTED→限流资源耗尽 / 3 INVALID_ARGUMENT→业务码参数
_GRPC_CODE_TEXT = {3: "invalid argument", 4: "deadline exceeded", 5: "not found",
                   7: "permission denied", 8: "resource exhausted", 13: "internal error",
                   14: "service unavailable", 15: "data loss", 16: "unauthenticated"}


def _span_error_line(sp: Span) -> str | None:
    """把一条错误 span 携带的错误信息合成一行文本,喂给 logsig.classify 归类。"""
    attrs = sp.attributes or {}
    parts: list[str] = []
    for k in _ERROR_ATTR_KEYS:
        if attrs.get(k):
            parts.append(str(attrs[k]))
    if sp.status_message:
        parts.append(str(sp.status_message))
    if _GRPC_STATUS_KEY in attrs:
        try:
            code = int(attrs[_GRPC_STATUS_KEY])
            if code in _GRPC_CODE_TEXT:
                parts.append(_GRPC_CODE_TEXT[code])
        except (TypeError, ValueError):
            pass
    for k in _HTTP_STATUS_KEYS:
        v = attrs.get(k)
        if v:
            parts.append(f"http status {v}")
    return " | ".join(parts) if parts else None


def error_lines(spans: list[Span], service: str, max_lines: int = 30) -> list[str]:
    """某服务错误 span 携带的错误信息文本行(去重,防同一错误刷屏)。
    平台没接 Loki 时日志维是盲的,但错误 span 的属性是 OTel 标准埋点自带的——
    这条通道不依赖任何额外采集,是日志维缺失时的兜底定性来源。"""
    out: list[str] = []
    seen: set[str] = set()
    for sp in spans:
        if sp.service != service or not sp.error:
            continue
        line = _span_error_line(sp)
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= max_lines:
            break
    return out


def version_timeline(spans: list[Span]) -> dict[str, dict]:
    """各服务在采样窗内出现过的 service.version(OTel 语义约定,resource 属性)。
    窗内出现 ≥2 个版本 = 发生过发版/回滚——生产环境最常见的根因来源之一,
    而 trace 资源属性本来就在手,只是之前从来没被看过。返回
    {service: {"from", "to", "changed_us", "switches"}};没有版本属性或没换版本不产生条目。"""
    seen: dict[str, dict[str, int]] = {}
    for sp in spans:
        v = (sp.resource or {}).get("service.version")
        if not v:
            continue
        vers = seen.setdefault(sp.service, {})
        if v not in vers or sp.start_us < vers[v]:
            vers[v] = sp.start_us
    out: dict[str, dict] = {}
    for svc, vers in seen.items():
        if len(vers) < 2:
            continue
        ordered = sorted(vers.items(), key=lambda kv: kv[1])
        out[svc] = {"from": ordered[0][0], "to": ordered[-1][0],
                    "changed_us": ordered[-1][1], "switches": len(vers)}
    return out


def condense_logs(lines: list[dict], cap: int = 20) -> dict:
    """错误日志压缩:同一错误刷屏时只留代表行,并给出最早出现时刻。
    去重键是"数字抹平"后的文本(同型错误的时间戳/PID 等数字不同算同一条);
    保留的行按时间新→旧排(与上游一致),每条带重复计数。错误爆发的**起点**
    (first_ts)比行数有用得多——它是时间锚的日志侧信号源,还给归因员知情。
    lines 是 [{ts, line, level?}](Loki 返回形状);空输入返回空结构。"""
    if not lines:
        return {"lines": [], "distinct": 0, "repeat": 0, "first_ts": None}
    import re as _re
    ordered = sorted(lines, key=lambda x: str(x.get("ts", "")), reverse=True)
    groups: dict[str, dict] = {}
    for item in ordered:
        text = str(item.get("line", ""))
        key = _re.sub(r"\d+", "#", text)
        g = groups.setdefault(key, {"item": item, "count": 0})
        g["count"] += 1
        # first_ts 要最早的那条:倒序遍历,后看到的更早,直接覆盖
        if str(item.get("ts", "")) < str(g["item"].get("ts", "")):
            g["item"] = item
    out = []
    for g in groups.values():
        it = g["item"]
        out.append({**it, "repeat": g["count"]})
    out.sort(key=lambda x: str(x.get("ts", "")), reverse=True)
    total = sum(g["count"] for g in groups.values())
    first = min((str(x.get("ts", "")) for x in lines), default=None)
    return {"lines": out[:cap], "distinct": len(groups), "repeat": total - len(groups),
            "first_ts": first}


@dataclass
class OperationStats:
    service: str
    operation: str
    kind: str | None
    calls: int
    errors: int
    error_rate: float
    p95_us: int
    mean_us: float
    child_calls: int
    child_p95_us: int
    self_p95_us: int | None


def _p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0


def operation_stats(spans: list[Span], min_calls: int = 5) -> list[OperationStats]:
    """Aggregate span evidence by service/operation/kind and estimate child wait."""
    by_id = {(sp.trace_id, sp.span_id): sp for sp in spans if sp.trace_id and sp.span_id}
    groups: dict[tuple[str, str, str | None], list[Span]] = defaultdict(list)
    for span in spans:
        groups[(span.service, span.operation, span.kind)].append(span)
    result = []
    for (service, operation, kind), items in groups.items():
        if len(items) < min_calls:
            continue
        child_durations: list[int] = []
        self_durations: list[int] = []
        for span in items:
            children_by_service: dict[str, list[Span]] = defaultdict(list)
            for candidate in spans:
                if (candidate.trace_id == span.trace_id
                        and candidate.parent_span_id == span.span_id):
                    children_by_service[candidate.service].append(candidate)
            children = [max(group, key=lambda child: child.duration_us)
                        for group in children_by_service.values()]
            child_total = min(span.duration_us, sum(child.duration_us for child in children))
            if children:
                child_durations.append(child_total)
                self_durations.append(max(span.duration_us - child_total, 0))
        durations = [span.duration_us for span in items]
        error_count = sum(span.error for span in items)
        result.append(OperationStats(
            service=service, operation=operation, kind=kind, calls=len(items),
            errors=error_count, error_rate=error_count / len(items),
            p95_us=_p95(durations), mean_us=round(statistics.mean(durations), 2),
            child_calls=len(child_durations), child_p95_us=_p95(child_durations),
            self_p95_us=_p95(self_durations) if self_durations else None,
        ))
    return sorted(result, key=lambda item: (item.service, item.operation, item.kind or ""))


def dependency_stats(spans: list[Span], min_calls: int = 3) -> list[dict]:
    """Summarize observed client calls or cross-service parent-child edges."""
    by_id = {(sp.trace_id, sp.span_id): sp for sp in spans if sp.trace_id and sp.span_id}
    edges: dict[tuple[str, str, str], list[Span]] = defaultdict(list)
    for span in spans:
        parent = by_id.get((span.trace_id, span.parent_span_id)) if span.parent_span_id else None
        if span.kind == "CLIENT":
            target = ((span.attributes or {}).get("rpc.service")
                      or (span.attributes or {}).get("peer.service") or span.peer)
            if target:
                edges[(span.service, target, span.operation)].append(span)
        elif parent and parent.service != span.service:
            edges[(parent.service, span.service, span.operation)].append(span)
    result = []
    for (source, target, operation), items in edges.items():
        if len(items) < min_calls:
            continue
        durations = [item.duration_us for item in items]
        errors = sum(item.error for item in items)
        result.append({"from": source, "to": target, "operation": operation,
                       "calls": len(items), "errors": errors,
                       "error_rate": errors / len(items), "p95_us": _p95(durations),
                       "mean_us": round(statistics.mean(durations), 2)})
    return sorted(result, key=lambda item: (item["from"], item["to"], item["operation"]))
