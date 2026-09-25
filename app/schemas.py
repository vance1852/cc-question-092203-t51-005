"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .timeutil import validate_timezone


def _check_iana_timezone(v: Optional[str]) -> Optional[str]:
    """站点时区校验：None 放行（更新场景），其余必须是合法 IANA 名称。"""
    if v is None:
        return v
    try:
        return validate_timezone(v)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str

    model_config = {"from_attributes": True}


# ---------- 换电站 ----------
class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    slot_total: int = Field(0, ge=0)
    battery_ready: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")
    # 站点所在地 IANA 时区，营业日按此切分
    timezone: str = "Asia/Shanghai"

    _check_timezone = field_validator("timezone")(_check_iana_timezone)


class StationCreate(StationBase):
    pass


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    slot_total: Optional[int] = Field(None, ge=0)
    battery_ready: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")
    timezone: Optional[str] = None

    _check_timezone = field_validator("timezone")(_check_iana_timezone)


class StationOut(StationBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 车辆 ----------
class VehicleBase(BaseModel):
    plate: str = Field(..., min_length=1, max_length=32)
    model: str = ""
    battery_capacity: float = Field(100.0, gt=0)
    current_soc: float = Field(100.0, ge=0, le=100)
    status: str = Field("idle", pattern="^(idle|running|charging|fault)$")


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    plate: Optional[str] = Field(None, min_length=1, max_length=32)
    model: Optional[str] = None
    battery_capacity: Optional[float] = Field(None, gt=0)
    current_soc: Optional[float] = Field(None, ge=0, le=100)
    status: Optional[str] = Field(None, pattern="^(idle|running|charging|fault)$")


class VehicleOut(VehicleBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 换电记录 ----------
class SwapCreate(BaseModel):
    vehicle_id: int
    station_id: int
    soc_before: float = Field(..., ge=0, le=100)
    soc_after: float = Field(100.0, ge=0, le=100)
    # 可选：补录换电时刻（ISO 8601，必须带时区偏移）；缺省为服务器当前 UTC 时间
    swapped_at: Optional[datetime] = None

    @field_validator("swapped_at")
    @classmethod
    def _require_timezone(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            raise ValueError("swapped_at 必须携带时区偏移（如 2026-09-25T08:00:00+08:00）")
        return v


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    soc_before: float
    soc_after: float
    # 换电完成时刻（UTC，带时区）
    swapped_at: datetime
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 仪表盘 ----------
class DashboardStats(BaseModel):
    station_total: int
    station_running: int
    vehicle_total: int
    vehicle_fault: int
    # 统计日期（当地营业日）内的换电次数
    swap_today: int
    battery_ready_total: int
    # 统计口径说明：IANA 时区、当地营业日、半开 UTC 区间 [window_start_utc, window_end_utc)
    timezone: str
    date: date
    window_start_utc: datetime
    window_end_utc: datetime
