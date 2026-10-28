# 12 · 上线准备：访问码、会话隔离与持久化

## 本章目标

读完能回答：

- 一个在本机跑得好好的 agent，放到公网上会出哪些问题？为什么先做这三件事？
- 访问码为什么不用存数据库也能验证？换了访问码，旧的放行凭证怎么自动失效？
- 不做登录，怎么让每个人只看到自己的会话？别人的会话为什么返回 404 而不是 403？
- 为什么一份 SQLAlchemy 代码能同时跑 SQLite 和 Postgres？两者又在哪里不一样？
- 为什么用纯 ASGI 中间件发 cookie，而不是 FastAPI 文档里常见的 `@app.middleware("http")`？

## 核心概念

### 公网和本机的区别

本机上只有你自己，你不会刷接口，也不会去看自己看不到的东西。公网上的访问者是匿名的、可能是脚本、也可能不守规矩。对这个项目来说，直接上线会有三个具体问题：

| 问题 | 原来的代码 | 后果 |
|---|---|---|
| **成本** | `/api/chat` 谁都能调 | 写个循环脚本就能花光 LLM 余额 |
| **隐私** | `GET /api/sessions` 返回**全部**会话 | 任何人打开侧栏都能看到别人问了什么 |
| **数据** | 内存存储 | 每次重新部署，所有会话都没了；访问量大了内存只涨不降 |

本章分别用**访问码**、**访客隔离**、**SQL 持久化**解决这三个问题。按 IP 限流、每日 token 预算这些「持码人也不能刷」的措施还没做，见延伸思考。

### Cookie 的几个属性

访问码和访客隔离都靠 cookie。服务端在响应头里写 `Set-Cookie`，浏览器存下来，之后访问同一个站点时自动带上。这里用到的属性：

| 属性 | 作用 | 为什么要 |
|---|---|---|
| `HttpOnly` | 页面里的 JavaScript 读不到 | 就算页面有 XSS 漏洞，攻击脚本也偷不走凭证 |
| `SameSite=Lax` | 别的网站发起的 POST 不带这个 cookie | 防跨站请求伪造（CSRF）：恶意网站没法借你的身份调 `/api/chat` |
| `Secure` | 只经 HTTPS 发送 | 线上防窃听。本地是 http，开了浏览器就不回传，所以做成配置项 `COOKIE_SECURE` |
| `Max-Age` | 一年后过期 | 不设的话关浏览器就丢 |

### HMAC

HMAC（Hash-based Message Authentication Code）用一个密钥对一段消息算摘要：`HMAC(key, msg)`。不知道 key 就算不出正确的值，知道结果也反推不出 key。这里拿它来生成「访问凭证」，见下文。

## 代码走读

```
backend/app/
├── access.py           AccessConfig（访问码）、require_access（门槛）、VisitorMiddleware（访客 id）
├── routes/access.py    POST /api/access
├── main.py             挂中间件、给受保护的路由加依赖
├── chat_service.py     _owned()：所有按 id 读会话的入口都检查归属
backend/store/
├── base.py             StoredSession.owner；open() / close() 生命周期
├── memory.py           按 owner 过滤
├── sql.py              SqlSessionStore（SQLite / Postgres）
└── models.py           sessions 表加 owner 列
frontend/src/
├── components/AccessGate.tsx   输入访问码的页面
└── chat/useChat.ts             遇到 401 → locked
```

### 访问码

[access.py](../backend/app/access.py) 的核心是一个配置对象：

```python
@dataclass(frozen=True)
class AccessConfig:
    code: str = ""  # 空 = 不设门槛
    cookie_secure: bool = False

    def token(self) -> str:
        return hmac.new(self.code.encode(), b"aoki-access-v1", hashlib.sha256).hexdigest()
```

流程：

```
浏览器                                 服务端
  │ GET /api/sessions（无 cookie）  ──▶ require_access：没有 aoki_access → 401
  │ ◀── 401「需要访问码」               前端换成 AccessGate 页面
  │ POST /api/access {"code": "..."} ─▶ check_code 对比 → 对了
  │ ◀── 204 Set-Cookie: aoki_access=HMAC(访问码, "aoki-access-v1")
  │ GET /api/sessions（带 cookie）  ──▶ require_access：cookie == token() → 放行
```

**cookie 里存的是 HMAC，不是访问码本身**，也不是一个随机 token：

- 存访问码本身：cookie 泄露就等于访问码泄露
- 存随机 token：服务端要记住发过哪些 token，就得有一张表；重启、多实例都要考虑
- 存 HMAC(访问码, 固定串)：服务端**什么都不用存**，每次重新算一遍比对即可。不知道访问码就伪造不出来。**换访问码后，算出来的值变了，所有旧 cookie 自动失效**，不用另外做「注销」

