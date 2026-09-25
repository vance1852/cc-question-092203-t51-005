# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆和换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 标准库 `zoneinfo`（依赖 `tzdata` 提供 IANA 时区库）处理时区与夏令时
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
- 换电站增删改查（`/api/stations`）
- 车辆增删改查（`/api/vehicles`）
- 换电记录查询与登记（`/api/swaps`，会联动更新车辆电量与站点可用电池）
- 仪表盘统计（`/api/dashboard/stats`）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 时间与营业日语义

财务按站点当地营业日核对换电量，全平台遵循同一套语义（实现见 `app/tz_utils.py`）：

- **存储**：`swap_records.swapped_at` 统一存 **naive UTC**（不带时区的 UTC）。
  写入前先换算到 UTC 再落库；历史由 `datetime.utcnow()` 写入的记录本身就是
  naive UTC，**默认原样读取、无需迁移**。
- **站点时区**：每座站点带一个经校验的 IANA 时区字段 `timezone`
  （如 `Asia/Shanghai`、`America/New_York`），可在站点的新增/编辑接口设置；
  拒绝 `PST`、`UTC+8` 这类缩写或固定偏移。默认值由环境变量
  `APP_DEFAULT_TIMEZONE` 决定，为 `Asia/Shanghai`。
- **聚合**：把「站点本地日期」先换算成明确的半开 UTC 区间 `[start, end)`，
  再做 `>= start AND < end` 过滤。左闭右开使本地零点对应的 UTC 边界只归属一天；
  夏令时切换当天由 zoneinfo 自动得到 23（春令）/ 25（秋令）小时的窗口，
  **不重复也不漏计**。
- **仪表盘**：`GET /api/dashboard/stats` 支持 `date=YYYY-MM-DD` 指定营业日；
  不传时各站默认取其时区下的当前营业日。响应中 `meta` 说明统计口径，
  `stations[]` 逐站给出当地日期、时区、半开 UTC 区间和笔数，可按当地日期复核；
  `swap_today` 是各站计数之和。未来时刻的记录按时间戳归属计入所查日期，
  跨日查询时每条记录恰好出现一次。
- **换电登记**：`POST /api/swaps` 可选传 `swapped_at`。naive 时间按站点本地
  时区解释（春令时跳空中不存在的墙钟时间返回 422，提示携带偏移）；带偏移的
  时间按偏移精确换算，可区分秋令时重复的那个小时；响应同时返回 UTC 时刻
  `swapped_at`、站点时区和站点本地时刻 `swapped_at_local`。
- **非时间类统计**（站点/车辆/电池数等）口径与取值不变。

### 老数据兼容与迁移

启动时 `app/migrations.py` 自动、幂等地处理旧库（无需手工操作）：

1. 旧 `stations` 表缺少 `timezone` 列时补列，存量站点回退到
   `APP_DEFAULT_TIMEZONE`；随后可逐站在接口里改成正确时区，统计即时生效。
2. 旧 `swap_records` 表补 `timezone_migrated` 标记列；旧记录默认按 naive UTC
   正确读取。仅当确认旧系统其实是按某本地墙钟直接存的时间戳，才设置
   `APP_LEGACY_TIMESTAMPS_TZ=<IANA 名>`（如 `Asia/Shanghai`）做一次性纠偏，
   以标记列保证只执行一次，重复启动不再平移。

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
