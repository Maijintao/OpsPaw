from typing import Literal

from pydantic import BaseModel, Field


class CreateTicketRequest(BaseModel):
    conversation_id: int
    description: str = Field(min_length=1)
    ticket_type: Literal["故障", "变更", "咨询"]


class CreateTicketResponse(BaseModel):
    ticket_no: str
    status: str = "已转人工"


class CreateChangeRequest(BaseModel):
    conversation_id: int
    service_id: str = Field(min_length=1)
    change_type: Literal["重启服务", "扩容实例", "回滚版本", "其他"]


class CreateChangeResponse(BaseModel):
    ticket_no: str
    status: str = "变更申请已提交"


class ResumeRequest(BaseModel):
    conversation_id: int
    service_id: str | None = Field(default=None, min_length=1)   # ch06 服务选择器点选
    confirmed: bool | None = None                                # ch08 工单预览 确认/取消
