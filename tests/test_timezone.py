"""时区与营业日统计测试。

覆盖：
- IANA 时区配置校验（站点创建/更新、仪表盘与换电查询参数）
- 本地日期 -> 半开 UTC 区间的转换（含夏令时 23/25 小时当天）
- 跨过北京时间零点的交易归属当地营业日
- 边界时刻：窗口起点计入、终点不计入（半开区间）
- 夏令时切换当天不重复、不漏计（含秋季"重复一小时"）
- 未来记录不计入今天、计入其对应日期
- 历史无时区记录（naive UTC）的兼容读取
- 换电记录按站点当地日期复核
- 非时间类统计不受日期/时区参数影响
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_TIMEZONE
from app.database import SessionLocal
from app.main import app
from app.models import SwapRecord, Vehicle
from app.seed import init_db
from app.timeutil import current_local_date, local_date_to_utc_window, validate_timezone

init_db()
client = TestClient(app)

UTC = timezone.utc


def _auth_headers() -> dict:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _stats(headers, **params) -> dict:
    resp = client.get("/api/dashboard/stats", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _swap_count(headers, day: str, tz: str) -> int:
    return _stats(headers, date=day, timezone=tz)["swap_today"]


def _insert_swap(swapped_at: datetime, station_id: int = None) -> int:
    """直接落库一条换电记录（指定时刻），返回记录 id。"""
    db = SessionLocal()
    try:
        if station_id is None:
            from app.models import Station

            station_id = db.query(Station).first().id
        vehicle_id = db.query(Vehicle).first().id
        record = SwapRecord(
            vehicle_id=vehicle_id, station_id=station_id,
            soc_before=10.0, soc_after=100.0, swapped_at=swapped_at,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record.id
    finally:
        db.close()


# ---------- 时区校验与窗口换算（单元级） ----------

def test_validate_timezone_accepts_iana_names():
    assert validate_timezone("Asia/Shanghai") == "Asia/Shanghai"
    assert validate_timezone("America/New_York") == "America/New_York"
    assert validate_timezone("UTC") == "UTC"


def test_validate_timezone_rejects_invalid():
    for bad in ["", "foo", "北京时间", "UTC+8", "../etc/passwd", "GMT+8:00"]:
        with pytest.raises(ValueError):
            validate_timezone(bad)


def test_window_shanghai_is_utc_plus_8():
    start, end = local_date_to_utc_window(date(2026, 9, 25), "Asia/Shanghai")
    assert start == datetime(2026, 9, 24, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)


def test_window_dst_spring_forward_is_23_hours():
    # 美国东部 2026-03-08 02:00 拨快一小时，当天只有 23 小时
    start, end = local_date_to_utc_window(date(2026, 3, 8), "America/New_York")
    assert start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=23)


def test_window_dst_fall_back_is_25_hours():
    # 美国东部 2026-11-01 02:00 拨回一小时，当天有 25 小时
    start, end = local_date_to_utc_window(date(2026, 11, 1), "America/New_York")
    assert start == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=25)


def test_adjacent_windows_connect_without_gap_or_overlap():
    # 相邻日期的区间必须首尾相接：夏令时切换前后同样成立
    for tz, days in [
        ("America/New_York", [date(2026, 3, 6), date(2026, 3, 7), date(2026, 3, 8), date(2026, 3, 9), date(2026, 3, 10)]),
        ("America/New_York", [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]),
        ("Asia/Shanghai", [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26)]),
    ]:
        windows = [local_date_to_utc_window(d, tz) for d in days]
        for (prev_start, prev_end), (next_start, next_end) in zip(windows, windows[1:]):
            assert prev_end == next_start
            assert prev_start < prev_end


# ---------- 站点时区配置 ----------

def test_station_timezone_validation():
    headers = _auth_headers()
    bad = client.post("/api/stations", json={"name": "非法时区站", "timezone": "Mars/Olympus"}, headers=headers)
    assert bad.status_code == 422

    created = client.post(
        "/api/stations",
        json={"name": f"纽约测试站{uuid.uuid4().hex[:6]}", "slot_total": 4, "battery_ready": 2, "timezone": "America/New_York"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["timezone"] == "America/New_York"

    bad_update = client.put(f"/api/stations/{sid}", json={"timezone": "UTC+8"}, headers=headers)
    assert bad_update.status_code == 422
    ok_update = client.put(f"/api/stations/{sid}", json={"timezone": "Asia/Shanghai"}, headers=headers)
    assert ok_update.status_code == 200
    assert ok_update.json()["timezone"] == "Asia/Shanghai"


def test_dashboard_and_swaps_reject_bad_timezone():
    headers = _auth_headers()
    assert client.get("/api/dashboard/stats", params={"timezone": "foo"}, headers=headers).status_code == 422
    assert client.get("/api/swaps", params={"date": "2026-09-25", "timezone": "foo"}, headers=headers).status_code == 422
    assert client.get("/api/dashboard/stats", params={"date": "not-a-date"}, headers=headers).status_code == 422


# ---------- 仪表盘：统计口径说明与默认营业日 ----------

def test_dashboard_output_declares_timezone_and_window():
    data = _stats(_auth_headers(), date="2026-09-25", timezone="Asia/Shanghai")
    assert data["timezone"] == "Asia/Shanghai"
    assert data["date"] == "2026-09-25"
    assert data["window_start_utc"] == "2026-09-24T16:00:00Z"
    assert data["window_end_utc"] == "2026-09-25T16:00:00Z"


def test_dashboard_defaults_to_current_business_day():
    data = _stats(_auth_headers())
    assert data["timezone"] == DEFAULT_TIMEZONE
    assert data["date"] == current_local_date(DEFAULT_TIMEZONE).isoformat()
    start, end = local_date_to_utc_window(current_local_date(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE)
    assert data["window_start_utc"] == start.isoformat().replace("+00:00", "Z")
    assert data["window_end_utc"] == end.isoformat().replace("+00:00", "Z")


def test_non_time_stats_unaffected_by_date_params():
    headers = _auth_headers()
    plain = _stats(headers)
    dated = _stats(headers, date="2020-01-01", timezone="America/New_York")
    for key in ["station_total", "station_running", "vehicle_total", "vehicle_fault", "battery_ready_total"]:
        assert plain[key] == dated[key]


# ---------- 跨零点归属与边界时刻 ----------

def test_cross_midnight_beijing_counts_in_local_business_day():
    """北京时间 2026-09-25 00:30（UTC 2026-09-24 16:30）完成的交易，
    旧口径（UTC 午夜切分）会算到 24 号，新口径必须算到当地营业日 25 号。"""
    headers = _auth_headers()
    before_25 = _swap_count(headers, "2026-09-25", "Asia/Shanghai")
    before_24 = _swap_count(headers, "2026-09-24", "Asia/Shanghai")
    _insert_swap(datetime(2026, 9, 24, 16, 30, tzinfo=UTC))
    assert _swap_count(headers, "2026-09-25", "Asia/Shanghai") == before_25 + 1
    assert _swap_count(headers, "2026-09-24", "Asia/Shanghai") == before_24


def test_window_boundary_is_half_open():
    """上海 2026-09-25 窗口为 [2026-09-24T16:00Z, 2026-09-25T16:00Z)：
    起点计入、终点不计入（归入次日）、终点前一微秒计入。"""
    headers = _auth_headers()
    day, nxt = "2026-09-25", "2026-09-26"
    before_day = _swap_count(headers, day, "Asia/Shanghai")
    before_nxt = _swap_count(headers, nxt, "Asia/Shanghai")

    _insert_swap(datetime(2026, 9, 24, 16, 0, 0, tzinfo=UTC))          # 恰好窗口起点
    _insert_swap(datetime(2026, 9, 25, 15, 59, 59, 999999, tzinfo=UTC))  # 终点前一微秒
    _insert_swap(datetime(2026, 9, 25, 16, 0, 0, tzinfo=UTC))          # 恰好窗口终点

    assert _swap_count(headers, day, "Asia/Shanghai") == before_day + 2
    assert _swap_count(headers, nxt, "Asia/Shanghai") == before_nxt + 1


# ---------- 夏令时切换当天 ----------

def test_dst_spring_forward_day_no_double_count_or_miss():
    """美东 2026-03-08（23 小时）：窗口内记录各计一次，窗口终点归入 03-09。"""
    headers = _auth_headers()
    tz = "America/New_York"
    before_08 = _swap_count(headers, "2026-03-08", tz)
    before_09 = _swap_count(headers, "2026-03-09", tz)

    _insert_swap(datetime(2026, 3, 8, 5, 0, tzinfo=UTC))   # 恰好窗口起点（当地 00:00 EST）
    _insert_swap(datetime(2026, 3, 8, 7, 30, tzinfo=UTC))  # 跳变后当地 03:30 EDT
    _insert_swap(datetime(2026, 3, 9, 4, 0, tzinfo=UTC))   # 恰好窗口终点（当地 03-09 00:00）

    assert _swap_count(headers, "2026-03-08", tz) == before_08 + 2
    assert _swap_count(headers, "2026-03-09", tz) == before_09 + 1


def test_dst_fall_back_repeated_hour_counted_once():
    """美东 2026-11-01（25 小时）：当地 01:30 出现两次（EDT 与 EST 各一次），
    两笔交易都必须恰好计入当天一次，不得重复也不得漏到次日。"""
    headers = _auth_headers()
    tz = "America/New_York"
    before_01 = _swap_count(headers, "2026-11-01", tz)
    before_02 = _swap_count(headers, "2026-11-02", tz)

    _insert_swap(datetime(2026, 11, 1, 5, 30, tzinfo=UTC))  # 01:30 EDT（第一次）
    _insert_swap(datetime(2026, 11, 1, 6, 30, tzinfo=UTC))  # 01:30 EST（重复小时的第二次）

    assert _swap_count(headers, "2026-11-01", tz) == before_01 + 2
    assert _swap_count(headers, "2026-11-02", tz) == before_02


# ---------- 未来记录 ----------

def test_future_records_not_in_today_but_in_their_own_date():
    headers = _auth_headers()
    tomorrow = current_local_date(DEFAULT_TIMEZONE) + timedelta(days=1)
    start, _ = local_date_to_utc_window(tomorrow, DEFAULT_TIMEZONE)
    future_moment = start + timedelta(hours=12)

    today_str = current_local_date(DEFAULT_TIMEZONE).isoformat()
    tomorrow_str = tomorrow.isoformat()
    before_today = _stats(headers)["swap_today"]
    before_tomorrow = _swap_count(headers, tomorrow_str, DEFAULT_TIMEZONE)

    _insert_swap(future_moment)

    assert _stats(headers, date=today_str)["swap_today"] == before_today
    assert _swap_count(headers, tomorrow_str, DEFAULT_TIMEZONE) == before_tomorrow + 1


def test_create_swap_with_future_swapped_at_via_api():
    headers = _auth_headers()
    stations = client.get("/api/stations", headers=headers).json()
    station = next(s for s in stations if s["battery_ready"] > 0)
    vehicle = client.post(
        "/api/vehicles", json={"plate": f"沪EV{uuid.uuid4().hex[:4]}", "current_soc": 10.0}, headers=headers
    ).json()

    future_local = datetime.now(UTC) + timedelta(days=3)
    # 显式带 +08:00 偏移的未来时刻
    future_str = future_local.astimezone(timezone(timedelta(hours=8))).isoformat()
    target_date = (future_local.astimezone(timezone(timedelta(hours=8)))).date().isoformat()

    before = _swap_count(headers, target_date, "Asia/Shanghai")
    resp = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": station["id"],
              "soc_before": 10.0, "soc_after": 100.0, "swapped_at": future_str},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert _swap_count(headers, target_date, "Asia/Shanghai") == before + 1


# ---------- 历史无时区记录兼容读取 ----------

def test_legacy_naive_record_read_as_utc():
    """历史记录是无时区 UTC 字符串：读取应附加 UTC 语义，并按当地营业日正确统计。"""
    headers = _auth_headers()
    before = _swap_count(headers, "2026-09-25", "Asia/Shanghai")

    # 模拟旧代码写入：naive datetime（语义为 UTC），北京时间 2026-09-25 10:00
    record_id = _insert_swap(datetime(2026, 9, 25, 2, 0, 0))  # 无时区

    db = SessionLocal()
    try:
        record = db.get(SwapRecord, record_id)
        assert record.swapped_at.tzinfo is not None
        assert record.swapped_at.utcoffset() == timedelta(0)
        assert record.swapped_at == datetime(2026, 9, 25, 2, 0, tzinfo=UTC)
    finally:
        db.close()

    assert _swap_count(headers, "2026-09-25", "Asia/Shanghai") == before + 1


def test_create_swap_rejects_naive_swapped_at():
    headers = _auth_headers()
    stations = client.get("/api/stations", headers=headers).json()
    station = next(s for s in stations if s["battery_ready"] > 0)
    vehicles = client.get("/api/vehicles", headers=headers).json()
    resp = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicles[0]["id"], "station_id": station["id"],
              "soc_before": 10.0, "soc_after": 100.0, "swapped_at": "2026-09-25T08:00:00"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_create_swap_converts_offset_to_utc():
    headers = _auth_headers()
    stations = client.get("/api/stations", headers=headers).json()
    station = next(s for s in stations if s["battery_ready"] > 0)
    vehicle = client.post(
        "/api/vehicles", json={"plate": f"沪EV{uuid.uuid4().hex[:4]}", "current_soc": 10.0}, headers=headers
    ).json()
    resp = client.post(
        "/api/swaps",
        json={"vehicle_id": vehicle["id"], "station_id": station["id"],
              "soc_before": 10.0, "soc_after": 100.0, "swapped_at": "2026-09-25T08:00:00+08:00"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    # 北京时间 08:00 = UTC 00:00，输出统一为 UTC
    assert resp.json()["swapped_at"] == "2026-09-25T00:00:00Z"


# ---------- 按站点当地日期复核 ----------

def test_swaps_filter_by_station_local_date():
    headers = _auth_headers()
    station = client.post(
        "/api/stations",
        json={"name": f"纽约复核站{uuid.uuid4().hex[:6]}", "slot_total": 4, "battery_ready": 2, "timezone": "America/New_York"},
        headers=headers,
    ).json()
    # UTC 2026-11-01 12:00 = 美东 2026-11-01 07:00（当地营业日 11-01）
    record_id = _insert_swap(datetime(2026, 11, 1, 12, 0, tzinfo=UTC), station_id=station["id"])

    on_day = client.get("/api/swaps", params={"station_id": station["id"], "date": "2026-11-01"}, headers=headers)
    assert on_day.status_code == 200
    ids = [r["id"] for r in on_day.json()]
    assert record_id in ids
    # 输出的换电时间带 UTC 时区标记
    record = next(r for r in on_day.json() if r["id"] == record_id)
    assert record["swapped_at"].endswith("Z") or record["swapped_at"].endswith("+00:00")

    for other_day in ["2026-10-31", "2026-11-02"]:
        other = client.get("/api/swaps", params={"station_id": station["id"], "date": other_day}, headers=headers)
        assert record_id not in [r["id"] for r in other.json()]


def test_swaps_filter_uses_station_timezone_over_default():
    """同一 UTC 时刻，按站点时区（美东）与默认时区（上海）应归入不同营业日。"""
    headers = _auth_headers()
    station = client.post(
        "/api/stations",
        json={"name": f"纽约边界站{uuid.uuid4().hex[:6]}", "slot_total": 4, "battery_ready": 2, "timezone": "America/New_York"},
        headers=headers,
    ).json()
    # UTC 2026-09-25 02:00：美东 2026-09-24 22:00，上海 2026-09-25 10:00
    record_id = _insert_swap(datetime(2026, 9, 25, 2, 0, tzinfo=UTC), station_id=station["id"])

    ny_24 = client.get("/api/swaps", params={"station_id": station["id"], "date": "2026-09-24"}, headers=headers)
    assert record_id in [r["id"] for r in ny_24.json()]
    ny_25 = client.get("/api/swaps", params={"station_id": station["id"], "date": "2026-09-25"}, headers=headers)
    assert record_id not in [r["id"] for r in ny_25.json()]
    # 显式 timezone 参数优先于站点时区
    sh_25 = client.get(
        "/api/swaps",
        params={"station_id": station["id"], "date": "2026-09-25", "timezone": "Asia/Shanghai"},
        headers=headers,
    )
    assert record_id in [r["id"] for r in sh_25.json()]


def test_swaps_filter_unknown_station_404():
    resp = client.get("/api/swaps", params={"station_id": 999999, "date": "2026-09-25"}, headers=_auth_headers())
    assert resp.status_code == 404
