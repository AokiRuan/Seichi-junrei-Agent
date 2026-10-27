# 10 · React 前端

## 本章目标

读完能回答：

- 浏览器自带的 `EventSource` 为什么用不了？手写一个 SSE 解析器要处理哪些边界情况？
- 同一轮回复里文字和工具调用交替出现，前端用什么数据结构表示？
- 刷新页面后，怎么从存储的消息恢复出和实时对话一模一样的界面？
- 地图上的点从哪来？为什么不从 LLM 的回答文字里解析？
- 开发时前后端在两个端口，部署时在一个容器里，前端代码要不要区分这两种情况？

## 核心概念

### 前端在系统里的位置

前端只认识后端的三个接口，对 LLM、MCP、harness 一无所知：

| 接口 | 用途 |
|---|---|
| `POST /api/chat` | 发消息，响应是 SSE 事件流（`session` / `token` / `tool_call` / `tool_result` / `done` / `error`） |
| `GET /api/sessions` | 侧栏的会话列表 |
| `GET /api/sessions/{id}` | 切换会话、刷新页面时恢复完整对话 |

事件格式在 [08 章](08-fastapi-sse.md) 定义。前端的全部工作就是：**把事件流和存储的消息，翻译成界面**。

### 为什么不用 EventSource

浏览器有现成的 SSE 客户端 `EventSource`，但它**只能发 GET、不能带请求体、不能加自定义请求头**。而 `/api/chat` 是 POST + JSON 请求体。

备选方案：

- **改成 GET，把消息放在 URL 参数里**：URL 有长度限制，用户输入会出现在服务器访问日志里，而且 GET 按语义不应该产生副作用（这个请求会写会话）
- **两步走：先 POST 创建任务、再用 EventSource GET 订阅**：多一次往返，后端要多维护一个"待订阅任务"的状态
- **fetch + 手写解析** ✅：`fetch` 能拿到响应体的字节流（`ReadableStream`），按 SSE 规则自己切分。解析器只有 40 行

代价是要自己处理 `EventSource` 帮你处理好的事——见下面的代码走读。另一个代价是 `EventSource` 的**自动重连**也没了，但对聊天来说断了重连反而是错的（会重复发送消息），所以不算损失。

### 两条路径，一个模型

界面上的一轮对话可能来自两个地方：

```
实时：SSE 事件 ──applyEvent()──────┐
                                   ├──▶ ChatItem[] ──▶ 渲染
恢复：StoredMessage[] ──historyToItems()──┘
```

两条路径产出**同一种数据结构**，渲染代码只写一份。这条约束有测试保证：`model.test.ts` 里用同一轮对话的事件序列和存储消息分别构建，断言结果相同。

### 派生状态：地图不单独存

地图上的点**不是一份独立的状态**，而是每次从对话内容里推导出来的：

```tsx
const mapData = useMemo(() => extractMapData(chat.items), [chat.items])
```

如果地图有自己的状态（比如收到 `tool_result` 时 `setSpots(...)`），就要在实时对话、刷新恢复、切换会话、新建对话四个地方分别维护它，漏一个就会出现"对话换了、地图没换"。只有一份数据源，就不存在不一致。

## 代码走读

```
frontend/src/
├── api/
│   ├── types.ts        后端接口的数据形状
│   ├── sse.ts          SSE 解析器
│   └── client.ts       三个接口的调用
├── chat/
│   ├── model.ts        界面模型 + applyEvent / historyToItems（纯函数）
│   ├── useChat.ts      状态与操作（React hook）
│   └── tools.ts        工具调用的中文描述
├── map/
│   └── extract.ts      从工具结果提取地图数据（纯函数）
├── components/
│   ├── Sidebar.tsx     会话侧栏
│   ├── ChatPanel.tsx   消息列表 + 输入框
│   ├── ToolStep.tsx    一次工具调用的展示
│   └── MapPanel.tsx    地图
├── App.tsx             三栏布局
└── index.css           全部样式
```

分层原则和后端一样：**逻辑放纯函数（`model.ts`、`extract.ts`、`sse.ts`），React 组件只负责渲染**。纯函数不依赖浏览器、不发请求，可以直接写单元测试。

