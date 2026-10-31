"""按数据源自动生成 Grafana 面板。

## 为什么是"生成"而不是"预置一份"

客户环境里指标族叫什么、服务名落在哪个标签,都不一样。预置一份面板等于要求甲方
适配我们——换一套命名就废。这里复用诊断侧那套指标发现(detect._discover),把
"这个平台有哪些指标、服务名在哪个标签"探出来,再拼成面板 JSON 推给 Grafana。

发现不到就如实报错,不生成一份画不出东西的空面板。

## 边界

- 面板 UID 按平台固定(opspaw-svc-{target_id}),重复调用是覆盖更新,不会堆一堆。
- Grafana 端点是环境变量:面板是"外部系统"的东西,不该硬编码进代码。
"""
import logging
import os
import time

import httpx

from app.db import repository

logger = logging.getLogger("app")

# Grafana 端点:默认指向靶机上那套(生产部署应通过环境变量注入)
GRAFANA_URL = os.environ.get("OPSPAW_GRAFANA_URL", "http://42.193.188.243:3001").rstrip("/")
GRAFANA_USER = os.environ.get("OPSPAW_GRAFANA_USER", "admin")
GRAFANA_PASS = os.environ.get("OPSPAW_GRAFANA_PASS", "opspaw123")

_TIMEOUT = 15.0


# 进程内小缓存:生成面板要探指标族、查数据源、再 POST 给 Grafana,四秒多;
# 服务清单要扫一遍服务目录,一秒半。这两个都是"几分钟内不会变"的东西,
# 每次进页面都重算一遍,页面就是卡。
_CACHE: dict[str, tuple[float, object]] = {}


def _cache_get(key: str, ttl: float):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_set(key: str, val) -> None:
    _CACHE[key] = (time.time(), val)


async def _api(method: str, path: str, json_body: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.request(method, f"{GRAFANA_URL}{path}", json=json_body,
                            auth=(GRAFANA_USER, GRAFANA_PASS))
        r.raise_for_status()
        return r.json() if r.content else {}


async def _default_ds_uid() -> str:
    """Grafana 里默认 Prometheus 数据源的 uid。写死某个环境的 uid 就等于绑死环境。"""
    rows = await _api("GET", "/api/datasources")
    prom = [r for r in rows if r.get("type") == "prometheus"]
    if not prom:
        raise ValueError("Grafana 里没有 Prometheus 数据源")
    pick = next((r for r in prom if r.get("isDefault")), prom[0])
    return pick["uid"]


async def _available_families(prom: dict) -> set[str]:
    url = f"{prom['url'].rstrip('/')}/api/v1/label/__name__/values"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(url, headers=prom.get("headers"))
        r.raise_for_status()
        return set((r.json() or {}).get("data") or [])


def _panel(pid: int, title: str, expr: str, unit: str, x: int, y: int,
           ds_uid: str, w: int = 12, h: int = 8, desc: str = "",
           legend: str | None = None) -> dict:
    return {
        "id": pid, "type": "timeseries", "title": title, "description": desc,
        "datasource": {"type": "prometheus", "uid": ds_uid},
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8}},
            "overrides": [],
        },
        "targets": [{"refId": "A", "expr": expr,
                     "legendFormat": legend or "{{__name__}}",
                     "datasource": {"type": "prometheus", "uid": ds_uid}}],
        "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi"}},
    }


