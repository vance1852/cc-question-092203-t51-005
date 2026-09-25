"""首次启动时初始化数据库：建表 + 内置管理员 + 种子业务数据。"""
from datetime import timedelta

from sqlalchemy.orm import Session

from .auth import hash_password
from .config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from .database import Base, SessionLocal, engine
from .migrations import run_migrations
from .models import Station, SwapRecord, User, Vehicle
from .tz_utils import utc_now_naive


def init_db() -> None:
    """创建所有表、执行兼容迁移并灌入种子数据（幂等：已存在则跳过）。"""
    Base.metadata.create_all(bind=engine)
    run_migrations()
    db: Session = SessionLocal()
    try:
        _seed_admin(db)
        _seed_business(db)
        db.commit()
    finally:
        db.close()


def _seed_admin(db: Session) -> None:
    if db.query(User).filter(User.username == DEFAULT_ADMIN_USERNAME).first():
        return
    db.add(
        User(
            username=DEFAULT_ADMIN_USERNAME,
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            display_name="平台管理员",
        )
    )


def _seed_business(db: Session) -> None:
    if db.query(Station).count() > 0:
        return

    # 站点分布在不同时区，其中纽约站实行夏令时，用于验证营业日跨零点与
    # DST 跳变的统计口径。
    stations = [
        Station(name="城东物流园换电站", address="城东大道 128 号", timezone="Asia/Shanghai",
                slot_total=20, battery_ready=14, status="running"),
        Station(name="临港枢纽换电站", address="临港四路 9 号", timezone="Asia/Shanghai",
                slot_total=16, battery_ready=11, status="running"),
        Station(name="北郊配送中心换电站", address="北环高速出口 3 公里", timezone="Asia/Shanghai",
                slot_total=12, battery_ready=4, status="maintenance"),
        Station(name="纽约港保税区换电站", address="Port Jersey Blvd 1", timezone="America/New_York",
                slot_total=24, battery_ready=20, status="running"),
    ]
    db.add_all(stations)
    db.flush()

    vehicles = [
        Vehicle(plate="沪EV1234", model="远程星瀚 H", battery_capacity=141.0, current_soc=82.0, status="running"),
        Vehicle(plate="沪EV5678", model="比亚迪 T5", battery_capacity=100.0, current_soc=23.0, status="charging"),
        Vehicle(plate="苏EV9012", model="江淮恺达 EX8", battery_capacity=120.0, current_soc=56.0, status="idle"),
        Vehicle(plate="浙EV3456", model="开瑞优优 EV", battery_capacity=42.0, current_soc=9.0, status="fault"),
        Vehicle(plate="沪EV7788", model="远程星智 G", battery_capacity=160.0, current_soc=95.0, status="running"),
    ]
    db.add_all(vehicles)
    db.flush()

    # swapped_at 一律 naive UTC。
    now = utc_now_naive()
    swaps = [
        SwapRecord(vehicle_id=vehicles[0].id, station_id=stations[0].id, soc_before=12.0, soc_after=100.0, swapped_at=now - timedelta(hours=2)),
        SwapRecord(vehicle_id=vehicles[1].id, station_id=stations[1].id, soc_before=8.0, soc_after=98.0, swapped_at=now - timedelta(hours=5)),
        SwapRecord(vehicle_id=vehicles[2].id, station_id=stations[0].id, soc_before=15.0, soc_after=100.0, swapped_at=now - timedelta(days=1, hours=1)),
        SwapRecord(vehicle_id=vehicles[4].id, station_id=stations[3].id, soc_before=20.0, soc_after=100.0, swapped_at=now - timedelta(minutes=40)),
    ]
    db.add_all(swaps)