### api/sse.ts：SSE 解析器

[sse.ts](../frontend/src/api/sse.ts)

SSE 是按行的文本协议。后端 FastAPI 的实际输出（用 `fastapi.sse.format_sse_event` 实测，行分隔符是 `\n`）：

```text
event: token
data: {"content": "我来"}
                              ← 空行 = 一个事件结束
: ping                        ← 冒号开头 = 注释（保活），忽略
```

解析器要处理三个边界情况：

**1. 一个汉字被切成两半。** 网络分块不管字符边界。一个汉字在 UTF-8 里占 3 个字节，可能前 1 个字节在这个 chunk、后 2 个在下一个。如果对每个 chunk 单独 `new TextDecoder().decode(chunk)`，两边都会解出乱码 `�`。所以用 `TextDecoderStream`：它是有状态的，会把不完整的字节留到下一次拼接。

**2. 一行被切成两半。** 同理，一个 chunk 可能结束在 `data: {"cont` 这样的半行。所以按 `\n` 切分后，**最后一段留在 buffer 里**，等下一个 chunk 来了再拼：

```ts
const lines = buffer.split('\n')
buffer = lines.pop() ?? ''   // 最后一段可能是半行
```

**3. 兼容 `\r\n`。** 规范允许三种换行。我们后端只发 `\n`，但解析器去掉行尾的 `\r` 来兼容 `\r\n`，代价只有一行。

测试 [sse.test.ts](../frontend/src/api/sse.test.ts) 里最狠的一条是**逐字节**送入整个事件流，断言结果和一次性送入完全相同。

### api/client.ts：两种失败

[client.ts](../frontend/src/api/client.ts)

`streamChat()` 是一个 async generator，调用方用 `for await` 逐个拿事件。它有两种失败方式，对应后端的两种失败：

| 失败发生在 | 后端表现 | 前端表现 |
|---|---|---|
| 流开始**之前**（会话不存在、消息为空） | HTTP 404 / 422 | `fetch` 拿到非 2xx，抛 `ApiError` |
| 流开始**之后**（LLM 调用失败） | HTTP 200，最后一个事件是 `error` | 正常产出一个 `error` 事件 |

这正是 08 章"校验放在依赖里"的另一面：后端保证了可预见的错误都在流开始前以状态码返回，前端才能用状态码区分。

路径全部是相对的 `/api/...`，没有写死后端地址。原因见下面"开发时的代理"。

### chat/model.ts：片段数组

[model.ts](../frontend/src/chat/model.ts)

一轮 agent 回复的真实样子是这样的（05 章实测）：

```
我来查一下。                         ← 文字
[查询作品 莉可丽丝 ✓]                ← 工具
[查询圣地 #364450 ✓]                 ← 工具
《莉可丽丝》的圣地有 5 处：……       ← 文字
```

所以一轮回复存成**按到达顺序排列的片段数组**：

```ts
interface AssistantItem {
  parts: (TextPart | ToolPart)[]
  status: 'streaming' | 'done' | 'error' | 'stopped'
}
```

`applyEvent(item, event)` 把一个事件合并进去：

- `token`：最后一个片段是文字就接上，否则开一个新文字片段
- `tool_call`：追加一个 `status: 'running'` 的工具片段
- `tool_result`：**按 `tool_call_id` 找到**对应的工具片段，填入结果。不能按顺序假设"最后一个工具就是它"——一轮里可能并行调了两个天气查询，结果谁先回来不一定
- `error`：还在 running 的工具标记为"未完成"，否则界面上会有一个永远在转的圈

`applyEvent` **不修改入参、返回新对象**。React 判断要不要重新渲染靠的是引用变化：如果原地修改 `item.parts.push(...)`，引用没变，界面不会更新。

`historyToItems(messages)` 做反方向的折叠。存储格式按 LLM 协议组织（user → assistant 带 tool_calls → tool → tool → assistant），界面按"轮"组织，所以它把两条 user 之间的所有 assistant / tool 消息合并成一个 `AssistantItem`，用 `tool_call_id` 把 tool 消息配回发起它的调用。这正是后端在 tool 消息里额外存 `tool_name` / `is_error` / `data` 的原因（08 章"会话存储：OpenAI 格式的超集"）。

