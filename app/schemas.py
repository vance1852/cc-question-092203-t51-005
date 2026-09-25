"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .config import DEFAULT_TIMEZONE
from .tz_utils import get_timezone


def _validate_timezone(value: str) -> str:
    """校验 IANA 时区，非法时由 Pydantic 转为 422。"""
    try:
        return get_timezone(value).key
    except ValueError as exc:
        raise ValueError(str(exc))


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
    timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        description="站点所在地 IANA 时区，如 Asia/Shanghai、America/New_York",
    )
    slot_total: int = Field(0, ge=0)
    battery_ready: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")

    @field_validator("timezone")
    @classmethod
    def _tz_valid(cls, v: str) -> str:
        return _validate_timezone(v)


class StationCreate(StationBase):
    pass


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    timezone: Optional[str] = Field(None, description="IANA 时区，如 Asia/Shanghai")
    slot_total: Optional[int] = Field(None, ge=0)
    battery_ready: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")

    @field_validator("timezone")
    @classmethod
    def _tz_valid(cls, v: Optional[str]) -> Optional[str]:
        return _validate_timezone(v) if v is not None else v


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
    # 可选：换电发生时刻。naive 时间按站点本地时区解释；带偏移则按偏移精确
    # 换算（用于区分秋令时重复的本地时刻）；不传取服务端当前 UTC 时刻。
    swapped_at: Optional[datetime] = Field(None, description="换电时间，naive 按站点时区解释，建议携带时区偏移")


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    soc_before: float
    soc_after: float
    # 权威存储值：带 UTC 偏移的换电时刻
    swapped_at: datetime
    # 站点时区，以及该时刻在站点当地的墙钟时间（带当时偏移），便于按当地营业日复核
    station_timezone: str
    swapped_at_local: datetime
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 仪表盘 ----------
class DayInterval(BaseModel):
    """本地营业日对应的半开 UTC 区间。"""

    date: str = Field(..., description="营业日（站点本地日历日，YYYY-MM-DD）")
    timezone: str = Field(..., description="统计所用 IANA 时区")
    interval_start: datetime = Field(..., description="UTC 区间起点（含）")
    interval_end: datetime = Field(..., description="UTC 区间终点（不含）")


class StationDayCount(DayInterval):
    station_id: int
    station_name: str
    swap_count: int


class DashboardMeta(BaseModel):
    """统计口径说明。"""

    requested_date: Optional[str] = Field(
        None, description="查询指定的营业日；未指定时各站取其时区下的当前营业日"
    )
    timezone_basis: str = Field(
        "station_local",
        description="固定为 station_local：每座站点均按自身 IANA 时区把本地日期换算为半开 UTC 区间",
    )
    default_timezone: str = Field(..., description="平台默认时区（新建站点/老数据回退值）")


class DashboardStats(BaseModel):
    # 非时间类统计：语义与取值保持不变
    station_total: int
    station_running: int
    vehicle_total: int
    vehicle_fault: int
    battery_ready_total: int
    # 指定营业日的换电笔数：各站按自身时区的半开 UTC 区间 [start, end) 聚合后求和
    swap_today: int
    meta: DashboardMeta
    # 各站点的当地营业日、时区、半开 UTC 区间与笔数，可逐站按当地日期复核
    stations: list[StationDayCount]
