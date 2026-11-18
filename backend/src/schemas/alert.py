from pydantic import BaseModel, Field


class AlertEntryInput(BaseModel):
    channel: str = Field(min_length=1, max_length=120)
    day: str = Field(min_length=8, max_length=10)          # ISO 日期 YYYY-MM-DD
    revision: int = Field(ge=0)
    computed_net: float
    recognized_net: float
    diff: float | None = None
    batch_id: str | None = None


class AlertBatchInput(BaseModel):
    batch_id: str = Field(min_length=2, max_length=80)
    source: str = Field(default='manual', min_length=2, max_length=120)
    entries: list[AlertEntryInput] = Field(min_length=1, max_length=5000)
