# Aoki-Agent

日本动画圣地巡礼 agent：FastAPI 后端 + 自建 agent loop/harness + MCP 工具 + React 前端。计划见 [docs/00-project-plan.md](docs/00-project-plan.md)。

**这是一个学习项目。用户的目标是理解自建 agent 项目的全流程，文档与代码同等重要。**

## 代码与文档同步（必须遵守）

学习手册在 [docs/](docs/)，索引与章节模板见 [docs/README.md](docs/README.md)。

- **改代码的同一轮里更新对应章节**。不要攒到最后补，也不要等用户提醒
- **新模块 → 新章节**：按 docs/README.md 的六段模板写（本章目标 / 核心概念 / 代码走读 / 设计取舍 / 动手验证 / 延伸思考），并更新索引里的状态
- **踩到坑就记进 [docs/pitfalls.md](docs/pitfalls.md)**：报错、库版本差异、第三方 API 与文档不符等，按「现象 → 根因 → 解法 → 教训」
- **文档里的命令必须实测过**。没法实测的（比如依赖未安装的工具）明确标注「未实测」
- **讲为什么，不只讲是什么**：每个设计决策都说明备选方案和不选它的理由
- **文档描述当前真实状态**，用 ✅ 已实现 / 🚧 骨架待填 / ⏳ 未开始 标注
- **代码模块的 docstring 顶部指向对应章节**，如 `讲解见 docs/03-mcp-server.md`
- 计划或决策变化时同步更新 docs/00-project-plan.md
- 文档与代码注释用中文

## 运行约定

- 所有命令在项目根目录执行，用 `python -m 模块路径`，不要按文件路径运行脚本（项目用绝对导入 `backend.xxx`）
- venv 由 uv 创建，**没有 pip**：装包用 `uv pip install`；国内网络加 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple`
- Windows 终端需 `$env:PYTHONIOENCODING = "utf-8"`，否则打印 emoji 崩溃
- 测试：`python -m pytest`（配置见 pytest.ini，`asyncio_mode = auto`；打真实外部 API 的测试标记为 `network`，默认不跑）
- 评测（讲解见 docs/16-evaluation.md）：`python -m evals.run`（会消耗 LLM token，需要 Anitabi 快照；`--only` / `--repeat` / `--label`）；对比 `python -m evals.report A.json B.json`；改检查规则后用 `--rescore` 重新打分，不要为此重跑。改 prompt、工具描述、harness 后跑一次和基线对比
- 命令行和 agent 对话：`python -m backend.cli "问题"`（会消耗 LLM token）；列出全部工具：`python -m backend.agent.mcp_client`
- 启动：`uvicorn backend.app.main:app --reload`；接口都在 `/api` 下（`/api/chat`、`/api/sessions`），`/health` 在根路径；手动测接口用 `curl.exe`（PowerShell 的 `curl` 是 `Invoke-WebRequest` 别名），JSON 请求体写文件用 `-Encoding utf8`（`ascii` 会把中文变成 `?`）
- 公网相关（讲解见 docs/12-going-public.md）：`ACCESS_CODE` 非空时 `/api/chat`、`/api/sessions` 需要先 `POST /api/access` 拿 cookie；会话按访客 cookie 隔离，curl 测接口要带 `-c cookies.txt -b cookies.txt`，否则每次都是新访客。`DATABASE_URL` 空 = 内存，`sqlite+aiosqlite:///aoki.db` = 本地文件，`postgresql://...` = Postgres；Postgres 存储测试：`docker run -d --name aoki-pg -e POSTGRES_USER=aoki -e POSTGRES_PASSWORD=aoki -e POSTGRES_DB=aoki -p 5432:5432 postgres:16-alpine` 后设 `TEST_DATABASE_URL=postgresql+asyncpg://aoki:aoki@localhost:5432/aoki`（Docker Desktop 需先启动）
- 测试替身在 `backend/tests/fakes.py`；`conftest.py` 清空了 Langfuse key，测试永不往线上写数据，并固定 `SEICHI_DATA_SOURCE=mock`
- 圣地数据：`python -m backend.mcp_servers.seichi.anitabi` 生成 Anitabi 本地快照（不进 git，**不要频繁执行**：对方有 Cloudflare 限流）。`SEICHI_DATA_SOURCE` = auto（默认，有快照用快照）/ anitabi / mock，只能设成真正的环境变量（MCP 子进程经 `PASSTHROUGH_ENV` 拿到它，`.env` 里写了无效）
- 后台启动 uvicorn 前先确认 8000 端口空闲，启动后看日志有没有 bind 失败——否则测的是旧进程里的旧代码。报 `WinError 10013` 是端口被 Windows 动态保留了（`netsh interface ipv4 show excludedportrange protocol=tcp`），临时换 `--port 8800`
- 快照算法调整后用 `python -m backend.mcp_servers.seichi.anitabi --offline` 重建（读 `data/anitabi_raw/` 缓存，不发请求）
- Langfuse 项目在 **JP 区域**（`jp.cloud.langfuse.com`）；按 trace 读数据用 `GET /api/public/v2/observations`（旧的 traces 列表接口已 410，单 trace 接口限 5 次/分钟）
- 前端（`frontend/`，讲解见 docs/10-frontend.md）：Node 是免安装版，在 `%LOCALAPPDATA%\Programs\node-v24.22.1-win-x64`（已加入用户 PATH，新开的终端才生效；旧终端里先 `$env:Path = "$env:LOCALAPPDATA\Programs\node-v24.22.1-win-x64;$env:Path"`）。npm 已配国内镜像。在 `frontend/` 下：`npm test`（vitest）、`npm run lint`（oxlint）、`npm run build`（含 `tsc -b` 类型检查——vitest 不查类型）、`npm run dev`
- 开发时访问 `http://localhost:5173`（不是 127.0.0.1——Vite 监听的是 IPv6 的 ::1），Vite 把 `/api` 代理到 8000；`npm run build` 后 FastAPI 直接在 8000 提供页面
- 看页面效果可以用无头 Edge 截图（`msedge --headless=new --screenshot=... --virtual-time-budget=15000 URL`）；窗口最小宽度约 500px，窄屏截图用 600px 宽
- `MCPClientPool` 必须在同一个 asyncio task 里打开和关闭（用 `async with`），不要用 pytest 的 async yield fixture 管理它

