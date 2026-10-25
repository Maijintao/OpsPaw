from enum import Enum

from pydantic import BaseModel, Field, field_validator


class ExtractRequest(BaseModel):
    text: str = Field(min_length=1, description="值班同学对故障/诉求的描述原文")


class RequestType(str, Enum):
    RESTART = "重启"
    ROLLBACK = "回滚"
    SCALE = "扩容"
    COMPLAINT = "投诉"
    OTHER = "其他"


class OpsTicket(BaseModel):
    """从值班同学的描述中提取的结构化工单。"""

    service_id: str | None = Field(
        default=None, description="服务编号,原文未出现则为 null,禁止编造"
    )
    request_type: RequestType = Field(description="诉求类型")
    expected_action: str = Field(description="用户期望的处理动作,一句话概括")

    @field_validator("service_id", mode="before")
    @classmethod
    def _normalize_missing_service_id(cls, v: object) -> object:
        # 模型偶尔把"没有服务编号"表达成占位字符串而非省略参数,统一归一为 None
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "n/a", "无"}:
            return None
        return v
