# 02 · 工程搭建

## 本章目标

读完能回答：

- 每个目录放什么，新代码该往哪写？
- 这个虚拟环境为什么没有 pip？依赖该怎么装？
- `.env` 里的 `LLM_API_KEY` 是怎么变成代码里的 `settings.llm_api_key` 的？
- 为什么所有命令都必须在项目根目录执行？

## 目录结构

```
Aoki-Agent/
├── backend/
│   ├── app/                  Web 层（见 08 章）
│   │   ├── main.py             create_app() 与 lifespan（启动/关闭时做什么）
│   │   ├── components.py       组装点：创建并连接所有组件，CLI 也用它
│   │   ├── chat_service.py     一轮对话：载入历史 → 运行 agent → 入库
│   │   ├── deps.py · schemas.py  FastAPI 依赖、请求/响应模型
│   │   ├── config.py           所有配置，从环境变量读
│   │   └── routes/             /api/chat（SSE）与 /api/sessions
│   ├── agent/                核心：自建 loop、harness、LLM 与 MCP 客户端（见 05~07 章）
│   ├── cli.py                命令行入口，不经 HTTP 直接和 agent 对话（见 05 章）
│   ├── store/                会话持久化：接口 + 内存 / SQL（SQLite、Postgres）两种实现
│   ├── mcp_servers/          三个自建 MCP server
│   │   ├── seichi/             圣地巡礼，读本地数据（见 03 章）
│   │   ├── weather/            天气，调 Open-Meteo（见 04 章）
│   │   └── route/              路线与地理编码，调 OSRM + Nominatim（见 04 章）
│   └── tests/                pytest 测试
├── frontend/                 React + Vite + TypeScript（见 10 章）
│   ├── src/                    api/（SSE 与接口）· chat/（对话模型）· map/ · components/
│   ├── package.json            前端依赖与 npm 脚本；package-lock.json 锁定精确版本
│   └── dist/                   npm run build 的产物，由 FastAPI 托管（被 .gitignore 排除）
├── evals/                    评测（Day 6+）
├── docs/                     本学习手册
├── learn/                    学习期代码，只读参考
├── requirements.txt          Python 依赖
├── pytest.ini                测试配置
├── .env.example              环境变量模板（提交到 git）
└── .env                      真实环境变量（被 .gitignore 排除，绝不提交）
```

**新代码往哪放**的判断方法：问自己"这段代码知不知道 HTTP 的存在"。知道 → `app/`；不知道、属于 agent 运行逻辑 → `agent/`；和存数据有关 → `store/`；是给 LLM 用的工具 → 做成 MCP server 放 `mcp_servers/`。

### 为什么旧代码放 learn/ 而不是删掉

学习期基于 `hello_agents` 写的代码，和现在的写法有很多值得对照的地方（见 01 章末尾的对照表）。放进 `learn/` 保留参考，但它**不参与构建、不参与部署、不参与测试**——`pytest.ini` 的 `testpaths` 只指向 `backend/tests`。

## Python 环境：uv

### 这个 venv 为什么没有 pip

