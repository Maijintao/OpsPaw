"""归因结果结构与支持信号，供置信表达、结果说明和存档共同使用。"""
import re


class Attribution:
    """归因结论合同:四分语义 + 根因服务 + 证据链 + 缺口。"""
    def __init__(self, verdict: str, root_cause_service: str | None = None,
                 amplifiers: list[str] | None = None, unrelated: list[str] | None = None,
                 chain: list[str] | None = None, confidence_note: str = "",
                 missing: list[str] | None = None,
                 suggested_actions: list[str] | None = None,
                 refill: list | None = None):
        self.verdict = verdict
        self.root_cause_service = root_cause_service
        self.amplifiers = amplifiers or []
        self.unrelated = unrelated or []
        self.chain = chain or []
        self.confidence_note = confidence_note
        self.missing = missing or []
        self.suggested_actions = suggested_actions or []
        self.refill = refill or []


def _reachable(edges: set, src: str, dst: str) -> bool:
    """无向可达:src 与 dst 在调用图(含影子边)上是否有通路。影子边让挂死零遥测
    服务也有图上位置,断连判定才不会误伤它们。"""
    if src == dst:
        return True
    adj: dict[str, set[str]] = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen, stack = {src}, [src]
    while stack:
        for nxt in adj.get(stack.pop(), ()):
            if nxt == dst:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def support_signals(root: str, symptom: str, candidates: list[dict],
                    devs: dict, edges: set,
                    window_start_us: int | None = None) -> set[str]:
    """信号盘点:四类独立信号全部代码可判——错误指认/拓扑方向/时间先后/资源偏离,
    供置信档位消费。时间信号受窗边界纪律约束(任一侧锚点贴窗起点即停用)。"""
    sig: set[str] = set()
    dev, sym_dev = devs.get(root), devs.get(symptom)
    pack = next((c for c in candidates if c["service"] == root), None)
    if ((pack and (pack.get("direction") == "error_ref"
                   or int(pack.get("ref_count") or 0) > 0))
            or (dev and "error" in dev.abnormal_dims)):
        sig.add("error")      # 错误指认:被失败调用点名,或自身错误维异常
    if dev is not None and ("hang" in dev.abnormal_dims or "shadow" in dev.abnormal_dims):
        sig.add("hang_blame")  # 挂死/影子形态实锤:静默+停声后被指认
    if dev is None or _reachable(edges, root, symptom):
        sig.add("topology")   # 拓扑方向:与症状服务有通路
    if (dev and sym_dev and dev.onset_trusted(window_start_us)
            and sym_dev.onset_trusted(window_start_us)
            and dev.first_anomaly_us < sym_dev.first_anomaly_us):
        sig.add("time")       # 时间先后:异常早于症状服务
    if dev and ("cpu" in dev.abnormal_dims or "mem" in dev.abnormal_dims):
        sig.add("resource")   # 资源偏离:CPU/内存超自身基线
    if dev is not None and dev.self_latency_abnormal:
        sig.add("latency_self")  # 时延源头:自身 p95 超出下游能解释的范围
    return sig