### chat/useChat.ts：状态放在哪

[useChat.ts](../frontend/src/chat/useChat.ts)

所有状态和操作集中在一个自定义 hook 里，组件拿到的是现成的 `items`、`send()`、`stop()` 等。几个值得注意的点：

**当前会话 id 放在网址里**（`?s=xxx`）。刷新页面时从网址读出来、调 `GET /api/sessions/{id}` 恢复。用 `history.replaceState` 而不是 `pushState`，切换会话不会塞满浏览器的后退历史。

**停止生成用 `AbortController`。** `fetch` 收到 abort 信号后会中断连接，`for await` 抛出异常，`catch` 里判断 `signal.aborted` 区分"用户主动停止"和"网络出错"。后端那边，Starlette 发现客户端断开后会取消生成器，ChatService 不会走到 `append_messages`——**停止的这一轮不会被存下来**，和出错时的行为一致（08 章）。

AbortController 放在 `useRef` 而不是 `useState` 里：它不参与渲染，改它不需要触发重渲染。

**切换会话时先 abort 当前的流。** 否则旧会话还在流式输出，它的事件会被写进新会话的界面。

**会话 404 时的提示。** 当前用的是内存存储（Postgres 在 Day 4），后端一重启会话就没了，网址里的旧 id 会 404。这里给出明确提示并开始新对话，而不是显示一个空白页面。

### 工具过程可视化

[tools.ts](../frontend/src/chat/tools.ts) · [ToolStep.tsx](../frontend/src/components/ToolStep.tsx)

这是 agent demo 的核心观感：用户能看到 agent **在做什么**，而不只是等一个结果。每个工具片段显示成一行：

```
⟳ 正在查询圣地…  作品 #364450                         （running）
✓ 查询圣地  作品 #364450  → 5 个圣地                  （ok）
! 获取天气预报  35.32, 139.55  → 天气服务暂时不可用    （降级）
```

动作名（"查询圣地"）、参数摘要、结果摘要都由 `tools.ts` 从工具名、参数、结果里摘出来。点击可以展开看**原始参数和结果 JSON**——这正是 LLM 拿到的东西，排查"它为什么这么回答"时很有用。

有一个细节：天气和路线 server 在外部 API 失败时，返回的是 `{"error": "..."}` 而 `is_error` 是 `false`（04 章：server 主动降级，让 LLM 能据此回答用户）。对前端来说这仍然是"没拿到数据"，所以 `describeResult` 单独识别 `{error}`，显示成黄色警告而不是绿色对勾。

真正的工具失败（`is_error: true`）时，`data` 是空的。为了让界面能显示失败原因，这次给后端的 `tool_result` 事件加了一个 `error` 字段（见下文"后端配合的改动"）。

### 地图：只认结构化数据

[extract.ts](../frontend/src/map/extract.ts) · [MapPanel.tsx](../frontend/src/components/MapPanel.tsx)

地图上的点**只从工具返回的结构化 `data` 里取，从不解析 LLM 写的文字**。05 章实测过 LLM 抄写坐标时会抄错一位数字，而 `list_spots` 返回的 `lat` / `lng` 是数据源里的原值。

`extractMapData(items)` 遍历对话里所有成功的工具调用：

| 工具 | 地图上画什么 |
|---|---|
| `list_spots` / `get_spot` | 圣地圆点，按 `spot_id` 去重（多轮里同一地点会被查多次）；点击弹出图片、出现的集数、镜头数，以及跳回 Anitabi 的链接 |
| `plan_route` | 只取**最近一次**的路线：编号站点 + 虚线连线，右下角路线卡片 |
| `geocode` | 第一个候选地点（通常是车站等起点），与路线站点重合时不画 |
| 其他 | 不影响地图 |

几个实测后调整过的地方：