def metric_specs(info: dict, families: set[str], service: str | None) -> list[dict]:
    """五个指标各自怎么查。面板和自渲染共用这一份,口径不会分叉。

    service 三种写法:
      "$service" —— Grafana 变量(生成面板用)
      "cart"     —— 只看这一个服务
      None       —— 按服务分组,一次拿全部(前端做"谁在变"的对比图用)
    """
    svc = info["service_label"]
    kind = info.get("kind_label")
    status = info.get("status_label") or "status_code"
    fam = info["family"]
    dur = info.get("dur_family")
    kind_filter = f',{kind}="SPAN_KIND_SERVER"' if kind else ""
    cpu_fam = next((f for f in ("container_cpu_usage_seconds_total",
                                "container_cpu_usage_nanoseconds_total",
                                "process_cpu_seconds_total") if f in families), None)
    mem_fam = next((f for f in ("container_memory_working_set_bytes",
                                "container_memory_usage_total_bytes",
                                "container_memory_rss",
                                "container_memory_usage_bytes") if f in families), None)
    cname = "container_name"

    # 一律按服务分组:选"全部"时每个服务一条线(参考面板就是这种自动聚合对比),
    # 选单个服务时过滤后自然只剩一条。不分组的话 All 会塌成一根求和线,看不出谁在变。
    grp = f" by ({svc})"
    flt = f',{svc}=~"{service}"' if service is not None else ""

    out = [
        {"key": "calls", "title": "调用量", "unit": "次/秒", "unit_gf": "reqps",
         "desc": "每秒收到的请求数(SERVER span)",
         "legend": "{{%s}}" % svc,
         "expr": f'sum{grp}(rate({{__name__="{fam}"{kind_filter}{flt}}}[5m]))'},
        {"key": "error", "title": "错误率", "unit": "%", "unit_gf": "percentunit",
         "desc": "失败请求占比",
         "legend": "{{%s}}" % svc,
         "expr": (f'sum{grp}(rate({{__name__="{fam}",{status}=~"STATUS_CODE_ERROR"{flt}}}[5m]))'
                  f' / sum{grp}(rate({{__name__="{fam}"{flt}}}[5m]))')},
    ]
    if dur:
        dgrp = f" by (le, {svc})"
        out.append({"key": "latency", "title": "p95 时延", "unit": "ms", "unit_gf": "ms",
                    "desc": "95% 的请求快于这个值",
                    "legend": "{{%s}}" % svc,
                    "expr": (f'histogram_quantile(0.95, sum{dgrp}'
                             f'(rate({{__name__="{dur}"{flt}}}[5m])))')})
    if cpu_fam:
        div = "/ 1e9" if "nanoseconds" in cpu_fam else ""
        cgrp = f" by ({cname})"
        cflt = f',{cname}=~"{service}"' if service is not None else ""
        out.append({"key": "cpu", "title": "CPU", "unit": "核", "unit_gf": "none",
                    "desc": "容器实际消耗的核数",
                    "legend": "{{%s}}" % cname,
                    "expr": f'sum{cgrp}(rate({{__name__="{cpu_fam}"{cflt}}}[5m])) {div}'.strip()})
    if mem_fam:
        mgrp = f" by ({cname})"
        mflt = f',{cname}=~"{service}"' if service is not None else ""
        out.append({"key": "memory", "title": "内存", "unit": "MB", "unit_gf": "decmbytes",
                    "desc": "容器当前占用内存",
                    "legend": "{{%s}}" % cname,
                    "expr": f'sum{mgrp}({{__name__="{mem_fam}"{mflt}}}) / 1024 / 1024'})
    return out


def build_dashboard(info: dict, target_name: str, uid: str,
                    families: set[str], ds_uid: str) -> dict:
    """把"发现到的指标"拼成面板定义(给"在 Grafana 打开"用)。

    前端主视图是我们自己画的五张图;这份面板是同一个数据源的另一种看法,
    客户想加自己的图就去 Grafana 里加。
    """
    svc = info["service_label"]
    fam = info["family"]
    panels = []
    for i, spec in enumerate(metric_specs(info, families, "$service")):
        panels.append(_panel(i + 1, f'{spec["title"]}（{spec["unit"]}）', spec["expr"],
                             spec["unit_gf"], (i % 2) * 12, (i // 2) * 8, ds_uid,
                             desc=spec["desc"], legend=spec.get("legend")))
    return {
        "uid": uid,
        "title": f"服务总览 · {target_name}",
        "tags": ["opspaw", "auto-generated"],
        "timezone": "browser",
        "editable": True,
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": [{
            "name": "service", "label": "服务", "type": "query",
            "datasource": {"type": "prometheus", "uid": ds_uid},
            "query": f'label_values({{__name__="{fam}"}}, {svc})',
            # 允许选"全部":allValue 用 .* 才能让 {svc}=~"$service" 匹配到所有服务,
            # 否则选了 All 反而查不到东西。
            "refresh": 2, "includeAll": True, "allValue": ".*", "multi": False,
            "current": {},
        }]},
        "panels": panels,
    }


