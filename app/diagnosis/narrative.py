"""诊断开方(管线终点,归因/门禁/证伪/置信之后):把最终结论翻译成给人看的
根因说明——像医生开方,分四个字段输出:描述/根因/证据原因/给修复 agent 的交代。
纯语言生成,不产生新事实,所有内容必须来自管线已有结论。

定位:这段输出同时是后续修复 agent 的信息传递载体——分字段是为了下游好消费:
修复 agent 拿到卡片就能按字段取用(根因是什么、依据什么、要注意什么),
人看详情页也是一栏一栏读,不是一坨文字。
预算纪律说明:归因 ≤2 次 + 开方 1 次 = 3 次,SOP 上限 3 次以内;开方放在
门禁**之后**是刻意的——门禁可能把 root_cause 降成 critical_failure,放在归因里
一起出的话,开方内容可能和最终结论对不上。(对抗证伪自 2026-10-15 起只记录不改判,
不再影响终态。)
失败不炸管线:LLM 挂了就用确定性兜底拼一份,结论字段本身不受影响。
"""
import logging

from pydantic import BaseModel, Field

from app.core.llm import structured

logger = logging.getLogger("app")

_VERDICT_TEXT = {
    "root_cause": "定位到根因服务 {s}",
    "critical_failure": "关键故障 {s},根因服务本身无法进一步解释",
    "impact": "症状服务只是被波及,证据里没有找到根因",
    "inconclusive": "证据不足,未能定位根因",
    # 三种"没有根因"分开说,免得值班员把"平台正常"和"看不到"当成一回事
    "clear": "全平台各维都在自身历史范围内,未发现异常",
    "victims_only": "有异常,但都指向被下游带慢的受害者,指不出源头",
    "no_data": "数据不可用,无法判断有没有故障",
}


class Prescription(BaseModel):
    """开方输出:分字段的根因说明,人和修复 agent 各取所需。"""
    description: str = Field(description="症状与结论概览:发生了什么、最终判定是什么")
    fault: str = Field(description="病根:根因服务及故障形态;未定位到就如实说现状")
    reason: str = Field(description="证据原因:为什么这么判,把关键证据翻译成大白话")
    other: str = Field(default="", description="给修复 agent 的交接注意事项,没有就留空")


def _fallback(service: str, symptom: str, result: dict) -> dict:
    """LLM 不可用时的确定性兜底:结论字段原样拼,不编一个字。"""
    verdict = result.get("verdict", "inconclusive")
    root = result.get("root_cause_service")
    head = _VERDICT_TEXT.get(verdict, verdict).format(s=root or "")
    description = f"服务 {service} 异常({symptom or '巡检发现异常'})。诊断结论:{head}。"
    fault_type = (result.get("fault_type") or {}).get("text")
    if root:
        fault = (f"{root}:{'被多条失败调用指认且自身无遥测' if result.get('chain') else '详见证据链'}"
                 + (f";形态:{fault_type}" if fault_type else ""))
    else:
        fault = "未定位到根因服务,详见证据缺口"
    # 因果分层照搬进兜底:次生放大与无关干扰要跟首因分开说,别混成"一堆都坏了"
    amps = result.get("amplifiers") or []
    unrel = result.get("unrelated") or []
    if amps:
        fault += f";次生放大:{'、'.join(amps)}(被首因带起来,不是并列首因)"
    if unrel:
        fault += f";与本症状无因果通路的干扰:{'、'.join(unrel)}"
    reason = ";".join(result.get("chain") or []) or "证据链为空,详见详情页数据质量"
    notes = []
    if result.get("missing"):
        notes.append("证据缺口:" + ";".join(result["missing"][:2]))
    if result.get("suggested_actions"):
        notes.append("建议动作:" + ";".join(result["suggested_actions"]))
    return {"description": description, "fault": fault,
            "reason": reason, "other": "。".join(notes)}


async def prescribe(service: str, symptom: str, result: dict) -> dict:
    """按最终诊断结论开方。输入是 result 终稿(verdict/root/chain/confidence/
    suggested_actions/missing 都已定稿),输出分字段的根因说明。"""
    conf = result.get("confidence") or {}
    prompt = (
        "你是值班 SRE 医生,诊断管线刚跑完,现在给这次故障\"开方\"。"
        "像医生向病人解释病情一样,但按下面四个字段组织,给后续修复 agent 当任务输入。\n"
        f"症状服务:{service};用户报告的症状:{symptom or '未描述(系统自主巡检发现异常)'}。\n"
        f"最终结论:{_VERDICT_TEXT.get(result.get('verdict', ''), result.get('verdict', ''))}"
        f"{',根因服务:' + result['root_cause_service'] if result.get('root_cause_service') else ''}。\n"
        f"故障形态(代码判定,照它写、不要自己另判一个):"
        f"{(result.get('fault_type') or {}).get('text', '未定性')}"
        f";判据:{'、'.join((result.get('fault_type') or {}).get('basis') or []) or '无'}\n"
        f"次生放大(被首因带起来的服务,不是并列首因):"
        f"{'、'.join(result.get('amplifiers') or []) or '无'}\n"
        f"无关干扰(有异常但与本症状没有因果通路):"
        f"{'、'.join(result.get('unrelated') or []) or '无'}\n"
        "证据链(每条以 [E#] 引用编号):\n" +
        "".join(f"- {c}\n" for c in (result.get("chain") or [])) +
        f"证据充分度:{conf.get('tier', 'unknown')},{conf.get('summary', '')}\n"
        f"修复入手方向:{';'.join(result.get('suggested_actions') or []) or '无'}\n"
        f"证据缺口:{';'.join(result.get('missing') or []) or '无'}\n"
        "字段要求(全部大白话,不堆术语,不带 [E#] 编号,不编造证据外的事实,"
        "不把\"怀疑\"说成\"确认\"):\n"
        "1. description:两三句说清\"发生了什么、最终判定是什么\";\n"
        "2. fault:病根是什么服务、什么形态——形态直接用上面「故障形态」那个字段给的结果,"
        "不要自己另判一个;没定位到根因就如实写\"未定位到根因\"并概括现状;\n"
        "3. reason:为什么这么判——把证据链里的关键事实翻译成大白话,讲清因果:谁先坏、"
        "谁是被它带起来的、谁其实跟本案无关;有次生放大或无关干扰时必须分开说清,"
        "不要把一串异常服务混成一句「都坏了」。被降级或证据不足的如实说\"目前能确定什么、"
        "还差什么证据\";\n"
        "4. other:给修复 agent 的交接注意事项——执行修复时要注意什么(先查什么/"
        "有什么风险/别碰什么),证据缺口里与修复相关的要求里点出来;实在没有就留空字符串。\n"
        "每个字段 1-3 句,不要分点编号,不要客套开头结尾。")
    try:
        out = await structured(Prescription, temperature=0).ainvoke(prompt)
        if (out.description or "").strip() or (out.fault or "").strip():
            return {"description": (out.description or "").strip(),
                    "fault": (out.fault or "").strip(),
                    "reason": (out.reason or "").strip(),
                    "other": (out.other or "").strip()}
        logger.warning("开方输出字段全空,用确定性兜底")
    except Exception as e:  # noqa: BLE001  开方是增强,不能反过来弄死诊断终态
        logger.warning("开方 LLM 调用失败(%s: %s),用确定性兜底",
                       type(e).__name__, e)
    return _fallback(service, symptom, result)
