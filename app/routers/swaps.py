"""换电记录路由（需登录）。"""
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import DEFAULT_TIMEZONE
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import SwapCreate, SwapOut
from ..timeutil import local_date_to_utc_window, validate_timezone

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


def _to_out(record: SwapRecord) -> SwapOut:
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=record.swapped_at,
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
    )


@router.get("", response_model=list[SwapOut])
def list_swaps(
    station_id: Optional[int] = Query(None, description="按站点过滤"),
    date: Optional[date_type] = Query(None, description="按当地营业日过滤（YYYY-MM-DD），时区取站点时区或 timezone 参数"),
    timezone: Optional[str] = Query(None, description="IANA 时区名，配合 date 使用；缺省优先取站点时区，否则用平台默认时区"),
    db: Session = Depends(get_db),
):
    query = db.query(SwapRecord)
    station = None
    if station_id is not None:
        station = db.get(Station, station_id)
        if not station:
            raise HTTPException(status_code=404, detail="换电站不存在")
        query = query.filter(SwapRecord.station_id == station_id)
    if timezone is not None:
        try:
            validate_timezone(timezone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if date is not None:
        # 按当地营业日复核：本地日期 -> 半开 UTC 区间，边界不重复、不漏计
        tz_name = timezone or (station.timezone if station else DEFAULT_TIMEZONE)
        start_utc, end_utc = local_date_to_utc_window(date, tz_name)
        query = query.filter(SwapRecord.swapped_at >= start_utc, SwapRecord.swapped_at < end_utc)
    records = query.order_by(SwapRecord.swapped_at.desc()).all()
    return [_to_out(r) for r in records]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(payload: SwapCreate, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, payload.vehicle_id)
    if not vehicle:
        raise HTTPException(status_code=404, detail="车辆不存在")
    station = db.get(Station, payload.station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    if station.battery_ready <= 0:
        raise HTTPException(status_code=422, detail="该换电站暂无满电电池可换")
    if payload.soc_after <= payload.soc_before:
        raise HTTPException(status_code=422, detail="换电后电量应高于换电前电量")

    record = SwapRecord(
        vehicle_id=payload.vehicle_id,
        station_id=payload.station_id,
        soc_before=payload.soc_before,
        soc_after=payload.soc_after,
        # 缺省为当前 UTC 时间；显式传入时 schema 已强制要求携带时区
        swapped_at=payload.swapped_at or datetime.now(timezone.utc),
    )
    # 换电后更新车辆电量、扣减站点可用电池
    vehicle.current_soc = payload.soc_after
    station.battery_ready -= 1
    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_out(record)
