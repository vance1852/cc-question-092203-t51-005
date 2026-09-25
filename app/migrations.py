"""轻量启动迁移：兼容历史无时区记录与旧表结构。

SQLite + SQLAlchemy 场景下不引入 Alembic，这里做两件幂等的小事：

1. 老库的 ``stations`` 表没有 ``timezone`` 列时自动补上，并把现有站点置为
   全局默认时区（``APP_DEFAULT_TIMEZONE``）。
2. 历史 ``swap_records.swapped_at`` 全部由 ``datetime.utcnow()`` 写入，按约定
   本就是 naive UTC，**默认原样读取、无需迁移**。仅当部署方确认老数据其实是
   按某本地墙钟时间写入时，可设置环境变量
   ``APP_LEGACY_TIMESTAMPS_TZ=Asia/Shanghai`` 做一次性纠偏：把这些时间从该
   时区解释为 UTC（只执行一次，以 ``swap_records.timezone_migrated`` 标记）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from .config import DEFAULT_TIMEZONE
from .database import engine
from .tz_utils import get_timezone


def _table_columns(table: str) -> set[str]:
    inspector = inspect(engine)
    if not inspector.has_table(table):
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def run_migrations() -> None:
    """建表后执行，全部幂等。"""
    # 默认时区非法时快速失败，避免建表后才在各站点上逐个报错。
    get_timezone(DEFAULT_TIMEZONE)
    _ensure_station_timezone_column()
    _ensure_swap_migration_column()
    _migrate_legacy_timestamps()


def _ensure_station_timezone_column() -> None:
    cols = _table_columns("stations")
    if not cols:  # 表尚未建立，建表时会直接带上该列
        return
    if "timezone" not in cols:
        # SQLite 的 ALTER TABLE 不支持用绑定参数作 DEFAULT；DEFAULT_TIMEZONE 已在
        # run_migrations 中通过 IANA 校验（仅含字母数字与有限符号），这里转义后内联。
        tz_literal = DEFAULT_TIMEZONE.replace("'", "''")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE stations ADD COLUMN timezone VARCHAR(64) "
                    f"NOT NULL DEFAULT '{tz_literal}'"
                )
            )


def _ensure_swap_migration_column() -> None:
    """老库补 timezone_migrated 标记列，保证 ORM 查询不因缺列报错。"""
    cols = _table_columns("swap_records")
    if not cols or "timezone_migrated" in cols:
        return
    with engine.begin() as conn:
        # 新列在老行上为 NULL：视为「尚未纠偏」；纠偏后置 1。
        conn.execute(text("ALTER TABLE swap_records ADD COLUMN timezone_migrated BOOLEAN"))


def _migrate_legacy_timestamps() -> None:
    legacy_tz_name = os.getenv("APP_LEGACY_TIMESTAMPS_TZ", "").strip()
    if not legacy_tz_name:
        return
    if not _table_columns("swap_records"):
        return
    legacy_tz = get_timezone(legacy_tz_name)

    with engine.begin() as conn:
        pending = conn.execute(
            text(
                "SELECT id, swapped_at FROM swap_records "
                "WHERE timezone_migrated IS NULL OR timezone_migrated = 0"
            )
        ).all()
        for row_id, old_value in pending:
            # 老值是按本地墙钟写入的 naive 时间，附加部署时区后转 UTC。
            naive_local = _parse_datetime(old_value)
            utc_value = naive_local.replace(tzinfo=legacy_tz).astimezone(timezone.utc).replace(tzinfo=None)
            conn.execute(
                text("UPDATE swap_records SET swapped_at = :ts, timezone_migrated = 1 WHERE id = :id"),
                {"ts": utc_value.strftime("%Y-%m-%d %H:%M:%S.%f"), "id": row_id},
            )


def _parse_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析历史时间值: {value!r}")