`.venv` 是用 [uv](https://docs.astral.sh/uv/) 创建的（看 `.venv/pyvenv.cfg` 里有一行 `uv = 0.12.5`）。uv 是 Rust 写的 Python 包管理器，比 pip 快一个数量级，**它创建的 venv 默认不装 pip**。所以：

```powershell
python -m pip install xxx     # ❌ No module named pip
uv pip install xxx            # ✅
```

### 国内网络：换镜像

直连 pypi.org 会超时（实测 3 次重试 132 秒后失败）。换清华镜像后 5 秒装完。建议设成用户级环境变量，一劳永逸：

```powershell
[Environment]::SetEnvironmentVariable("UV_DEFAULT_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple", "User")
```

设完需要**重开终端**才生效。临时用一次可以加参数：`uv pip install -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple`

## 依赖逐组说明

[requirements.txt](../requirements.txt) 按用途分了组：

| 组 | 包 | 用途 |
|---|---|---|
| Web | `fastapi` `uvicorn[standard]` | Web 框架与 ASGI 服务器。`[standard]` 带上了 uvloop/httptools 等性能组件 |
| 配置 | `pydantic` `pydantic-settings` `python-dotenv` | 数据校验；从环境变量/.env 读配置 |
| LLM | `openai` | 所有候选 LLM（DeepSeek 等）都兼容 OpenAI 接口，一个 SDK 通吃 |
| MCP | `mcp>=2.2` | MCP 协议的服务端与客户端 |
| 持久化 | `sqlalchemy[asyncio]` `asyncpg` `aiosqlite` | 异步 ORM、Postgres 与 SQLite 的异步驱动（[12 章](12-going-public.md)） |
| 可观测 | `langfuse` | LLM 调用链追踪 |
| 开发 | `ruff` `pytest` `pytest-asyncio` `httpx` | lint、测试、异步测试、FastAPI 测试客户端的底层 |

### 为什么唯独 mcp 钉了版本

MCP SDK 从 1.x 到 2.x 有大量破坏性改名（`FastMCP` → `MCPServer`、`inputSchema` → `input_schema` 等，见 [踩坑日志](pitfalls.md)）。**网上绝大多数教程和示例还是 1.x 写法**。钉住 `>=2.2` 是为了防止将来某次重装意外降到 1.x，然后代码莫名其妙报错。

其他包没钉版本，是 demo 阶段的取舍——见下文「设计取舍」。

## 配置：从 .env 到 settings

[config.py](../backend/app/config.py) 用 `pydantic-settings`：

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    database_url: str = ""
    ...

settings = Settings()
```

**映射规则**：字段名 `llm_api_key` 自动对应环境变量 `LLM_API_KEY`（不区分大小写）。不需要写任何 `os.getenv`。

**优先级**（高 → 低）：

1. 真实环境变量（比如 App Runner 上配置的、或终端里 `$env:LLM_MODEL = "xxx"`）
2. `.env` 文件
3. 字段默认值

这个优先级正是部署需要的：本地用 `.env`，生产环境不放 `.env` 文件、直接配环境变量，代码一行不改。

**两个细节**：

- `extra="ignore"`：`.env` 里有 Settings 没声明的变量时静默忽略。不加的话，你 `.env` 里残留一个旧变量（比如学习期的 `DEEPSEEK_API_KEY`）程序就启动失败
- `env_file=".env"` 是**相对于当前工作目录**的路径——这是"必须在项目根目录执行命令"的原因之一

对比学习期 [learn/core/config.py](../learn/core/config.py) 的写法：手写 `os.getenv` + 手动类型转换（`float(os.getenv("TEMPERATURE", "0.7"))`）。pydantic-settings 把这些全自动化了，还附带类型校验——`MAX_ITERATIONS=abc` 会在启动时直接报错，而不是运行到一半才炸。

## 包与导入：为什么必须在根目录运行

项目用**绝对导入**：`from backend.app.main import app`。Python 找 `backend` 这个包的方式是在 `sys.path` 里搜，而 `sys.path` 里有没有项目根目录，取决于你**怎么启动**：

| 启动方式 | `sys.path[0]` 是 | 能找到 `backend` 吗 |
|---|---|---|
| `python -m pytest`（在根目录） | 当前目录 = 根目录 | ✅ |
| `python -m backend.mcp_servers.seichi.server` | 当前目录 = 根目录 | ✅ |
| `python backend/mcp_servers/seichi/server.py` | 脚本所在目录 | ❌ 且相对导入也会失败 |

**规则：永远在根目录，用 `python -m 模块路径` 的方式运行。** 学习期踩过的 `test` 包重名问题也是同一类原因，见 [踩坑日志](pitfalls.md)。

## 运行与测试

### 启动后端

```powershell
uvicorn backend.app.main:app --reload
```

- `backend.app.main:app` 的意思是：模块 `backend.app.main` 里的变量 `app`
- `--reload`：改代码自动重启，只在开发时用
- 圣地数据默认读 Anitabi 本地快照，第一次需要先生成：`python -m backend.mcp_servers.seichi.anitabi`（约 15 秒）。没有快照时自动用 mock 数据，见 [03 章](03-mcp-server.md#真实数据anitabi-快照)
- 启动后访问 http://127.0.0.1:8000/docs —— FastAPI 根据路由**自动生成的交互式 API 文档**，可以直接在页面上发请求测试
- 如果已经执行过 `npm run build`（`frontend/dist` 存在），http://127.0.0.1:8000 就是前端页面。开发前端时的两终端启动方式见 [10 章](10-frontend.md#动手验证)

### 跑测试

[pytest.ini](../pytest.ini)：

```ini
[pytest]
testpaths = backend/tests
asyncio_mode = auto
addopts = -m "not network"
markers =
    network: 访问真实外部 API 的集成测试（受网络与限流影响，默认不跑）
```

- `asyncio_mode = auto` 让 `async def test_xxx()` 直接能跑，不用每个都加 `@pytest.mark.asyncio`。项目里大量是异步代码，这个配置省很多事
- `addopts = -m "not network"` 让打真实外部 API 的测试默认不跑，原因见 [04 章](04-external-mcp-servers.md#5-真实网络的测试单独放)

```powershell
python -m pytest            # 全部单元测试（不连网络）
python -m pytest -v         # 显示每个测试名
python -m pytest -k seichi  # 只跑名字含 seichi 的
python -m pytest -m network # 只跑访问真实外部 API 的集成测试
```

当前的测试：

- [test_app.py](../backend/tests/test_app.py) —— 应用能起来、`/health` 正常
- 前端的单元测试用 vitest，在 `frontend/` 下 `npm test`（见 10 章）
- [test_seichi.py](../backend/tests/test_seichi.py) —— 圣地 MCP server，包括一个**真实 stdio 子进程往返**的测试（见 03 章）。固定用 mock 数据
- [test_anitabi.py](../backend/tests/test_anitabi.py) —— Anitabi 快照的解析、合并与查询，用手写的原始数组测试；本地有快照时多一个真实数据的粗略检查（见 03 章）
- [test_weather.py](../backend/tests/test_weather.py) · [test_route.py](../backend/tests/test_route.py) —— 天气与路线 MCP server，用 MockTransport 模拟外部服务（见 04 章）
- [test_llm.py](../backend/tests/test_llm.py) —— LLM 客户端对响应的解析、system prompt 的日期（见 05 章）
- [test_loop.py](../backend/tests/test_loop.py) —— 用按剧本回复的假 LLM 测试 agent loop，不花 token（见 05 章）
- [test_harness.py](../backend/tests/test_harness.py) —— 上下文裁剪、重复调用检测（见 06 章）
- [test_mcp_client.py](../backend/tests/test_mcp_client.py) —— 真实拉起子进程测试工具聚合与三种调用结局（见 07 章）
- [test_api.py](../backend/tests/test_api.py) · [test_store.py](../backend/tests/test_store.py) —— HTTP 接口与会话存储，注入假 LLM 走真实路由与 SSE 编码（见 08 章）
- [test_tracing.py](../backend/tests/test_tracing.py) —— Langfuse 装饰器，用假客户端，不连网络（见 09 章）
- [fakes.py](../backend/tests/fakes.py) —— 假 LLM、假工具等测试替身（不是测试文件）
- [conftest.py](../backend/tests/conftest.py) —— pytest 在所有测试前自动执行：清空 Langfuse key，保证测试不往线上写数据
- [test_external_network.py](../backend/tests/test_external_network.py) —— 真实调用三个外部服务的集成测试，默认不跑
- [stdio_helper.py](../backend/tests/stdio_helper.py) —— 把 MCP server 拉成子进程的辅助函数（不是测试文件，文件名不以 `test_` 开头所以不会被收集）

## 设计取舍

**requirements.txt 还是 pyproject.toml + 锁文件？**

规范做法是 `pyproject.toml` 声明依赖 + `uv.lock` 锁定精确版本，保证每次安装结果完全一致。这里用了更简单的 `requirements.txt` 且大多不钉版本，代价是：今天装和下周装，可能拿到不同版本。对 5 天 demo 可以接受；Day 4 写 Dockerfile 时如果发现构建不稳定，再用 `uv pip compile requirements.txt -o requirements.lock` 生成锁文件。

**为什么不用 conda？**

项目最初的 venv 以 anaconda 的 Python 为基础解释器；系统重装后 anaconda 不在了，venv 随之失效，于是改由 uv 自行下载并管理 Python 3.12（重建方法见 [踩坑日志](pitfalls.md#换电脑重装系统后-venv-失效ide-报无法解析导入)）。conda 适合科学计算那种带大量 C 依赖的场景；Web 项目用 venv 更轻，Docker 里也更好复现。

## 动手验证

```powershell
# 1. 依赖都在
python -c "import fastapi, mcp, sqlalchemy, langfuse; print('ok')"

# 2. 测试全过
python -m pytest -v

# 3. 起服务，浏览器打开 http://127.0.0.1:8000/docs
uvicorn backend.app.main:app --reload

# 4. 验证环境变量优先级：终端里的值会覆盖 .env
$env:LLM_MODEL = "from-env"; python -c "from backend.app.config import Settings; print(Settings().llm_model)"
Remove-Item Env:LLM_MODEL
```

## 延伸思考

1. `settings = Settings()` 在模块导入时就执行了。这意味着什么？如果测试想用不同的配置，会遇到什么麻烦？（提示：FastAPI 有个叫依赖注入的机制，Day 2 会用到）
2. `.env.example` 提交到 git、`.env` 不提交。如果有人 clone 了项目，他怎么知道需要配哪些变量？如果你新增了一个配置项但忘了更新 `.env.example`，会发生什么？
3. `DATABASE_URL` 为空时回退内存 store——这个"回退"逻辑应该写在 `config.py` 里，还是 `main.py` 的 lifespan 里？为什么？