async def series(target_id: int, service: str, minutes: int = 60) -> dict:
    """取五张图的全部曲线(按服务分组),给前端自己画。

    为什么按服务分组而不是只查一个:参考面板要回答的是"**谁**在变"——
    只画一条线看不出对比。选中的服务由前端高亮,其余画成淡色底噪。
    表达式与 Grafana 面板共用 metric_specs,两边口径不会分叉。
    """
    from app.diagnosis import detect

    target = await repository.get_target(target_id)
    if target is None:
        raise ValueError("接入平台不存在")
    prom_src = next((x for x in (target.sources or []) if x.get("type") == "prometheus"), None)
    if prom_src is None:
        raise ValueError("该平台没有接入 Prometheus")
    prom = {"url": str(prom_src["url"]).rstrip("/"), "headers": prom_src.get("headers")}
    info = await detect._discover(prom)
    if info is None:
        raise ValueError("这个 Prometheus 里没找到调用量指标族")
    families = await _available_families(prom)
    svc_label = info["service_label"]

    import time as _t
    end = _t.time()
    start = end - minutes * 60
    cards = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        for spec in metric_specs(info, families, None):
            r = await c.get(f"{prom['url']}/api/v1/query_range",
                            headers=prom.get("headers"),
                            params={"query": spec["expr"], "start": start,
                                    "end": end, "step": 60})
            series: list[dict] = []
            if r.status_code == 200:
                res = ((r.json() or {}).get("data") or {}).get("result") or []
                for item in res:
                    name = ((item.get("metric") or {}).get(svc_label)
                            or (item.get("metric") or {}).get("container_name")
                            or "?")
                    pts = [[float(t), float(v)] for t, v in item.get("values") or []]
                    if pts:
                        series.append({"name": name, "points": pts})
            series.sort(key=lambda x: x["name"])
            focus = next((x for x in series if x["name"] == service), None)
            cards.append({"key": spec["key"], "title": spec["title"],
                          "unit": spec["unit"], "desc": spec["desc"],
                          "series": series,
                          "focus": service,
                          "latest": focus["points"][-1][1] if focus and focus["points"] else None})
    return {"service": service, "target": target.name, "minutes": minutes,
            "cards": cards}


async def ensure_dashboard(target_id: int) -> dict:
    """确保该平台有一份能画的面板,返回 {uid, grafana_url, title, services}。

    重复调用是覆盖更新(同一 uid),不会堆面板。发现不到指标就抛错,不生成空面板。
    """
    from app.diagnosis import detect

    hit = _cache_get(f"dash:{target_id}", 600)
    if hit is not None:
        return hit

    target = await repository.get_target(target_id)
    if target is None:
        raise ValueError("接入平台不存在")
    prom = next((s for s in (target.sources or []) if s.get("type") == "prometheus"), None)
    if prom is None:
        raise ValueError("该平台没有接入 Prometheus,画不出面板")
    prom = {"url": str(prom["url"]).rstrip("/"), "headers": prom.get("headers")}

    info = await detect._discover(prom)
    if info is None:
        raise ValueError("这个 Prometheus 里没找到调用量指标族,画不出面板")
    families = await _available_families(prom)

    uid = f"opspaw-svc-{target_id}"
    dash = build_dashboard(info, target.name, uid, families, await _default_ds_uid())
    # 数据源 uid 走变量占位,避免把某个环境的数据源 uid 写死进面板
    await _api("POST", "/api/dashboards/db",
               {"dashboard": dash, "overwrite": True, "uid": uid})

    # 服务清单给前端下拉用:与诊断同一份来源,不另建
    from app.tools import service_catalog
    services, _err = await service_catalog.discover_services()
    names = sorted({s["service_id"] for s in services if s.get("target") == target.name})

    out = {"uid": uid, "title": dash["title"], "grafana_url": GRAFANA_URL,
           "services": names, "target": target.name, "target_id": target.id}
    _cache_set(f"dash:{target_id}", out)
    # 顺手把服务清单也塞进缓存:面板预热时已经算过一遍了,别让页面再等一次
    _cache_set(f"svcs:{target_id}", {"services": names, "target": target.name})
    return out
