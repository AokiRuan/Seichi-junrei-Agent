# 踩坑日志

开发过程中真实遇到的问题。每条按「现象 → 根因 → 解法 → 教训」记录。新坑追加在对应分类末尾。

**目录**：[Python 导入与运行](#python-导入与运行) · [Windows 环境](#windows-环境) · [依赖与包管理](#依赖与包管理) · [MCP SDK 2.x](#mcp-sdk-2x) · [第三方 API](#第三方-api) · [LLM 行为与输出](#llm-行为与输出) · [Web 与 HTTP](#web-与-http) · [可观测性](#可观测性) · [前端](#前端)

---

## Python 导入与运行

### 测试目录叫 `test` 导致 ModuleNotFoundError

- **现象**：`python -m test.test_simple_agent` 报 `No module named test.test_simple_agent`，文件明明就在 `test/` 下
- **根因**：Python 标准库自带一个叫 `test` 的包（CPython 自己的测试套件，位于 `anaconda3\Lib\test\`）。项目的 `test/` 没有 `__init__.py`，是"命名空间包"；导入系统扫描 `sys.path` 时，命名空间包只是候选，**一旦在后面的路径找到同名的常规包（带 `__init__.py`），常规包胜出**。于是 `test` 被解析成了标准库那个
- **解法**：目录改名为 `tests`
- **教训**：别用标准库已有的名字给自己的包命名（`test`、`email`、`logging`、`queue`……）。不确定时 `python -c "import 名字; print(名字.__file__)"` 查一下

### 直接运行脚本，找不到同级目录的包

- **现象**：`python tests/test_simple_agent.py` 报 `No module named 'agent'`
- **根因**：直接运行脚本时，`sys.path[0]` 是**脚本所在目录**（`tests/`），不是项目根目录。`agent/` 在根目录下，自然找不到
- **解法**：在项目根目录用 `python -m tests.test_simple_agent`。`-m` 模式下 `sys.path[0]` 是当前工作目录
- **教训**：项目内一律 `python -m 模块路径`，一律在根目录执行。详见 [02 章](02-project-setup.md#包与导入为什么必须在根目录运行)

---

## Windows 环境

### 打印 emoji 报 UnicodeEncodeError

- **现象**：`print(f"✅ ...")` 报 `'gbk' codec can't encode character '✅'`
- **根因**：中文 Windows 终端默认代码页是 GBK，GBK 字符集里没有 emoji。Python 按终端编码输出，遇到编不了的字符就崩
- **解法**：`$env:PYTHONIOENCODING = "utf-8"` 强制 Python 用 UTF-8 输出。想永久生效可以设成用户级环境变量
- **教训**：Docker/Linux 下不会有这个问题，只在 Windows 本地开发时出现。**不要为了绕过它去删代码里的 emoji**——那是在修症状

### PowerShell 里 `python -c` 传多行代码被破坏

- **现象**：用 here-string 给 `python -c` 传多行代码，报 `SyntaxError: '(' was never closed`，f-string 的引号被吃掉了
- **根因**：PowerShell 5.1 调用原生程序时，对参数里的双引号的转义规则和 Python 期望的不一致
- **解法**：多于一行的代码，写成临时 `.py` 文件再执行
- **教训**：`python -c` 只用来跑单行、不含双引号嵌套的代码

### PowerShell 里的 `curl` 不是 curl

- **现象**：照着网上教程写 `curl -N -X POST ...`，报参数错误或返回一个对象而不是原始文本
- **根因**：PowerShell 5.1 里 `curl` 是 `Invoke-WebRequest` 的别名，参数完全不同
- **解法**：写 `curl.exe`（Windows 10 起自带）。JSON 请求体写进文件用 `--data-binary "@req.json"`，避免命令行里的引号被 PowerShell 吃掉；显示中文前执行 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`
- **详见**：[08 章 · 动手验证](08-fastapi-sse.md#动手验证)

### `Out-File -Encoding ascii` 把中文变成问号，LLM 收到的是 `??`

- **现象**：发"继续"给 agent，回答却是"抱歉，是我上一条回复太啰嗦了吗？"
- **根因**：请求体用 `Out-File -Encoding ascii` 写的文件，ASCII 编码不了中文，"继续"被替换成了 `??`。LLM 实际收到的是两个问号，于是理解成用户表示疑惑
- **解法**：用 `-Encoding utf8`（PowerShell 5.1 会写入 BOM，实测 FastAPI 能正常解析）
- **教训**：**LLM 的回答莫名其妙时，先确认它收到的输入是不是你以为的那样**。这也是 trace 记录完整 input 的价值（[09 章](09-langfuse.md)）

---

### uvicorn 报 `[WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试`

- **现象**：一直用得好好的 8000 端口，某天启动 uvicorn 突然报 10013；`Get-NetTCPConnection -LocalPort 8000` 查不到任何占用
- **根因**：端口没被别的进程占，而是被 **Windows 保留**了。`netsh interface ipv4 show excludedportrange protocol=tcp` 显示 7936–8035 这一段被排除——Hyper-V / WSL / Docker 用的 WinNAT 会在开机时动态保留几段端口，每次开机可能不一样
- **解法**：临时换个不在保留范围里的端口（`uvicorn ... --port 8800`）。根治需要管理员 PowerShell：`net stop winnat; net start winnat` 释放动态保留，或者 `netsh int ipv4 add excludedportrange protocol=tcp startport=8000 numberofports=1` 把 8000 固定保留给自己用（未实测，需要管理员权限）
- **教训**：「端口被占用」（10048）和「没有权限绑定」（10013）是两回事。后者先查系统保留端口

## 依赖与包管理

### venv 里没有 pip

- **现象**：`python -m pip install ...` 报 `No module named pip`
- **根因**：`.venv` 是 `uv` 创建的（`.venv/pyvenv.cfg` 里有 `uv = ...`），uv 创建的 venv 默认不装 pip
- **解法**：用 `uv pip install ...`
- **教训**：遇到陌生环境先看 `pyvenv.cfg`，弄清楚是谁建的

### 换电脑/重装系统后 venv 失效，IDE 报「无法解析导入」

- **现象**：VS Code 提示 `无法解析导入"openai"`；运行 `.venv\Scripts\python.exe` 报 `No Python at 'C:\Users\YUZI\anaconda3\python.exe'`
- **根因**：venv 不是独立的 Python，它只是指向一个「基础解释器」（见 `.venv/pyvenv.cfg` 里的 `home =`）。原来的基础解释器在 anaconda 里，系统重装后 anaconda 不在了，venv 随之失效
- **解法**：venv 是可重新生成的，直接重建：`uv venv --python 3.12 --clear .venv`（uv 会自动下载 Python 3.12），再 `uv pip install -r requirements.txt`。VS Code 里在 `.vscode/settings.json` 设置 `python.defaultInterpreterPath` 指向 `.venv`
- **同一次重装还丢了 `.env.example`**：它从来没有被提交到 git，只存在于旧机器的磁盘上。后来发现时已经重新创建
- **教训**：venv 不能拷贝、不能跨机器复用，也不进 git，只有 `requirements.txt` 是可靠的环境描述。**模板类文件（`.env.example`）一创建就要提交**，否则它和 `.env` 一样只活在本地。重建时依赖版本可能变化（这次 openai 从 3.6 升到了 3.30，测试仍全部通过）——这正是 [02 章](02-project-setup.md#设计取舍)提到的不钉版本的代价，Day 4 写 Dockerfile 时考虑生成锁文件

### pypi.org 连接超时

- **现象**：`uv pip install` 重试 3 次、132 秒后报 `operation timed out`
- **根因**：国内网络直连 pypi.org 不稳定
- **解法**：`--index-url https://pypi.tuna.tsinghua.edu.cn/simple`，或设用户级环境变量 `UV_DEFAULT_INDEX`
- **教训**：写 Dockerfile 时同样要考虑——CI 跑在 GitHub（海外）不需要镜像，本地构建镜像时可能需要。别把镜像地址硬编码进 Dockerfile

---

## MCP SDK 2.x

**总教训：MCP SDK 1.x → 2.x 有大量破坏性改名，而网上绝大多数教程是 1.x 写法。写 MCP 代码时不要信搜索结果，直接看已安装版本的实际 API：**

```python
import inspect
from mcp.server.mcpserver import MCPServer
print(inspect.signature(MCPServer.tool))
```

### `FastMCP` 不存在

- **现象**：`from mcp.server.fastmcp import FastMCP` 报 `No module named 'mcp.server.fastmcp'`
- **根因**：2.x 把 `FastMCP` 改名为 `MCPServer`，导入路径也变了（错误信息里直接给了迁移指南链接）
- **解法**：`from mcp.server.mcpserver import MCPServer`。`@mcp.tool()` 装饰器和 `run()` 用法不变
- **预防**：`requirements.txt` 里钉住 `mcp>=2.2`

### `tool.inputSchema` 不存在

- **现象**：`'Tool' object has no attribute 'inputSchema'. Did you mean: 'input_schema'?`
- **根因**：2.x 的 Python 对象字段统一改成 snake_case：`inputSchema` → `input_schema`、`isError` → `is_error`、`structuredContent` → `structured_content`。**注意：JSON-RPC 线上传输的字段名仍是 camelCase**，只有 Python 侧改了
- **解法**：用 snake_case

### `list_tools()` 返回 coroutine

- **现象**：`TypeError: 'coroutine' object is not iterable`
- **根因**：2.x 里 `MCPServer.list_tools()` 是 async 方法
- **解法**：`await server.mcp.list_tools()`

### 返回 list 的工具，结构化结果被包了一层

- **现象**：工具返回 `[{...}]`，client 拿到的 `structured_content` 是 `{"result": [{...}]}`
- **根因**：MCP 规范要求 `structuredContent` 必须是 JSON object，所以 SDK 把非 object 的返回值包进 `result` 键
- **解法**：client 端解析时判断并解包
- **状态**：Day 2 写 mcp_client 时要处理

### 工具里抛异常，LLM 看不到原因

- **现象**：工具里 `raise ValueError("bad x=1")`，client 只收到 `Error executing tool boom`
- **根因**：SDK 的安全设计——未预期的异常不把内部信息泄露给 client。同时，**跨进程调用时异常不会让 client 抛错**，而是返回 `is_error=True` 的正常结果
- **解法**：可预期的错误主动 `return {"error": "具体原因"}`；client 端检查 `is_error` 而不是 `try/except`
- **详细对比**：[03 章 · 原则 5](03-mcp-server.md#5-错误是数据不是异常)

### 测试 teardown 报 "exit cancel scope in a different task"

- **现象**：用 pytest 异步 fixture 管理 MCP 连接（`yield` 前 connect、后 close），测试本身全部通过，但每个测试在 teardown 阶段报 `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`
- **根因**：MCP SDK 的 `stdio_client` 基于 anyio，要求连接在同一个 asyncio task 里打开和关闭；pytest-asyncio 在不同的 task 里执行 fixture 的 setup 和 teardown
- **解法**：`MCPClientPool` 实现 `__aenter__` / `__aexit__`，每个测试里用 `async with`
- **教训**：FastAPI 的 lifespan（一个函数里 yield 前后）天然满足这个约束（实测旧式 startup/shutdown 事件也在同一个 task 里）；真正会踩坑的是「在请求处理函数里懒加载打开」——每个请求是独立的 task。详见 [07 章](07-mcp-client.md#生命周期必须在同一个-task-里打开和关闭)

### 子进程拿不到终端里设置的环境变量

- **现象**：（预防性发现，未实际出错）在终端设置的环境变量，MCP server 子进程里读不到
- **根因**：实测 SDK 启动子进程时用 `get_default_environment() | server.env`，默认只有 12 个系统变量（PATH、APPDATA、TEMP 等），出于安全考虑不继承其他变量
- **解法**：需要的变量显式传入 `StdioServerParameters(env=...)`。项目里透传了代理相关变量，否则在代理环境下天气/路线 server 会连不上外网，且只报一个笼统的超时
- **教训**：遇到「终端里能用、子进程里不行」，先查环境变量

---

## 第三方 API

### Anitabi 文档说有坐标，实际没有

- **现象**：文档示例里每个地标都有 `geo: [lat, lng]`，实测两部作品 0/84、0/63
- **根因**：线上 API 已变更，文档未同步
- **解法**：当前用 mock 数据；将来需从 `originURL` 提取（覆盖 42%）+ 地理编码补齐。**2026-10-21 更新**：参考开源客户端 boiboif/anitabi-app 发现，网站地图页自用的 `www.anitabi.cn/d/g.json` + `g{n}.json` 两份文件拼起来有**全部**地标坐标（实测 1552 部作品、52244 个地标、100% 有坐标）。细节见 [项目计划 4.1](00-project-plan.md#新发现网站内部数据文件有完整坐标2026-10-21)
- **教训**：**第三方 API 先写探测脚本、统计字段出现率，再写业务代码**。官方文档 / API 走不通时，看看**官方自己的前端**是怎么拿数据的——它总得把坐标送到浏览器里画地图，开源的第三方客户端往往已经把这条路摸清了。方法见 [03 章 · 先探测再写代码](03-mcp-server.md#案例先探测再写代码)

### Cloudflare 限流

- **现象**：约 25 个请求后全部返回 403 + HTML 挑战页，45 秒后仍未恢复
- **根因**：Anitabi 前置了 Cloudflare 防护
- **解法**：探测时控制频率；正式使用必须加本地缓存
- **教训**：演示当天依赖一个会限流的外部 API 是赌博。关键路径上的外部依赖要有离线兜底

### OSRM 官方 demo 的步行结果其实是驾车

- **现象**：`router.project-osrm.org/route/v1/foot/...` 和 `/driving/...` 返回完全相同的结果（京都音乐厅 → 宇治桥：21.6km、32 分钟）。不报错，看起来一切正常
- **根因**：官方 demo 只部署了驾车 profile，URL 里的 profile 段被忽略
- **解法**：改用 FOSSGIS 实例，它按 profile 分开部署：`routing.openstreetmap.de/routed-foot/...`（同一段路步行 20.5km、4.5 小时，符合步行速度）
- **教训**：**不报错的错误结果最危险**。探测时要检查结果是否符合常识（21km 步行 32 分钟显然不对），而不只是看状态码是不是 200

### 地理编码匹配到同名的其他地点

- **现象**：用 Nominatim 核对 mock 数据时，「須賀神社 男坂」没查到，退回只查「須賀神社」，结果匹配到仙台的一座同名神社，和东京的原坐标相差 302km
- **根因**：日本同名的神社、车站、公园非常多，只给名称时 Nominatim 按知名度排序
- **解法**：查询时加上城市名（实测「須賀神社 新宿区」就能找到正确的那座，离原坐标 115m）；从多个候选里选离预期位置最近的；偏差过大的结果人工确认，不自动采纳
- **教训**：这就是 `geocode` 工具返回多个候选、并在描述里要求 LLM「根据上下文选择」的原因。批量处理地理编码结果时，不能不看就直接替换

---

## LLM 行为与输出

### 流式输出每个字重复两遍

- **现象**：「没问题没问题！！我来我来帮你帮你」
- **根因**：`hello_agents` 的 `HelloAgentsLLM.think()` 是个生成器，它**在 yield 之前自己已经 print 了一遍**；调用方 `for chunk in llm.think(...)` 里又 print 了一遍
- **解法**：调用方只消费、不再打印
- **教训**：生成器最好只负责产出数据，不要在里面做 print 这类副作用。这正是本项目 `AgentLoop` 只 yield `AgentEvent`、由上层决定怎么展示的原因（[01 章 · 原则二](01-architecture.md#原则二用事件流对外输出而不是直接写响应)）

### 中文提问，LLM 却用英文说「我来查一下」

- **现象**：DeepSeek 返回 tool_calls 时附带的说明文字是英文（`I'll look up that anime first.`），最终回答却是中文
- **根因**：模型在「调工具前的过渡语」上有英文的倾向；system prompt 里抽象的「使用用户的语言」没能覆盖到这一句
- **解法**：把规则写具体：「调用工具前的说明文字也一样：用户用中文提问，就用中文说『我来查一下』，不要用英文」
- **教训**：写 prompt 时，**具体的例子比抽象的规则管用**

### LLM 在回答里抄错了坐标

- **现象**：LLM 把京都音乐厅的纬度 35.0504 写成了 34.0504
- **根因**：LLM 生成文本时抄写长数字不可靠
- **解法**：不让它抄——前端地图用 `tool_result` 事件里的结构化数据打点，这条通道不经过 LLM；system prompt 里要求不要在回答里写经纬度
- **教训**：**精确数据（坐标、价格、ID）走结构化通道，LLM 只负责自然语言**。详见 [05 章实测发现](05-agent-loop.md#实测发现)

### 流式 tool_calls 的参数被切得很碎

- **现象**：（探测发现，未出错）流式模式下工具参数 `{"lat": 34.99, "lng": ...}` 被切成 `{`、`"`、`lat`、`"`、`: `、`34`、`.`、`99`……，`"lng"` 甚至被切成 `"l"` + `"ng"`
- **根因**：LLM 按 token 输出，参数 JSON 也是一个 token 一个 token 生成的
- **解法**：按 `index` 累积拼接，**整轮流结束后才解析 JSON**；`id` 和 `name` 只在每个调用的第一片出现
- **教训**：流式 tool_calls 在流结束前永远不是合法 JSON，别试图提前解析。详见 [05 章 · 流式](05-agent-loop.md#流式把分片拼回完整回复)

### 流式模式下没有 token 用量

- **现象**：（预防性发现）流式请求默认不返回 `usage`
- **解法**：请求时加 `stream_options={"include_usage": True}`。另外 usage 的位置因服务商而异：DeepSeek 放在最后一片（和 `finish_reason` 一起），OpenAI 官方额外发一片 `choices` 为空的。两种都要处理

---

### LLM 把「总时长」说成「含往返电车」

- **现象**：巡礼规划工具返回 `total_min: 225`（含去程电车），LLM 回答「总时长约 225 分钟，含往返电车，14:00 前后就能回到新宿」；还把 OSRM 按路网算出的步行时间说成「路线服务估算值」
- **根因**：工具输出没写清楚边界。LLM 看到一个总时长，就按常识补全了「往返」；看到 `assumptions` 里有估算字样，就把所有时间都当成估算
- **解法**：把事实写进数据，而不是在 prompt 里叮嘱：`assumptions` 加「总时长含去程交通，不含返程」，新增 `walk_source: "OSRM 按实际路网计算"`。第二次实测 LLM 主动说「不含返程，回京都站要另留约 30 分钟」
- **教训**：LLM 说错工具结果时，先看工具输出是不是有歧义。**边界、单位、数据来源都应该是数据的一部分**

## Web 与 HTTP

### SSE 里的中文被转义成 `上低`

- **现象**：curl 看到的是 `"title": "上低音号..."`
- **根因**：FastAPI 的 `ServerSentEvent(data=...)` 序列化 JSON 时默认转义非 ASCII 字符
- **解法**：用 `raw_data=json.dumps(data, ensure_ascii=False)` 自己序列化。不加 `indent` 的 `json.dumps` 输出一定是单行，不会破坏 SSE 的行格式
- **教训**：转义后前端照样能解析，但调试时没法读，体积也翻倍

### 流式接口里没法返回 404

- **现象**：（设计时发现）在 SSE 路由函数（生成器）里检查会话是否存在，不存在时 `raise HTTPException(404)`，客户端收到的却是 200
- **根因**：FastAPI 先发出响应头（200）再开始执行生成器，发现问题时状态码已经发出去了
- **解法**：把校验放进依赖（`Depends`），依赖在响应开始前执行
- **教训**：**流式接口的所有校验都要在第一个字节发出之前完成**。详见 [08 章](08-fastapi-sse.md#为什么-404-必须在依赖里判断)

### 发给客户端的错误，服务端没有记录

- **现象**：第二轮对话收到 `error` 事件，去看 uvicorn 日志，什么都没有
- **根因**：loop 把 LLM 失败转成了 `error` 事件发给客户端，这是对的，但没有任何地方记日志
- **解法**：ChatService 收到 error 事件时 `logger.warning`
- **教训**：把错误"优雅地"交给客户端时，别忘了服务端也要留一份记录，否则线上出问题无从排查

---

## 可观测性

### Langfuse 报 401，key 明明是对的

- **现象**：`Invalid credentials. Confirm that you've configured the correct host.`
- **根因**：Langfuse Cloud 分区域部署（EU `cloud.langfuse.com` / US `us.cloud.langfuse.com` / JP `jp.cloud.langfuse.com`），key 只在创建项目的区域有效。`.env` 用的是默认的 EU 区域，项目建在 JP 区域
- **解法**：`LANGFUSE_HOST` 改成项目所在区域。不确定时用同一对 key 依次请求各区域的 `/api/public/projects`，返回 200 的就是
- **教训**：报错信息里"configured the correct host"那半句才是关键

### `langfuse.openai` drop-in 客户端不生效

- **现象**：（探测发现）显式创建 `Langfuse(public_key=..., ...)` 后，`get_client() is lf` 为 False
- **根因**：drop-in 客户端通过 `get_client()` 找 Langfuse 实例，而它读的是 `LANGFUSE_*` 环境变量。我们的配置来自 `.env` + pydantic-settings，不在 `os.environ` 里
- **解法**：不用 drop-in，改用装饰器（`TracedLLM` / `TracedTools`）显式传入客户端
- **详见**：[09 章 · 实测发现](09-langfuse.md#2-drop-in-客户端找不到我们的配置)

### Langfuse 不可达时 flush() 阻塞 5 秒

- **现象**：（实测）对不可达的地址调用 `flush()`，阻塞约 5 秒才返回
- **根因**：flush 要等数据发送完成或超时
- **解法**：不在每次请求后 flush，只在进程退出时 `shutdown()` 一次。平时由后台线程批量发送，不阻塞请求

### 查 trace 的接口：429、410、ApiError

- **现象**：验证脚本轮询 `api.trace.get()`，一直报 `ApiError`
- **根因**：单 trace 查询接口免费版限 5 次/分钟，超了返回 429，SDK 把它包成了笼统的 `ApiError`；列出 trace 的 `GET /api/public/traces` 已经下线（410 Gone）
- **解法**：用 `GET /api/public/v2/observations?traceId=...&fromStartTime=...&toStartTime=...`，需要 token 用量等字段时加 `fields=core,basic,usage,model,metrics,io,metadata`
- **教训**：SDK 抛出笼统的错误时，直接用 HTTP 请求一次看响应体，原因往往写得很清楚

## 前端

### Leaflet 圆点的 className 不生效，显示成默认的蓝色空心圈

- **现象**：`<CircleMarker pathOptions={{ className: 'spot-marker' }}>` 配合 CSS `.spot-marker { fill: ... }`，截图里圆点仍是 Leaflet 默认的蓝色空心圈，CSS 完全没起作用
- **根因**：读 react-leaflet 源码（`@react-leaflet/core/lib/path.js`）：`pathOptions` 是在元素创建**之后**通过 `instance.setStyle(options)` 应用的。而 Leaflet 只在创建 SVG 元素时（`_initPath`）读 `className`，`setStyle` 不会再加类名
- **解法**：颜色直接写进 `pathOptions`（`color` / `fillColor` / `fillOpacity` / `weight`），并放在模块级常量里——react-leaflet 按**引用**比较 `pathOptions`，每次渲染传新的对象字面量会导致每次都调 `setStyle`
- **教训**：包装库（react-leaflet）的 prop 不一定原样传给底层库（Leaflet）的构造参数。样式不生效时去看包装层怎么应用这个 prop

### Leaflet 默认 Marker 图标经过打包后显示成破图

- **现象**：这是 Leaflet + 打包工具的知名问题，本项目**预先规避、未实际触发**：默认 `Marker` 的图标图片路径是 Leaflet 的 CSS 里写的相对路径，经过 Vite / webpack 打包后路径对不上
- **解法**：不用默认图标。圣地用 `CircleMarker`（SVG 矢量圆，不需要图片），路线站点用 `L.divIcon`（一段 HTML，可以用 CSS 写样式、显示编号）
- **教训**：用到"库自带的静态资源"时，先想想打包后它还找不找得到

### `curl http://127.0.0.1:5173` 没有响应，`localhost:5173` 却可以

- **现象**：Vite 开发服务器启动后，`curl http://127.0.0.1:5173/` 返回空，`curl http://localhost:5173/` 正常
- **根因**：Vite 默认只监听 `localhost`，在这台 Windows 上解析成了 IPv6 的 `::1`，没有监听 IPv4 的 `127.0.0.1`。uvicorn 则相反，默认监听 `127.0.0.1`
- **解法**：开发时用 `http://localhost:5173` 访问。Vite 代理的目标写 `http://127.0.0.1:8000`（明确走 IPv4，对上 uvicorn）
- **教训**：`localhost` 和 `127.0.0.1` 不是一回事——前者是名字，可能解析成 IPv6

### Markdown 表格显示成一堆竖线

- **现象**：LLM 回答里的表格在页面上显示成 `| 圣地 | 出现集数 | ... |---|---|` 这样的原文
- **根因**：表格不属于标准 Markdown（CommonMark），是 GitHub 的扩展语法（GFM）。`react-markdown` 默认只支持 CommonMark
- **解法**：`npm install remark-gfm`，`<Markdown remarkPlugins={[remarkGfm]}>`
- **教训**：LLM 输出的"Markdown"通常是 GFM（表格、删除线、任务列表），渲染器要按 GFM 配

### TypeScript 6：`TextDecoderStream` 不能接在 `ReadableStream<Uint8Array>` 后面

- **现象**：`npm run build` 报 `Argument of type 'TextDecoderStream' is not assignable to parameter of type 'ReadableWritablePair<string, Uint8Array<ArrayBufferLike>>'`，而 vitest 照常通过
- **根因**：新版 TypeScript 的 DOM 类型里 `Uint8Array` 带了泛型参数（`Uint8Array<ArrayBuffer>` vs `Uint8Array<ArrayBufferLike>`，后者包含 `SharedArrayBuffer`）。`fetch` 的 `res.body` 是 `ReadableStream<Uint8Array<ArrayBuffer>>`，写成不带泛型的 `ReadableStream<Uint8Array>` 就变宽了，接不上 `TextDecoderStream`。vitest 只转译、不做类型检查，所以测试能过
- **解法**：参数类型写 `ReadableStream<Uint8Array<ArrayBuffer>>`
- **教训**：vitest / Vite 开发模式都不做类型检查（只删掉类型），类型错误要靠 `tsc`（`npm run build` 里的 `tsc -b`）发现

### 装好 Node 后，VS Code 终端里仍然找不到 npm

- **现象**：Node 已加入用户 PATH，在 VS Code 里新开终端执行 `npm run build`，报「无法将"npm"项识别为 cmdlet…」
- **根因**：进程的环境变量是在**启动时**从父进程复制来的。VS Code 的集成终端继承的是 VS Code 进程的环境，而 VS Code 是在修改 PATH 之前启动的——新开终端标签页拿到的仍是旧 PATH
- **解法**：彻底退出 VS Code（所有窗口）再打开。临时办法是在当前终端里补上：`$env:Path = "$env:LOCALAPPDATA\Programs\node-v24.22.1-win-x64;$env:Path"`
- **教训**：改了系统 / 用户环境变量后，"重开终端"要重开到最顶层的那个程序

### 无头浏览器截图：窗口设 400px 宽，页面却按 492px 排版

- **现象**：用 `msedge --headless=new --window-size=400,860 --screenshot` 检查窄屏布局，截图右侧文字被截断，看起来像页面横向溢出
- **根因**：浏览器窗口有最小宽度。用 `--dump-dom` 打印 `innerWidth` 实测是 **492**，页面按 492px 排版，截图却只截了 400px
- **解法**：窄屏截图用 600px 宽（仍低于 900px 的窄屏断点），或先打印 `innerWidth` 确认
- **教训**：验证工具本身也可能有偏差。看到异常先确认测量是否可信，再改代码

### 测了半天，跑的其实是旧代码（端口被旧进程占着）

- **现象**：改完圣地数据层后重启 uvicorn 做浏览器测试，结果显示「6 个圣地」——这是 mock 数据的数量，快照明明已经生成了
- **根因**：之前测试时启动的 uvicorn 一直没关，还占着 8000 端口。新进程启动到一半报 `[Errno 10048] error while attempting to bind on address`，然后自己退出了；浏览器连上的是**旧进程**，跑的是改动前的代码。命令是放在后台启动的，报错只写进了日志，终端里看不到
- **解法**：启动前先确认端口空闲（PowerShell：`Get-NetTCPConnection -LocalPort 8000 -State Listen`），启动后看一眼日志里有没有 `ERROR`
- **教训**：「改了代码但行为没变」时，第一个怀疑对象是**跑的到底是不是新代码**。后台启动的服务尤其要看日志确认它真的起来了

### 评测规则误判：回答写「不含返程」，被判成出现了「含返程」

- **现象**：第一次试跑评测，`plan-bocchi-shinjuku` 的 `answer.not_contains` 失败，详情是「出现了 ['含返程']」。打开回答一看，写的是「总时长包含去程交通，**不含返程**」——完全正确
- **根因**：`not_contains` 按子串判断，「不含返程」里就包含「含返程」。禁用词恰好是一个可以被否定的说法
- **解法**：判断时往前看两个字，前面有「不 / 没 / 未 / 非 / 无」的不算（`checks._affirmed`），「不包含返程」也能识别。改完用 `python -m evals.run --rescore` 对那次结果重新打分，没有重跑
- **教训**：评测规则自己也会错，而且错的方向不一定是「太松」——太严同样会误导（让人去修一个不存在的问题）。每条失败都要打开原文确认一次，确认是 agent 错了再去改 agent。这也是生成与打分要分开的原因：规则错了只需重新打分

### 评测对比被网络超时搅乱：改进生效了，总分却下降

- **现象**：加了工具熔断后重跑评测，故障类用例从 33% 升到 100%，但总的 pass^1 从 90% **降**到 84%、交付率从 100% 降到 92%
- **根因**：两个和改动无关的因素混在了一起。① 4 次失败是 DeepSeek 接口 `APITimeoutError`，属于网络 / 服务商的问题；② 另外几条变化出现在熔断根本不会触发的用例上（这些用例里没有工具出错），是 LLM 本身的随机性——每条只跑 2 次，一次的波动就是 50 个百分点
- **解法**：LLM 接口出错的用例整条重跑（`--infra-retries`，默认 2 次），重跑次数单独报告，不计入分数；评估某个改动时，对受影响的类别多跑几次（`--category fault --repeat 5`），并且开关各跑一组做对照
- **教训**：看对比结果之前先问两个问题：这个变化是改动造成的吗（改动能影响到这条用例吗）？样本量够不够区分信号和噪声？只看总分很容易得出相反的结论
