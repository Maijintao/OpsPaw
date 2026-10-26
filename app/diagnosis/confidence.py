"""结论置信表达(纯代码,LLM 不碰):三档标签 + 按类归拢的证据清单 + 采集盲点。

设计依据(2026-10-12 社区调研定稿):
- 不报数字置信度——48 模型临床横评证明 LLM 自报数字全线过度自信,且"边推理边报数"
  进一步失准(推理污染效应);Datadog/Dynatrace/HolmesGPT 对用户均不报百分比。
  分数(候选排序)只用于内部排序,不对结论承诺正确率。
- 档位只回答"撑住这个结论的证据本身靠不靠谱",采集能力缺口不扣档位、进盲点清单
  单独陈述——那是运维待办,不是这次的结论不可靠(否则某平台永远缺一类采集,
  该服务的结论就这辈子到不了高)。
- 措辞分级(HolmesGPT 三级证据分类的代码化):因果指向是直接证据(被错误 span 点名/
  自查有实锤/挂死形态)→ 肯定语气;靠旁证推断(时间先后+调用关系)→ 对冲语气"很可能"。

档位规则:
- 高:root_cause 过全部门槛(信号门槛+代码反证),样本足,结论依赖的数据没打折;
- 中:结论成立但依赖了打折的维度——样本不足统计下限(错误率/时延数字本身不可信),
  或结论靠资源偏离而资源基线是 T-2h 回退窗(可能含故障期);
- 中(critical_failure):关键故障已坐实,根因指向的证据强度未过硬指标
  (门禁/证伪自 2026-10-15 起**只记录不改判**,这个档位只来自归因员自己的判定);
- 低:没定位到根因(inconclusive/impact)。
"""
from app.diagnosis import attr_contract

TIER_TEXT = {"high": "高", "medium": "中", "low": "低"}
CATEGORY_TEXT = {"error": "报错指向", "resource": "资源用量", "latency": "时延",
                 "time": "时间先后", "topology": "调用关系", "shadow": "挂死形态"}
KIND_TEXT = {"direct": "直接证据", "inference": "推理链"}


def _entries(root: str, service: str, dev, pack: dict | None,
             sig: set[str]) -> list[dict]:
    """把撑住结论的事实按类归拢。kind 标注该条是直接观测还是推理——
    展示层据此让用户一眼看出哪些是实锤、哪些是推断。"""
    out: list[dict] = []

    def add(category: str, kind: str, fact: str) -> None:
        out.append({"category": category, "kind": kind, "fact": fact})

    if dev is None:
        # 影子服务:零遥测 + 被失败请求点名,挂死形态本身就是直接证据
        add("shadow", "direct", f"{root} 自身零遥测,被调用方的失败请求直接点名"
                                f"(服务整体挂死形态)")
        return out
    if "hang" in dev.abnormal_dims:
        # 挂死形态:此前正常出遥测、现在完全静默,本身就是直接证据
        add("shadow", "direct", f"{root} 自身遥测静默(此前正常出 span),典型挂死形态")
    if "error" in dev.abnormal_dims or "error" in sig:
        rate = f",错误率 {dev.error_rate:.1%}" if dev.calls else ""
        add("error", "direct", f"{root} 存在错误调用{rate}")
    if "resource" in sig and dev is not None:
        dims = "/".join(d for d in ("cpu", "mem") if d in dev.abnormal_dims)
        add("resource", "direct", f"{root} 的 {dims} 用量显著超出正常水平")
    if "latency" in dev.abnormal_dims:
        add("latency", "direct",
            f"{root} 时延显著异常(p95 {dev.latency_p95_us / 1000:.0f}ms)")
    if root != service:
        # 自查场景"与自己调用相连"不是证据,只对甩锅场景列调用关系
        if "topology" in sig:
            add("topology", "inference", f"{root} 与症状服务 {service} 在调用链路上相连")
        if "time" in sig:
            add("time", "inference", f"{root} 的异常早于症状服务 {service} 出现(果不先于因)")
    return out


