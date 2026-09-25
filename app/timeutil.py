"""时区与营业日工具。

统一换电时间的存储和查询语义：

- 存储：swapped_at 一律为 UTC 时刻（SQLite 中保存为无时区字符串，
  格式与历史数据一致，见 models.UTCDateTime）。
- 读取：无时区值一律按 UTC 解释（历史数据即此语义），向上层返回
  带 UTC 时区的 datetime。
- 聚合：把"站点当地日期"转换成明确的半开 UTC 区间 [start_utc, end_utc)
  再过滤。相邻日期的区间首尾相接，边界时刻不重复、不漏计；
  夏令时切换当天区间自然变为 23 或 25 小时，规则同样成立。
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["validate_timezone", "local_date_to_utc_window", "current_local_date"]


def validate_timezone(name: str) -> str:
    """校验 IANA 时区名（如 Asia/Shanghai、America/New_York）。

    合法则原样返回，非法（含空串、相对路径、非 IANA 名称）抛 ValueError。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("时区不能为空，需为 IANA 时区名（如 Asia/Shanghai）")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"无效的 IANA 时区: {name!r}") from exc
    return name


def local_date_to_utc_window(local_date: date, tz_name: str) -> tuple[datetime, datetime]:
    """把当地日期转换为半开 UTC 区间 [start_utc, end_utc)。

    区间为 [当地当日 00:00, 当地次日 00:00) 对应的 UTC 时刻，左闭右开。
    夏令时切换当天区间自然为 23 或 25 小时；相邻日期的区间首尾相接，
    因此按此区间聚合不会重复也不会漏计。
    """
    tz = ZoneInfo(validate_timezone(tz_name))
    start = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    return start, end


def current_local_date(tz_name: str) -> date:
    """指定 IANA 时区当前所在的当地日期（即"当前营业日"）。"""
    return datetime.now(ZoneInfo(validate_timezone(tz_name))).date()
