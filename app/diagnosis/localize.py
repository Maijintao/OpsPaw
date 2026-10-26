"""调用操作到服务名称的匹配，供平台扫描使用。"""
def _op_points_to(operation: str, service: str) -> bool:
    """操作名是否点名某服务。段级匹配(按 . / _ - 切段):段==服务名,或段==服务名+service
    后缀(otel-demo 的全名是 oteldemo.CurrencyService/Convert——首段是包名 oteldemo,
    首段规则咬不住,必须按段对服务名模糊匹配;真实教训:currency 挂死整晚指认不到)。

    比对前两边都归一化成纯字母数字:服务名里的连字符在操作名里往往被吃掉
    (product-catalog ↔ ProductCatalogService),不归一化的话带连字符的服务名永远匹配不上
    ——真实事故:product-catalog / frontend-proxy / load-generator / image-provider /
    frontend-web 这几个服务在"错误指认 + 挂死影子"那条路上整类失明,挂了也没人指认得出来。"""
    op = (operation or "").lower()
    svc = (service or "").lower()
    if not svc or not op:
        return False
    import re
    svc_key = re.sub(r"[^a-z0-9]", "", svc)
    if not svc_key:
        return False
    segs = [re.sub(r"[^a-z0-9]", "", s) for s in re.split(r"[^a-z0-9]+", op)]
    return any(seg == svc_key or seg == svc_key + "service" for seg in segs)
