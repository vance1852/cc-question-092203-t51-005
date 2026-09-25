"""仪表盘统计路由（需登录）。

换电量按"当地营业日"统计：把本地日期转换成半开 UTC 区间
[window_start_utc, window_end_utc) 再聚合，输出中说明统计时区与区间。
"""
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import DEFAULT_TIMEZONE
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import DashboardStats
from ..timeutil import current_local_date, local_date_to_utc_window, validate_timezone

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


@router.get("/stats", response_model=DashboardStats)
def stats(
    date: Optional[date_type] = Query(None, description="统计的当地营业日（YYYY-MM-DD），缺省为统计时区的当前营业日"),
    timezone: Optional[str] = Query(None, description="IANA 时区名（如 Asia/Shanghai），缺省为平台默认时区"),
    db: Session = Depends(get_db),
):
    tz_name = timezone or DEFAULT_TIMEZONE
    try:
        validate_timezone(tz_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    stat_date = date or current_local_date(tz_name)
    start_utc, end_utc = local_date_to_utc_window(stat_date, tz_name)
    return DashboardStats(
        station_total=db.query(Station).count(),
        station_running=db.query(Station).filter(Station.status == "running").count(),
        vehicle_total=db.query(Vehicle).count(),
        vehicle_fault=db.query(Vehicle).filter(Vehicle.status == "fault").count(),
        swap_today=db.query(SwapRecord)
        .filter(SwapRecord.swapped_at >= start_utc, SwapRecord.swapped_at < end_utc)
        .count(),
        battery_ready_total=db.query(func.coalesce(func.sum(Station.battery_ready), 0)).scalar() or 0,
        timezone=tz_name,
        date=stat_date,
        window_start_utc=start_utc,
        window_end_utc=end_utc,
    )
