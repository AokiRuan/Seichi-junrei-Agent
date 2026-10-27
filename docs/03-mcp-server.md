# 03 · MCP Server：圣地巡礼

## 本章目标

读完能回答：

- MCP 是什么？和"在后端写个 Python 函数给 LLM 调"有什么区别？
- stdio 传输下，两个进程之间到底在传什么？为什么 server 里不能 `print()`？
- `@mcp.tool()` 装饰一个普通函数之后，LLM 是怎么知道这个工具的用途和参数的？
- 给 LLM 用的工具，和给程序员用的函数，设计上有什么不同？
- 为什么数据源要单独抽一层 Repository？
- 面对一个陌生的第三方 API，怎么在写代码之前摸清它的真实行为？
- 一部作品有几百个地标时，给 LLM 的工具该怎么设计？

## 核心概念

### MCP 解决什么问题

没有 MCP 时，每个 agent 框架都有自己的工具定义方式——LangChain 一套、OpenAI Assistants 一套、[学习期的 hello_agents](../learn/tools/registry.py) 又一套。写好的工具换个框架就得重写。

**MCP（Model Context Protocol）把"工具提供方"和"工具使用方"用一个标准协议隔开**：

```
┌────────────────┐    MCP 协议     ┌────────────────┐
│  MCP Client    │ ◀────────────▶ │  MCP Server    │
│  (我们的 agent) │  JSON-RPC 2.0  │  (圣地巡礼工具) │
└────────────────┘                └────────────────┘
```

同一个 seichi server，我们的 agent 能用，Claude Desktop、Cursor 也能直接接——不用改一行代码。

MCP 定义了三种能力：**tools**（可调用的函数）、**resources**（可读取的数据）、**prompts**（提示词模板）。本项目只用 tools。

### stdio 传输：两个进程怎么说话

MCP 支持三种传输方式：`stdio`、`sse`、`streamable-http`。我们用 stdio：

1. Client 把 server **作为子进程启动**
2. Client 往子进程的 **stdin** 写 JSON-RPC 请求
3. Server 从 stdin 读请求，处理完往 **stdout** 写响应
4. Client 从子进程的 stdout 读响应

一次工具调用在管道里大致长这样：

```jsonc
// client → server (stdin)
{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
 "params": {"name": "search_anime", "arguments": {"title": "莉可丽丝"}}}

// server → client (stdout)
{"jsonrpc": "2.0", "id": 3, "result": {"content": [...], "structuredContent": {...}, "isError": false}}
```

完整的会话顺序是：`initialize`（握手、交换能力）→ `tools/list`（拉取工具清单）→ 若干次 `tools/call`。

**由此得出一条硬规则：stdio server 里绝对不能 `print()`。** stdout 是协议通道，你 print 的任何东西都会被 client 当成 JSON-RPC 消息解析，直接破坏通信。调试信息写 stderr（`print(..., file=sys.stderr)` 或用 `logging`，它默认就写 stderr）。

### LLM 怎么知道有哪些工具：JSON Schema

LLM 最终读到的是文字，但不是随手写的说明，而是一份**结构化的工具定义**。以天气工具为例，Day 2 调 LLM 时放进请求 `tools` 参数的内容是（实测输出，有精简）：

```json
{
  "type": "function",
  "function": {
    "name": "get_weather_forecast",
    "description": "查询某地未来几天的逐日天气预报（最多 16 天）。……",
    "parameters": {
      "type": "object",
      "properties": {
        "lat":  {"type": "number",  "minimum": -90, "maximum": 90, "description": "纬度"},
        "lng":  {"type": "number",  "minimum": -180, "maximum": 180, "description": "经度"},
        "days": {"type": "integer", "minimum": 1, "maximum": 16, "default": 7, "description": "预报天数，从今天算起"}
      },
      "required": ["lat", "lng"]
    }
  }
}
```

`parameters` 里的内容就是 **JSON Schema**：一种描述"一段 JSON 数据应该长什么样"的标准格式。`type` 是类型，`properties` 是有哪些字段，`required` 是哪些必填，`minimum` / `maximum` 是取值范围。