**视野自动缩放：有路线时只框路线。** 最初是把所有点都框进视野。实测《上低音号》有一个圣地（京都音乐厅）在京都市区，离宇治 15 公里，全框进来后宇治的路线缩成右下角一团。所以 `focusPoints()` 在有路线时只返回路线站点——用户此时关心的是怎么走。缩放时右下角还会留出路线卡片的宽度，免得卡片盖住路线。

**重合点按距离而不是名字判断。** `geocode` 查的是「京阪宇治駅」，LLM 传给 `plan_route` 的名字是「京阪宇治站」，差一个字。按名字去重失败，两个标记叠在一起盖住了第 1 站。改成坐标相距约 100 米内视为同一点。

**自动缩放的依赖是字符串，不是数组。** `FitBounds` 组件在点集变化时调 `map.flyToBounds()`。流式输出时每来一个 token 整个页面重渲染一次，points 每次都是新数组——拿数组当 `useEffect` 依赖，地图会每个字都重新飞一次。所以把点集拼成字符串 key 作为依赖，字符串按值比较。

**`list_spots` 的格式变过一次。** 接入真实数据后（[03 章](03-mcp-server.md#工具接口为什么要改)），`list_spots` 从直接返回数组改成 `{total_spots, shown, spots: [...]}`，并且为了省 LLM 的 token，作品名、作品 id 只在顶层写一次，来源链接也不再逐个写。`extractMapData` 两种格式都认，缺的字段从顶层补、链接用 `anime_id + spot_id` 拼。工具步骤的摘要也随之显示「共 41 处，列出 15 处」。

**署名。** 圣地数据遵循 CC BY-NC-SA 4.0，地图右下角的版权栏里加了「圣地数据 © Anitabi」，和 OpenStreetMap 的底图版权放在一起。

**巡礼攻略卡片。** `plan_pilgrimage` 的结果在工具步骤下方渲染成图文攻略，地图上同时画出编号路线与出发地，详见 [11 章](11-pilgrimage-planner.md#攻略卡片)。

**路线是直线示意。** `plan_route` 返回站点顺序和每段的距离、时间，但不返回道路几何，所以地图上画的是站点之间的直线。要画真实道路需要让路线 server 返回 OSRM 的 `geometry`（见延伸思考）。

**不用 Leaflet 的默认图标，样式写进 pathOptions**，原因见 [踩坑日志](pitfalls.md#前端)。

### 后端配合的改动

**1. 接口挂到 `/api` 下。** [main.py](../backend/app/main.py)

```python
app.include_router(chat.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
...
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
```

根路径 `/` 留给前端页面。备选是接口留在根路径、页面挂到 `/app`——但那样用户访问的网址就多了一段，而且页面路径和接口路径混在同一层，将来加接口要小心别和静态文件重名。统一前缀后，开发时 Vite 只需代理 `/api` 一条规则。`/health` 留在根路径，给部署平台的健康检查用（Day 5）。

`mount("/")` 必须写在所有路由之后：Starlette 按注册顺序匹配，挂在 `/` 上的静态文件会接住所有路径。没有构建产物时（只跑后端、跑测试）跳过挂载。`html=True` 让 `/` 返回 `index.html`。前端没有客户端路由（会话 id 在查询参数里），所以不需要"找不到的路径都回退到 index.html"的 SPA fallback。

**2. `tool_result` 出错时带上错误说明。** [loop.py](../backend/agent/loop.py)

```python
payload = {"is_error": outcome.is_error, "data": outcome.data}
if outcome.is_error:
    payload["error"] = outcome.text
```

原来出错时 `data` 是 `None`，前端只知道失败了、不知道为什么。恢复历史时不需要这个字段：存储的 tool 消息的 `content` 就是这段文字。

### 开发时的代理

[vite.config.ts](../frontend/vite.config.ts)

开发时页面由 Vite 开发服务器提供（`localhost:5173`，改代码热更新），后端在 `127.0.0.1:8000`。两个端口 = 两个源（origin），浏览器默认禁止跨源请求，需要后端开 CORS。

更简单的做法是让 Vite 转发：

```ts
server: { proxy: { '/api': 'http://127.0.0.1:8000' } }
```

浏览器只和 5173 打交道，看到的是同源请求。部署时页面和接口都由 FastAPI 在同一个端口提供，同样是同源。**前端代码在两种环境下完全一样**，都写相对路径 `/api/...`，不需要配置"后端地址"环境变量，后端也不需要 CORS。

SSE 经过代理会不会被缓冲（攒够了才转发）？实测不会：通过 5173 代理请求，首个 token 在 0.65 秒到达，203 个 token 事件分散在 36 秒里陆续到达。

## 实测

用 Chrome DevTools Protocol（CDP）驱动无头 Edge，像用户一样点示例问题，每 0.1 秒读一次页面状态，然后刷新页面、点侧栏切换会话（2026-10-21）：

**场景三（排路线）**，DeepSeek，约 20 秒完成：

```text
12.8s  正在查询作品… 吹响！上低音号 / 正在查找地点… 京阪宇治駅      ← 两个转圈：并行调用
13.8s  ! 查询作品 吹响！上低音号 → 没有找到作品                     ← 黄色：搜索结果为空
       ✓ 查找地点 京阪宇治駅 → 京阪宇治駅
       ✓ 查询作品 上低音号 / 響け！ユーフォニアム / 宇治 → 吹响吧！上低音号   ← LLM 换三种写法并行重试
20.0s  ✓ 查询圣地 作品 #115908 → 6 个圣地
       ✓ 规划路线 5 个地点 · 步行 → 5 站 · 5.11 km · 约 68 分钟
```

LLM 第一次把作品名写成「吹响！上低音号」，mock 数据里是「吹响吧！上低音号」，模糊匹配没命中。它自己换了三种写法**并行**重试——这正是工具过程可视化的价值：不展示过程的话，用户只会觉得"有点慢"，看不出 agent 走了弯路。（搜索匹配不够宽容是数据层的问题，记入延伸思考。）

**刷新页面**后：网址、工具步骤列表、地图标记数、路线站点数、侧栏选中项全部和刷新前一致。回答文字少了约 20 个字符，是末尾的「5 轮 · 15,217 tokens」——token 用量只在实时的 `done` 事件里有，没有存进会话。

**LLM 超时**（实测时恰好遇到一次 `APITimeoutError`）：界面显示「出错了：LLM 调用失败：APITimeoutError…」；刷新后这一轮不见了——出错的轮次不入库（08 章），会话本身留在侧栏里，是空的。

**中途停止**：用 `curl --max-time 3` 模拟用户在生成中途断开。70 秒后（完整生成约 37 秒）查询该会话，消息数仍是 0——后端确实取消了生成，而不是在后台跑完再存。

## 设计取舍

| 决策 | 备选 | 为什么这样选 |
|---|---|---|
| fetch + 手写 SSE 解析 | `EventSource`；第三方库 `@microsoft/fetch-event-source` | EventSource 不支持 POST；第三方库功能多（重连、重试），聊天场景都用不上，40 行代码能看懂全部细节 |
| `useState` + 纯函数 | Redux / Zustand 等状态库 | 状态只有一个页面在用，一个 hook 够了。纯函数保证了可测试性，换状态库不会更好测 |
| 地图数据从对话派生 | 地图单独存状态 | 单一数据源，四种场景（实时 / 刷新 / 切换 / 新建）天然一致 |
| 只用工具的结构化数据上图 | 解析 LLM 回答里的地名或坐标 | LLM 会抄错坐标；结构化数据是数据源原值 |
| Leaflet + OpenStreetMap | Google Maps / Mapbox | 免费、无需 API key；demo 流量下 OSM 瓦片的使用政策允许 |
| 一个 `index.css` | CSS Modules / Tailwind | 不到 600 行、按区块分段，一个文件能看全；引入 Tailwind 要多学一套类名 |
| `react-markdown` 渲染回答 | 纯文本显示 | LLM 爱用标题、列表、表格，纯文本下一堆 `**` 和 `|`。要加 `remark-gfm` 才支持表格（实测踩过） |
| 会话 id 放网址查询参数 | localStorage；React Router | 刷新可恢复、能复制到别的标签页；只有一个页面，不需要路由库 |
| 接口统一 `/api` 前缀 | 接口在根路径、页面挂子路径 | 用户网址最短；页面和接口路径不会冲突；代理只需一条规则 |

## 动手验证

以下命令都在 PowerShell、项目根目录下实测过。

```powershell
# 0. 装 Node（只需一次）。本项目实测用的是从国内镜像下载的免安装版 v24.22.1：
#    https://registry.npmmirror.com/-/binary/node/v24.22.1/node-v24.22.1-win-x64.zip
#    解压到 %LOCALAPPDATA%\Programs\ 并加入用户 PATH。也可以从 nodejs.org 下载 LTS 安装包。
#    装完要重开终端；在 VS Code 里要彻底退出 VS Code 再打开，只开新终端标签页不够（见踩坑日志）：
node --version    # v24.22.1
npm config set registry https://registry.npmmirror.com   # 国内镜像

# 1. 装依赖、跑前端单元测试（不需要后端，不花 token）
cd frontend
npm install
npm test          # 3 个文件，17 个测试
npm run lint      # oxlint
cd ..

# 2. 开发模式：两个终端
#    终端 A：后端
uvicorn backend.app.main:app --reload
#    终端 B：前端（改代码浏览器自动刷新）
cd frontend; npm run dev
#    浏览器打开 http://localhost:5173 。注意是 localhost 不是 127.0.0.1，原因见踩坑日志

# 3. 部署模式：构建后由 FastAPI 一起提供，只需一个进程
cd frontend; npm run build; cd ..
uvicorn backend.app.main:app
#    浏览器打开 http://127.0.0.1:8000

# 4. 在浏览器里依次点空白页上的三个示例问题，对照 00 章的三个核心场景：
#    - 查圣地：两个工具步骤依次出现、转圈变对勾；地图飞到浅草，5 个圆点，点击弹出详情
#    - 查天气：天气工具显示「7 天预报 · 今天…」，回答里有表格
#    - 排路线：地图上出现编号站点和虚线，右下角路线卡片
#    然后刷新页面：对话、工具步骤、地图应该原样恢复；左侧点另一个会话能切换
```

没有浏览器自动化工具时，可以用 Edge 的无头模式截图检查页面（本章的界面调整都是这样验证的）：

```powershell
$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
& $edge --headless=new --disable-gpu --hide-scrollbars --window-size=1440,900 `
  --virtual-time-budget=15000 --screenshot="$PWD\shot.png" "http://127.0.0.1:8000/?s=<会话id>"
```

`--virtual-time-budget` 让页面先跑 15 秒（虚拟时间）再截图，等请求返回、地图瓦片加载。

## 延伸思考

1. 流式输出时每个 token 都触发一次 `setItems`，整个消息列表重新渲染，长对话里 Markdown 也要重新解析。在什么规模下这会成为性能问题？可以怎么优化（提示：`React.memo` 包住已完成的消息；或者把 token 攒一帧再更新）？
2. 用户点"停止"后，后端那一轮没有存下来，但前端界面上还留着生成了一半的文字。刷新页面后这段文字就消失了。这算 bug 吗？如果要"停止时也保存已生成的部分"，前后端分别要改什么？
3. 想让地图画出真实的步行道路而不是直线：OSRM 的 route 接口加 `overview=full&geometries=geojson` 会返回道路坐标串。但这串坐标可能有几百个点，放进 `plan_route` 的结果里会发生什么（提示：这个结果也会作为工具结果发给 LLM）？怎么做到"给前端但不给 LLM"？
4. 天气场景里地图是空的。要不要在查天气的位置画一个标记？如果画，它应该和圣地标记长得一样吗？
5. 场景三里 LLM 把作品名写成「吹响！上低音号」就搜不到了，多花了一轮重试。这该在哪一层解决：prompt 里告诉 LLM 多试几种写法？`search_anime` 做更宽容的匹配（去掉标点、只比较核心字）？还是返回"相近的作品"让 LLM 选？
6. `types.ts` 靠手工和后端的 Pydantic 模型对齐。FastAPI 能自动生成 OpenAPI 文档（`/openapi.json`），有工具能从它生成 TypeScript 类型。什么时候值得引入这一步？SSE 事件的格式能被 OpenAPI 描述吗？
