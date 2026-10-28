# 01 · 架构与设计思想

## 本章目标

读完能回答：

- 用户在网页上问一句「莉可丽丝的圣地在哪」，这句话经过了哪些模块、每一步发生了什么？
- 为什么 `backend/` 要拆成 `app` / `agent` / `store` / `mcp_servers` 四块，它们之间谁能依赖谁？
- 这个项目里反复出现的几种写法（抽象接口、yield 事件、返回错误而不抛异常）背后是什么道理？

## 核心概念

**Agent 和普通后端的区别**：普通后端的流程是写死的——收到请求、查库、返回。Agent 的流程是 **LLM 在运行时决定的**：它看到用户问题后，自己决定调哪个工具、调几次、什么时候停。你的代码负责的不是"流程"，而是"让 LLM 能安全地决定流程"的那套运行环境——这就是 **harness**。

**Tool calling**：现代 LLM API 允许你在请求里附上一组工具的描述（名字、用途、参数的 JSON Schema）。LLM 如果觉得需要，会在回复里返回一个结构化的 `tool_calls` 字段（"我要调 `list_spots`，参数是 `{anime_id: 364450}`"），而不是普通文本。**LLM 本身不执行任何东西**——执行是你的代码做的，结果再塞回对话让 LLM 继续。

## 全景图

```
┌──────────────────────────────┐
│   React                      │  frontend/                                 ✅
└──────────────┬───────────────┘
               │ POST /api/chat  ←→  SSE 事件流
┌──────────────▼───────────────┐
│   app/  FastAPI              │  路由、SSE 编码、生命周期、ChatService      ✅
└──────┬───────────────┬───────┘
       │               │
       ▼               ▼
┌─────────────┐  ┌─────────────────────────────┐
│ store/      │  │ agent/                       │  ← 项目核心
│ 会话持久化   │  │  loop     主循环（流式） ✅   │
│ 内存 ✅      │  │  harness  边界策略       ✅   │
│ SQL 存储 ✅  │  │  llm      LLM 抽象       ✅   │
│             │  │  mcp_client 工具聚合     ✅   │
│             │  │  events   对外事件契约   ✅   │
│             │  │  tracing  Langfuse       ✅   │
└─────────────┘  └───────────────┬─────────────┘
                                 │ JSON-RPC over stdio
                  ┌──────────────┼──────────────┐
                  ▼              ▼              ▼
           mcp_servers/seichi   .../weather    .../route
           圣地巡礼 ✅           天气 ✅         路线 ✅
           （本地数据）          Open-Meteo      OSRM + Nominatim
```

## 一次请求的完整旅程

以「莉可丽丝的圣地在哪？」为例。右侧标注实现状态和负责的文件。

| # | 发生了什么 | 负责模块 | 状态 |
|---|---|---|---|
| 1 | 前端 `POST /api/chat`，带上 `session_id` 和用户消息 | [frontend/src/api/client.ts](../frontend/src/api/client.ts) | ✅ |
| 2 | ChatService 从 store 读出这个会话的历史消息，加上 system prompt 和本轮用户消息 | [app/chat_service.py](../backend/app/chat_service.py) · [store/](../backend/store/) | ✅ |
| 3 | 调 `AgentLoop.run(messages)`，拿到一个事件流 | [agent/loop.py](../backend/agent/loop.py) | ✅ |
| 4 | loop 把历史 + **所有 MCP 工具的 schema** 一起发给 LLM | [agent/llm.py](../backend/agent/llm.py) · [agent/mcp_client.py](../backend/agent/mcp_client.py) | ✅ |
| 5 | LLM 回复 `tool_calls: search_anime(title="莉可丽丝")` | LLM | — |
| 6 | loop yield 一个 `tool_call` 事件 → 前端显示「正在查询作品…」 | [agent/events.py](../backend/agent/events.py) · [frontend/src/chat/model.ts](../frontend/src/chat/model.ts) | ✅ |
| 7 | mcp_client 把调用经 stdin 发给 seichi 子进程，从 stdout 读回结果 | [agent/mcp_client.py](../backend/agent/mcp_client.py) | ✅ |
| 8 | seichi server 查数据，返回 `anime_id: 364450` | [mcp_servers/seichi/](../backend/mcp_servers/seichi/) | ✅ |
| 9 | loop yield `tool_result`，把结果追加进 messages，**回到第 4 步** | loop | ✅ |
| 10 | 第二轮：LLM 看到 anime_id，决定调 `list_spots(364450)`，重复 5~9 | — | — |
| 11 | 第三轮：LLM 拿到 5 个地标，不再调工具，开始输出文字 → 一串 `token` 事件，逐字流式 | loop | ✅ |
| 12 | `done` 事件；ChatService 把本轮消息（含工具调用记录与结构化结果）写回 store，路由把事件编码成 SSE | chat_service.py · routes/chat.py | ✅ |
| 13 | 前端把 `tool_result` 里的坐标画到地图上；刷新页面时从 `GET /api/sessions/{id}` 恢复同样的界面 | [frontend/src/map/extract.ts](../frontend/src/map/extract.ts) | ✅ |