比较时用 `hmac.compare_digest` 而不是 `==`。`==` 遇到第一个不同的字符就返回，耗时会泄露「前几位对了」，理论上可以逐位猜。`compare_digest` 的耗时与内容无关。

**门槛挂在路由上，不挂在路由函数里**（[main.py](../backend/app/main.py)）：

```python
gated = [Depends(require_access)]
app.include_router(chat.router, prefix="/api", dependencies=gated)
app.include_router(sessions.router, prefix="/api", dependencies=gated)
app.include_router(access.router, prefix="/api")      # 输访问码的接口本身不设门槛
```

有两个原因：

1. `/api/chat` 是流式响应。响应一旦开始，状态码 200 就已经发出去了，之后才发现没权限就返回不了 401 了。这和 [08 章](08-fastapi-sse.md#为什么-404-必须在依赖里判断)「404 必须在依赖里判断」是同一个道理
2. **路由级依赖先于路由自己的依赖执行**。`/api/chat` 自己的依赖 `resolve_chat_session` 会**新建会话**，如果它先跑，没有访问码的请求也会在库里留下一个空会话。`test_rejected_chat_creates_no_session` 专门测了这个顺序

`/health` 和静态页面不设门槛：部署平台要探活，而用户得先看到页面才能输入访问码。

### 会话隔离

**访客 id** 由 [VisitorMiddleware](../backend/app/access.py) 负责：凡是 `/api` 开头的请求，读 cookie `aoki_visitor`，没有或格式不对就用 `secrets.token_urlsafe(24)` 生成一个新的，放进 `request.state.visitor_id`，并在响应头里加上 `Set-Cookie`。

访客 id **不签名**。它是 192 位随机数，本身就是凭证：猜中别人的 id 和猜中一个 UUID 一样不现实。签名能防的是伪造，而伪造一个没人用过的 id 什么也得不到。

**归属检查放在 ChatService**（[chat_service.py](../backend/app/chat_service.py)），不放在路由里：

```python
async def _owned(self, owner: str, session_id: str) -> StoredSession:
    session = await self._c.store.get_session(session_id)
    if session is None or session.owner != owner:
        raise SessionNotFound(session_id)
    return session
```

`open_session`（续聊）和 `get_history`（恢复会话）都经过它，CLI 也走同一个 ChatService（它的访客名固定为 `"cli"`）。store 本身只管存取、不判断权限，这样换存储实现时不会漏掉检查。

**别人的会话返回 404，不返回 403**。403 等于告诉对方「这个 id 存在，只是你没权限」，404 则什么都不透露。GitHub 的私有仓库也是这么处理的。

`owner` 字段不出现在接口响应里：`SessionOut` 没有这个字段，pydantic 构造时会丢掉多余字段，测试里有断言。

### 为什么写纯 ASGI 中间件

FastAPI 文档里最常见的中间件写法是：

```python
@app.middleware("http")
async def visitor(request, call_next):
    response = await call_next(request)
    response.set_cookie(...)
    return response
```

它底层是 Starlette 的 `BaseHTTPMiddleware`，会把下游的响应包成一个新的流式响应再转发出去。这对普通请求没问题，但这个项目的核心接口是 SSE 长连接。经过这一层后，客户端断开的感知、取消的传播都多了一道转手，历史上出过不少问题。所以没有用它。

纯 ASGI 中间件只做一件事：在 `http.response.start` 消息（即响应头）发出时插一行 `set-cookie`，响应体原样透传：

```python
async def send_with_cookie(message):
    if message["type"] == "http.response.start":
        MutableHeaders(scope=message).append("set-cookie", ...)
    await send(message)
```

还有一种做法是在依赖里注入 `Response` 设 cookie，但它对 `EventSourceResponse` 这类流式响应是否生效要看 FastAPI 的实现细节。中间件对所有接口一视同仁，更可靠。

### SQL 存储：一份代码两种库

[sql.py](../backend/store/sql.py) 的 `SqlSessionStore` 用 SQLAlchemy 的异步接口实现了 `SessionStore`：

```python
self._engine = create_async_engine(normalize_database_url(url), pool_pre_ping=True)
self._db = async_sessionmaker(self._engine, expire_on_commit=False)

async def append_messages(self, session_id, messages):
    async with self._db.begin() as db:           # 一个事务：正常退出提交，异常回滚
        session = await db.get(Session, session_id)
        if session is None:
            raise SessionNotFound(session_id)
        session.updated_at = utcnow()
        db.add_all(Message(session_id=session_id, **m.model_dump()) for m in messages)
```

几个要点：

- **`DATABASE_URL` 决定用哪个库**。`sqlite+aiosqlite:///aoki.db` 是本地一个文件，`postgresql://...` 是线上。[models.py](../backend/store/models.py) 只用两边都支持的类型（`JSON`、带时区的 `DateTime`），所以同一份代码两边都能跑
- **`begin()` 让一轮消息成为一个事务**：存了工具调用却没存工具结果的历史，再发给 LLM 会被拒绝（[08 章](08-fastapi-sse.md#会话存储openai-格式的超集)）
- **`expire_on_commit=False`**：SQLAlchemy 默认在提交后让对象「过期」，下次读属性时回数据库查。async 下那时会话已经关了，会直接报错
- **`pool_pre_ping=True`**：托管 Postgres 会掐掉空闲连接。不加的话，服务闲置一阵后的第一个请求会拿到一条已经断开的连接
- **按自增 id 排序，不按时间**：同一轮的几条消息可能时间相同，而 tool 消息必须紧跟在对应的 assistant 消息后面
- **`open()` / `close()`**：启动时 `create_all` 建表，关闭时释放连接池。这两个方法加在 `SessionStore` 基类上，默认什么都不做，内存实现不用管。[components.py](../backend/app/components.py) 在 lifespan 里调用它们。配置了数据库却连不上时，`open()` 抛异常，**启动直接失败**，不会悄悄退回内存

两种库的差异要靠测试兜住。[test_store.py](../backend/tests/test_store.py) 的 fixture 带三个参数，同一组用例跑内存、SQLite、Postgres 三种实现。实际就踩到了一处：SQLite 读回来的时间没有时区（见[踩坑日志](pitfalls.md#sqlite-读回来的时间没有时区)），`_utc()` 统一补上。

**连接串归一化**：托管平台（Neon、Supabase、RDS）给的连接串开头是 `postgres://`，还带着 `sslmode=require`。SQLAlchemy 看到 `postgres://` 会去找同步驱动，asyncpg 也不认 `sslmode`。`normalize_database_url` 把它们换成 `postgresql+asyncpg://` 和 `ssl=`，这样平台给的连接串可以原样填进去。

### 前端：401 → 访问码页面

[useChat.ts](../frontend/src/chat/useChat.ts) 里，任何接口返回 401 都会把 `locked` 置为 true，[App.tsx](../frontend/src/App.tsx) 整个换成 [AccessGate.tsx](../frontend/src/components/AccessGate.tsx)。输对访问码后重新拉侧栏、恢复网址里的会话。

前端**不保存访问码**：提交一次，之后靠服务端种下的 HttpOnly cookie，JS 读不到它，也不用每个请求手动加请求头。`fetch` 对同源请求默认带 cookie，开发时 Vite 代理也会原样转发 cookie，所以前端调用接口的代码一行都没改。

## 实测

本地 docker 起 Postgres 16，服务配置 `ACCESS_CODE=sakura-test`、`DATABASE_URL=postgresql://aoki:aoki@localhost:5432/aoki`（故意用平台风格的连接串，验证归一化）：

```
GET  /api/sessions（无 cookie）        → 401 {"detail":"需要访问码"}
POST /api/access {"code":"wrong"}      → 401
POST /api/access {"code":"sakura-test"} → 204
     set-cookie: aoki_access=6281c926…; HttpOnly; Max-Age=31536000; Path=/; SameSite=lax
POST /api/chat「你好，简单打个招呼就行」 → session … done（1 轮，3473 tokens）
GET  /api/sessions                     → 1 条

—— 杀掉进程，重新启动 ——

GET  /api/sessions（原来的 cookie）     → 还是那 1 条
另一个浏览器（输了访问码、新访客 id）   → []，读那条会话的 id → 404
```

库里的数据：

```
              owner               |         title
----------------------------------+------------------------
 Rv2jrPOeC64K_YVWXLhZS-AMPwpsAGPT | 你好，简单打个招呼就行
```

无头 Edge 打开首页（没有 cookie）直接显示访问码页面。

## 设计取舍

**访问码 vs 登录**

正经的做法是账号系统或第三方登录（GitHub / Google OAuth）。这个站的目标是「给面试官、朋友演示」，访问码一句话就能发给对方，没有注册流程，服务端也不用存用户表。代价是：所有人共用一个码，没法单独吊销某个人；码被转发出去就挡不住。演示站点可以接受。真要对外开放，下一步是 OAuth 加上按用户的配额。

**为什么不用 HTTP Basic Auth**

Basic Auth 一行配置就能做，浏览器会弹原生登录框。没用它是因为：弹框没法定制样式也没有说明文字；浏览器缓存凭证后很难「退出」；它每个请求都带着 base64 编码的密码（base64 不是加密）。

**访问码放 cookie vs 放请求头**

另一种做法是前端把访问码存进 localStorage，每个请求加 `X-Access-Code` 头。这样服务端可以完全不碰 cookie，但前端每个调用点都要加头，访问码明文躺在 localStorage 里，有 XSS 就能被偷走。cookie 方案中前端代码不用改，凭证 JS 也读不到。cookie 的老问题 CSRF 由 `SameSite=Lax` 挡住：跨站 POST 不带 cookie，而且 `/api/chat` 要求 `Content-Type: application/json`，普通表单发不出这种请求。

**访客 cookie 丢了，会话就找不回来**

清 cookie、换浏览器、换设备，都等于换了一个人。这是匿名隔离的代价：想跨设备同步，就得有账号。数据还在库里，只是没有入口能访问到它。

**SQLite 还是 Postgres**

线上用哪个，取决于部署方式，不取决于数据量：

- App Runner、Cloud Run 这类容器平台：文件系统是临时的，重新部署就清空，只能用容器外的 Postgres
- 一台带持久磁盘的 VM（EC2 + docker compose、Fly.io volume）：SQLite 完全够用，少维护一个服务

代码两者都支持，决定可以推迟到部署那一章。

**不引入 Alembic**

`create_all` 只建不存在的表，**不会给已有的表加列**。这次给 `sessions` 加了 `owner` 列，如果你本地已经有一个旧版 `aoki.db`，启动不会报错，写入时才会报「no such column」，删掉文件重建即可。线上一旦有了真实数据，再改表结构就该上迁移工具了。

## 动手验证

```powershell
$env:PYTHONIOENCODING = "utf-8"

# 1. 测试（不花 token）：访问码、隔离、三种存储
python -m pytest -v backend/tests/test_api.py backend/tests/test_store.py

# 2. 起一个 Postgres 跑存储测试（需要 Docker Desktop 在运行）
docker run -d --name aoki-pg -e POSTGRES_USER=aoki -e POSTGRES_PASSWORD=aoki -e POSTGRES_DB=aoki -p 5432:5432 postgres:16-alpine
$env:TEST_DATABASE_URL = "postgresql+asyncpg://aoki:aoki@localhost:5432/aoki"
python -m pytest backend/tests/test_store.py      # 这次 Postgres 的 5 个用例也会跑
Remove-Item Env:TEST_DATABASE_URL

# 3. 带访问码、用 SQLite 起服务（数据存在根目录的 aoki.db，已被 .gitignore 排除）
$env:ACCESS_CODE = "sakura-test"
$env:DATABASE_URL = "sqlite+aiosqlite:///aoki.db"
uvicorn backend.app.main:app

# 4. 另开一个终端：没有访问码 → 401；输入访问码 → 204；之后就能用了
curl.exe http://127.0.0.1:8000/api/sessions
'{"code": "sakura-test"}' | Out-File -Encoding utf8 code.json
curl.exe -c cookies.txt -b cookies.txt -X POST http://127.0.0.1:8000/api/access -H "Content-Type: application/json" --data-binary "@code.json" -w "%{http_code}`n"
curl.exe -b cookies.txt -c cookies.txt http://127.0.0.1:8000/api/sessions
```

浏览器里试：`npm run build` 后打开 `http://localhost:8000`，会先看到访问码页面。输入后正常聊几句，然后用另一个浏览器（或无痕窗口）打开，侧栏是空的；把第一个浏览器网址里的 `?s=...` 拷过去，会提示「找不到这个会话」。重启 uvicorn，第一个浏览器刷新后会话还在。

用完删掉 Postgres 容器：`docker rm -f aoki-pg`。

## 延伸思考

1. 访问码挡住了陌生人，但拿到码的人照样能写脚本刷接口。按 IP 限流和「全站每日 token 预算」分别该放在哪一层？loop 已经在 `done` 事件里报告了 `usage`，预算超了以后，是拒绝新请求，还是让正在进行的这一轮先说完？
2. 访问码被人转发到了群里。现在的设计下怎么止损？如果想给每个面试官发一个独立的码、并且能单独吊销，`AccessConfig` 要怎么改？还能做到无状态吗？
3. 服务扩到两个实例时，本章的哪些东西不受影响（提示：HMAC 是无状态的），哪些会出问题？再想想第 04 章 Nominatim「每秒 1 次」的限速是在哪里实现的。
4. 匿名访客只来一次就再也不来，他们的会话会在库里永远留着。该按什么规则清理？清理任务放在哪里跑？
5. 要给 `messages` 表加一列，而线上库里已经有数据了。不用 Alembic 的话你会怎么做？什么时候值得引入迁移工具？
