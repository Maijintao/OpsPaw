"""诊断发起工具:Agent 判断用户在问根因(为什么错/什么坏了)时调用。

只负责发起,不负责诊断——管线跑在独立任务里,结论走前端卡片,不进聊天逐条播报。
这是「诊断不是聊天层」的工具侧表达:聊天图里没有诊断循环。
"""
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.tools import registry


@tool
async def start_diagnosis(
    service: Annotated[str, Field(description="要诊断的服务名,以服务目录(query_catalog)查到的写法为准。用户用代词(他/它/该服务/刚才那个)指代上文已出现的服务时,直接取上文服务名。用户说不清是哪个服务、只知道现象(如「下单失败」「平台好像挂了」「反馈有问题」)时留空——系统会先自动扫描平台找异常服务,不要反过来要求用户先报出服务名")] = "",
    symptom: Annotated[str, Field(description="症状描述,例如 错误率升高、CPU 过高、响应缓慢、下单失败;说不清就留空")] = "",
) -> dict:
    """发起根因分析任务(确定性归因管线)。当用户想知道服务为什么异常/根因是什么、
    且用查询类工具看不出明确答案时调用。**用户说不清具体是哪个服务时也要调用**——
    service 留空即可,系统会先扫描平台找最可疑的服务再归因;绝不要反过来要求用户
    先自己定位到服务(运维本来就是因为不知道才来问的)。任务后台异步执行,结论会在前端
    诊断卡片里展示(含异常拓扑与证据链),不要在对话里逐条复述分析过程,也不要编造结论;
    只需告知用户分析已发起,完成后看卡片。"""
    from app.diagnosis import runner
    svc = (service or "").strip()
    diag_id = runner.start(svc, (symptom or "").strip())
    return {"diagnosis_id": diag_id, "service": svc or "(自动扫描)", "status": "running",
            "note": "诊断任务已发起,结论由前端卡片展示;不要在对话里编造或转述过程"}


registry.register(registry.spec_from_langchain_tool(start_diagnosis, source="builtin"))
