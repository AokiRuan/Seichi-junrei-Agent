# 07 · MCP Client

## 本章目标

读完能回答：

- 03、04 章写的三个 MCP server，agent 这一侧是怎么把它们接进来的？
- 工具调用失败有哪几种？分别怎么告诉 LLM？
- 为什么子进程连不上外网的问题可能和环境变量有关？
- `Attempted to exit cancel scope in a different task` 这个报错是什么意思？为什么用 `async with` 就解决了？

## 核心概念

[03 章](03-mcp-server.md)和 [04 章](04-external-mcp-servers.md)写的是 MCP 的 **server** 端：把功能暴露成工具。本章是 **client** 端：agent 这边怎么连上这些 server、怎么调用。

client 要解决的问题是**聚合**：LLM 面对的是一份统一的工具清单（6 个工具），它不需要、也不应该知道这些工具分别来自哪个 server、通过什么方式通信。

```
                         ┌─ seichi 子进程 ─ search_anime / list_spots / get_spot
loop ─ MCPClientPool ────┼─ weather 子进程 ─ get_weather_forecast
                         └─ route 子进程 ─── geocode / plan_route
```

对 loop 来说，工具层只有两个方法：

```python
class ToolExecutor(Protocol):
    def tool_schemas(self) -> list[dict]: ...                        # 给 LLM 的工具清单
    async def call_tool(self, name, arguments) -> ToolOutcome: ...   # 执行一个工具
```