def _blind_spots(data_quality: dict, log_result: dict, sparse: bool) -> list[str]:
    """采集缺口单列,不扣档位:平台没接的采集是运维待办;查询失败如实区分。"""
    out: list[str] = []
    q = data_quality or {}
    for label, key in (("CPU", "cpu"), ("内存", "memory")):
        st = q.get(key) or {}
        cur, base = st.get("current"), st.get("baseline")
        if cur == "no_data":
            out.append(f"{label}指标未采集(平台没有对应指标),资源类故障在这一维是盲的")
        elif cur == "query_failed":
            out.append(f"{label}指标本次查询失败,资源维度这单看不了")
        elif cur == "available" and base == "no_data":
            out.append(f"{label}有当前值但没有可比的历史水平,偏离幅度只能看个大概")
        elif cur == "available" and st.get("baseline_window") == "t2h":
            out.append(f"{label}的历史水平取自 2 小时前的回退窗(可能含故障期),"
                       f"偏离比值仅供参考")
    if sparse:
        out.append("调用记录样本不足统计下限(20 条),错误率/时延数字仅供参考")
    lg = (log_result or {}).get("status")
    if lg == "no_loki_source":
        out.append("该平台未接入日志采集,报错的具体文本这一维是盲的")
    elif lg == "query_failed":
        out.append("日志本次查询失败,报错文本只能靠调用记录推断")
    return out


def _tone(root: str | None, service: str, dev, pack: dict | None) -> str:
    """措辞分级:因果指向是直接证据(被点名/自查/挂死形态)→ definitive;
    靠旁证(时间先后+调用关系)推断因果 → hedged。"""
    if root is None:
        return "hedged"
    if root == service or dev is None or (pack or {}).get("direction") == "error_ref":
        return "definitive"
    return "hedged"


def assess(*, attr, service: str, devs: dict, candidates: list[dict],
           edges: set, window_start_us: int | None,
           data_quality: dict, log_result: dict, sparse: bool) -> dict:
    """诊断终态的置信表达。全部字段代码可算、可复现、零 token;
    LLM 的定性说明在 attribution.confidence_note,这里不重复它。"""
    verdict = attr.verdict
    root = attr.root_cause_service
    blind = _blind_spots(data_quality, log_result, sparse)

    if verdict in ("inconclusive", "impact"):
        return {"tier": "low", "tone": "hedged",
                "summary": ("证据不足以定位根因,缺口见证据缺口清单" if verdict == "inconclusive"
                            else "症状服务只是被波及,证据里没有根因"),
                "evidence": [], "blind_spots": blind}

    dev = devs.get(root) if root else None
    pack = next((c for c in candidates if c["service"] == root), None) if root else None
    sig = attr_contract.support_signals(root or "", service, candidates, devs, edges,
                                      window_start_us) if root else set()
    entries = _entries(root, service, dev, pack, sig) if root else []

    if verdict == "critical_failure":
        return {"tier": "medium", "tone": "hedged",
                "summary": (f"关键故障 {root} 已坐实,但根因指向的证据强度未过硬指标——"
                            "按指认对象优先排查,证据见清单"),
                "evidence": entries, "blind_spots": blind}

    # root_cause:档位看"结论依赖的维度有没有打折",不看平台采集全不全
    material = []
    if sparse:
        material.append("调用样本不足统计下限,错误率/时延数字不可信")
    if dev is not None and ("cpu" in dev.abnormal_dims or "mem" in dev.abnormal_dims) \
            and dev.res_baseline == "t2h":
        material.append("结论依赖的资源偏离用的是回退基线(可能含故障期)")
    tone = _tone(root, service, dev, pack)
    tier = "medium" if material else "high"
    if tone == "definitive":
        summary = "结论有直接证据支撑(报错指向/实锤形态),全部代码检验通过"
    else:
        summary = ("结论主要靠推理链(时间先后+调用关系)支撑,无直接实锤——"
                   "按指向优先排查,建议人工确认")
    if material:
        summary += ";注意:" + "、".join(material)
    # 因果分层要进摘要:否则用户只看到一个首因,看不出"谁是果、谁无关"
    amps = getattr(attr, "amplifiers", None) or []
    unrel = getattr(attr, "unrelated", None) or []
    if amps:
        summary += f";次生放大:{'、'.join(amps)}"
    if unrel:
        summary += f";与本症状无因果通路的干扰:{'、'.join(unrel)}"
    return {"tier": tier, "tone": tone, "summary": summary,
            "evidence": entries, "blind_spots": blind}
