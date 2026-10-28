# 08 · FastAPI 与 SSE

## 本章目标

读完能回答：

- SSE 是什么？浏览器和服务器之间到底传了些什么字节？
- FastAPI 的 lifespan 是做什么的？为什么 MCP 子进程要在那里启动？
- 路由、ChatService、loop 三层各管什么？为什么不让路由直接调 loop？
- 流式响应开始之后，为什么就没法再返回 404 了？
- 会话存储为什么要把消息存成「OpenAI 格式的超集」？
- 怎么在不连 LLM 的情况下测试一个流式 HTTP 接口？

## 核心概念

### SSE：服务器推送事件

普通 HTTP 是"一问一答"：客户端发请求，服务器返回一个完整响应，结束。Agent 的回答要几秒到十几秒，等全部生成完再返回，用户就要盯着空白屏幕等。

**SSE（Server-Sent Events）** 让服务器在一个 HTTP 响应里**持续推送**多条消息。响应头是 `Content-Type: text/event-stream`，响应体是纯文本，格式非常简单：

```
event: token
data: {"content": "我来"}

event: token
data: {"content": "查一下"}

: ping

event: done
data: {"payload": {"iterations": 3}}

```

- 每条消息由若干行 `字段: 值` 组成，**空行表示一条消息结束**
- `event:` 是事件名，`data:` 是内容
- 以 `:` 开头的行是注释，客户端会忽略

整个协议就这么多，所以用 `curl` 直接就能看。