[05 章的测试](05-agent-loop.md#测试)里的 `FakeTools` 就是只实现了这两个方法。

## 代码走读

[mcp_client.py](../backend/agent/mcp_client.py)

### 启动与工具发现

```python
DEFAULT_SERVERS = (
    ServerSpec("seichi",  "backend.mcp_servers.seichi.server"),
    ServerSpec("weather", "backend.mcp_servers.weather.server"),
    ServerSpec("route",   "backend.mcp_servers.route.server"),
)
```

`connect_all()` 依次启动每个 server，完成握手，拉取工具列表，建立"工具名 → 所属 server 的会话"的映射。

**工具名冲突时直接失败**。两个 server 都提供了叫 `search` 的工具时，与其让后注册的悄悄覆盖先注册的（LLM 以为调的是 A，实际调到了 B），不如启动时就报错。**配置错误应该在启动时暴露，而不是在运行时以奇怪的方式表现出来。**

**启动失败时清理自己**。如果第三个 server 启动失败，前两个已经启动的子进程不能留着不管，`connect_all()` 会在抛出异常前关掉它们。

#### `MCPClientPool` 的五个字段

`__init__` 只准备空容器，`connect_all()` 才往里填。连接完成后实测打印的内容：

```text
_servers : ['seichi', 'weather', 'route']                      ← 要启动哪些 server
_stack   : <contextlib.AsyncExitStack ...>                     ← 负责最后把它们全关掉
_sessions:
   search_anime         -> ClientSession 0x193f040ac30   ┐
   list_spots           -> ClientSession 0x193f040ac30   ├ 同一个对象：seichi 的连接
   get_spot             -> ClientSession 0x193f040ac30   ┘
   get_weather_forecast -> ClientSession 0x193f0448470     ← weather 的连接
   geocode              -> ClientSession 0x193f0449790   ┐ route 的连接
   plan_route           -> ClientSession 0x193f0449790   ┘
_owner   : {'search_anime': 'seichi', ..., 'plan_route': 'route'}
_schemas : [ {"type": "function", "function": {"name": "search_anime", ...}}, ... ] 共 6 个
```

| 字段 | 谁用 | 作用 |
|---|---|---|
| `_servers` | `connect_all` | 配置：启动清单 |
| `_stack` | `connect_all` / `close` | 记下每个打开的连接，`close()` 时按相反顺序逐个关闭、结束子进程 |
| `_sessions` | `call_tool` | **路由表**：LLM 只说"调 `plan_route`"，靠它查到该发给哪个子进程 |
| `_owner` | 报错信息、`tool_owner()` | 工具来自哪个 server 的**名字**，给人看的（冲突报错、追踪排查） |
| `_schemas` | `tool_schemas()` → loop → LLM | 已转成 OpenAI 格式的工具清单，每次请求都带上 |

为什么 `_sessions` 按**工具名**而不是 server 名做 key？因为调用时手里只有工具名，按工具名建表查一次就到；按 server 名建表的话，每次调用还得先找"这个工具属于谁"。三个工具共享同一个 `ClientSession` 对象，只是多了几个指向它的 key，不会多开连接。

为什么 `_owner` 和 `_sessions` 分开存？`ClientSession` 对象本身不知道自己叫 "seichi"，打印出来只是个内存地址；报错时需要可读的名字。

**`AsyncExitStack` 是什么？** 一个"待关闭清单"。普通的 `async with` 只能写死个数：

```python
async with open_a() as a, open_b() as b, open_c() as c:   # 写死三个
    ...
```

server 的个数来自配置，写不死。`AsyncExitStack` 让你在循环里一个个 `enter_async_context(...)` 进去，最后一次 `aclose()` 把它们**逆序**全部关掉——效果等同于嵌套的 `async with`，但个数是动态的。中途某个 server 启动失败时，`connect_all` 里的 `close()` 也正是靠它关掉已启动的那几个。

三个子进程依次启动，实测共约 3 秒。可以并行启动来加快，但这只在应用启动时发生一次，不值得增加复杂度。

### 子进程的环境变量

```python
PASSTHROUGH_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", ..., "SEICHI_DATA_SOURCE")

env = {"PYTHONIOENCODING": "utf-8"}
env.update({k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ})
```

为什么要显式传代理变量？实测 MCP SDK 启动子进程时的环境变量是这样组成的：

```python
env = get_default_environment() | (server.env or {})
```

`get_default_environment()` 在 Windows 上只包含 12 个系统变量：`APPDATA`、`PATH`、`SYSTEMROOT`、`TEMP`、`USERPROFILE` 等。**你在终端里设置的其他环境变量，子进程都拿不到。**

这是 SDK 出于安全考虑的设计：MCP server 可能是第三方写的，不应该默认拿到你所有的环境变量（里面可能有各种 API key）。代价是，如果你通过代理上网，天气和路线 server 会连不上外部 API，而且报错只会是一个笼统的超时，很难想到原因。所以把代理相关的变量显式传进去。

同理，将来如果某个 server 需要 API key，也要通过 `env` 显式传入，而不能指望它自己从环境里读到。圣地 server 的数据源开关 `SEICHI_DATA_SOURCE` 就是这样加进白名单的（[03 章](03-mcp-server.md#两个实现一套查询逻辑)）：不加的话，测试进程设了 `mock`，被拉起的子进程却看不到，照样去读快照。

另外两个参数：

- `command=sys.executable`：用**当前 venv 的 Python** 启动子进程。直接写 `"python"` 的话，可能启动的是系统里另一个 Python，找不到项目依赖
- `cwd=PROJECT_ROOT`：子进程要以 `python -m backend.mcp_servers.xxx` 的方式启动，工作目录必须是项目根目录（原因见 [02 章](02-project-setup.md#包与导入为什么必须在根目录运行)）

> **Python 知识点：`Path(__file__).resolve().parents[2]`**
>
> `__file__` 是当前模块文件的路径；`.resolve()` 转成绝对路径；`.parents[n]` 往上走 n+1 层目录：
>
> ```text
> mcp_client.py 本身   C:\...\Aoki-Agent\backend\agent\mcp_client.py
> parents[0]           C:\...\Aoki-Agent\backend\agent
> parents[1]           C:\...\Aoki-Agent\backend
> parents[2]           C:\...\Aoki-Agent            ← 项目根
> ```
>
> 为什么不用 `os.getcwd()`？当前目录取决于用户在哪启动程序，不可靠；文件自己的位置是固定的。代价是：如果把 `mcp_client.py` 挪到别的层级，这个数字要跟着改。

### 转成 LLM 的工具格式

```python
def _to_openai_schema(tool):
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": inspect.cleandoc(tool.description or ""),
            "parameters": tool.input_schema,
        },
    }
```

MCP 的工具定义和 OpenAI 的 tools 格式几乎一一对应，只是外面包了一层。`inspect.cleandoc` 去掉 docstring 里的缩进：不清理的话，每个工具描述里都带着一堆 `\n    `，每次调用 LLM 都要为这些空格付 token 费用（[03 章](03-mcp-server.md#llm-怎么知道有哪些工具json-schema)导出 schema 时发现的）。

### 调用与结果：三种结局

`call_tool()` 返回一个 `ToolOutcome`：

```python
@dataclass
class ToolOutcome:
    data: Any       # 结构化结果，给前端用（地图打点等）
    is_error: bool  # 协议层面是否失败
    text: str       # 回填给 LLM 的文本
```

**`data` 和 `text` 分开**，对应 [05 章实测发现 2](05-agent-loop.md#实测发现)：精确的结构化数据走 `data` 直接给前端，LLM 只看 `text`。

一次工具调用可能有三种结局，实测结果如下：

| 情况 | 例子 | `is_error` | LLM 看到的 |
|---|---|---|---|
| 成功 | `search_anime("莉可丽丝")` | False | 结果的 JSON |
| **业务上的失败** | `get_spot("bad-id")` | **False** | `{"error": "未找到圣地 bad-id"}` |
| 协议层面的失败 | 参数类型错、工具不存在、工具内部异常、子进程崩溃 | True | 错误原因 |

第二行值得注意："查不到"对工具来说是**正常执行完毕、如实报告结果**，协议上是成功的。"参数类型不对"才是调用本身失败了。这个区分会影响将来的评测统计：工具失败率应该只统计第三行。

处理结果时的三个细节，都是 Day 1 记下的：

- **检查 `is_error`，而不是用 try/except**。跨进程调用时工具出错不会让 client 抛异常（[03 章原则 5](03-mcp-server.md#5-错误是数据不是异常)）
- **解包 `{"result": [...]}`**。返回列表的工具，SDK 会把结构化结果包一层（[踩坑日志](pitfalls.md#返回-list-的工具结构化结果被包了一层)）
- **JSON 用 `ensure_ascii=False`**。不加的话中文会变成 `\u8389\u53ef` 这样的转义序列：LLM 能读懂，但一个汉字变成了 6 个 ASCII 字符，白白增加 token 消耗

还有第 4 种情况：**子进程崩溃、管道断开**。`session.call_tool()` 会抛异常，这里捕获后同样转成 `is_error=True` 的结果交给 LLM，不让它打断对话。

### 生命周期：必须在同一个 task 里打开和关闭

写测试时遇到了这个报错：

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

要看懂它，需要三块拼图：task 是什么、`stdio_client` 里面藏了什么、anyio 为什么要检查"同一个 task"。

#### 拼图 1：asyncio 的 task

asyncio 里真正被事件循环调度的单位是 **task**（`asyncio.Task`），不是协程函数。

- `await foo()`：`foo` 在**当前 task** 里接着跑，只是多了一层调用
- `asyncio.create_task(foo())`：创建一个**新 task**，和当前 task 并发
- `asyncio.current_task()`：返回正在运行的 task 对象

所以"同一个 task"的直观理解是：**从同一条 `await` 调用链上走下来的代码**。一个 `async def` 函数从头执行到尾（中间不管 `await` 多少次），始终在同一个 task 里。

#### 拼图 2：`stdio_client` 里面开了一个 task group

用 `inspect.getsource` 看 MCP SDK 的 `stdio_client`，核心是这几行：

```python
async with anyio.create_task_group() as tg:
    tg.start_soon(stdout_reader)    # 后台 task：不停读子进程的 stdout
    tg.start_soon(stdin_writer)     # 后台 task：把请求写进子进程的 stdin
    yield read_stream, write_stream # ← 把连接交给调用方，task group 保持打开
```

也就是说，每个 MCP 连接背后有两个后台 task 在搬运数据。**task group** 是 anyio 的结构化并发工具：它负责这些子 task 的生死——退出 `async with` 时，要取消并等待所有子 task 结束。task group 内部自带一个 **cancel scope**（取消作用域）。

`async with` 里有 `yield`，所以这个 task group 的"打开"发生在我们调用 `connect_all()` 时，"关闭"要等到我们调用 `close()` 时——中间隔了整个应用的运行期。

#### 拼图 3：cancel scope 绑定的是"进入它的那个 task"

cancel scope 要实现"超时/取消时打断里面的代码"，手段是**取消宿主 task**（task group 的子 task 出错时，也是靠取消宿主 task 来通知外层）。所以它在进入时记下"我属于哪个 task"，退出时检查当前 task 还是不是它。不是的话说明结构被打乱了——取消信号会打到错误的 task 上——anyio 选择直接报错，而不是带病运行。

最小复现（只用 anyio，不涉及 MCP）：

```python
class Pool:
    async def open(self):
        self.tg = anyio.create_task_group()
        await self.tg.__aenter__()              # async with 的前半段
        self.tg.start_soon(worker, "reader")

    async def close(self):
        self.tg.cancel_scope.cancel()
        await self.tg.__aexit__(None, None, None)   # async with 的后半段

# 同一个 task 里开、关
p = Pool(); await p.open(); await p.close()                 # OK

# 不同 task 里开、关
p = Pool()
await asyncio.create_task(p.open(),  name="task-A")
await asyncio.create_task(p.close(), name="task-B")
```

实测输出：

```text
same task: OK

open  in task-A
close in task-B
different tasks: Attempted to exit cancel scope in a different task than it was entered in
```

和测试里的报错一字不差。`MCPClientPool` 的 `connect_all()` / `close()` 本质上就是这里的 `open()` / `close()`。

#### pytest 的异步 fixture 为什么会触发

```python
@pytest.fixture
async def pool():
    p = MCPClientPool()
    await p.connect_all()   # ← setup
    yield p
    await p.close()         # ← teardown
```

看起来是同一个函数，但 pytest-asyncio 是**分几次**驱动这个生成器的：setup 跑到 `yield`，然后去跑测试，最后再回来跑 `yield` 之后的部分。每一段各自包在一个 task 里。实测（pytest-asyncio 1.4.0）：

```python
@pytest.fixture
async def which_task():
    print("setup    task:", asyncio.current_task().get_name())
    yield
    print("teardown task:", asyncio.current_task().get_name())
```

```text
setup    task: Task-1
test     task: Task-2
teardown task: Task-3
```

三段在三个不同的 task 里。于是 setup 里打开的 task group，在 teardown 里关闭时就报错。测试本身都通过了，只是清理时报错。

#### 解法：`async with`

给 `MCPClientPool` 实现 `__aenter__` / `__aexit__`，在**测试函数内部**用 `async with`：

```python
async def test_xxx():
    async with MCPClientPool() as pool:
        ...                       # 打开和关闭都在这个测试函数里，必然是同一个 task
```

为什么 `async with` 能保证？`__aenter__` 和 `__aexit__` 由同一个函数在同一条执行路径上调用，中间只有 `await`，没有 `create_task`——由拼图 1，它们必然在同一个 task 里。即使代码中途抛异常，`__aexit__` 也会执行，不会留下孤儿子进程。

代价是每个测试都要重新启动子进程（三个 server 约 3 秒）。所以 [test_mcp_client.py](../backend/tests/test_mcp_client.py) 里只有"聚合全部工具"那个测试启动全部 server，其余测试用 `MCPClientPool(SEICHI_ONLY)` 只启动一个。

#### 项目其他地方

- **CLI**：`main()` 里 `async with default_components(...)`，退出时（包括出错时）一定会关掉三个子进程
- **FastAPI lifespan**：lifespan 是一个 `asynccontextmanager`，`yield` 前打开、`yield` 后关闭。实测 `yield` 前后是同一个 task（`id(asyncio.current_task())` 相同）
- **旧式 `on_startup` / `on_shutdown` 事件**：实测这两个也在同一个 task 里执行（Starlette 把它们放在同一个 lifespan task 中依次调用），所以**不会**触发这个错误。项目仍然用 lifespan，是因为事件写法已被 FastAPI 标记为过时，而且打开的对象只能放全局变量里传给 shutdown，不如 `async with` 一气呵成
- **真正的陷阱是"懒加载"**：比如"第一个请求进来时才 `connect_all()`，关闭时在 shutdown 里 `close()`"。实测每个请求都在**各自独立的 task** 里处理（两次请求的 task id 不同，也都不同于 lifespan 的），这样开、关就跨了 task，必然报错。资源要在 lifespan 里统一开关，请求只使用、不负责生命周期

## 设计取舍

**每个 server 一个子进程，还是把工具都写在一个进程里？**

一个进程更省资源，启动更快。分开的好处是隔离：路线 server 因为网络问题卡死或崩溃，圣地查询不受影响。每个 server 也可以单独开发、单独测试、单独给其他 MCP 客户端（比如 Claude Desktop）使用。

**工具冲突时报错，还是自动加前缀？**

有些框架会自动把工具名改成 `seichi__search_anime` 这样来避免冲突。好处是不会报错，坏处是工具名变长了，LLM 看到的名字和 server 里定义的不一样，排查问题时要多一层转换。本项目只有 3 个自己写的 server，冲突说明设计有问题，直接报错更合适。

## 动手验证

```powershell
# 真实拉起子进程测试（不需要联网，也不花 token）
python -m pytest -v backend/tests/test_mcp_client.py

# 列出 LLM 实际能看到的全部工具，以及各自来自哪个 server
python -m backend.agent.mcp_client
```

## 延伸思考

1. 某个 server 的子进程在运行中途崩溃了，现在的代码会怎样？之后对这个 server 的每次调用都会失败。要实现自动重启，应该放在哪一层？
2. `ToolOutcome.text` 里是工具结果的完整 JSON。`list_spots` 如果返回 500 个地标，这段文本会非常长。这个问题应该在 server 端解决（[03 章原则 3](03-mcp-server.md#3-输出要省-token)），还是 client 端截断？各有什么问题？
3. 为什么业务失败（查不到）的 `is_error` 是 False？如果改成 True，对 LLM 的行为会有影响吗？对评测统计呢？
