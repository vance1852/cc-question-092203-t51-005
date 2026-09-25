"""仪表盘统计路由（需登录）。

换电量按 **站点当地营业日** 核对：把站点 IANA 时区下的本地日期换算成半开
UTC 区间 ``[start, end)`` 后再过滤聚合。区间左闭右开保证边界时刻（本地零点，
对应记录的 UTC 瞬间）只计入一天；夏令时切换当天区间自动为 23/25 小时，
不重不漏。未来时刻的记录只要落在所查营业日区间内同样计入（按时间戳归属
判定，跨日查询时每条记录恰好出现一次）。
"""
from datetime import date as date_cls

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import DEFAULT_TIMEZONE
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import (
    DashboardMeta,
    DashboardStats,
    DayInterval,
    StationDayCount,
)
from ..tz_utils import current_local_date, get_timezone, local_day_to_utc_range, to_aware_utc

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


@router.get("/stats", response_model=DashboardStats)
def stats(
    db: Session = Depends(get_db),
    date: date_cls | None = Query(
        None,
        alias="date",
        description="营业日 YYYY-MM-DD（站点本地日历日）；不传则各站取其时区下的当前营业日",
    ),
):
    stations = db.query(Station).order_by(Station.id).all()

    station_rows: list[StationDayCount] = []
    swap_total = 0
    for station in stations:
        tz = get_timezone(station.timezone)
        # 指定日期时各站统一使用该本地日历日；否则取该站点当前营业日。
        day = date or current_local_date(tz)
        start_naive, end_naive = local_day_to_utc_range(day, tz)
        count = (
            db.query(func.count(SwapRecord.id))
            .filter(
                SwapRecord.station_id == station.id,
                SwapRecord.swapped_at >= start_naive,
                SwapRecord.swapped_at < end_naive,  # 半开区间：右端点计入次日
            )
            .scalar()
            or 0
        )
        swap_total += int(count)
        window = DayInterval(
            date=day.isoformat(),
            timezone=tz.key,
            interval_start=to_aware_utc(start_naive),
            interval_end=to_aware_utc(end_naive),
        )
        station_rows.append(
            StationDayCount(
                **window.model_dump(),
                station_id=station.id,
                station_name=station.name,
                swap_count=int(count),
            )
        )

    return DashboardStats(
        # 以下为非时间类统计，口径与取值保持不变
        station_total=db.query(Station).count(),
        station_running=db.query(Station).filter(Station.status == "running").count(),
        vehicle_total=db.query(Vehicle).count(),
        vehicle_fault=db.query(Vehicle).filter(Vehicle.status == "fault").count(),
        battery_ready_total=db.query(func.coalesce(func.sum(Station.battery_ready), 0)).scalar() or 0,
        swap_today=swap_total,
        meta=DashboardMeta(
            requested_date=date.isoformat() if date else None,
            timezone_basis="station_local",
            default_timezone=DEFAULT_TIMEZONE,
        ),
        stations=station_rows,
    )
