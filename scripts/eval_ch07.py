"""ch07 摘要 prompt 标注样例验证(纯 Prompt 任务以 eval 代 TDD)。需聊天上游可调通。
断言:严格 JSON(structured output 保证)、关键事实保留、无编造实体、长度达标、寒暄不留。
用法:uv run python -m scripts.eval_ch07"""
import asyncio
import re

from app.core.summarizer import summarize_dialog

# 样例1:事实保留——服务编号/工单号/处置诉求必须进摘要
CASE1_DIALOG = """用户:你好在吗
值班猫:您好,我是 OpsPaw 值班猫,请问有什么能帮您?
用户:服务 1001 一直报 5xx,工单号 T20260701001 一直没人跟
值班猫:已查到 1001 有 3 个实例 5xx 超阈值,预计 10 分钟内给结论。
用户:太慢了,我手机13800138000,有进展先打我电话
值班猫:好的,已在工单备注:有进展致电13800138000。
用户:对了 1001 的磁盘水位多少
值班猫:1001 当前磁盘使用率 62%,未达 85% 告警线。"""
CASE1_MUST = ["T20260701001", "13800138000", "1001"]
CASE1_BAN = ["在吗", "您好,我是 OpsPaw 值班猫"]          # 寒暄不留

# 样例2:无编造——摘要里的数字实体必须都在原文出现过
CASE2_DIALOG = """用户:2002 能重启吗
值班猫:2002 状态运行中、CPU 65% 未达阈值,窗口内可直接执行重启。
用户:那帮我重启,原因是内存持续上涨
值班猫:好的,已为您记录变更诉求:2002,原因「内存持续上涨」。"""

# 样例3:滚动合并——旧摘要里的事实不得丢
CASE3_OLD = "用户问过服务1001的5xx排查,工单T20260701001待跟进;问过磁盘水位(62%)。"
CASE3_DIALOG = """用户:1001 的日志里全是 OOM
值班猫:查到多次 oom_kill 事件,建议先 dump 堆转储再重启止血,您确认要发起变更申请吗?
用户:可以,先建单并通知二线跟进
值班猫:已登记服务1001的OOM故障,工单T20260701002等待二线接手。"""
CASE3_MUST = ["1001", "T20260701001", "T20260701002"]


def check(name: str, summary: str, must=(), ban=(), src_digits: str = "") -> bool:
    ok = True
    problems = []
    if not (20 <= len(summary) <= 250):
        ok, problems = False, problems + [f"长度{len(summary)}出界[20,250]"]
    for kw in must:
        if kw not in summary:
            ok, problems = False, problems + [f"关键事实丢失:{kw}"]
    for kw in ban:
        if kw in summary:
            ok, problems = False, problems + [f"寒暄残留:{kw}"]
    if src_digits:
        src_nums = set(re.findall(r"\d{4,}", src_digits))
        for n in set(re.findall(r"\d{4,}", summary)):
            if n not in src_nums:
                ok, problems = False, problems + [f"编造数字实体:{n}"]
    print(f"{'✅' if ok else '❌'} {name} len={len(summary)}")
    print(f"   摘要:{summary}")
    if problems:
        print(f"   问题:{problems}")
    return ok


async def main():
    results = []
    s1 = await summarize_dialog("", CASE1_DIALOG)
    results.append(check("样例1 事实保留+寒暄不留", s1, must=CASE1_MUST, ban=CASE1_BAN,
                         src_digits=CASE1_DIALOG))
    s2 = await summarize_dialog("", CASE2_DIALOG)
    results.append(check("样例2 无编造(数字实体⊆原文)", s2, must=["2002"],
                         src_digits=CASE2_DIALOG))
    s3 = await summarize_dialog(CASE3_OLD, CASE3_DIALOG)
    results.append(check("样例3 滚动合并旧事实不丢", s3, must=CASE3_MUST,
                         src_digits=CASE3_OLD + CASE3_DIALOG))
    print(f"\n{sum(results)}/{len(results)} 通过")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
