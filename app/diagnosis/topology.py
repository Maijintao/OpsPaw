"""拓扑构建(管线第③段):span parent-child 恢复服务级调用图。

不依赖预设服务清单——链路里真实出现过的调用就是图,天然换靶子。
边方向 = caller → callee(parent 服务调用 child 服务),边权 = 调用频次。
"""
from collections import defaultdict

from app.diagnosis.collect import Span


class ServiceGraph:
    def __init__(self) -> None:
        self.nodes: set[str] = set()
        self.edges: dict[tuple[str, str], int] = defaultdict(int)   # (caller, callee) -> 次数

    def neighbors(self, service: str) -> tuple[set[str], set[str]]:
        """返回 (上游 callers, 下游 callees)。传播定位只看一跳,图不必全量展开。"""
        upstream = {a for (a, b) in self.edges if b == service and a != service}
        downstream = {b for (a, b) in self.edges if a == service and b != service}
        return upstream, downstream


def build(spans: list[Span]) -> ServiceGraph:
    """span parent-child → 服务级边。跨服务才算边(服务内部父子不画环);
    parent 在本批 span 里找不到的(采样截断)挂成缺失父,不计边。

    影子节点:client span 带 peer 且对端在本批 span 里一次都没出现——对端挂死时
    自身零 span,这条影子边是它进拓扑的唯一通道(真实验证:docker stop currency 后
    checkout 的失败 client span op=oteldemo.CurrencyService/Convert,peer=currency)。"""
    g = ServiceGraph()
    by_id = {(sp.trace_id, sp.span_id): sp for sp in spans
             if sp.trace_id and sp.span_id}
    seen_services = {sp.service for sp in spans}
    for sp in spans:
        g.nodes.add(sp.service)
        parent = by_id.get((sp.trace_id, sp.parent_span_id)) \
            if sp.trace_id and sp.parent_span_id else None
        if parent and parent.service != sp.service:
            g.edges[(parent.service, sp.service)] += 1
        # 影子边:对端有名字、对端零遥测、不指向自己
        if sp.peer and sp.peer != sp.service and sp.peer not in seen_services:
            g.nodes.add(sp.peer)
            g.edges[(sp.service, sp.peer)] += 1
    return g