贯穿全程：每一轮 LLM 调用、每一次工具调用都被 Langfuse 记录成 trace（[agent/tracing.py](../backend/agent/tracing.py)，✅）。

注意第 9 步的"回到第 4 步"——**这个循环就是 agent loop**。它跑几圈是 LLM 决定的，不是你决定的。这也是为什么需要 harness：万一 LLM 永远不停呢？

## 五条设计原则

整个 `backend/` 的结构都是这五条原则推出来的。理解它们，你就能自己判断新代码该放哪、该怎么写。

### 原则一：Agent loop 在最中心，对外界一无所知

loop 不知道有 HTTP，不知道有数据库，不知道用的是哪家 LLM。所有外部依赖都通过参数传进来：

```python
class AgentLoop:
    def __init__(self, llm, mcp_pool, harness): ...
```

依赖方向只能从外往里：

```
app/routes ──▶ agent/loop ──▶ agent/llm        (接口)
    │              │     └──▶ agent/mcp_client (接口)
    │              └────────▶ agent/events     (契约)
    └──────────▶ store/base (接口) ◀── store/memory · store/sql
```

**判断标准**：`agent/` 目录下的文件永远不应该 `import` `app/` 或 `store/` 里的东西。写代码时如果发现需要反向 import，停下来——那是设计出问题了。

好处：loop 可以脱离 Web 服务单独测试、单独跑评测；换 Web 框架、换数据库都不碰 loop。

### 原则二：用事件流对外输出，而不是直接写响应

最直觉的写法是 loop 里直接往 HTTP 响应写字符串：

```python
# ❌ 反例
async def run(self, messages, response):
    async for chunk in self.llm.stream_chat(...):
        await response.write(f"data: {chunk}\n\n")
```

这样写有三个问题：评测脚本想测 agent 必须先起 HTTP 服务；想加 WebSocket 必须改 loop；`data: xxx` 是个字符串，前后端之间没有契约。

所以 loop 只 **yield 语义化的事件对象**（[events.py](../backend/agent/events.py)）：

```
AgentLoop.run()  ──yield AgentEvent──▶  chat.py 翻译成 SSE  ──▶ 前端
                                   └──▶  评测脚本直接消费，不起服务
```

这个模式叫**端口与适配器**：loop 定义端口（事件长什么样），HTTP 层和评测脚本是两个不同的适配器。

`AgentEvent` 里有两个细节值得注意：

- 用 pydantic `BaseModel` + `Literal` 约束事件类型——写错事件名在 IDE 里就报红，序列化成 JSON 发给前端也是一行 `.model_dump_json()`
- `tool_call` 和 `tool_result` 共享同一个 `tool_call_id`。LLM 可能一次并发调多个工具，前端靠这个 id 才能把"正在查询…"的占位和后到的结果对上

### 原则三：loop 与 harness 分离

[loop.py](../backend/agent/loop.py) 只写正常流程：问 LLM → 有工具就调 → 结果塞回去 → 再问。这段逻辑其实非常短。

[harness.py](../backend/agent/harness.py) 写的全是"不正常的时候怎么办"：

- 转了 8 圈还不收敛怎么办 → `should_continue`
- 上下文太长了砍哪些 → `trim`（注意 `tool_calls` 和对应的 tool 结果必须成对保留，否则多数 LLM 服务直接报错）
- 工具报错了是中断还是继续 → `on_tool_error`

**真实项目里，第二部分的代码量和难度远大于第一部分。** 混在一起写，loop 会迅速变成一坨嵌套 if。分开之后，你可以单独拿 harness 做实验——改迭代上限、换裁剪策略——主流程一行不动。

这也是"自建 harness"比"自建 loop"更有学习价值的原因：loop 到处都能抄到，harness 是在真实场景里一点点攒出来的经验。

### 原则四：会变的东西先立接口

项目里已经有三处用了同一个手法：

