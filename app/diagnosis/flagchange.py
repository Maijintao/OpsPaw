"""功能开关变更探测(免费信号通道,2026-10-24 实证后新增)。

原理:按 OTel feature_flag 语义上报的求值遥测,标签带三样东西——开关名
(feature_flag_key)、求值出的取值(feature_flag_result_variant)、消费该开关的
服务(service_name)。开关一翻,就多出一条"新取值"的时间序列:序列的出生时刻
就是变更时刻,service_name 就是变更落点。实测(2026-10-24 跑批注入 cartFailure
期间):Prometheus 里直接可见 cartFailure|100%|cart。

判据是离散事件("标签集合里出现了基线没有的取值"),不是数值判定——零阈值。
基线比对剔除已知故障窗(复用 detect._incident_windows):跑批昨天注入过同款
开关,不剔除的话"历史里见过"会把今天的翻转变判成"没变"(与指标基线洗刷同一条
道理)。

按标签发现序列、不认指标名:旧版 flagd(feature_flag_evaluation_*)、新版
(feature_flag.flagd.impression)与任何按语义规范上报的环境通吃。**哪些服务的
SDK 真的会上报这组指标因栈而异**(实测本靶机只有 cart 上报)——探测不到就如实
说没有,不猜;这是增强信号,覆盖不到的场景由边级错误归属(edgeview)兜着。

已知局限(如实):慢性配置(几天前就翻了、基线已把它当常态)探测不到——
变更信号只对"新鲜变更"负责。
"""
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("app")

FLAG_KEY_LABEL = "feature_flag_key"
FLAG_VARIANT_LABEL = "feature_flag_result_variant"
# 求值方身份标签按 detect 同一套口径发现(service_name/service/service_id)


@dataclass
class FlagChange:
    service: str                      # 消费该开关的服务(变更落点)
    flag: str                         # 开关名
    variant: str                      # 新出现的取值
    prev_variants: tuple[str, ...]    # 基线窗(剔除故障期)出现过的取值
    onset_us: int                     # 新序列首样本时刻(微秒)


def _parse(rows: list[dict], window_start: float, window_end: float,
           incidents: list[tuple[float, float]]) -> list[FlagChange]:
    """把一次 query_range 的结果解析成变更列表(纯函数,单测在这里)。

    rows: PromQL `{feature_flag_key!=""}` 的 query_range 结果(series 含 values)。
    规则:某 (服务, 开关, 取值) 在当前窗有样本、且在"窗前历史且不在已知故障窗内"
    从未出现过 → 变更。当前窗内的旧取值样本(切换瞬间的过渡)不算新。
    """
    samples: dict[tuple[str, str], dict[str, list[float]]] = {}
    for s in rows:
        m = s.get("metric") or {}
        flag, variant = m.get(FLAG_KEY_LABEL), m.get(FLAG_VARIANT_LABEL)
        if not flag or variant is None:
            continue
        svc = next((m[k] for k in ("service_name", "service", "service_id") if m.get(k)), "")
        if not svc:
            continue    # 落不到服务身上,定位层用不上
        bucket = samples.setdefault((svc, flag), {}).setdefault(variant, [])
        bucket.extend(float(v[0]) for v in (s.get("values") or []))
    from app.diagnosis.detect import _excluded
    out: list[FlagChange] = []
    for (svc, flag), variants in sorted(samples.items()):
        cur = {v for v, ts in variants.items()
               if any(window_start <= t <= window_end for t in ts)}
        base = {v for v, ts in variants.items()
                if any(t < window_start and not _excluded(t, incidents) for t in ts)}
        for v in sorted(cur - base):
            onset = min(t for t in variants[v] if window_start <= t <= window_end)
            out.append(FlagChange(service=svc, flag=flag, variant=v,
                                  prev_variants=tuple(sorted(base)),
                                  onset_us=int(onset * 1e6)))
    return out


async def detect(prom: dict, window_start: float, window_end: float,
                 now: float | None = None) -> tuple[list[FlagChange], str | None]:
    """探测当前诊断窗内"新出现"的开关取值。返回 (变更列表, 说明或 None)。

    增强信号,任何失败都不抛:拿不到就返回空列表 + 一句如实说明。
    """
    from app.diagnosis import collect, detect as _detect
    now = now if now is not None else time.time()
    collect._spend()
    try:
        body = await _detect._get_json(
            f"{prom['url']}/api/v1/query_range",
            params={"query": f'{{{FLAG_KEY_LABEL}!=""}}',
                    "start": now - _detect.HISTORY_HOURS * 3600,
                    "end": now, "step": _detect.HISTORY_STEP_S},
            headers=prom.get("headers"), timeout=_detect.RANGE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return [], f"开关求值序列查询失败({type(exc).__name__})"
    rows = ((body or {}).get("data") or {}).get("result") or []
    if not rows:
        return [], "无开关求值遥测(未采集 flagd/feature_flag 指标或无服务上报)"
    try:
        incidents = await _detect._incident_windows(now)
    except Exception:  # noqa: BLE001
        incidents = []
    changes = _parse(rows, window_start, window_end, incidents)
    if changes:
        return changes, None
    # 以前这里返回 ([], None):查到了遥测、但一条变更都没解析出来,却不说明原因,
    # 从外面看就是"这个通道不存在"。2026-10-26 排查发现两个常见原因:
    #   ① 命中的指标族没有 feature_flag_result_variant 标签(本靶机的
    #      feature_flag_evaluation_requests_total / active_count 都没有,
    #      只有 success_total 有);
    #   ② 同款开关历史上翻过,取值已在基线里(跑批每天翻一次就会这样)。
    _with_variant = sum(1 for s in rows
                        if (s.get("metric") or {}).get(FLAG_VARIANT_LABEL) is not None)
    if not _with_variant:
        return [], ("命中了开关求值遥测,但没有任何序列带 "
                    f"{FLAG_VARIANT_LABEL} 标签——这个环境的指标族报不出取值,"
                    "无法判断开关翻没翻")
    return [], (f"有 {_with_variant} 条带取值的开关序列,但当前窗内没有基线里没见过的新取值"
                "(同款开关历史上翻过,取值已进基线)")
