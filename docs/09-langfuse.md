# 09 · 可观测性：Langfuse

## 本章目标

读完能回答：

- agent 跑出了奇怪的结果，怎么知道它中间每一步看到了什么、决定了什么？
- trace、observation、generation、span 这些词分别指什么？
- 怎么在**不改 loop 一行代码**的前提下给它加上追踪？
- Langfuse 挂了，会不会拖垮我们的服务？
- 为什么 `.env` 里填了正确的 key，还是报 401？

## 核心概念

### 为什么 agent 特别需要可观测性

普通后端出 bug，看日志和堆栈基本就能定位：代码路径是固定的。Agent 的路径是 **LLM 在运行时决定的**：同一个问题，这次调了 3 个工具，下次可能调 2 个；这次回答对了，下次可能把坐标抄错（[05 章实测发现](05-agent-loop.md#实测发现)）。

要回答"它为什么这样回答"，你需要看到：

- 每一轮 LLM **收到的完整上下文**（system prompt + 历史 + 工具结果）
- 它**决定**调哪个工具、传了什么参数
- 工具**返回**了什么
- 每一步花了多少时间、多少 token

日志能记，但一次回答几十条日志散落在一起，很难看出结构。**Langfuse** 这类工具把一次回答组织成一棵树，在网页上逐层展开。

### 术语

| 术语 | 含义 | 本项目里 |
|---|---|---|
| **trace** | 一次完整的处理过程 | 一轮对话（用户问一句，agent 答一句） |
| **observation** | trace 里的一个节点，有开始和结束时间 | 下面三种都是 |
| **agent** | 表示一个 agent 执行过程的节点 | `agent-turn`，整轮的根节点 |
| **generation** | 一次 LLM 调用，带模型名、token 用量、首字时间 | `llm`，每轮 LLM 调用一个 |
| **tool** | 一次工具调用 | 以工具名命名，如 `search_anime` |
| **session** | 把多个 trace 归到一起 | 一个会话的多轮对话 |

实际记录下来的一个 trace（"莉可丽丝的圣地在哪？"）：

```
AGENT      agent-turn       3.91s   session=35d89e57…  input=莉可丽丝的圣地在哪？
  GENERATION llm            0.99s   首字 0.74s   tokens 1787 → 52
  TOOL       search_anime   0.05s
  GENERATION llm            0.76s                tokens 1937 → 41
  TOOL       list_spots     0.00s
  GENERATION llm            2.10s   首字 0.78s   tokens 2768 → 242
```

单看这棵树就能读出很多信息：

- 三轮 LLM 调用的输入 token **一路上涨**（1787 → 1937 → 2768）。每轮都把之前的工具结果带上了，`list_spots` 返回 5 个地标的完整 JSON 一下子增加了 800 多 token。这直观地解释了 [05 章](05-agent-loop.md#实测发现)说的"每个问题 6600～8000 tokens"从哪来
- 时间主要花在 LLM 上，工具调用（本地 MCP 子进程）几乎不花时间
- 第二轮**没有首字时间**：这一轮 LLM 没输出任何文字，只发了工具调用

## 代码走读

### 设计：装饰器模式

最直接的加法是在 loop 里到处写 Langfuse 调用：

```python
# ❌ 反例
async def run(self, messages):
    with langfuse.start_as_current_observation(...):        # loop 里混进了追踪代码
        resp = await self.llm.chat(...)
        langfuse.update_current_generation(usage=...)
        ...
```

这会让 loop 依赖 Langfuse；而且追踪代码和业务逻辑缠在一起，loop 会变得难读。

注意到 loop 只通过两个接口和外界打交道：`ChatModel`（LLM）和 `ToolExecutor`（工具）。那就给这两个接口各套一层"外壳"：外壳实现同样的接口，内部把调用转给真正的实现，同时顺手记录下来。

```
              ┌──────────── TracedLLM ────────────┐
loop ──stream()──▶  记录 generation  ──stream()──▶  LLMClient
              └───────────────────────────────────┘
              ┌─────────── TracedTools ───────────┐
loop ─call_tool()─▶  记录 tool  ─call_tool()─▶  MCPClientPool
              └───────────────────────────────────┘
```

loop 拿到的是外壳，但它只认接口，**完全不知道**自己被追踪了。这就是**装饰器模式**：不修改原对象，通过包一层来增加功能。`LLMClient`、`MCPClientPool`、`AgentLoop` 三个类一行没改。

这是 [05 章](05-agent-loop.md#llmpy让-loop-不认识-openai-sdk)用 `Protocol` 定义 `ChatModel` 的又一个回报：`TracedLLM` 没继承任何类，只因为有一个签名一致的 `stream()` 方法，就能直接替换进去。

### tracing.py

[tracing.py](../backend/agent/tracing.py) 四个类：

**`TracedLLM`**：每次 `stream()` 记录成一个 generation。

```python
async def stream(self, messages, tools=None):
    with self._lf.start_as_current_observation(
        name="llm", as_type="generation", model=self._model, input=messages, metadata={"tools": [...]},
    ) as gen:
        async for item in self._inner.stream(messages, tools):
            if isinstance(item, TextDelta) and 是第一片:
                gen.update(completion_start_time=现在)       # 首字时间
            elif isinstance(item, LLMResponse):
                gen.update(output=item.to_message(), usage_details=...)
            yield item                                       # 原样转发给 loop
```

`completion_start_time` 让 Langfuse 能算出**首字延迟**（time to first token）。对流式应用来说，用户感受到的"快不快"主要看它，而不是总耗时。

`usage_details` 要把 OpenAI 的字段名（`prompt_tokens`）换成 Langfuse 的（`input`）。

**`TracedTools`**：每次 `call_tool()` 记录成一个 tool 节点；工具失败（`is_error=True`）时标记 `level="ERROR"`，在 Langfuse 界面上会标红，方便筛选出所有失败的调用。

**`Tracing`**：对外的入口。

```python
tracing = Tracing.from_settings(public_key, secret_key, host)
llm   = tracing.wrap_llm(llm, model)       # 关闭时原样返回
tools = tracing.wrap_tools(tools)          # 关闭时原样返回
with tracing.turn(session_id=..., user_input=...) as turn:   # 关闭时什么都不做
    ...
    turn.finish(answer, iterations=..., usage=...)
```

**没配 key 时整个处于关闭状态**：`wrap_*` 直接返回原对象（连外壳都不套），`turn()` 返回一个什么都不做的空壳。调用方不需要写 `if tracing_enabled:` 之类的判断。这是 [01 章原则五](01-architecture.md#原则五降级而不是崩溃)：可观测性是锦上添花，没配不应该影响任何功能。

**`Turn`**：一轮对话的根节点。`turn()` 里用了 `propagate_attributes(session_id=...)`，把会话 id 打到这个 trace 上，Langfuse 的 Sessions 页面就能把同一个会话的多轮对话串起来看。

### 节点是怎么挂到一起的

`TracedLLM` 和 `TracedTools` 各自创建节点，并没有互相传递"父节点"，它们怎么知道要挂在 `agent-turn` 下面？

靠的是 `start_as_current_observation` 里的 **current**：它把新节点设为"当前节点"，存在 Python 的 `contextvars` 里（Langfuse 4.x 底层是 OpenTelemetry）。之后创建的节点自动成为当前节点的子节点。

`asyncio.gather` 并行执行工具时会创建新的 task，而 **asyncio 创建 task 时会复制当前的 contextvars**，所以并行的工具调用也能正确挂到 `agent-turn` 下。

### 在哪里调用 turn()

在 [ChatService.run_turn()](../backend/app/chat_service.py)，包住整个 `loop.run()`。CLI 和 HTTP 共用 ChatService，所以两边都有追踪。

## 实测发现

### 1. 401：key 对了，区域不对

第一次连 Langfuse，`auth_check` 返回 401 `Invalid credentials. Confirm that you've configured the correct host.`。

排查：key 的前缀（`pk-lf-`、`sk-lf-`）、长度、有没有多余空格、有没有填反，都没问题。然后用同一对 key 分别请求 Langfuse Cloud 的几个区域：

```
https://cloud.langfuse.com        401    ← EU，默认值
https://us.cloud.langfuse.com     401
https://jp.cloud.langfuse.com     200    ← 项目建在这里
https://hipaa.cloud.langfuse.com  401
```

**Langfuse Cloud 按区域独立部署，key 只在创建项目的那个区域有效。** `.env` 里的 `LANGFUSE_HOST` 是默认的 EU 区域，改成 `jp.cloud.langfuse.com` 就好了。判断方法：注册时浏览器地址栏里是哪个域名，就填哪个。

### 2. drop-in 客户端找不到我们的配置

Langfuse 提供了一个 `langfuse.openai.AsyncOpenAI`，号称"把 import 换一下就能自动追踪所有 LLM 调用"。探测发现：

```python
lf = Langfuse(public_key=..., secret_key=..., base_url=...)   # 用 .env 里的 key 显式创建
get_client() is lf    # → False
```

drop-in 客户端内部用 `get_client()` 找 Langfuse 实例，而 `get_client()` 是从 **`LANGFUSE_*` 环境变量**里读配置的。我们的配置来自 `.env` + pydantic-settings，并不在 `os.environ` 里（[02 章](02-project-setup.md#配置从-env-到-settings)），所以它找到的是另一个没有 key 的实例。

可以把 key 塞进 `os.environ` 让它找到，但这种"隐式全局配置"不好排查。装饰器方案显式地把客户端传进去，配置从哪来一目了然，而且记录的内容（首字时间、工具名列表）也完全可控。

### 3. Langfuse 挂了，服务不受影响

开发期间 Langfuse 日本区多次连不上（同时 DeepSeek 时通时断）。正好借此验证：

- **请求处理不受影响**。Langfuse 连不上的那段时间，`POST /chat` 正常完成了多轮对话。原因是 Langfuse SDK 把数据放进缓冲区，由**后台线程**批量发送，不在请求路径上
- **`flush()` 会阻塞**。用一个不可路由的地址实测，`flush()` 阻塞了 **5 秒**（导出超时）才返回。所以代码里只在进程退出时调用一次 `shutdown()`（它内部会 flush），而不是每轮对话后都 flush。最初的 CLI 写了"每轮结束就 flush"，Langfuse 一挂每轮就多卡 5 秒，已经改掉了

### 4. 查询接口的限制

想用 API 把刚写入的 trace 读回来验证，踩了三个坑：

- `GET /api/public/traces/{id}`：免费版每分钟只能调 **5 次**，超了返回 429。最初的验证脚本每 3 秒查一次，很快就被限流，而 SDK 把 429 包成了一个笼统的 `ApiError`，看不出原因
- `GET /api/public/traces`（列出 trace）：返回 **410 Gone**，这个接口已经下线
- 正确的做法是用 `GET /api/public/v2/observations`，带上 `traceId` 和时间范围；默认只返回基本字段，token 用量、首字时间要通过 `fields=core,basic,usage,model,metrics,io,metadata` 显式要

日常使用直接看网页就行，这些只在写脚本自动验证时才会遇到。

### 5. 没有成本数据

generation 的 cost 是空的：Langfuse 内置了常见模型的价格，但没有 `deepseek-chat`。需要在 Langfuse 里为它自定义模型价格（模型名匹配规则 + 输入/输出单价），之后的 trace 才会显示花了多少钱。（未实测，具体入口以 Langfuse 网页为准）

## 测试

[test_tracing.py](../backend/tests/test_tracing.py) 用一个**记录调用的假 Langfuse 客户端**测试装饰器：

```python
class FakeLangfuse:
    @contextmanager
    def start_as_current_observation(self, **kwargs):
        span = FakeSpan(kwargs)
        self.spans.append(span)
        yield span
```

它只实现了 `tracing.py` 用到的那一个方法。跑一轮完整的 loop 后检查：创建了 1 个 agent、2 个 generation、1 个 tool；generation 带着模型名、工具列表、换算后的 token 用量、首字时间；工具失败时标记了 ERROR。不连网络，也不往线上写数据。

## 设计取舍

**SaaS 还是自托管？**

Langfuse 是开源的，可以用 Docker 自己部署。选 Langfuse Cloud 免费版，是因为 5 天工期里不值得多维护一个服务（它自己还要 Postgres、ClickHouse、Redis）。代价是数据存在第三方、受免费额度限制，以及本章遇到的网络不稳定。

**为什么记录完整的 input？**

generation 的 `input` 是发给 LLM 的完整消息列表，包含 system prompt 和所有历史。数据量不小，但调试时最需要看的恰恰就是"LLM 当时到底看到了什么"。如果将来有隐私顾虑（用户说了敏感信息），Langfuse 提供 `mask` 参数在上报前脱敏。

## 动手验证

```powershell
# 1. .env 里配好 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST（注意区域）
# 2. 问一个问题（会消耗 LLM token）；CLI 退出时会把 trace 发出去
python -m backend.cli "莉可丽丝的圣地在哪？"
# 3. 打开 LANGFUSE_HOST 对应的网页 → Tracing，点开最新的 chat-turn

# 装饰器的单元测试（不连网络）
python -m pytest -v backend/tests/test_tracing.py
```

## 延伸思考

1. 第二轮 LLM 调用只发了工具调用、没有文字，所以没有首字时间。要不要把"第一片工具调用到达的时间"也算作首字时间？用户在这期间看到的是什么？
2. 每轮的输入 token 一路上涨，主要是工具结果撑大的。从 trace 里看，哪个工具的结果最"贵"？有什么办法让它变小（提示：[03 章原则 3](03-mcp-server.md#3-输出要省-token)）？
3. 现在每一轮对话都会被记录。如果用户量很大，全量记录的成本和数据量都会成问题。Langfuse 的 `sample_rate` 参数可以只记录一部分，但"只记录 10%"对排查某个用户反馈的具体问题有什么影响？
4. [16 章](README.md)的评测会用到 Langfuse 的 Datasets 功能。trace 里已经有了 input 和 output，评测还缺什么？