| 接口 | 当前实现 | 将来的实现 | 为什么会变 |
|---|---|---|---|
| [`SessionStore`](../backend/store/base.py) | 内存 | SQLite / Postgres（[12 章](12-going-public.md)） | Day 2 先跑通，之后再持久化；接口只加了 `owner` |
| [`LLMClient`](../backend/agent/llm.py) | DeepSeek | 任意 OpenAI 兼容服务 | DeepSeek 的 tool calling 稳定性是已知风险 |
| [`SeichiRepository`](../backend/mcp_servers/seichi/repository.py) | 本地 mock | Anitabi API | Anitabi 被限流，且缺坐标（见 [03](03-mcp-server.md)） |

**判断标准**：这个东西以后可能换吗？可能换就先立接口。反过来，不可能换的（比如 FastAPI 本身）就别抽象——为不存在的变化写抽象是过度设计。

第三行是个很好的例子：写计划时并不知道 Anitabi 会出问题，但因为数据源本来就被判断为"高风险、可能要换"，所以先立了接口。结果真出问题时，切 mock 只改了一个函数。

### 原则五：降级，而不是崩溃

- `DATABASE_URL` 为空 → 回退到内存 store。本地随手改代码不用先起 Postgres（[config.py](../backend/app/config.py)）
- Langfuse 没配 key → 静默跳过。可观测性是锦上添花，不该因为它没配就起不来服务
- 工具调用失败 → **把错误作为结果交给 LLM**，而不是抛异常中断对话（[harness.py](../backend/agent/harness.py) 的 `on_tool_error`，[seichi server](../backend/mcp_servers/seichi/server.py) 的 `get_spot`）

最后一条是 agent 系统特有的。传统后端遇到错误返回 500；agent 可以把"没找到这个圣地"告诉 LLM，让它自己决定是换个参数重试、还是如实告诉用户。**LLM 本身就是一个很好的错误处理器**，别剥夺它处理错误的机会。

## 设计取舍

**为什么工具用 MCP，而不是直接在后端写 Python 函数？**

直接写函数更简单，一个进程搞定。选 MCP 是因为：工具的 schema 由协议标准化（不用像 [learn/tools/registry.py](../learn/tools/registry.py) 那样手工拼 JSON Schema）；工具运行在独立进程，崩了不拖垮主服务；同一个 MCP server 可以被任何 MCP 客户端复用（Claude Desktop、Cursor 都能直接接）；外部的天气、路线服务也是 MCP，接入方式统一。代价是多了进程间通信和子进程管理。对学习项目来说，这个代价正好是值得学的东西。

**为什么用 SSE 而不是 WebSocket？**

对话是"用户发一条、服务端流式回一串"的单向推送，SSE 正好是为这个设计的：基于普通 HTTP，天然支持断线重连，代理和负载均衡器不用特殊配置。WebSocket 是双向的，这里用不上，反而要自己处理心跳和重连。

**为什么是单容器？**

FastAPI 托管前端静态文件、seichi server 作为子进程——一个镜像就是整个应用。Docker、CI、AWS 都只管一个东西，5 天工期内最省事。代价是前后端无法独立扩缩容，demo 不需要。

## 对照：learn/ 里的老写法

| 学习期写法 | 现在的写法 | 对应原则 |
|---|---|---|
| [registry.py](../learn/tools/registry.py) 手工拼 JSON Schema | MCP 从函数签名自动生成 | 接口交给协议 |
| [simple_agent.py](../learn/agent/simple_agent.py) 用正则从文本里抠 `[TOOL_CALL:xxx]` | LLM 原生 `tool_calls` 字段 | 契约交给类型 |
| loop 里直接 `print()` | yield `AgentEvent` | 输出交给上层 |

第二行尤其值得体会：正则解析文本是 LLM 还不支持 tool calling 时代的做法，LLM 稍微换个格式就解析失败。原生 `tool_calls` 是结构化 JSON，由 LLM 服务端保证格式。

## 动手验证

原则一可以用 grep 自查——`agent/` 下不应该出现对 `app` 或 `store` 的导入：

```powershell
Select-String -Path backend\agent\*.py -Pattern "from backend\.(app|store)|from \.\.(app|store)"
```

没有输出就是对的。

## 延伸思考

1. 原则一说 loop "不知道有数据库"。但第 12 步需要把 assistant 消息写回 store——这件事为什么放在 chat_service.py 而不是 loop 里？如果放进 loop 会破坏什么？（[08 章](08-fastapi-sse.md#分层路由chatserviceloop)有答案）
2. 原则五说"把错误交给 LLM"。有没有哪类错误**不应该**交给 LLM，而应该直接中断？（提示：想想 API key 失效、或者 LLM 服务本身挂了）
3. `tool_call_id` 解决了并发工具调用的对应问题。那 LLM 真的会并发调工具吗？在圣地巡礼的三个场景里，哪一个最可能出现？
