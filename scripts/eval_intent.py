"""意图标注评估:九类判对率 + confidence 可解析 + 怪问题落其他 + 多轮漂移。需聊天上游可调通。
用法:.venv/bin/python -m scripts.eval_intent"""
import asyncio

from app.core.intent import classify
from app.core.intent import INTENTS

SAMPLES = [
    ("3003 的 CPU 曲线长什么样", "日志指标"), ("查一下 data-sync-service 昨晚的报错日志", "日志指标"),
    ("payment-service 现在什么状态", "服务状态"), ("1001 有几个实例在跑", "服务状态"),
    ("告警阈值是多少", "运维知识"), ("变更窗口是几点", "运维知识"),
    ("把 1001 这个服务回滚了吧", "变更申请"), ("3003 内存爆了帮我扩容一下", "变更申请"),
    ("服务挂了帮我看看", "故障处理"), ("网关一直超时,帮我排查下", "故障处理"),
    ("你们这值班系统什么玩意,我要投诉", "投诉"), ("太差了给我个说法", "投诉"),
    ("帮我建个工单", "建单跟进"), ("支付服务一直报错,建个工单跟进一下", "建单跟进"),
    ("你好呀", "闲聊"), ("今天天气不错", "闲聊"),
    ("帮我写一段 Python 代码", "其他"), ("阿斯顿发发", "其他"),
]


async def main():
    passed = bad_json = 0
    for q, expect in SAMPLES:
        r = await classify(q)
        got = r.get("intent")
        conf = r.get("confidence")
        ok = got == expect
        json_ok = got in INTENTS \
            and isinstance(conf, float) and 0.0 <= conf <= 1.0
        bad_json += not json_ok
        passed += ok
        print(f"{'✅' if ok else '❌'} {q!r} -> {got}(conf={conf}) 期望={expect}")

    # 多轮漂移:日志指标→变更申请→日志指标,当前句意图应随上下文
    hist = "用户:3003 昨晚有没有报错\n值班猫:查到 3 条 ERROR 日志,疑似内存泄漏\n用户:那帮我把它重启了吧\n值班猫:好的,已为你发起变更申请评估\n"
    r = await classify("那它现在 CPU 正常了吗", hist)
    print(f"多轮漂移『那它现在 CPU 正常了吗』(变更后)-> {r['intent']}(期望 日志指标)")

    print(f"\n判对 {passed}/{len(SAMPLES)};JSON 越界 {bad_json} 条(glm 非确定性,抖动如实重跑记录)")


if __name__ == "__main__":
    asyncio.run(main())
