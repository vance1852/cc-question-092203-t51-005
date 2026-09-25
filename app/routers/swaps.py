"""换电记录路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Station, SwapRecord, Vehicle
from ..schemas import SwapCreate, SwapOut
from ..tz_utils import InvalidLocalTime, get_timezone, resolve_reported_time, to_aware_utc

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


def _to_out(record: SwapRecord) -> SwapOut:
    tz = get_timezone(record.station.timezone)
    swapped_utc = to_aware_utc(record.swapped_at)
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=swapped_utc,
        station_timezone=tz.key,
        swapped_at_local=swapped_utc.astimezone(tz),
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
    )


@router.get("", response_model=list[SwapOut])
def list_swaps(db: Session = Depends(get_db)):
    records = db.query(SwapRecord).order_by(SwapRecord.swapped_at.desc()).all()
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

    station_tz = get_timezone(station.timezone)
    # 统一存储语义：naive 上报按站点本地时区解释，最终落库为 naive UTC。
    try:
        swapped_utc_naive = resolve_reported_time(payload.swapped_at, station_tz)
    except InvalidLocalTime as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    record = SwapRecord(
        vehicle_id=payload.vehicle_id,
        station_id=payload.station_id,
        soc_before=payload.soc_before,
        soc_after=payload.soc_after,
        swapped_at=swapped_utc_naive,
    )
    # 换电后更新车辆电量、扣减站点可用电池
    vehicle.current_soc = payload.soc_after
    station.battery_ready -= 1
    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_out(record)
