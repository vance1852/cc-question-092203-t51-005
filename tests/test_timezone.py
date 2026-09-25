"""时区与营业日语义的专项测试。

覆盖：
- IANA 时区校验（缩写/固定偏移/空串拒绝）
- 本地日期 -> 半开 UTC 区间（夏令时当天 23/25 小时）
- 边界时刻（本地零点）与 DST 跳变当天不重不漏
- 北京跨零点归属、未来记录、默认当前营业日
- naive / 带偏移时间上报的解释，春令时不存在时刻 422
- 历史 naive UTC 记录兼容读取
- 旧表结构迁移（补 timezone 列）与 APP_LEGACY_TIMESTAMPS_TZ 一次性纠偏
- 非时间类统计取值保持不变
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import database, migrations
from app.auth import hash_password
from app.config import DEFAULT_TIMEZONE
from app.main import app
from app.models import Base, Station, SwapRecord, User, Vehicle
from app.tz_utils import local_day_to_utc_range
from zoneinfo import ZoneInfo


UTC = timezone.utc
NY = "America/New_York"
SH = "Asia/Shanghai"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db_file = tmp_path / "tz_test.db"
    eng = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    # routers 通过 database 模块全局访问 engine / SessionLocal
    monkeypatch.setattr(database, "engine", eng)
    monkeypatch.setattr(database, "SessionLocal", session_local)
    # seed / migrations 在模块顶部绑定了 engine
    monkeypatch.setattr(migrations, "engine", eng)

    Base.metadata.create_all(bind=eng)
    migrations.run_migrations()

    db = session_local()
    db.add(User(username="admin", password_hash=hash_password("admin123"), display_name="平台管理员"))
    db.commit()

    client = TestClient(app)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    yield {"client": client, "headers": headers, "db_factory": session_local, "engine": eng}

    db.close()


def _make_station(db, tz_name=SH, name=None, battery_ready=10):
    station = Station(
        name=name or f"测试站-{tz_name}",
        address="测试路",
        timezone=tz_name,
        slot_total=20,
        battery_ready=battery_ready,
        status="running",
    )
    db.add(station)
    db.flush()
    return station


def _make_vehicle(db, plate=None):
    vehicle = Vehicle(plate=plate or f"测{id(object()) % 100000:05d}", model="测试车", current_soc=10.0)
    db.add(vehicle)
    db.flush()
    return vehicle


def _add_swap(db, station, vehicle, when_utc_naive):
    record = SwapRecord(
        vehicle_id=vehicle.id,
        station_id=station.id,
        soc_before=10.0,
        soc_after=100.0,
        swapped_at=when_utc_naive,
    )
    db.add(record)
    db.commit()
    return record


def _stats(client, headers, day=None):
    params = {"date": day} if day else {}
    resp = client.get("/api/dashboard/stats", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _station_stat(stats_payload, station_id):
    return next(s for s in stats_payload["stations"] if s["station_id"] == station_id)


# ---------- 纯函数：区间换算 ----------
def test_day_range_normal_day():
    tz = ZoneInfo(SH)
    start, end = local_day_to_utc_range(datetime(2026, 9, 25).date(), tz)
    assert start == datetime(2026, 9, 24, 16, 0)
    assert end == datetime(2026, 9, 25, 16, 0)
    assert end - start == timedelta(hours=24)


def test_day_range_spring_forward_is_23h():
    tz = ZoneInfo(NY)
    # 2026-03-08 北美春令时：02:00 跳到 03:00，当天只有 23 小时
    start, end = local_day_to_utc_range(datetime(2026, 3, 8).date(), tz)
    assert start == datetime(2026, 3, 8, 5, 0)
    assert end == datetime(2026, 3, 9, 4, 0)
    assert end - start == timedelta(hours=23)


def test_day_range_fall_back_is_25h():
    tz = ZoneInfo(NY)
    # 2026-11-01 北美秋令时：02:00 回到 01:00，当天有 25 小时
    start, end = local_day_to_utc_range(datetime(2026, 11, 1).date(), tz)
    assert start == datetime(2026, 11, 1, 4, 0)
    assert end == datetime(2026, 11, 2, 5, 0)
    assert end - start == timedelta(hours=25)


# ---------- IANA 校验 ----------
def test_station_timezone_must_be_iana(ctx):
    client, headers = ctx["client"], ctx["headers"]
    for bad in ["PST", "UTC+8", "GMT-5", "Asia/NotCity", ""]:
        resp = client.post("/api/stations", json={"name": "非法时区站", "timezone": bad}, headers=headers)
        assert resp.status_code == 422, f"{bad!r} 应被拒绝: {resp.text}"

    ok = client.post(
        "/api/stations",
        json={"name": "纽约站", "timezone": "America/New_York", "slot_total": 4, "battery_ready": 2},
        headers=headers,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["timezone"] == "America/New_York"


def test_station_default_timezone(ctx):
    client, headers = ctx["client"], ctx["headers"]
    resp = client.post("/api/stations", json={"name": "默认时区站"}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["timezone"] == DEFAULT_TIMEZONE


def test_update_station_timezone_validated(ctx):
    client, headers = ctx["client"], ctx["headers"]
    sid = client.post("/api/stations", json={"name": "改时区站"}, headers=headers).json()["id"]
    bad = client.put(f"/api/stations/{sid}", json={"timezone": "PST"}, headers=headers)
    assert bad.status_code == 422
    ok = client.put(f"/api/stations/{sid}", json={"timezone": "Europe/London"}, headers=headers)
    assert ok.status_code == 200
    assert ok.json()["timezone"] == "Europe/London"


# ---------- 跨零点归属（需求中的核心问题） ----------
def test_shanghai_past_midnight_belongs_to_new_local_day(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, SH)
    vehicle = _make_vehicle(db)
    # 本地 2026-09-25 00:30（上海）= UTC 2026-09-24 16:30。
    # 旧逻辑按 UTC 午夜切分会把它算到 09-24，新口径必须算 09-25。
    _add_swap(db, station, vehicle, datetime(2026, 9, 24, 16, 30))

    payload = _stats(ctx["client"], ctx["headers"], day="2026-09-25")
    assert _station_stat(payload, station.id)["swap_count"] == 1
    payload_prev = _stats(ctx["client"], ctx["headers"], day="2026-09-24")
    assert _station_stat(payload_prev, station.id)["swap_count"] == 0


# ---------- 边界时刻：半开区间不重不漏 ----------
def test_exact_midnight_boundary_counted_once(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, NY)
    vehicle = _make_vehicle(db)
    # 2026-03-08 本地零点 = UTC 05:00（含）；前一刻 UTC 04:59:59 属 03-07；
    # 次日本地零点 = UTC 03-09 04:00（不含于 03-08）。
    _add_swap(db, station, vehicle, datetime(2026, 3, 8, 4, 59, 59))
    _add_swap(db, station, vehicle, datetime(2026, 3, 8, 5, 0, 0))
    _add_swap(db, station, vehicle, datetime(2026, 3, 9, 4, 0, 0))

    counts = {
        day: _station_stat(_stats(ctx["client"], ctx["headers"], day=day), station.id)["swap_count"]
        for day in ("2026-03-07", "2026-03-08", "2026-03-09")
    }
    assert counts == {"2026-03-07": 1, "2026-03-08": 1, "2026-03-09": 1}
    # 三天合计恰好 3 笔：边界笔没有重复也没有漏掉
    assert sum(counts.values()) == 3


# ---------- 夏令时跳变当天 ----------
def test_spring_forward_day_no_gap_no_double(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, NY)
    vehicle = _make_vehicle(db)
    # 春令当天 [05:00, 次日04:00)：窗口内放两笔，窗口紧邻外侧各一笔
    _add_swap(db, station, vehicle, datetime(2026, 3, 8, 5, 0))        # 本地 00:00 EDT
    _add_swap(db, station, vehicle, datetime(2026, 3, 8, 6, 30))       # 本地 01:30 EST（跳空前最后一小时）
    _add_swap(db, station, vehicle, datetime(2026, 3, 9, 3, 59, 59))   # 本地 23:59:59
    _add_swap(db, station, vehicle, datetime(2026, 3, 8, 4, 59, 59))   # 本地 03-07 23:59:59 EST（前一天末尾）
    _add_swap(db, station, vehicle, datetime(2026, 3, 9, 4, 0, 0))     # 次日零点

    day = _station_stat(_stats(ctx["client"], ctx["headers"], day="2026-03-08"), station.id)
    assert day["swap_count"] == 3
    assert datetime.fromisoformat(day["interval_start"]).utcoffset() == timedelta(0)
    # 接口自报的区间应是 23 小时窗口
    start = datetime.fromisoformat(day["interval_start"])
    end = datetime.fromisoformat(day["interval_end"])
    assert end - start == timedelta(hours=23)

    prev = _station_stat(_stats(ctx["client"], ctx["headers"], day="2026-03-07"), station.id)["swap_count"]
    nxt = _station_stat(_stats(ctx["client"], ctx["headers"], day="2026-03-09"), station.id)["swap_count"]
    assert prev == 1
    assert nxt == 1


def test_fall_back_day_25h_counted_once(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, NY)
    vehicle = _make_vehicle(db)
    # 秋令当天 [11-01 04:00, 11-02 05:00)：25 小时
    _add_swap(db, station, vehicle, datetime(2026, 11, 1, 4, 0))   # 本地 00:00 EDT
    _add_swap(db, station, vehicle, datetime(2026, 11, 1, 5, 30))  # 本地 01:30 EDT（第一次）
    _add_swap(db, station, vehicle, datetime(2026, 11, 1, 6, 30))  # 本地 01:30 EST（第二次，重复小时）
    _add_swap(db, station, vehicle, datetime(2026, 11, 2, 4, 59, 59))  # 仍属 11-01（本地 23:59:59 EST）
    _add_swap(db, station, vehicle, datetime(2026, 11, 2, 5, 0))  # 11-02 本地零点，不含

    day = _station_stat(_stats(ctx["client"], ctx["headers"], day="2026-11-01"), station.id)
    assert day["swap_count"] == 4
    start = datetime.fromisoformat(day["interval_start"])
    end = datetime.fromisoformat(day["interval_end"])
    assert end - start == timedelta(hours=25)

    nxt = _station_stat(_stats(ctx["client"], ctx["headers"], day="2026-11-02"), station.id)["swap_count"]
    assert nxt == 1
    assert day["swap_count"] + nxt == 5


# ---------- 未来记录 ----------
def test_future_swap_counted_for_explicit_day_not_today(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, SH)
    vehicle = _make_vehicle(db)
    future_utc = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)
    future_day_local = future_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(SH)).date().isoformat()
    _add_swap(db, station, vehicle, future_utc)

    today_payload = _stats(ctx["client"], ctx["headers"])  # 默认当前营业日
    assert _station_stat(today_payload, station.id)["swap_count"] == 0

    future_payload = _stats(ctx["client"], ctx["headers"], day=future_day_local)
    assert _station_stat(future_payload, station.id)["swap_count"] == 1


def test_default_day_is_current_local_business_day(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, SH)
    vehicle = _make_vehicle(db)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    _add_swap(db, station, vehicle, now_utc - timedelta(minutes=2))

    payload = _stats(ctx["client"], ctx["headers"])
    today_sh = datetime.now(ZoneInfo(SH)).date().isoformat()
    row = _station_stat(payload, station.id)
    assert row["date"] == today_sh
    assert row["swap_count"] == 1
    assert payload["meta"]["requested_date"] is None
    assert payload["meta"]["default_timezone"] == DEFAULT_TIMEZONE


# ---------- 接口输出口径 ----------
def test_stats_payload_explains_timezone_and_interval(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, NY)
    db.commit()
    payload = _stats(ctx["client"], ctx["headers"], day="2026-03-08")
    assert payload["meta"]["timezone_basis"] == "station_local"
    assert payload["meta"]["requested_date"] == "2026-03-08"
    row = _station_stat(payload, station.id)
    assert row["timezone"] == NY
    assert row["date"] == "2026-03-08"
    assert datetime.fromisoformat(row["interval_start"]) == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert datetime.fromisoformat(row["interval_end"]) == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    # swap_today 为各站计数之和
    assert payload["swap_today"] == sum(s["swap_count"] for s in payload["stations"])


# ---------- 换电时间上报解释 ----------
def test_swap_create_naive_time_interpreted_as_station_local(ctx):
    client, headers = ctx["client"], ctx["headers"]
    station = client.post(
        "/api/stations",
        json={"name": "纽约上报站", "timezone": NY, "slot_total": 10, "battery_ready": 10},
        headers=headers,
    ).json()
    vehicle = client.post("/api/vehicles", json={"plate": "测NAIVE1", "model": "t"}, headers=headers).json()
    # 11 月纽约为 EST（UTC-5）：本地 12:00 -> UTC 17:00
    resp = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vehicle["id"],
            "station_id": station["id"],
            "soc_before": 10.0,
            "soc_after": 100.0,
            "swapped_at": "2026-11-03T12:00:00",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert datetime.fromisoformat(body["swapped_at"]) == datetime(2026, 11, 3, 17, 0, tzinfo=UTC)
    assert body["station_timezone"] == NY
    assert datetime.fromisoformat(body["swapped_at_local"]) == datetime(
        2026, 11, 3, 12, 0, tzinfo=ZoneInfo("America/New_York")
    )


def test_swap_create_offset_time_distinguishes_fall_back_hour(ctx):
    client, headers = ctx["client"], ctx["headers"]
    station = client.post(
        "/api/stations",
        json={"name": "纽约秋令站", "timezone": NY, "slot_total": 10, "battery_ready": 10},
        headers=headers,
    ).json()
    v1 = client.post("/api/vehicles", json={"plate": "测FOLD01", "model": "t"}, headers=headers).json()
    v2 = client.post("/api/vehicles", json={"plate": "测FOLD02", "model": "t"}, headers=headers).json()
    # 同一个本地墙钟 01:30 在秋令当天出现两次，用偏移区分 -> 两个不同 UTC 瞬间
    r1 = client.post("/api/swaps", json={
        "vehicle_id": v1["id"], "station_id": station["id"],
        "soc_before": 10.0, "soc_after": 100.0,
        "swapped_at": "2026-11-01T01:30:00-04:00",
    }, headers=headers)
    r2 = client.post("/api/swaps", json={
        "vehicle_id": v2["id"], "station_id": station["id"],
        "soc_before": 10.0, "soc_after": 100.0,
        "swapped_at": "2026-11-01T01:30:00-05:00",
    }, headers=headers)
    assert r1.status_code == 201 and r2.status_code == 201
    assert datetime.fromisoformat(r1.json()["swapped_at"]) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert datetime.fromisoformat(r2.json()["swapped_at"]) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    # 两笔都在 11-01 的 25 小时窗口内，各计一次
    payload = _stats(client, headers, day="2026-11-01")
    assert _station_stat(payload, station["id"])["swap_count"] == 2


def test_swap_create_nonexistent_spring_time_rejected(ctx):
    client, headers = ctx["client"], ctx["headers"]
    station = client.post(
        "/api/stations",
        json={"name": "纽约春令站", "timezone": NY, "slot_total": 10, "battery_ready": 10},
        headers=headers,
    ).json()
    vehicle = client.post("/api/vehicles", json={"plate": "测GAP0001", "model": "t"}, headers=headers).json()
    # 02:30 在春令跳空中不存在（naive）-> 422
    resp = client.post("/api/swaps", json={
        "vehicle_id": vehicle["id"], "station_id": station["id"],
        "soc_before": 10.0, "soc_after": 100.0,
        "swapped_at": "2026-03-08T02:30:00",
    }, headers=headers)
    assert resp.status_code == 422
    # 同一时刻携带合法偏移（02:30-05:00 = UTC 07:30 = 本地 03:30 EDT）应接受
    ok = client.post("/api/swaps", json={
        "vehicle_id": vehicle["id"], "station_id": station["id"],
        "soc_before": 10.0, "soc_after": 100.0,
        "swapped_at": "2026-03-08T02:30:00-05:00",
    }, headers=headers)
    assert ok.status_code == 201, ok.text
    assert datetime.fromisoformat(ok.json()["swapped_at"]) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


# ---------- 历史数据兼容 ----------
def test_legacy_naive_utc_records_read_without_migration(ctx):
    db = ctx["db_factory"]()
    station = _make_station(db, SH)
    vehicle = _make_vehicle(db)
    # 模拟旧版本 datetime.utcnow() 风格的存量记录：naive，且无 timezone_migrated 标记
    record = SwapRecord(
        vehicle_id=vehicle.id, station_id=station.id,
        soc_before=10.0, soc_after=100.0,
        swapped_at=datetime(2026, 9, 24, 16, 30),
    )
    db.add(record)
    db.commit()
    payload = _stats(ctx["client"], ctx["headers"], day="2026-09-25")
    assert _station_stat(payload, station.id)["swap_count"] == 1


def test_legacy_table_gets_timezone_column(ctx, tmp_path, monkeypatch):
    eng = ctx["engine"]
    # 先删掉新表，重建为「旧 schema」：stations 无 timezone，swap_records 无迁移标记
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS swap_records"))
        conn.execute(text("DROP TABLE IF EXISTS stations"))
        conn.execute(text(
            "CREATE TABLE stations ("
            "id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, address VARCHAR(256), "
            "slot_total INTEGER, battery_ready INTEGER, status VARCHAR(16), created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE swap_records ("
            "id INTEGER PRIMARY KEY, vehicle_id INTEGER, station_id INTEGER, "
            "soc_before FLOAT, soc_after FLOAT, swapped_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO stations (name, address, slot_total, battery_ready, status, created_at) "
            "VALUES ('老站', '', 8, 3, 'running', '2026-01-01 00:00:00.000000')"
        ))

    migrations.run_migrations()

    station_cols = {c["name"] for c in inspect(eng).get_columns("stations")}
    assert "timezone" in station_cols
    swap_cols = {c["name"] for c in inspect(eng).get_columns("swap_records")}
    assert "timezone_migrated" in swap_cols
    with eng.begin() as conn:
        tz_value = conn.execute(text("SELECT timezone FROM stations WHERE name = '老站'")).scalar_one()
    assert tz_value == DEFAULT_TIMEZONE

    # 老表补列后 ORM 查询必须可用（且未设置纠偏变量时时间原样保留）
    db = ctx["db_factory"]()
    assert db.query(Station).filter(Station.name == "老站").one().timezone == DEFAULT_TIMEZONE
    assert db.query(SwapRecord).count() == 0
    db.close()


def test_legacy_wallclock_timestamps_one_time_correction(ctx, monkeypatch):
    eng = ctx["engine"]
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS swap_records"))
        conn.execute(text(
            "CREATE TABLE swap_records ("
            "id INTEGER PRIMARY KEY, vehicle_id INTEGER, station_id INTEGER, "
            "soc_before FLOAT, soc_after FLOAT, swapped_at DATETIME)"
        ))
        # 旧系统误把上海本地墙钟直接存入：本地 2026-09-25 00:30
        conn.execute(text(
            "INSERT INTO swap_records (vehicle_id, station_id, soc_before, soc_after, swapped_at) "
            "VALUES (1, 1, 10, 100, '2026-09-25 00:30:00.000000')"
        ))

    monkeypatch.setenv("APP_LEGACY_TIMESTAMPS_TZ", SH)
    migrations.run_migrations()
    with eng.begin() as conn:
        row = conn.execute(text("SELECT swapped_at, timezone_migrated FROM swap_records")).one()
    # 本地 00:30 上海 = 前一日 16:30 UTC
    assert str(row[0]) == "2026-09-24 16:30:00.000000"
    assert row[1] == 1

    # 再跑一次必须幂等：值不再变化、不重复平移
    migrations.run_migrations()
    with eng.begin() as conn:
        row = conn.execute(text("SELECT swapped_at, timezone_migrated FROM swap_records")).one()
    assert str(row[0]) == "2026-09-24 16:30:00.000000"
    assert row[1] == 1


# ---------- 非时间类统计保持原值 ----------
def test_non_time_stats_unchanged(ctx):
    db = ctx["db_factory"]()
    s1 = _make_station(db, SH, battery_ready=5)
    s2 = _make_station(db, NY, battery_ready=7)
    db.commit()
    v = _make_vehicle(db)
    db.commit()
    _add_swap(db, s1, v, datetime(2026, 9, 24, 16, 30))
    _add_swap(db, s2, v, datetime(2026, 3, 8, 6, 0))

    payload = _stats(ctx["client"], ctx["headers"], day="2026-09-25")
    assert payload["station_total"] == db.query(Station).count()
    assert payload["station_running"] == db.query(Station).filter(Station.status == "running").count()
    assert payload["vehicle_total"] == db.query(Vehicle).count()
    assert payload["vehicle_fault"] == db.query(Vehicle).filter(Vehicle.status == "fault").count()
    ready_sum = sum(x[0] for x in db.query(Station.battery_ready).all())
    assert payload["battery_ready_total"] == ready_sum
    # 09-25 只有上海站那笔（纽约站的 DST 笔在 3 月）
    assert payload["swap_today"] == 1
    assert _station_stat(payload, s1.id)["swap_count"] == 1
    assert _station_stat(payload, s2.id)["swap_count"] == 0
