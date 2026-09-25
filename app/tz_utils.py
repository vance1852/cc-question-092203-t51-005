"""统一的换电时间存储与营业日语义。

存储约定（全平台唯一，务必遵守）
--------------------------------
- ``SwapRecord.swapped_at`` 在 SQLite 中以 **naive UTC** 保存（DateTime 列不带时区）。
- 写入前一律先换算到 UTC 再去掉 ``tzinfo``；读取时一律按 UTC 重新附加时区。
  历史记录由 ``datetime.utcnow()`` 写入，本身就是 naive UTC，因此无需转换即可被
  正确读取（见 :mod:`app.migrations` 的可选纠偏能力）。
- 「营业日」按站点配置的 IANA 时区解释为本地日历日，再换算成 **半开 UTC 区间**
  ``[start, end)`` 聚合：边界 ``start`` 计入当日、``end`` 计入次日，天然不重不漏，
  夏令时切换当天区间长度自动为 23 / 25 小时。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

# tz 数据库中允许直接使用的顶层名称（其余顶层名称如 EST/PST/MST/HST 等 POSIX
# 缩写都会被拒绝，规范的 IANA 名称均为 Area/Location 形式）。
_ALLOWED_TOP_LEVEL = {"UTC"}


def get_timezone(name: str) -> ZoneInfo:
    """校验并返回 IANA 时区。

    接受 ``Asia/Shanghai``、``America/New_York``、``UTC`` 这类规范名称；
    拒绝空串、``PST`` 这类缩写、``UTC+8`` 这类固定偏移以及任何不存在的名称。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("时区不能为空")
    name = name.strip()
    is_canonical_shape = "/" in name or name in _ALLOWED_TOP_LEVEL
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:  # pragma: no cover - 分支随平台数据而定
        raise ValueError(f"非法 IANA 时区: {name!r}") from exc
    if not is_canonical_shape:
        raise ValueError(f"请使用 IANA 区域/城市形式的时区（如 Asia/Shanghai），收到缩写: {name!r}")
    return tz


def utc_now_naive() -> datetime:
    """当前时刻的 naive UTC，用于写入数据库。"""
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc_naive(value: datetime) -> datetime:
    """把任意 datetime 归一为 naive UTC（naive 输入按 UTC 对待）。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def resolve_reported_time(value: datetime | None, station_tz: ZoneInfo) -> datetime:
    """把上报的换电时间解释为 naive UTC。

    - 不带偏移的 naive 时间：视为 **站点本地时间**；若该本地时间在春令时
      “跳空”区间内并不存在（如北美 02:30），抛 :class:`InvalidLocalTime`，
      由路由层转为 422，提示调用方携带 UTC 偏移；
    - 带偏移的时间（如 ``2026-11-01T01:30:00-05:00``）：按其偏移精确换算，
      可区分秋令时重复出现的两个本地时刻；
    - ``None``：取当前 UTC 时刻。

    秋令时重复的本地时刻存在歧义（如北美 01:30 出现两次），naive 上报取第一次
    （fold=0，夏令时偏移）；要区分两次请携带 UTC 偏移。
    """
    if value is None:
        return utc_now_naive()
    if value.tzinfo is None:
        localized = value.replace(tzinfo=station_tz)
        # 春令时跳空：该墙钟时间不存在。经 UTC 往返后墙钟会被规约到另一时刻
        # （不能直接 astimezone(同一时区对象)，CPython 会原样返回）。
        roundtrip = localized.astimezone(UTC).astimezone(station_tz).replace(tzinfo=None)
        if roundtrip != value:
            raise InvalidLocalTime(
                f"本地时间 {value.isoformat()} 在时区 {station_tz.key} 的夏令时跳变中不存在，"
                "请携带 UTC 偏移（如 2026-03-08T03:30:00-04:00）后重试"
            )
        value = localized
    return value.astimezone(UTC).replace(tzinfo=None)


class InvalidLocalTime(ValueError):
    """上报的 naive 本地时间落在春令时跳空区间，物理上不存在。"""


def to_aware_utc(value: datetime) -> datetime:
    """读取数据库中的 naive UTC 时间并附加时区。"""
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)


def current_local_date(tz: ZoneInfo) -> date:
    """给定时区当前的本地日期（即该站点「当前营业日」）。"""
    return datetime.now(tz).date()


def local_day_to_utc_range(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """把某时区的本地日历日转换为半开 UTC 区间 ``[start, end)``（naive UTC）。

    直接以本地午夜为界由 zoneinfo 换算偏移，因此夏令时当天自动得到 23 或 25
    小时的窗口，区间内的每个 UTC 瞬间恰好对应一个属于该本地日期的本地时刻。
    """
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    start = start_local.astimezone(UTC).replace(tzinfo=None)
    end = end_local.astimezone(UTC).replace(tzinfo=None)
    return start, end
