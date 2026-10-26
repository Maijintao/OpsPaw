"""自动修复准入:哪些动作可以不问人直接做。

原则不是"保证修好"(没人能保证),而是"影响面被限制在一个服务、失败会停、事后能查":

- 只自动 `restart_service` / `start_service` / `set_flag`;`stop_service` / `restore_flags`
  永远不自动(那是主动制造影响面的动作);`truncate_service_logs` 不可逆,也要人点头;
- set_flag 敢自动的原因:写前的旧值记进审计,改错了再切回去就行——天然可回滚;
- 只对"重启可能有效"的故障形态自动:pod_failure / code_error / resource_stress;
  网络时延、丢包、配置、认证这类重启治不好的,自动执行只会白折腾 + 制造一次中断;
- 证据档位不能是 low;
- 执行前症状必须还在(present);unknown 不自动(无法确认就不冒险);
- 连接器在线且声明支持该动作(由 create 校验)。

不满足就走人工确认。判断全部是确定性的,不经过模型。
"""
from app.repair import catalog

AUTO_ACTIONS = {"restart_service", "start_service", "set_flag"}
AUTO_FAULTS = {"pod_failure", "code_error", "resource_stress"}
AUTO_TIERS = {"high", "medium"}


def evaluate(action: str, verdict: str | None, fault_type: str | None,
             confidence_tier: str | None, symptom_status: str) -> tuple[bool, list[str]]:
    """返回 (是否可自动执行, 不满足的原因)。原因直接给用户看。"""
    reasons: list[str] = []
    if action not in AUTO_ACTIONS:
        reasons.append(f"「{catalog.label(action)}」不在自动执行白名单")
    if verdict not in ("root_cause", "critical_failure"):
        reasons.append("诊断结论不是根因")
    if (fault_type or "unknown") not in AUTO_FAULTS:
        reasons.append(f"故障形态是「{fault_type or '未定性'}」,重启这类动作未必有效")
    if (confidence_tier or "low") not in AUTO_TIERS:
        reasons.append("证据档位偏低,需要人工确认")
    if symptom_status not in ("present", "skipped"):
        reasons.append("执行前无法确认症状仍在")
    return (not reasons, reasons)
