"""数据库模型。

业务主题：新能源物流车换电站运营管理。
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator

from .database import Base


class UTCDateTime(TypeDecorator):
    """UTC 时刻列：统一换电时间的存储和查询语义。

    - 写入：aware 时间先换算成 UTC 再落库；无时区值按 UTC 解释原样落库
      （SQLite 中保存为无时区字符串，与历史数据格式完全一致）。
    - 读取：无时区值一律附加 UTC 时区，向上层返回 aware datetime，
      因此历史无时区记录无需数据迁移即可被正确解读。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # 兼容历史写入路径：无时区值按 UTC 解释
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class User(Base):
    """后台用户（本平台只有 admin 一个管理员角色）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(64), nullable=False, default="管理员")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Station(Base):
    """换电站。"""

    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    address = Column(String(256), nullable=False, default="")
    # 电池仓位总数与当前满电可换电池数
    slot_total = Column(Integer, nullable=False, default=0)
    battery_ready = Column(Integer, nullable=False, default=0)
    # 运营状态：running 运营中 / maintenance 维护中 / offline 离线
    status = Column(String(16), nullable=False, default="running")
    # 站点所在地 IANA 时区（如 Asia/Shanghai），营业日统计按此切分
    timezone = Column(String(64), nullable=False, default="Asia/Shanghai")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")


class Vehicle(Base):
    """新能源物流车。"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(32), unique=True, nullable=False, index=True)
    model = Column(String(64), nullable=False, default="")
    battery_capacity = Column(Float, nullable=False, default=100.0)  # kWh
    current_soc = Column(Float, nullable=False, default=100.0)  # 0-100 百分比
    # 状态：idle 空闲 / running 运营 / charging 换电中 / fault 故障
    status = Column(String(16), nullable=False, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="vehicle")


class SwapRecord(Base):
    """换电记录。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    soc_before = Column(Float, nullable=False, default=0.0)
    soc_after = Column(Float, nullable=False, default=100.0)
    # 换电完成时刻，统一为 UTC（读取时返回带 UTC 时区的 datetime）
    swapped_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps")
    station = relationship("Station", back_populates="swaps")