**为什么不用 WebSocket**：对话是"用户发一句、服务器推一串"的单向推送，SSE 正好是为这个设计的，基于普通 HTTP，代理和负载均衡器不需要特殊配置。WebSocket 是双向的长连接，这里用不上，反而要自己处理心跳和重连（[01 章设计取舍](01-architecture.md#设计取舍)）。

### FastAPI 内置的 SSE 支持

我们装的 FastAPI 是 0.143。探测发现它已经**内置了 SSE 支持**（`fastapi.sse`），不需要网上教程常用的第三方库 `sse-starlette`：

```python
from fastapi.sse import EventSourceResponse, ServerSentEvent

@router.post("/chat", response_class=EventSourceResponse)
async def chat(...) -> AsyncIterable[ServerSentEvent]:
    yield ServerSentEvent(event="token", raw_data='{"content": "我来"}')
```

路由函数写成**异步生成器**（`async def` + `yield`），每 `yield` 一个 `ServerSentEvent`，FastAPI 就编码成一条 SSE 消息发出去。读 FastAPI 源码还发现它额外做了三件事：

- **自动保活**：一段时间没有新数据时，自动发一条 `: ping` 注释。实测：LLM 首字之前的等待期间就出现了 `: ping`。没有保活的话，中间的代理可能会因为"连接太久没动静"把它掐断
- **自动设置响应头** `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`，后者告诉 Nginx 不要缓冲这个响应（缓冲了流式就失效了，用户会等到最后一次性收到）
- **在单独的 task 里驱动生成器**，客户端断开时会取消它

## 分层：路由、ChatService、loop

```
routes/chat.py      HTTP 的事：解析请求、把事件编码成 SSE
      │
chat_service.py     会话的事：载入历史、组装消息、运行 agent、保存结果
      │
agent/loop.py       回答的事：调 LLM、调工具、循环（对会话和 HTTP 一无所知）
```

为什么中间要多一层 `ChatService`，而不是让路由直接调 loop？因为"一轮对话"除了运行 loop，还有一堆事情：从存储里取历史、拼 system prompt、把 OpenAI 格式的历史转换成存储格式、在 done 时入库、出错时决定不入库……这些放进路由，路由会变得很臃肿；放进 loop，就破坏了 [01 章原则一](01-architecture.md#原则一agent-loop-在最中心对外界一无所知)（loop 不能知道数据库的存在）。

另一个直接的好处：**CLI 和 HTTP 共用同一个 ChatService**。CLI 只是把"编码成 SSE"换成了"打印到终端"，两边行为完全一致。

## 代码走读

```
app/
├── main.py           create_app() 与 lifespan
├── components.py     组装点：创建 LLM、MCP、harness、存储、tracing
├── chat_service.py   一轮对话的完整流程
├── deps.py           FastAPI 依赖：get_service、resolve_chat_session
├── schemas.py        请求 / 响应模型
└── routes/
    ├── chat.py       POST /api/chat（SSE）
    └── sessions.py   GET /api/sessions、GET /api/sessions/{id}
store/
├── base.py           SessionStore 接口、StoredMessage
└── memory.py         内存实现
```

### main.py：lifespan 与 create_app

```python
def create_app(components_factory=None) -> FastAPI:
    factory = components_factory or (lambda: default_components(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with factory() as components:      # 启动：拉起 MCP 子进程等
            app.state.service = ChatService(components)
            yield                                # ← 应用在这里运行，处理所有请求
        # 离开 async with：关闭 MCP 子进程、上报剩余 trace

    app = FastAPI(lifespan=lifespan)
    app.include_router(chat.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    ...
    if FRONTEND_DIST.is_dir():                   # 有前端构建产物就一起托管（Day 3 加入）
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app

app = create_app()
```

接口统一挂在 `/api` 下，根路径 `/` 留给前端页面；`/health` 留在根路径给部署平台的健康检查。为什么这样分、`mount` 为什么必须最后写，见 [10 章「后端配合的改动」](10-frontend.md#后端配合的改动)。路由文件里写的仍是 `@router.post("/chat")`，前缀在 `include_router` 时统一加上——路由不需要知道自己被挂在哪。

**lifespan** 是 FastAPI 管理"应用级资源"的地方：`yield` 之前的代码在服务启动时执行一次，`yield` 之后的代码在服务关闭时执行一次。MCP 子进程、数据库连接池这类**整个应用共用、要正确关闭**的资源都应该放在这里。

它是**一个函数**，`yield` 前后在同一个 task 里执行，正好满足 [07 章](07-mcp-client.md#生命周期必须在同一个-task-里打开和关闭) MCP SDK"同一个 task 里打开和关闭"的要求。老式写法 `@app.on_event("startup")` / `@app.on_event("shutdown")` 是两个独立的函数，实测它们其实也跑在同一个 task 里（Starlette 在同一个 lifespan task 中依次调用），不会报错；但它已被标记为过时，且打开的对象只能借全局变量传给 shutdown。真正会跨 task 的是"在请求里懒加载打开"，见 07 章。

**`create_app()` 接受一个组件工厂**，这是可测试性的关键：生产环境用 `default_components`（真 LLM、真 MCP 子进程），测试传入一个装着假 LLM、假工具的工厂。**两者走的是完全相同的路由代码**，测试测到的就是线上跑的。

这种"用函数创建 app"的写法叫**应用工厂（application factory）**。对比直接在模块里写 `app = FastAPI(...)` 然后到处 import：后者的依赖在 import 时就定死了，测试想换成假的只能 monkeypatch。

### components.py：组装点

[components.py](../backend/app/components.py) 是 [05 章](05-agent-loop.md#clipy组装点)说的组装点，从 CLI 里挪出来、让 CLI 和 FastAPI 共用：

```python
async with MCPClientPool() as tools:
    loop = AgentLoop(
        llm=tracing.wrap_llm(llm, settings.llm_model),     # 09 章：套上追踪
        tools=tracing.wrap_tools(tools),
        harness=Harness(max_iterations=settings.max_iterations),
    )
    yield Components(loop=loop, store=store, tracing=tracing)
```

`build_store` 按 `DATABASE_URL` 选存储：空 → 内存，否则 → [SQL 实现](../backend/store/sql.py)（SQLite 或 Postgres，[12 章](12-going-public.md)）。配置了数据库却连不上时，`store.open()` 抛异常，**启动直接失败**，而不是悄悄退回内存存储。悄悄降级的后果是：你以为数据进了数据库，重启后才发现全丢了。[01 章原则五](01-architecture.md#原则五降级而不是崩溃)说"降级而不是崩溃"，前提是**降级是安全的、诚实的**。配置错误不属于这种情况。

### deps.py：依赖注入

路由不直接去 `app.state` 里拿东西，而是声明自己需要什么：

```python
def get_service(request: Request) -> ChatService:
    return request.app.state.service

@router.get("")
async def list_sessions(service: ChatService = Depends(get_service)): ...
```

`Depends` 是 FastAPI 的**依赖注入**：FastAPI 在调用路由函数之前，先调用 `get_service` 拿到 `ChatService`，再传进来。路由只说"我需要一个 ChatService"，不关心它从哪来。

### 为什么 404 必须在依赖里判断

`POST /api/chat` 带了一个不存在的 `session_id`，应该返回 404。直觉上写在路由函数里：

```python
# ❌ 反例
async def chat(body, service):
    session = await service.store.get_session(body.session_id)
    if session is None:
        raise HTTPException(404)        # 太晚了！
    yield ...
```

问题在于：路由函数是个生成器，**FastAPI 会先把响应头（状态码 200、`text/event-stream`）发出去，然后才开始执行生成器**。等生成器里发现会话不存在，200 早就发出去了，没法改成 404。

依赖则不同：**依赖在响应开始之前执行**，里面抛的 `HTTPException` 会变成正常的 404 响应。所以会话的查找/创建放在 `resolve_chat_session` 依赖里：

```python
async def resolve_chat_session(body: ChatRequest, service = Depends(get_service)) -> StoredSession:
    try:
        return await service.open_session(body.session_id, body.message)
    except SessionNotFound:
        raise HTTPException(status_code=404, ...)
```

依赖也能拿到请求体 `body`，FastAPI 只解析一次、在依赖和路由之间共享。

**通用规则：流式接口里，所有可能导致"请求失败"的校验，都要放在响应开始之前。** 响应开始之后发生的错误，只能作为流里的一个 `error` 事件告诉客户端。

### routes/chat.py：事件 → SSE

```python
@router.post("/chat", response_class=EventSourceResponse)
async def chat(body, session = Depends(resolve_chat_session), service = Depends(get_service)):
    yield _sse("session", {"session_id": session.id, "title": session.title})
    async for event in service.run_turn(session, body.message):
        yield _sse(event.type, _public(event))
```

三个细节：

**第一个事件是 `session`**。新建会话时，前端不知道会话 id，下一轮要带上它才能续聊。

**`_public()` 过滤字段**。`done` 事件的 payload 里有 `new_messages`（本轮全部消息，含工具结果的完整 JSON），那是给 ChatService 入库用的，可能很大，前端用不着，不发。

**用 `raw_data` 自己序列化**。`ServerSentEvent(data=...)` 会把中文转义成 `上低` 这样的序列。实测对比：

```
data=     → data: {"t": "宇治"}
raw_data= → data: {"t": "宇治"}
```

转义后前端照样能解析，但 curl 里没法读，而且每个汉字从 3 字节变成 6 字节。所以改成 `raw_data=json.dumps(data, ensure_ascii=False)`。会不会破坏 SSE 的行格式？不会：`json.dumps` 不加 `indent` 时输出一定是单行，字符串里的换行会被转成 `\n` 两个字符。

### chat_service.py：一轮对话

```python
async def run_turn(self, session, user_text):
    history = await store.get_messages(session.id)
    messages = [system_prompt, *(m.to_llm_message() for m in history), user_msg]

    with tracing.turn(session_id=session.id, user_input=user_text) as turn:   # 09 章
        async for event in loop.run(messages):
            记录 tool_call 的工具名、tool_result 的结构化结果
            if event.type == "done":
                await store.append_messages(session.id, [user_msg, *新消息])   # 先存
                turn.finish(...)
            elif event.type == "error":
                turn.fail(...); logger.warning(...)                            # 不存
            yield event                                                        # 再发
```

**done 时先存再发**。前端收到 `done` 后可能立刻刷新会话列表，这时数据必须已经在存储里了。

**出错时整轮不存**。假设 LLM 在第二轮工具调用后挂了，如果把已有的部分存进去，历史里就会出现"有工具调用、没有最终回答"的半截状态。下次再把这段历史发给 LLM，行为难以预料。所以要么整轮存，要么都不存，用户重新提问即可。代价是这一轮已经花掉的 token 白费了。

**出错时记日志**。实测时第二轮遇到过一次 `error`，但服务端日志里什么都没有：错误事件只发给了客户端，服务端完全不知情。所以加了 `logger.warning`。**发给客户端的错误，服务端也要留一份记录**，否则线上出问题无从排查。

### 会话存储：OpenAI 格式的超集

[store/base.py](../backend/store/base.py) 的 `StoredMessage`：

```python
class StoredMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str | None
    tool_calls: list[dict] | None      # assistant：OpenAI 格式，原样保存
    tool_call_id: str | None           # tool：对应哪个调用
    tool_name: str | None              # ↓ 以下三个只给前端用
    is_error: bool | None
    data: Any                          # 工具的结构化结果
    created_at: datetime

    def to_llm_message(self) -> dict:  # 只取 LLM 认识的字段
```

这个设计要同时满足两个使用者：

- **LLM**：下一轮要把历史原样发回去，必须是严格的 OpenAI 格式。多出来的字段（`data`、`tool_name`）发过去，轻则浪费 token，重则被拒绝
- **前端**：刷新页面后要恢复会话，不只是文字，还要复现"调了哪些工具"、地图上的打点。这需要工具名和**结构化结果** `data`

所以存储格式是"OpenAI 格式的超集"，发给 LLM 时用 `to_llm_message()` 过滤。

`data` 从哪来？loop 产出的 `new_messages` 是纯 OpenAI 格式，tool 消息里只有 `content`（给 LLM 看的文本）。结构化的 `data` 在 `tool_result` 事件里。所以 ChatService 在遍历事件时按 `tool_call_id` 记下每个工具的名字和结果，入库时补到对应的 tool 消息上。

这里修改了 Day 1 的设计：原来的 `StoredMessage` 有一个 `tool_results` 列表字段。真正写到这一步才发现，loop 产出的是"每个工具结果一条 tool 消息"的 OpenAI 格式，存成"一条 assistant 消息带一个结果列表"需要来回转换。**跟着数据的真实形状走，比提前设计的结构更好用。** SQLAlchemy 模型（[models.py](../backend/store/models.py)）也同步改了，[12 章](12-going-public.md)接数据库时直接用上了。

**`append_messages` 一次存一整轮**，而不是一条一条存。SQL 实现里放在同一个事务中（[sql.py](../backend/store/sql.py) 的 `begin()`），不会出现"存了工具调用、没存工具结果"的状态。实测 DeepSeek 遇到这种历史会直接拒绝（[06 章](06-harness.md#上下文裁剪)）。

会话还有一个 `owner` 字段（访客 id），侧栏只列自己的会话，见 [12 章](12-going-public.md#会话隔离)。

[store/memory.py](../backend/store/memory.py) 是内存实现。它的方法里都没有 `await`，在 asyncio 的单线程模型下天然是原子的，不需要加锁（和 [06 章重复调用检测](06-harness.md#重复调用检测)的道理一样）。`get_messages` 返回列表的**副本**，调用方修改返回值不会影响存储。

## 实测

用 uvicorn 起服务、curl 发请求，三个场景合在一个问题里（Day 2 实测，当时接口还没有 `/api` 前缀）：

```
POST /chat  {"message": "上低音号在宇治的圣地有哪些？这周六去那边天气怎么样？"}

event: session      → 新建会话
: ping              → LLM 首字前的等待期间，FastAPI 自动发的保活
event: token × 349  → 逐字流式
event: tool_call / tool_result × 3   → search_anime → list_spots → get_weather_forecast
event: done         → 4 轮 LLM 调用，11081 tokens，总耗时 13.5 秒
```

第二轮带上 `session_id` 续聊"从京阪宇治站出发，把宇治的几个点排一条步行路线"：**首字 0.8 秒**，LLM 直接用第一轮 `list_spots` 结果里的坐标调了 `plan_route`，说明历史正确地发回了 LLM。`GET /sessions/{id}` 返回完整的 12 条消息，每条 tool 消息都带着 `tool_name` 和结构化 `data`。

测试期间 Langfuse 一直连不上，服务完全不受影响（原因见 [09 章](09-langfuse.md)）。

**给 Day 3 前端的提示**：一轮回答里，文字可能分成好几段，中间夹着工具调用（"我来查一下" → 工具 → "找到了……"）。前端要按事件顺序渲染，遇到 `tool_call` 就结束当前文字段、显示工具卡片，之后的 `token` 开始新的一段。否则会把两段文字粘在一起（"……范围内）。## 宇治步行巡礼路线"）。

## 测试

[test_api.py](../backend/tests/test_api.py) 通过 `create_app(factory)` 注入假 LLM 和假工具，走的是**真实的路由、真实的 SSE 编码、真实的会话存储**：

```python
def make_client(llm, tools=None, access_code=""):
    @asynccontextmanager
    async def factory():
        yield Components(loop=AgentLoop(llm, tools or FakeTools(), Harness()),
                         store=InMemorySessionStore(), tracing=Tracing(None))
    return TestClient(create_app(factory, AccessConfig(code=access_code)))
```

`AccessConfig` 显式传入：测试不受本机 `.env` 里 `ACCESS_CODE` 的影响（访问码见 [12 章](12-going-public.md)）。

测试里自己写了一个 `parse_sse()` 把响应体解析回事件列表，顺便验证了格式：中文没有被转义、`Cache-Control: no-cache` 存在、保活注释能被正确跳过。

覆盖的场景：完整事件流、第二轮把历史发给 LLM（且不含 `data` 这种只给前端的字段）、会话恢复时拿得到工具数据、不存在的会话返回 404（在流开始之前）、空消息返回 422、LLM 出错时本轮不入库。访问码和访客隔离的测试在同一个文件末尾，讲解见 12 章。

[test_app.py](../backend/tests/test_app.py) 则用**真实的** `default_components`：真的拉起三个 MCP 子进程、真的走 lifespan 启动和关闭，只是不调 LLM。它保证组装代码本身是对的。

**测试不能往线上 Langfuse 写数据**。`.env` 里配了真实的 Langfuse key，`test_app.py` 走真实组装时会开启追踪。所以 [conftest.py](../backend/tests/conftest.py) 在所有测试开始前，用空的环境变量覆盖掉 key（[02 章](02-project-setup.md#配置从-env-到-settings)：环境变量优先级高于 `.env`）。

## 设计取舍

**会话的创建放在流开始之前，有什么代价？**

`resolve_chat_session` 在响应开始前就创建了会话。如果这一轮随后出错，就会留下一个空会话（有标题、没有消息）。可以等 done 时再创建，但那样第一个 `session` 事件就发不出会话 id 了。demo 阶段接受这个代价；正式产品可以定期清理空会话。

**客户端中途断开怎么办？**

用户关掉页面时，FastAPI 会取消驱动生成器的 task，`run_turn` 在某个 `await` 处收到 `CancelledError`，不会走到 done，**本轮不入库**。LLM 那边已经花掉的 token 无法挽回。这是可以接受的：断开的用户看不到回答，存进去也没意义。

**同一个会话并发两个请求？**

两个请求会各自读到相同的历史、各自生成回答、各自追加，历史顺序会乱。前端在一轮没结束时禁用发送按钮就能避免。服务端加锁是更彻底的方案，demo 不做。

## 动手验证

```powershell
# 1. 起服务。浏览器打开 http://127.0.0.1:8000/docs 可以看到自动生成的接口文档
uvicorn backend.app.main:app --reload

# 2. 另开一个终端发请求（会消耗 LLM token）
#    PowerShell 里 curl 是 Invoke-WebRequest 的别名，必须写 curl.exe
#    JSON 写在文件里，避免 PowerShell 把命令行里的引号吃掉
'{"message": "宇治这周六天气怎么样"}' | Out-File -Encoding utf8 req.json
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
curl.exe -N -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" --data-binary "@req.json"

#    会话按访客 cookie 隔离（12 章）：用 -c/-b 让 curl 存取 cookie，不然每次都是新访客、列表为空
curl.exe -N -c cookies.txt -b cookies.txt -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" --data-binary "@req.json"

# 3. 查看会话（同一个 cookie 文件）
curl.exe -b cookies.txt http://127.0.0.1:8000/api/sessions

# 4. 接口测试（不花 token）
python -m pytest -v backend/tests/test_api.py backend/tests/test_store.py
```

- `-N` 让 curl 不缓冲输出，才能看到逐字到达的效果
- `[Console]::OutputEncoding` 那一行让 PowerShell 按 UTF-8 显示 curl 的输出，否则中文是乱码
- `Out-File -Encoding utf8` 在 PowerShell 5.1 里会写入 BOM。实测 FastAPI 能正常解析带 BOM 的 JSON

## 延伸思考

1. `ChatService.run_turn` 在 `done` 时入库。如果入库本身失败（比如 Postgres 连接断了），会发生什么？用户会看到什么？`done` 还应该发吗？
2. 前端拿到的 `tool_result` 事件里有完整的 `data`。`list_spots` 返回 500 个地标时，这个事件会很大。应该在哪一层限制？
3. 现在所有会话对所有人可见（没有用户系统）。如果要加登录，`SessionStore` 接口需要怎么改？ChatService 呢？loop 需要改吗？
4. 为什么 `GET /sessions/{id}` 返回 `StoredMessage` 的全部字段，而 SSE 的 `done` 事件要过滤掉 `new_messages`？两者的使用场景有什么不同？