**文字描述就在 schema 里面**：docstring 成为工具级的 `description`，`Field(description=…)` 成为参数级的 `description`。所以不是"用文字描述"和"用 schema"二选一，schema = 文字描述 + 机器可读的精确约束。

LLM 服务商收到这份定义后，会把它转换并放进模型实际读取的提示词里（格式由服务商决定，我们看不到）。模型训练时见过大量 JSON Schema，读到 `"maximum": 16` 就像读到"最多 16 天"一样能理解。

对比学习期的 [learn/agent/simple_agent.py](../learn/agent/simple_agent.py)：它把工具说明拼成纯文字放进 system prompt，让 LLM 输出 `[TOOL_CALL:名字:参数]` 这样的文本，再用正则解析。换成 schema 后，同一份定义同时起三个作用：

1. **提示 LLM**：和文字一样，但更精确。"最多十几天"是模糊的，`"maximum": 16` 不是
2. **服务端校验**：MCP SDK 用同一份 schema 检查 LLM 传来的参数，越界的在执行前就会被拒绝（04 章实测 `days=30` 被拦下）
3. **LLM 的输出也是结构化的**：LLM 在 `tool_calls` 字段里返回严格的 JSON 参数，不再需要用正则解析文本

还有一个好处：schema 从函数签名**自动生成**，改了代码 schema 跟着变，不会和代码不一致。

**注意：LLM 看到约束不等于一定遵守**，它仍可能传越界的值。提示只是让出错变少，服务端校验才能保证出错无害，两者缺一不可。

为什么选 stdio 而不是 HTTP：seichi server 和后端跑在同一个容器里，子进程不需要端口、不需要网络配置、父进程退出子进程自动跟着退。外部的天气/路线服务如果是远程的，才需要 HTTP 传输。

## 代码走读

三个文件，职责分明：

```
mcp_servers/seichi/
├── server.py          MCP 协议层：把函数暴露成工具
├── repository.py      数据访问层：数据从哪来、怎么查
├── anitabi.py         下载 Anitabi 数据、生成快照（见下文「真实数据」）；快照 v2 起还算好巡礼片区
├── planner.py · itinerary.py   巡礼规划算法与编排（plan_pilgrimage 工具，见 11 章）
└── data/
    ├── anitabi_snapshot.json   真实数据快照（自己生成，不进 git）
    ├── anitabi_raw/            下载的原始文件，--offline 重建用（不进 git）
    └── mock_spots.json         手写的 mock，测试和没有快照时用
```

### server.py：从普通函数到 MCP 工具

[server.py](../backend/mcp_servers/seichi/server.py) 核心就这几行：

```python
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("seichi")
repo = get_repository()

@mcp.tool()
def list_spots(anime_id: int) -> list[dict[str, Any]]:
    """列出某作品的全部圣地：名称、经纬度、对应集数与时间点、所在城市。

    anime_id 来自 search_anime 的返回结果。
    """
    return repo.list_spots(anime_id)
```

`@mcp.tool()` 做了三件事：

1. **函数名 → 工具名**：`list_spots`
2. **docstring → 工具描述**：这段文字会原样发给 LLM，LLM 靠它判断什么时候该用这个工具
3. **类型标注 → 参数的 JSON Schema**：`anime_id: int` 自动变成

   ```json
   {"properties": {"anime_id": {"type": "integer", ...}}, "required": ["anime_id"]}
   ```

第 3 点就是 [learn/tools/registry.py](../learn/tools/registry.py) 里手工拼的那一大段 `to_openai_schema()`——现在从函数签名自动生成，不会再出现"改了函数参数但忘了改 schema"的问题。

类型标注不只是生成文档，**还会做运行时校验**。实测：传 `{"anime_id": "42"}` 会被自动转成整数 42；传 `{"anime_id": "nope"}` 会被拒绝，返回的错误信息里带着完整的 pydantic 校验详情——LLM 看到后可以自己修正参数重试。

**返回值**：工具返回 dict 或 list，SDK 会同时生成两份结果：`content`（JSON 序列化成的文本，给只认文本的 client 用）和 `structured_content`（结构化数据）。注意：**返回 list 时，`structured_content` 会被包成 `{"result": [...]}`**——Day 2 写 client 解析结果时要记得这一点。

