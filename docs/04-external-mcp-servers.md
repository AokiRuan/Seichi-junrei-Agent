# 04 · 外部 API 型 MCP Server：天气与路线

## 本章目标

读完能回答：

- 包装外部 REST API 的 MCP server，和 03 章读本地数据的 seichi server，要多操心哪些事？
- 不连网络，怎么测试一个"本职工作就是调网络"的模块？
- 外部服务挂了，工具该返回什么？
- `Annotated[int, Field(le=16)]` 这种写法，对 LLM 有什么用？
- "几个景点按什么顺序走最省时间"是怎么算的？

## 核心概念

### 为什么自己写，而不是用社区现成的 MCP server

计划原本写的是"天气/路线优先找社区现成的"。最后两个都自己写了，原因：

- 社区 MCP server 大多用 Node 实现，本机没装 Node
- Python 实现的多数基于 MCP SDK 1.x，和我们的 2.x 不兼容（见 [踩坑日志](pitfalls.md#mcp-sdk-2x)）
- 这两个 server 都只是薄包装，各自一两百行代码，自己写的成本低
- **学习价值**：seichi 读本地数据，天气和路线要调外部 API，会遇到网络失败、限流、延迟等新问题

### 外部 API 型 server 要多操心的五件事

| 问题 | seichi（本地数据） | 天气 / 路线（外部 API） |
|---|---|---|
| 会不会失败 | 基本不会 | 超时、断网、对方 5xx、限流 429 |
| 响应速度 | 毫秒级 | 200ms ~ 1.3s（实测） |
| 使用规则 | 无 | 要带 User-Agent、限制请求频率 |
| 怎么测试 | 直接调用 | 不能依赖真实网络 |
| 数据可信度 | 自己决定 | 要先探测验证 |

本章代码就是围绕这五件事组织的。

### 选了哪些外部服务

都是免费、不需要 API key 的公共服务，都经过实测（2026-09-29）：

| 服务 | 用途 | 实测 |
|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | 逐日天气预报 | 200，约 1.1s；最多 16 天，超出返回 400 |
| OSRM（[FOSSGIS 实例](https://routing.openstreetmap.de/)） | 步行/驾车的时间与距离 | 200，约 300ms；步行 20.5km 算出 4.5 小时，符合步行速度 |
| [Nominatim](https://nominatim.openstreetmap.org/) | 地名 → 坐标 | 200，约 200ms~1.3s |

**OSRM 为什么用 FOSSGIS 实例，而不是官方 demo**：探测时发现，官方 demo（`router.project-osrm.org`）的 `/foot/` 和 `/driving/` 返回**完全相同**的结果（21.6km、32 分钟）。它只部署了驾车路线，你请求步行它不报错，直接给你驾车时间。如果没有探测直接用它，agent 会告诉用户"步行 32 分钟可以走完 21 公里"。FOSSGIS 按出行方式分开部署（`/routed-foot/`、`/routed-car/`），结果才正确。这又是一次"先探测再写代码"的收获，见 [踩坑日志](pitfalls.md#osrm-官方-demo-的步行结果其实是驾车)。

## 代码走读

两个 server 用同样的分层：

```
mcp_servers/weather/              mcp_servers/route/
├── client.py   网络层 + 纯函数      ├── client.py   网络层 + 纯函数 + 排序算法
└── server.py   MCP 工具定义         └── server.py   MCP 工具定义
```

这和 seichi 的 `repository.py` / `server.py` 分层是同一个思路：**server.py 只管"怎么暴露给 LLM"，client.py 负责"数据从哪来"**。

### weather：一个工具

[weather/server.py](../backend/mcp_servers/weather/server.py)：

```python
@mcp.tool()
async def get_weather_forecast(
    lat: Annotated[float, Field(ge=-90, le=90, description="纬度")],
    lng: Annotated[float, Field(ge=-180, le=180, description="经度")],
    days: Annotated[int, Field(ge=1, le=MAX_FORECAST_DAYS, description="预报天数，从今天算起")] = 7,
) -> dict[str, Any]:
```

和 03 章的 seichi 比，有两个新东西：

**① `async def`**。工具要等网络响应，写成异步函数，等待期间事件循环可以处理别的请求。MCP SDK 对同步和异步工具都支持，调网络就用 async。

**② `Annotated[类型, Field(约束)]`**。这是 pydantic 的写法，把约束直接写进类型标注。生成的 JSON Schema 里会带上 `"minimum": 1, "maximum": 16`，效果有两个：

- **LLM 能看到约束**：schema 会随工具定义一起放进 LLM 的提示词（原理见 [03 章：LLM 怎么知道有哪些工具](03-mcp-server.md#llm-怎么知道有哪些工具json-schema)），写明了最大 16，LLM 传错的概率就小很多。但这只是**降低概率**，不能保证 LLM 一定遵守
- **越界参数在发请求之前就被拦下**：这才是真正的保障。实测传 `days=30`，SDK 在调用函数之前就返回校验错误 `less than or equal to 16`，不会浪费一次网络请求，LLM 看到错误原因后可以自己修正重试

这是 [03 章原则 2「类型标注就是参数约束」](03-mcp-server.md#2-类型标注就是参数约束)的进一步用法：`int` 只能约束类型，`Field` 还能约束取值范围。

[weather/client.py](../backend/mcp_servers/weather/client.py) 的 `parse_daily()` 做格式转换。Open-Meteo 返回的是**按字段分列**的格式：

```json
{"time": ["2026-09-29", "2026-09-30"], "temperature_2m_max": [21.9, 26.0], "weather_code": [63, 2]}
```

转成**按天分行**的格式，并补充 LLM 需要的信息：

```json
{"today": "2026-09-29", "days": [
  {"date": "2026-09-29", "weekday": "周二", "weather": "中雨", "temp_max_c": 21.9, ...},
  {"date": "2026-09-30", "weekday": "周三", "weather": "局部多云", "temp_max_c": 26.0, ...}
]}
```

其中几处是专门为 LLM 加的：

- **`weekday`**：用户问"这周末天气怎么样"时，LLM 要知道哪两天是周末。LLM 不擅长从日期推算星期几，直接给出来更可靠
- **`today`**：LLM 不知道当前日期（它的知识停在训练数据截止那天）。`timezone=auto` 让 Open-Meteo 按坐标所在地的时区返回日期，列表第一天就是日本当地的"今天"
- **`weather: "中雨"` 而不是 `weather_code: 63`**：WMO 天气代码是一串数字，LLM 不一定认识，直接给中文描述

### route：两个工具

[route/server.py](../backend/mcp_servers/route/server.py) 有两个工具：

- **`geocode(query)`**：地名 → 坐标。用户说"从京都站出发"，京都站不在圣地列表里，需要先查出坐标
- **`plan_route(stops, mode, optimize)`**：给定一组点，算出访问顺序、每段的距离和时间

没有单独做"A 到 B 要多久"的工具：`plan_route` 传两个点、设 `optimize=false` 就是这个功能。**工具越少，LLM 选错工具的机会越少**，工具描述里写明这种用法就行。

#### 嵌套参数：用 pydantic 模型

```python
class Stop(BaseModel):
    name: str = Field(description="地点名称，用于在结果中标识")
    lat: float = Field(ge=-90, le=90, description="纬度")
    lng: float = Field(ge=-180, le=180, description="经度")

async def plan_route(stops: Annotated[list[Stop], Field(min_length=2, max_length=12)], ...):
```

参数是"对象的列表"时，用 pydantic 模型描述对象的结构。SDK 会生成嵌套的 JSON Schema，LLM 按这个结构传参数，传进函数的已经是校验过的 `Stop` 对象。

#### 距离矩阵：一次请求拿到 n×n 结果

`plan_route` 要比较不同访问顺序的总时间，需要**每两个点之间**的时间。4 个点就是 12 个方向的时间，逐对请求要发 12 次。OSRM 的 `table` 服务一次请求返回整个矩阵：

```python
coords = ";".join(f"{lng},{lat}" for lat, lng in points)   # ← 注意顺序
url = f"{OSRM_BASES[mode]}/table/v1/driving/{coords}"
```

**OSRM 的坐标顺序是「经度,纬度」**，和我们习惯的「纬度,经度」相反（GeoJSON 也是经度在前）。写反了不会报错，只是算出一个大海里某个点的路线。所以测试里专门检查了请求 URL 的坐标顺序（`test_plan_route_optimizes_and_sends_lng_lat`）。

#### 排序算法：穷举 vs 最近邻

有了时间矩阵，问题变成：从第一个点出发，经过所有其他点各一次，怎么排顺序总时间最短？这是旅行商问题（TSP）的一个变种，没有已知的快速精确解法。[client.py](../backend/mcp_servers/route/client.py) 的 `best_order()` 分两种情况：

- **点数 ≤ 8：穷举所有排列**。起点固定，剩下 7 个点有 7! = 5040 种排列，逐个算总时间取最小，结果一定是最优的
- **点数 > 8：最近邻启发式**。每一步走到"离当前位置最近的、还没去过的点"。很快，但不保证最优

为什么在 8 这里切换：排列数随点数阶乘增长，12 个点就是 11! ≈ 4000 万种，穷举要跑好几秒。一天的巡礼行程通常不超过 8 个点，大多数情况都能拿到最优解。返回结果里的 `method` 字段标明用了哪种方法（`exact` / `nearest_neighbor` / `given`）。

#### 给 LLM 的提示字段

- **`suggest_transit`**：OSRM 只算步行和驾车，**不包含电车和巴士**，而日本巡礼主要靠电车。所以步行超过 45 分钟的路段会标记 `suggest_transit: true`，工具描述里告诉 LLM 看到这个标记就建议用户改乘公共交通。工具做不到的事情，也要让 LLM 知道
- **`estimated`**：见下一节

## 关键设计

### 1. 降级：服务挂了，给估算值而不是报错

`RouteClient.matrix()` 在 OSRM 不可用时（超时、断网、返回错误），不抛异常，改用估算：

```
估算时间 = 直线距离 × 1.3（绕路系数） ÷ 平均速度（步行 4.5km/h，驾车 30km/h）
```

同时在结果里标记 `estimated: true`，工具描述里告诉 LLM："estimated=true 表示时间为估算值，请提醒用户仅供参考"。

这是 [01 章原则五「降级而不是崩溃」](01-architecture.md#原则五降级而不是崩溃)的具体做法。对比两种处理方式：

| | OSRM 挂了时用户听到的 |
|---|---|
| 返回错误 | "抱歉，路线服务暂时不可用" |
| 降级估算 | "建议按 A → C → B 的顺序，步行约 40 分钟（估算值，仅供参考）" |

第二种有用得多。关键是**降级必须诚实**：必须标明是估算值，不能把估算值当准确值给用户。

OSRM 也可能只对个别点对返回 `null`（比如某个点在路网覆盖不到的地方），这时只对那几个点对用估算值补上，同样标记 `estimated`。

**天气为什么没做降级？** 天气没有合理的估算方法，编造一个天气比如实说"服务不可用"更糟糕。所以天气服务出错时返回 `{"error": ...}`。**能不能降级，取决于有没有一个诚实的近似值可以给。**

### 2. 依赖注入：不连网络也能测试

两个 client 的构造函数都有一个 `transport` 参数：

```python
class WeatherClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, ...):
```

- 生产环境：`transport=None`，httpx 走真实网络
- 测试：注入 `httpx.MockTransport(handler)`，所有请求都交给 `handler` 函数处理，由我们决定返回什么

```python
def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=FAKE_RESPONSE)          # 模拟正常响应

def handler(request):
    raise httpx.ConnectError("unreachable", request=request)  # 模拟断网

lambda request: httpx.Response(503)                          # 模拟服务端错误
```

"断网时是否正确降级"这种情况，用真实网络根本测不了，有了 MockTransport 就是一行代码。`handler` 还能检查收到的请求，比如确认坐标顺序、确认带了 User-Agent。

server.py 里 `client` 是模块级变量，测试用 pytest 的 `monkeypatch` 把它替换成注入了 MockTransport 的实例：

```python
monkeypatch.setattr(server, "client", WeatherClient(transport=httpx.MockTransport(handler)))
```

`monkeypatch` 会在测试结束后自动恢复原值，不影响其他测试。

### 3. 纯函数与网络分离

`parse_daily()`、`haversine_km()`、`best_order()` 都是**纯函数**：输入相同，输出一定相同，不碰网络、不读文件。它们可以用最简单的方式测试（直接传参数、检查返回值），连 MockTransport 都不需要。

写代码时的判断方法：**能不碰网络就能完成的逻辑，都抽成纯函数**。网络层剩下的代码越少越好。

### 4. 遵守公共服务的使用政策

OSM 社区的服务是志愿者维护的，使用政策有明确要求：

- **带可识别的 User-Agent**：所有请求都带 `Aoki-Agent/0.1 (learning project; ...)`。服务方看到异常流量时能知道是谁
- **Nominatim 每秒最多 1 次请求**：`_Throttle` 类保证两次地理编码请求至少间隔 1.1 秒。用 `asyncio.Lock` 保证多个并发调用也会排队
- **缓存**：同一个地名只查一次，结果存在 `_geocode_cache` 里

这一点在 03 章已经吃过亏：Anitabi 在大约 25 个请求后就返回 403（被 Cloudflare 拦截）。

### 5. 真实网络的测试单独放

[test_external_network.py](../backend/tests/test_external_network.py) 会真实调用 Open-Meteo、OSRM、Nominatim。它用于确认第三方服务的实际行为和我们的假设一致，但受网络和限流影响，不适合每次都跑。

[pytest.ini](../pytest.ini) 里：

```ini
addopts = -m "not network"
markers =
    network: 访问真实外部 API 的集成测试（受网络与限流影响，默认不跑）
```

默认跳过，需要时显式 `python -m pytest -m network`。单元测试（MockTransport）保证逻辑正确，集成测试（真实网络）保证对外部服务的假设正确。两种测试各管一件事。

## 设计取舍

**每次调用新建 AsyncClient，还是全局共享一个？**

共享一个 `httpx.AsyncClient` 可以复用 TCP 连接，性能更好，是 httpx 官方推荐的做法。这里选择每次调用新建，原因是 **AsyncClient 创建后会绑定到当时的事件循环**。pytest 的每个异步测试函数用各自的事件循环，如果共享一个模块级的 client，第二个测试就会报 `Event loop is closed`。实测过：两个测试共用一个模块级 `AsyncClient` 请求 Open-Meteo，第一个通过，第二个在复用连接池里的连接时抛出 `RuntimeError: Event loop is closed`。

代价是每次请求多一次建立连接的开销（几十毫秒）。一个工具调用本身要几百毫秒到一秒多，这点开销可以接受。如果将来要优化，正确做法是在 server 启动时（lifespan）创建 client，关闭时释放。

**OSRM table 还是 trip 服务？**

OSRM 自带 `trip` 服务，能直接求解访问顺序。选择 table 加自己写排序，原因是：排序算法是很好的学习材料；`method` 字段能明确告诉 LLM 结果是不是最优的；而且 table 失败时，同一套排序算法可以直接用在估算矩阵上。用 trip 的话，降级时还得另写一套逻辑。

**要不要支持公共交通？**

日本巡礼主要靠电车，路线规划理想情况下应该包含电车换乘。但免费的公共交通路线 API 基本没有（Google Maps 需要绑卡，日本本地的服务如 NAVITIME 需要商业授权）。demo 阶段的做法是：步行和驾车算准确，用 `suggest_transit` 标记诚实地告诉 LLM"这段该坐车"，由 LLM 给出一般性建议。

## 附：用 geocode 校正 mock 数据

路线服务写好后，用 `geocode` 核对了 [mock_spots.json](../backend/mcp_servers/seichi/data/mock_spots.json) 里全部 22 个地标的坐标：

- 大部分偏差在 250m 以内，demo 可以接受
- 鎌倉高校前駅、気象神社、大洗的几个地标偏差 0.6~2.4km，已经改为 Nominatim 返回的坐标
- **须贺神社的查询结果匹配到了仙台的另一座同名神社，偏差 302km**。所以不能不看结果就批量替换：同名地点很多，这正是 `geocode` 工具返回多个候选、并在描述里要求 LLM "根据上下文选择"的原因

## 动手验证

```powershell
# 1. 单元测试（不连网络）
python -m pytest -v -k "weather or route"

# 2. 集成测试（真实调用三个外部服务）
python -m pytest -m network -v

# 3. 直接调用：宇治未来 3 天的天气
python -c "import asyncio, json; from backend.mcp_servers.weather.server import get_weather_forecast as f; print(json.dumps(asyncio.run(f(lat=34.89, lng=135.81, days=3)), ensure_ascii=False, indent=2))"
```

## 延伸思考

1. `suggest_transit` 的阈值是 45 分钟。应该写死在代码里，还是作为参数让 LLM 传？如果用户说"我很能走，两小时以内都步行"，现在的设计能满足吗？
2. 降级估算用了"绕路系数 1.3"。这个数字在市区和山区一样吗？如果估算结果偏差很大，`estimated: true` 足以保护用户吗？
3. 地理编码缓存 `_geocode_cache` 没有上限，也不会过期。在 demo 里没问题，在长期运行的服务里会有什么问题？怎么改？
4. 天气工具返回的是一个坐标的预报。用户问"这周末去镰仓巡礼天气怎么样"，LLM 需要先拿到镰仓的坐标。它可以从哪两个工具拿到？哪个更合适？

> **后续（2026-10-27）**：路线包里新增了 [transit.py](../backend/mcp_servers/route/transit.py)（电车换乘客户端），它不是独立的 MCP 工具，而是被圣地 server 的 `plan_pilgrimage` 作为库调用；`RouteClient.matrix()` 同样被复用来算巡礼路线的步行时间矩阵。详见 [11 章](11-pilgrimage-planner.md#电车transit-api)。
