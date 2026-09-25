# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆和换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，无额外依赖

所有数据本地、离线可运行，不依赖任何外部服务。

## 运行

```bash
pip install -r requirements.txt
python run.py
```

服务启动在 `http://127.0.0.1:7634`，首次启动自动建表并灌入种子数据。
交互式文档：`http://127.0.0.1:7634/docs`。

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查（`/api/stations`，含站点 IANA 时区配置）
- 车辆增删改查（`/api/vehicles`）
- 换电记录查询与登记（`/api/swaps`，会联动更新车辆电量与站点可用电池）
- 仪表盘统计（`/api/dashboard/stats`，按当地营业日统计换电量）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 换电时间的时区语义

财务按**站点所在地的营业日**核对换电量，系统统一如下口径：

- **存储**：`swapped_at` 一律为 UTC 时刻（SQLite 中保存为无时区字符串，与历史数据格式一致）。
- **读取**：无时区值一律按 UTC 解释，接口输出带时区的 UTC 时间（如 `2026-09-25T00:00:00Z`）。
- **站点时区**：`Station.timezone` 为 IANA 时区名（如 `Asia/Shanghai`、`America/New_York`），
  创建/更新站点时校验，非法名称返回 422。平台默认统计时区可用环境变量
  `APP_DEFAULT_TIMEZONE` 覆盖（默认 `Asia/Shanghai`，启动即校验）。
- **聚合**：把当地日期转换成**半开 UTC 区间** `[当地当日 00:00, 当地次日 00:00)` 再过滤。
  相邻日期的区间首尾相接，边界时刻不重复、不漏计；夏令时切换当天区间自然为
  23 或 25 小时，规则同样成立（春季拨快当天 23 小时、秋季拨回当天 25 小时，
  "重复的一小时"里的交易各计一次）。

### 仪表盘统计

```
GET /api/dashboard/stats?date=2026-09-25&timezone=Asia/Shanghai
```

- `date` 可选，缺省为统计时区的**当前营业日**；`timezone` 可选，缺省为平台默认时区。
- 响应除原有统计项外，附带统计口径说明：`timezone`、`date`、
  `window_start_utc` / `window_end_utc`（半开 UTC 区间）。
- 未来记录只计入其对应日期的窗口，不会计入今天。

### 按站点当地日期复核

```
GET /api/swaps?station_id=3&date=2026-09-25
GET /api/swaps?date=2026-09-25&timezone=America/New_York
```

- 指定 `station_id` 时按**该站点配置的时区**切分营业日；`timezone` 参数可显式覆盖。
- 登记换电（`POST /api/swaps`）可传可选字段 `swapped_at` 补录时刻，
  必须携带时区偏移（如 `2026-09-25T08:00:00+08:00`），缺省为服务器当前 UTC 时间。

### 历史数据兼容与迁移

- 历史 `swap_records.swapped_at` 本就是无时区 UTC 字符串，与新存储格式完全一致，
  读取层（`app/models.py` 的 `UTCDateTime`）统一附加 UTC 语义，**无需改写数据**。
- 启动时自动执行轻量迁移：为已有 `stations` 表补充 `timezone` 列，
  旧站点回填平台默认时区（`app/seed.py` 的 `_migrate_schema`，幂等）。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