最后：

```python
if __name__ == "__main__":
    mcp.run(transport="stdio")
```

这个文件用了相对导入（`from .repository import ...`），所以必须以模块方式启动：`python -m backend.mcp_servers.seichi.server`。原因见 [02 章](02-project-setup.md#包与导入为什么必须在根目录运行)。

### repository.py：把"数据从哪来"隔离出去

[repository.py](../backend/mcp_servers/seichi/repository.py) 定义了接口和实现：

```python
class SeichiRepository(ABC):
    def search_anime(self, title: str, limit: int = 5) -> list[dict]: ...
    def list_spots(self, anime_id: int, limit=15, near=None, radius_km=3) -> dict | None: ...
    def get_spot(self, spot_id: str) -> dict | None: ...

class AnitabiRepository(...):    # 读快照
class MockRepository(...):       # 读手写的 mock

def get_repository() -> SeichiRepository:
    ...                          # 按环境变量 SEICHI_DATA_SOURCE 选，见下文「真实数据」
```

（这是当前的接口。Day 1 时 `list_spots` 只有 `anime_id` 一个参数、直接返回列表，接入真实数据后改了，原因见下文。）

> **Python 知识点：`ABC` 与 `@abstractmethod`**
>
> `ABC`（Abstract Base Class，抽象基类）配合 `@abstractmethod`，定义"子类必须实现哪些方法"的合同，并由 Python 在运行时强制检查。实测：
>
> - `SeichiRepository()` 直接实例化 → `TypeError: Can't instantiate abstract class ... without an implementation for abstract methods 'get_spot', 'list_spots', 'search_anime'`
> - 子类漏实现 `get_spot` → **类定义不报错，实例化时报错**，并指明漏了哪个
>
> 价值在于把错误提前：不用 ABC 的话，漏写的方法要等 LLM 第一次调它时才抛 `AttributeError`（可能在演示现场）；用了 ABC，服务启动、`get_repository()` 创建实例的那一刻就会失败。
>
> 为什么不用另外两种写法：普通类 + `raise NotImplementedError` 要到调用时才报错，适合只有一个实现的占位（如 `agent/llm.py` 里的骨架）；`typing.Protocol` 只在 mypy/pyright 静态检查时生效，运行时不阻止实例化不完整的类，适合约束不能修改的第三方类。我们的实现类都是自己写的、注定有多个，所以用 ABC。`store/base.py` 的 `SessionStore` 同理。

server.py 只认识 `SeichiRepository` 这个接口，不知道数据来自 JSON 还是 HTTP API。这是 [01 章原则四](01-architecture.md#原则四会变的东西先立接口)的直接应用——而且这次是**真用上了**：数据源原计划用 Anitabi，实测发现不可用后，切 mock 只改了 `get_repository()` 一处；后来再切回真实数据，agent 和 MCP client 也一行没改。

几个实现细节：

- **`@lru_cache` 加载 JSON**：数据文件只在第一次访问时读一次，之后都从内存拿（`_load_json.cache_info()` 可以看命中情况）。需要注意三点：
  - **返回的是同一个对象**。实测改了 `_load()` 返回的 dict，其他调用方拿到的也是改过的数据。所以 `_anime_brief()` / `_spot_out()` 都新建 dict 返回，从不修改原始数据——这是这个文件必须保持的隐性约定
  - 改了 JSON 要重启进程才生效，测试里可用 `_load_json.cache_clear()`
  - `_load_json` 故意写成模块级函数而不是方法：`lru_cache` 加在方法上时 `self` 会成为缓存 key 的一部分，每个实例各缓存一份，而且缓存会一直持有实例引用、导致无法回收
- **`_anime_brief()` 故意不带地标列表**：见下文「给 LLM 设计工具」第 3 条
- **`_spot_out()` 做字段转换**：把 Anitabi 的 `geo: [lat, lng]` 拆成 `lat` / `lng`，`ep` / `s` 改名成 `episode` / `timestamp_sec`。原因见下文第 4 条

## 给 LLM 设计工具的六条原则

这是本章最重要的部分。**工具的使用者是 LLM，不是程序员**——这个视角转换会改变很多设计决策。

### 1. docstring 是写给 LLM 的 prompt

程序员看 docstring 是为了理解实现；LLM 看 docstring 是为了**决定要不要调、怎么调**。所以要写清楚：这个工具干什么、参数从哪来、拿到结果后下一步做什么。

```python
"""按作品名搜索动画，返回作品 id 与基本信息。

拿到 anime_id 后用 list_spots 查该作品的圣地。       ← 告诉 LLM 下一步
支持中文名、日文原名、城市名模糊匹配。                ← 告诉 LLM 能传什么
"""
```

"拿到 anime_id 后用 list_spots"这句话看起来多余，但它直接影响 LLM 的调用链规划。没有它，LLM 可能拿着作品名直接去调 `list_spots`，然后失败。

### 2. 类型标注就是参数约束

`anime_id: int` 不只是给 IDE 看的。它生成的 schema 会告诉 LLM "这里要整数"，同时在运行时拦住错误输入。能用 `int` 就别用 `str`，能用 `Literal["a", "b"]` 就别用自由字符串——约束越紧，LLM 越不容易传错。

### 3. 输出要省 token

`search_anime` 只返回作品摘要和 `spots_count`，**不返回地标列表**。如果搜一个关键词命中 3 部作品、每部 80 个地标，一次返回 240 条数据，会直接塞满 LLM 的上下文窗口——而 LLM 这时候可能只是想确认"是哪部作品"。

分层返回：先给摘要，LLM 需要细节时再调下一个工具。

### 4. 字段名要自解释

```python
"lat": 35.7108,          # 而不是 "geo": [35.7108, 139.7967]
"lng": 139.7967,
"name_ja": "浅草寺 雷門",  # 而不是 "name"
"name_cn": "浅草寺 雷门",  # 而不是 "cn"
"episode": 1,            # 而不是 "ep"
"timestamp_sec": 210,    # 而不是 "s"
```

`geo: [a, b]` 哪个是纬度哪个是经度？LLM 和前端都得猜（而且不同系统的约定还真不一样，GeoJSON 就是先经度后纬度）。`s` 是什么？秒？季？LLM 看不懂的字段，它就会瞎用或忽略。

### 5. 错误是数据，不是异常

```python
@mcp.tool()
def get_spot(spot_id: str) -> dict:
    spot = repo.get_spot(spot_id)
    if spot is None:
        return {"error": f"未找到圣地 {spot_id}"}   # 而不是 raise
    return spot
```

实测对比了三种错误在 stdio 传输下的表现：

| 情况 | client 收到的 `is_error` | LLM 看到的信息 |
|---|---|---|
| 工具里 `raise ValueError("bad x=1")` | `True` | `Error executing tool boom` —— **真实原因被隐藏了** |
| 参数类型不对 | `True` | 完整的 pydantic 校验错误，LLM 能据此修正 |
| 主动 `return {"error": "..."}` | `False` | 你写的错误信息，完全可控 |

第一行是关键：**未捕获的异常，其消息不会传给 client**（这是 SDK 的安全设计，防止泄露服务端内部信息）。LLM 只知道"出错了"，不知道为什么，也就没法调整。主动返回错误信息，LLM 才能决定"换个 id 重试"还是"告诉用户没找到"。

另一个发现对 Day 2 很重要：**跨进程时，工具出错 client 端不会抛异常**，而是拿到 `is_error=True` 的正常结果。所以 mcp_client 不能靠 `try/except` 判断工具是否失败，必须检查 `is_error`。

### 6. 工具粒度：宁可多一步，不要一个大工具

也可以只做一个 `find_spots_by_title(title)` 一步到位。拆成 search → list → get 三步的理由：

- 搜索可能命中多部作品（搜"莉可丽丝"会命中正片、新作、剧场版），需要 LLM 或用户确认是哪一部
- 用户可能只是问"有哪些作品在宇治取景"，根本不需要地标详情
- 每个工具职责单一，描述写得清楚，LLM 更容易选对

代价是多了 LLM 调用轮次（更慢、更费 token）。这个取舍在评测阶段可以用数据验证。

## 案例：先探测，再写代码

计划里 Day 1 上午的任务是"验证 Anitabi API"。这个验证过程本身值得单独讲，因为它**避免了一个 Day 3 才会暴露的致命问题**。

### 方法

1. **读文档**，列出要用的端点
2. **写探测脚本打真实请求**，不要只信文档
3. **统计字段出现率，而不是看一条样本**：

   ```python
   from collections import Counter
   c = Counter(key for point in points for key in point)
   # {'id': '84/84', 'name': '84/84', 'image': '83/84', 'ep': '80/84', ...}
   ```

   只看第一条样本，你会漏掉"大部分数据缺某个字段"这种问题
4. **换几个样本交叉验证**：一部作品有问题可能是个例，两部都有问题就是系统性的
5. **注意限流**：探测时别一口气发几十个请求

### 发现

文档里每个地标都有 `geo` 坐标。实测：

- 两部作品的地标，`geo` 字段出现率 **0/84、0/63**——线上 API 已经不返回了
- 部分坐标能从 `originURL` 里救回（Google Maps 链接带 `ll=纬度,经度` 参数），但只覆盖 **42%**
- 约 25 个请求后被 Cloudflare 403，45 秒后仍未恢复
- Anitabi **没有按标题搜索的端点**；用 Bangumi 官方的 `POST api.bgm.tv/v0/search/subjects` 可以补上

**后续（2026-10-21）**：参考开源客户端 [boiboif/anitabi-app](https://github.com/boiboif/anitabi-app) 找到了另一条路——网站地图页自己用的数据文件 `www.anitabi.cn/d/g.json` 里有全部地标的坐标。用同样的方法验证：只发 2 个请求，统计出 1552 部作品、52244 个地标**坐标出现率 100%**。思路是：**官方前端要在浏览器里画地图，坐标总得传到浏览器**，所以去看前端拿数据的请求。详见 [项目计划 4.1 节](00-project-plan.md#新发现网站内部数据文件有完整坐标2026-10-21)。

**如果照着文档直接写代码**：Day 1 的 MCP server 能跑，Day 2 的 agent 能跑，到 Day 3 做地图打点时才发现——没有坐标。那时候已经没有时间调整方案了。

完整实测数据见 [项目计划 4.1 节](00-project-plan.md#41-圣地巡礼-mcp-server自建)。

### mock 数据的设计

[mock_spots.json](../backend/mcp_servers/seichi/data/mock_spots.json)：5 部作品、22 个地标，坐标零缺失。

设计要点是**格式对齐上游**：结构和 Anitabi 的响应完全一致，唯一的差异是给每个地标补上了 `geo`。这样**差异本身就是待办清单**——将来接真实 API 时，`AnitabiRepository` 要额外解决的事情就是"坐标补全"，一目了然。

`_meta` 字段诚实地标注了数据的局限：哪些 id 是真实验证过的、坐标只是近似值、不能用于实际导航。mock 数据最怕的是被当成真数据用。

## 真实数据：Anitabi 快照

Day 1 因为没有坐标退回了 mock。Day 3 之后参考开源客户端找到了网站地图页自用的数据文件（发现过程见上面「后续」），把数据源换成了真实数据。**server.py 的工具接口变了一些（下面会讲为什么），但 agent、loop、MCP client 一行没改**——数据层的隔离在这里第二次用上了。

### 数据从哪来、为什么做成快照

[anitabi.py](../backend/mcp_servers/seichi/anitabi.py) 下载两类文件、按地标 id 拼接：

| 文件 | 内容 |
|---|---|
| `/d/g.json?d=…` | 全部作品（标题、城市、封面），以及每部作品所有地标的 `[id, 纬度, 经度, 优先级, id, 纬度, …]` 扁平数组 |
| `/d/g{n}.json?d=…` | 第 n 片作品（每片 250 部）的地标详情：名称、图片、集数、秒数……**不含坐标** |

`d` 是"距 1970 年的天数 + 6，转 36 进制"，每天换一次，用来让 CDN 回源拿新数据。

```powershell
python -m backend.mcp_servers.seichi.anitabi
# 完成：1552 部作品、51166 个镜头 → 23225 个地点，15.5 MB，用时 14s
```

**为什么是"下载一次存成快照"，而不是每次工具调用都实时请求？**

- **Cloudflare 限流**：Day 1 探测时连发约 25 个请求就被 403 了。快照只需 8 个请求（g.json + 7 片），逐个发、中间停 1 秒
- **演示稳定性**：快照在本地，演示时圣地查询零网络依赖。实测 server 启动 0.7 秒，单次搜索十几毫秒（第一次建索引约 260 毫秒）
- **使用规范**：数据遵循 CC BY-NC-SA 4.0（非商业、署名、相同方式共享），参考项目的贡献指南也要求不绕过速率限制。低频下载本身就是对数据提供方的尊重
- **不随代码分发**：快照在 `.gitignore` 里。把别人的数据集放进公开仓库是再分发，许可上有灰色地带；每个环境自己下载（部署时在 Docker 构建里下载，Day 4）

代价是数据不是实时的。地标数据变化很慢，隔段时间重新跑一次即可。

**格式是"按位置的数组"**（第 10 个元素是纬度……），没有字段名、没有兼容承诺，网站改版就可能失效。所以字段位置集中定义成常量（`G_LAT`、`P_FIELDS`），解析失败的条目直接丢弃，而不是让整个快照失败。万一将来完全失效，`get_repository()` 在没有快照时会自动退回 mock，demo 不会挂。

**署名**：地图右下角写了「圣地数据 © Anitabi (CC BY-NC-SA 4.0)」，每个圣地弹窗有「在 Anitabi 查看」的跳转链接（`https://www.anitabi.cn/map?bangumiId=…&pid=…`，页面能打开，但 `pid` 能否直接定位到该地标**未实测**）。

### 把镜头合并成地点

真实数据和 mock 最大的不同是**量**：《上低音号》一部就有 582 个地标。仔细看会发现很多是**同一地点的不同镜头**——北宇治高校（原型莵道高中）一处就有 214 个。直接给 LLM 一个 582 条的列表既浪费 token，也没法回答"有哪些圣地"。

`group_locations()` 分两步合并：

1. **先按文件夹**：数据维护者手工建的"文件夹"地标（`is_folder`），子地标用 `fid` 指向它。这是人工分组，最可信。实测文件夹内的点最远能相距 1 公里（「京坂木幡站」文件夹里有电车内的镜头），按距离合并会拆散它们
2. **再按距离**：剩下的按优先级从高到低，离已有地点中心 100 米以内就并进去

为什么不按名字合并？实测名字里常带镜头描述：「宇治桥 不甘心」「宇治桥 不甘心 全景2」「电车内 薯条吃太多了」。同一地点名字各不相同。

为什么是 100 米？实测对比（只按距离合并时）：

| 合并半径 | 《上低音号》582 个地标 → | 《摇曳露营△》二期 1073 个 → |
|---|---|---|
| 50 米 | 78 个地点（33 个孤点） | 310 |
| **100 米** | **52** | **225** |
| 200 米 | 36 | 174 |
| 400 米 | 27（最大的一个吞了 236 个镜头） | 140 |

50 米把同一座桥拆成好几处；200 米开始把相邻的两个地点并在一起。再加上文件夹合并，《上低音号》最终是 **40 个地点**。

合并时的两个细节：

- **地点名优先用文件夹名**；文件夹没有中文名就留空，**不借成员的**——实测「京坂木幡站」文件夹的成员里有「京阪黄檗站」的镜头，借过来就张冠李戴了
- **名称清洗**：原始数据里混着零宽空格（`京都​コンサート​ホール`），要去掉。但**不能做 NFKC 全角转半角**——实测它把「吹响吧！上低音号」的「！」变成了「!」。NFKC 只在搜索比较时用

### 两个实现，一套查询逻辑

[repository.py](../backend/mcp_servers/seichi/repository.py)：

```python
class LocationRepository(SeichiRepository):    # 查询逻辑：搜索、列表、详情
    def _anime(self) -> list[dict]: ...         # 子类只负责「数据从哪来」

class AnitabiRepository(LocationRepository):   # 读快照
class MockRepository(LocationRepository):      # 读 mock_spots.json，加载时转成快照格式
```

mock 在加载时被转换成和快照一样的结构（每个地标就是一个只有一个镜头的地点），所以查询逻辑只写一份。如果两套逻辑各写一遍，测试测的是 mock 那套，线上跑的却是另一套。

`get_repository()` 按环境变量 `SEICHI_DATA_SOURCE` 选择：`auto`（默认，有快照用快照）/ `anitabi` / `mock`。

**测试固定用 mock**：[conftest.py](../backend/tests/conftest.py) 设了 `SEICHI_DATA_SOURCE=mock`。否则测试结果取决于这台机器有没有快照、快照是哪天下载的。有两个地方要注意：

- 必须在 conftest 的**模块顶层**设，不能在 fixture 里：`server.py` 在 import 时就创建了 `repo`
- MCP client 测试会把 server 拉成**子进程**，子进程只继承白名单里的环境变量（[07 章](07-mcp-client.md#子进程的环境变量)），所以 `SEICHI_DATA_SOURCE` 也加进了 `PASSTHROUGH_ENV`。同理，这个变量**只能设成真正的环境变量**，写进 `.env` 不会生效——`.env` 由 FastAPI 的配置类读取，不会进入 `os.environ`

快照本身的解析与合并在 [test_anitabi.py](../backend/tests/test_anitabi.py) 里用手写的原始数组测试（格式与线上文件一致，不连网络）；最后一个测试读本地真实快照做粗略检查，没有快照时跳过。

### 工具接口为什么要改

数据量变了，原来的接口就不合适了。

**`list_spots` 返回总数 + 前 N 处，而不是全部：**

```json
{"anime_id": 115908, "anime_title": "吹响吧！上低音号", "city": "宇治市",
 "source": "Anitabi（anitabi.cn），CC BY-NC-SA 4.0",
 "total_spots": 40, "shown": 15, "sorted_by": "镜头数（同一地点在动画里出现的次数）从多到少",
 "spots": [{"spot_id": "7eyih3xg", "name_ja": "莵道高", "lat": 34.908983, "lng": 135.815702,
            "episode": 1, "timestamp_sec": 164, "episodes": [1, 2, 3, …], "scenes_count": 214, "image": "…"}, …]}
```

`total_spots` 让 LLM 知道"还有没列出的"，能如实说"共 41 处，以下是最有代表性的 15 处"（实测它确实这么说了）。按镜头数排序，是因为镜头多的地点通常就是名场面。

**加了"某点附近"过滤**：`near_lat` / `near_lng` / `radius_km`。用户问「下北泽站附近有哪些」时，LLM 先 `geocode` 查车站坐标，再带着坐标调 `list_spots`。这比让 LLM 拿到全部地点再自己按坐标筛靠谱得多——LLM 不擅长算距离。

**结果瘦身**：实测 LLM 规划路线时会传 `limit=50`，结果有 16466 个字符，那一轮对话用了 29457 个 token。逐字段统计，两个 URL（图片、来源链接）占 34%，每个地点里重复的作品名、作品 id、城市占 13%。改成作品级字段只在顶层写一次、空值不输出、来源链接由前端用 `anime_id + spot_id` 拼，同样 40 个地点降到 **9563 个字符**。这就是原则 3（输出要省 token）在真实数据量下的样子。`get_spot`（单个地点）仍然输出全部字段。

**搜索容错**：`_norm()` 做 NFKC + 小写 + 去标点，再用"二元组重合度"兜底。[10 章实测](10-frontend.md#实测)里 LLM 把「吹响吧！上低音号」写成「吹响！上低音号」，旧的子串匹配搜不到，LLM 只好换三种写法重试；现在重合度 0.8，一次命中。按城市搜到的作品排在按标题搜到的后面；同样匹配度时地标多的排前面（续作、剧场版通常地标少）。

### 实测

在浏览器里问三个问题（DeepSeek，2026-10-21）：

| 问题 | 工具调用 | 结果 |
|---|---|---|
| 上低音号在宇治有哪些圣地？从京阪宇治站出发帮我排一条步行路线 | search_anime「吹响！上低音号」∥ geocode · list_spots(limit=40) · plan_route | 一次搜中，不再重试；从 40 个地点里挑 8 个，排成 5.96 km / 约 80 分钟的路线 |
| 孤独摇滚的圣地在哪？ | search_anime · list_spots | **mock 里没有的作品**。共 41 处列出 15 处，LLM 自己按下北泽 / 金泽八景 / 江之岛分组介绍。9772 tokens |
| 孤独摇滚在下北泽站附近有哪些圣地？ | search_anime ∥ geocode「下北沢駅」· list_spots(near, radius_km=1) | 12 处，都带距离（0.07~0.46 km）。9947 tokens |

`∥` 表示同一轮里并行发出的调用。

## 设计取舍

**mock、实时 API、本地快照**

Day 1 选 mock 的理由是演示时零网络依赖、零限流风险、毫秒级响应。本地快照**保留了这三个优点，同时数据是真的**（1552 部作品）。实时请求 Anitabi 是唯一被排除的方案：限流、演示风险、使用规范三方面都不合适。mock 没有删掉：测试用它（结果稳定），没有快照时退回它（demo 不挂）。

**`lru_cache` vs 每次读文件**

每次调用都读 JSON 最简单，改数据立即生效；但每次工具调用都有一次磁盘 IO。用 `lru_cache` 只读一次。对只读的小数据集，缓存是对的；如果数据会在运行时更新，就不能这么做。

## 动手验证

```powershell
# 1. 跑 seichi 的全部测试（含一个真实 stdio 子进程往返）
python -m pytest -v -k seichi

# 2. 手动启动 server —— 它会"卡住"不动，这是正常的：
#    它在等 stdin 上的 JSON-RPC 消息。Ctrl+C 退出
python -m backend.mcp_servers.seichi.server

# 3. 下载真实数据，生成快照（约 15 秒、8 个请求；不要频繁执行）
python -m backend.mcp_servers.seichi.anitabi

# 4. 进程内直接调工具函数看看返回格式（有快照时读的是真实数据）
python -c "from backend.mcp_servers.seichi import server; import json; print(json.dumps(server.list_spots(115908, limit=3), ensure_ascii=False, indent=2))"

# 5. 强制用 mock 数据（只对当前终端有效）
$env:SEICHI_DATA_SOURCE = "mock"; python -c "from backend.mcp_servers.seichi import server; print(type(server.repo).__name__)"
Remove-Item Env:SEICHI_DATA_SOURCE
```

重点读一下 [test_seichi.py](../backend/tests/test_seichi.py) 里的 `test_stdio_roundtrip_as_subprocess`——它把 server 拉成子进程、走完整的 `initialize` → `call_tool` 协议流程。**这就是 Day 2 的 `mcp_client.py` 要做的事情的最小原型。**

图形化调试工具 MCP Inspector 需要 Node.js，装好之后可以用（未实测）：

```powershell
npx @modelcontextprotocol/inspector python -m backend.mcp_servers.seichi.server
```

## 延伸思考

1. 原则 5 说"错误是数据"。但 `get_spot` 返回 `{"error": ...}` 时 `is_error=False`——从协议层面看，这次调用是"成功"的。这会不会让 client 端（比如将来的评测脚本）难以统计工具失败率？有什么办法两全？
2. `search_anime` 现在能容忍写错一两个字。用户说"那个京都吹小号的动画"，能搜到吗？如果搜不到，应该改进工具，还是依赖 LLM 自己把用户的话改写成作品名？
3. ~~如果将来接入 Anitabi 的 580 个地标，`list_spots` 一次全返回会有什么问题？~~ 已经遇到了，答案见「真实数据」一节。新的问题：合并半径 100 米对城市里的作品合适，对《摇曳露营△》这种在山里、地点相隔几公里的作品也合适吗？要不要按作品的地点密度自动调整（数据里每个地标有 `density`：到最近邻点的距离，单位米）？
4. MCP server 是独立进程。如果它崩了，agent 这边会发生什么？谁负责重启它？
5. `list_spots` 结果里的图片 URL 是给前端地图弹窗用的，LLM 用不上，却占了约 17% 的 token。MCP 的工具结果本来就分 `content`（给模型看）和 `structuredContent`（给程序用）两部分。怎么利用这一点，让 LLM 和前端各拿各需要的？