## 架构约束

- **依赖方向**：`backend/agent/` 不得 import `backend/app/` 或 `backend/store/`。loop 对 HTTP、数据库、具体 LLM 一无所知，外部依赖通过构造参数注入
- loop 只 yield `AgentEvent`（backend/agent/events.py），不直接写响应、不 print
- 分层：路由只做 SSE 编码 → `ChatService`（会话、持久化，CLI 共用）→ loop。流式接口的校验放在依赖里（响应开始后无法再返回 4xx）
- 追踪用装饰器（`TracedLLM` / `TracedTools`）包住 LLM 与工具，不往 loop 里写 Langfuse 代码
- 会变的东西先立接口（`SessionStore`、`LLMClient`、`SeichiRepository`）
- 工具错误作为数据返回给 LLM，不抛异常中断对话
- 挑点、排序、算时间等组合优化与算术交给确定性代码（`seichi/planner.py` 纯函数，网络客户端由 `itinerary.py` 注入），LLM 只负责理解需求和写评论；工具输出要写明边界与数据来源（如「不含返程」），不指望 LLM 猜对
- 前端：逻辑写成纯函数（`chat/model.ts`、`map/extract.ts`、`api/sse.ts`）并配单元测试，组件只渲染；地图只用工具返回的结构化 `data`，不解析 LLM 的文字；地图等派生数据从对话 `items` 推导，不单独存状态

## MCP SDK 2.x

装的是 mcp 2.x，与网上多数 1.x 教程不兼容：`FastMCP` → `MCPServer`（`from mcp.server.mcpserver import MCPServer`），Python 侧字段 snake_case（`input_schema`、`is_error`、`structured_content`），`list_tools()` 是 async。**写 MCP 代码前用 `inspect.signature` 核实实际 API。** stdio server 里禁止 print 到 stdout。
