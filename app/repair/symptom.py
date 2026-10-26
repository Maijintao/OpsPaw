"""修复前症状复检:用触发诊断的同一套指标判据,确认问题还在不在。

为什么需要:预检查只回答"容器在不在跑",不回答"故障还在不在"。故障可能已经自己恢复
(流量退潮、上游修复、临时抖动),这时候再重启一次纯属添乱。这里拿近 2 小时的自身历史
分布做同一套分位判据——判据不变,只是把回看从 24h 缩到 2h(24h 在靶机上要 40 秒,
修复前卡这么久不可接受)。

三种结果:
- present  :仍有异常维 → 照常执行;
- resolved :各维都在自身历史范围内 → 不执行,计划落 symptom_resolved;
- unknown  :没配 Prometheus / 查不到画像 → 如实记"无法复检",由用户已确认的计划继续,
             但结果里必须写明"症状未复检",不能冒充已验证。
"""
import logging

from app.db import repository
from app.diagnosis import collect, detect

logger = logging.getLogger("app.repair")

CHECK_HOURS = 2


async def check(target_id: int, service: str) -> dict:
    target = await repository.get_target(target_id)
    if target is None:
        return {"status": "unknown", "reason": "接入平台不存在"}
    prom = next((s for s in (target.sources or []) if s.get("type") == "prometheus"), None)
    if prom is None:
        return {"status": "unknown", "reason": "平台没有配 Prometheus,无法复检症状"}

    collect.budget_start(20, 60)
    try:
        profiles = await detect.service_profiles(
            {"url": str(prom["url"]).rstrip("/"), "headers": prom.get("headers")},
            [service], hours=CHECK_HOURS)
    except Exception as exc:  # noqa: BLE001  复检失败不拦已确认的计划,但要如实记
        logger.warning("修复前症状复检失败 service=%s:%s", service, type(exc).__name__)
        return {"status": "unknown", "reason": f"指标复检失败({type(exc).__name__})"}
    finally:
        collect.budget_reset()

    prof = profiles.get(service)
    if not prof:
        return {"status": "unknown", "reason": "该服务没有可用的指标画像"}
    # 铁律:没数据 ≠ 健康。服务停机/零流量时四个指标全是 None(调用量缺省 0 但那不是
    # 数据),分位判据自然"无异常维",但那叫"看不见",不叫"症状已消失"
    # (实测 2026-10-19:cart 停机被误判 resolved,修复被拒)。err_rate=0.0 是
    # "有数据且零错误",与 None(根本没有这条序列)是两回事,不能混。
    if all(prof.get(k) is None for k in ("err_rate", "p95_ms", "cpu", "mem")):
        return {"status": "unknown",
                "reason": "近窗没有任何指标数据(服务可能已无流量或停机),无法断言症状消失"}
    evidence = {k: prof.get(k) for k in
                ("err_rate", "base_err_rate", "p95_ms", "base_p95_ms",
                 "calls", "base_calls", "traffic_collapse")}
    dims = prof.get("abnormal_dims") or []
    if not dims:
        return {"status": "resolved", "evidence": evidence,
                "reason": "错误率/时延/调用量/CPU/内存都在自身历史范围内,症状已消失"}
    return {"status": "present", "dims": dims, "evidence": evidence,
            "reason": "仍有异常维度:" + "、".join(dims)}
