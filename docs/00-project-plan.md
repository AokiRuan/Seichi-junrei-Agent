# Aoki-Agent 项目计划

> 最后更新：2026-09-18

## 一、项目定位

做一个**日本动画圣地巡礼 agent** 的完整前后端。用户说「我想去《你的名字》的取景地」，agent 查圣地、看天气、规划路线、给出行程建议——所有能力通过 MCP 工具接入，agent loop 与 harness 自建。

**目标：5 天 vibe coding 出可用 demo（评测除外），部署到 AWS 可公网访问。**

这是一个学习驱动的项目，但学习方式从「继承改写现成框架」切换为「在真实场景下自建 loop 与 harness」。技术栈刻意覆盖一条完整的工程链路：MCP → agent → API → 前端 → 可观测 → 容器 → 云 → CI/CD。

## 二、Demo 核心场景

范围控制的锚点。Demo 只需要把这三个场景跑顺：

1. **查圣地** — 「《リコリス・リコイル》的圣地在哪？」→ 列出取景地，地图上打点
2. **查天气** — 「这周末去镰仓巡礼，天气怎么样？」→ 结合圣地位置查天气预报
3. **规划路线** — 「帮我安排一天走完这几个点」→ 按地理位置排序、估算移动时间、给出行程

三个场景分别对应三类 MCP 工具，也分别验证 agent 的单工具调用、多工具组合、多轮推理能力。

## 三、技术架构

```
┌──────────────────────────────┐
│   React (Vite + TS)          │  聊天面板 + 地图面板 (Leaflet)
└──────────────┬───────────────┘
               │ SSE
┌──────────────▼───────────────┐
│   FastAPI                    │  /api/chat 流式端点 · 会话管理 ──▶ Postgres
├──────────────────────────────┤
│   Agent Loop + Harness (自建) │  ← 本项目核心
│   · LLM 调用 / tool_calls 解析 │
│   · 迭代上限 / 错误恢复        │
│   · 上下文管理 / 流式事件      │
├──────────────────────────────┤
│   MCP Client                 │  工具发现 → schema → 调用分发
└───┬──────────┬──────────┬────┘
    ▼          ▼          ▼
 圣地巡礼     天气        路线/地图
 MCP Server  MCP        MCP
 (自建)      (外部)     (外部)
    │
    ▼
 Langfuse ──── 全链路 trace
```

部署形态：**单容器 + 外部数据库**（FastAPI 托管 React 静态文件，自建 MCP server 作为 stdio 子进程；Postgres 在容器外），ECR → App Runner，GitHub Actions 驱动。

## 四、模块拆解

### 4.1 圣地巡礼 MCP Server（自建） ✅

- **职责**：把圣地巡礼数据封装成 MCP 工具，供 agent 调用
- **工具设计**：
  - `search_anime(title)` — 按作品名模糊搜索（中 / 日 / 英 / 城市，容忍个别错字），返回作品 id 与基本信息
  - `list_spots(anime_id, limit, near_lat, near_lng, radius_km)` — 该作品的圣地：同一地点的镜头已合并，按镜头数排序，返回总数与前 N 处，可限定在某点附近
  - `get_spot(spot_id)` — 单个圣地详情，含各个镜头
- **实现**：MCP SDK 2.x + stdio 传输，用 MCP Inspector 单独验证
- **数据源：Anitabi 本地快照（2026-10-21 起）**，没有快照时自动退回 mock。`repository.py` 把数据源抽象成 `SeichiRepository` 接口，
  server.py 只依赖接口。详见 [docs/03「真实数据」](03-mcp-server.md#真实数据anitabi-快照)

#### Anitabi API 实测结论（2026-09-28）

能用的：

| 端点 | 结果 |
|---|---|
| `GET api.anitabi.cn/bangumi/{id}/lite` | 200，97ms。作品元信息 + 前 10 地标 + **作品级**地图中心坐标 |
| `GET api.anitabi.cn/bangumi/{id}/points/detail` | 200。全部地标（115908 返回 84 条） |
| `POST api.bgm.tv/v0/search/subjects` | 200。**补上了文档缺口**——Anitabi 没有按标题搜索的端点，用 Bangumi 官方搜索拿 subjectID |

三个坑：

1. **地标级 `geo` 线上已不再返回**。文档示例里有，实测两部作品 **0/84、0/63**，字段全集里没有 `geo`。只剩作品级中心点
2. **坐标只能部分救回**。`originURL` 里 Google Maps 的 `ll=` 参数可提取坐标，但仅覆盖 **35/84 = 42%**；Anitabi 自有来源的 49 个地标 URL 里只有 id 没有坐标
3. **Cloudflare 限流严格**。约 25 个请求后被 403，45 秒后仍未恢复。**演示当天实时调用有翻车风险**

另：`pointsLength` 声称 582 个地标，`points/detail` 实际只返回 84 个。

#### 新发现：网站内部数据文件有完整坐标（2026-10-21）

参考开源客户端 [boiboif/anitabi-app](https://github.com/boiboif/anitabi-app)（GPL-3.0，作者用 DevTools 分析 anitabi.cn 地图页的请求得出）：
地图页不走 `api.anitabi.cn`，而是直接下载 `www.anitabi.cn/d/` 下的几个 JSON 文件。只读其文档与代码、自己发了 2 个请求实测：

| 文件 | 内容 | 实测 |
|---|---|---|
| `GET /d/g.json?d={天数+6 的 36 进制}` | 全部作品：中/英/日标题、城市、封面、作品中心点，以及**每个地标的 `[id, 纬度, 经度, 优先级]` 扁平数组** | 200，930 KB，2.2s。**1552 部作品、52244 个地标，坐标 100% 齐全**，0 条格式异常 |
| `GET /d/g{n}.json?d=...` | 第 n 片（每片 250 部，片大小就是 `g.json[1]`）的地标详情：名称、图片、集数、秒数、备注、来源，**不含坐标** | `g0.json` 200，1 MB，1.7s。分片顺序与 `g.json` 一致 |

按地标 id 拼接两份文件即得完整数据。《上低音号》（115908）：**582/582 个地标拿到坐标**——公开 API 的 `points/detail` 只返回 84 个，`pointsLength` 声称的 582 在这里对上了。

`d` 参数是"距 1970 年的天数 + 6，转 36 进制"，每天变一次，用来让 CDN 回源（实测当天为 `g0h`）。

需要注意的地方：

- **不是公开 API**：是网站前端自用的数据文件，格式是**按位置的数组**（第 10 个元素是纬度……），网站一改版就可能失效，没有任何兼容承诺
- **数据量对 LLM 来说太大**：一部作品 582 个地标，其中 38 个是"文件夹"（`isFolder`，子地标用 `fid` 指向它），很多地标是同一地点的不同镜头。直接返回给 LLM 会撑爆上下文，`list_spots` 需要折叠 / 筛选
- **中文名稀疏**：`cn` 只有 35/582，`name` 是日文；名称里混有零宽空格（`​`），需要清洗
- **使用规范**：数据遵循 CC BY-NC-SA 4.0（非商业、署名、相同方式共享）；该客户端的贡献指南也要求不得绕过速率限制等技术措施。合理用法是**低频下载一次存成本地快照**，而不是每次对话都去请求
- 参考项目是 GPL-3.0：可以学习其中的格式知识，**不复制它的代码**

#### 当前方案：本地快照 ✅（2026-10-21）

`python -m backend.mcp_servers.seichi.anitabi` 下载 `g.json` + 7 个分片（8 个请求、约 15 秒），拼接坐标、把同一地点的镜头合并成地点，
存成 `data/anitabi_snapshot.json`（15.5 MB，不进 git）：**1552 部作品、51166 个镜头 → 23225 个地点**。上面「需要注意的地方」的处理：

- 格式不稳定 → 字段位置集中成常量，解析失败的条目丢弃；没有快照时 `get_repository()` 退回 mock
- 数据量大 → 先按文件夹、再按 100 米合并成地点；`list_spots` 只返回前 N 处 + 总数，支持按坐标附近过滤；结果瘦身（40 处从 16466 降到 9563 字符）
- 中文名稀疏、零宽空格 → 清洗不可见字符；搜索时 NFKC 归一化并容忍个别错字
- 使用规范 → 低频下载一次；地图上署名「圣地数据 © Anitabi (CC BY-NC-SA 4.0)」，弹窗可跳回 Anitabi
- 部署 → Docker 构建时下载快照（Day 4）

测试固定用 mock（`SEICHI_DATA_SOURCE=mock`），结果不随快照变化。

#### 早期方案：mock 数据

`data/mock_spots.json` 按 Anitabi 格式伪造，5 部作品 22 个地标，坐标零缺失。现在只用于测试和没有快照时兜底。

### 4.2 天气与路线 MCP Server（自建） ✅

**决定：两个都自建**，不用社区现成的——社区实现多为 Node（本机未装）或基于 MCP SDK 1.x；两者都是薄包装，自建成本低且有学习价值。详见 [docs/04](04-external-mcp-servers.md)。

| Server | 工具 | 外部服务（实测 2026-09-29） |
|---|---|---|
| `weather` | `get_weather_forecast(lat, lng, days≤16)` | Open-Meteo，~1.1s，免 key |
| `route` | `geocode(query)` · `plan_route(stops, mode, optimize)` | OSRM FOSSGIS 实例（步行/驾车），Nominatim |

要点：

- **OSRM 用 FOSSGIS 实例而不是官方 demo**：官方 demo 请求步行会静默返回驾车结果
- **路线服务不可用时降级**为直线距离估算，结果标记 `estimated`；天气不降级（没有诚实的近似值）
- **不含公共交通**：步行超 45 分钟的路段标记 `suggest_transit`，由 LLM 建议改乘电车
- 排序：≤8 个点穷举求最优，更多用最近邻启发式

### 4.3 Agent Loop + Harness（自建）

不再依赖 `hello_agents`。核心循环：

```
messages → LLM(tools=mcp_schemas) → 有 tool_calls?
   ├─ 是 → 经 MCP client 执行 → 结果追加到 messages → 回到开头
   └─ 否 → 输出最终回答
```

Harness 需要覆盖的点：
- **工具发现**：启动时连接所有 MCP server，拉取工具列表，转成 LLM 的 tool schema
- **迭代上限**：防止死循环，超限时强制让 LLM 总结
- **错误恢复**：工具调用失败不中断，把错误信息作为 tool result 交给 LLM 自行处理
- **流式事件**：loop 内部产出结构化事件流（token / tool_call / tool_result / done），供 SSE 转发
- **上下文管理**：demo 阶段做简单截断即可
- **可观测**：每一轮 LLM 调用和工具调用都打到 Langfuse

`tools/registry.py` 里的 schema 导出思路可以借鉴，但 MCP 协议本身已提供工具 schema，registry 的角色变成「MCP client 的聚合层」。

### 4.4 后端 FastAPI ✅（Postgres 待 Day 4）

- `POST /api/chat` — SSE 流式（FastAPI 内置 `EventSourceResponse`），事件类型：`session` / `token` / `tool_call` / `tool_result` / `done` / `error`；带 `session_id`，不传则新建，不存在返回 404
- `GET /api/sessions` — 会话列表（id、标题、更新时间）
- `GET /api/sessions/{id}` — 拉取该会话的完整消息，用于刷新页面后恢复
- 托管 React 构建产物（`frontend/dist` 挂在 `/`），单容器对外。接口统一加 `/api` 前缀，`/health` 留在根路径给健康检查（2026-10-21）
- 启动时拉起 MCP server 子进程并完成工具发现
- 三层：路由（SSE 编码）→ `ChatService`（会话与持久化，CLI 共用）→ loop。详见 [docs/08](08-fastapi-sse.md)

**会话持久化**：

- Postgres + SQLAlchemy（async），两张表：`sessions`（id / title / created_at / updated_at）、`messages`（自增 id / session_id / role / content / tool_calls / tool_call_id / tool_name / is_error / data / created_at）
- 消息存成「OpenAI 格式的超集」：发给 LLM 时只取 role / content / tool_calls / tool_call_id；`tool_name` / `is_error` / `data`（工具的结构化结果）只给前端，用来复现工具调用过程与地图打点。（2026-10-14 修订：原设计的 `tool_results` 列表字段与 loop 产出的消息形状不符）
- 一轮对话整体入库（`append_messages`），出错的轮次不入库
- 会话标题：首条用户消息截断即可，不额外调 LLM 生成
- 建表用 `create_all` 启动时执行，demo 不引入 Alembic
- 先定义 `SessionStore` 接口，Day 2 用内存实现让 loop 跑起来，Day 4 换 Postgres 实现——接口不变，前端与 loop 都不用动

### 4.5 前端 React ✅

- Vite + React + TypeScript
- **双面板**：左侧聊天，右侧地图（Leaflet + OpenStreetMap，免费无 key）
- SSE 消费，**工具调用过程可视化**——用户能看到「正在查询圣地…」「正在获取天气…」，这是 agent demo 的核心观感
- `tool_result` 里的圣地坐标自动打点到地图，路线场景画出连线
- 会话侧栏：列出历史会话，点击切换；刷新页面后从 `GET /api/sessions/{id}` 恢复，包括工具调用记录与地图打点
- 实现要点（详见 [docs/10](10-frontend.md)）：fetch + 手写 SSE 解析（EventSource 不支持 POST）；实时事件与存储消息走两个纯函数、产出同一个界面模型；地图数据从对话派生、只用工具的结构化结果；路线是站点间直线示意（`plan_route` 不返回道路几何）

### 4.6 可观测性 Langfuse ✅

- 用 Langfuse Cloud 免费版（**JP 区域** `jp.cloud.langfuse.com`），不自托管
- ~~`@observe()` + `langfuse.openai` drop-in~~ → **装饰器模式**：`TracedLLM` / `TracedTools` 包住 LLM 与工具，loop 一行不改。drop-in 依赖 `LANGFUSE_*` 环境变量，与我们的 pydantic-settings 配置方式不兼容。详见 [docs/09](09-langfuse.md)
- trace 结构：`agent-turn`（agent，带 session_id）→ `llm`（generation，含 token 与首字时间）/ 工具名（tool）
- Langfuse 不可达时请求不受影响（后台线程发送）；只在进程退出时 flush
- Demo 价值：演示时打开 trace 页面，展示 agent 的完整决策链——这是「自建 harness」最直观的证明

### 4.7 Docker

- 多阶段构建：node 阶段 build React → python 阶段 runtime
- 单镜像：FastAPI + 静态文件 + 自建 MCP server（stdio 子进程，无需网络配置）
- `docker-compose.yml` 本地一键起：app + postgres 两个服务，作为部署失败时的兜底演示方式

### 4.8 AWS 部署

- **选型：ECR + App Runner**。理由：从镜像直接跑、自动 HTTPS、零运维、按需计费，demo 成本几美元
- **数据库必须在容器外**：App Runner 的文件系统是临时的，重启/重部署就丢，容器内 SQLite 不可行。用托管 Postgres，只需一个连接串环境变量
- 备选：EC2 + docker compose。更土但出问题时更容易排查，且 Postgres 可以直接跟着 compose 跑
- 环境变量（LLM key、Langfuse key、地图 key、`DATABASE_URL`）走 App Runner 配置，不进镜像

### 4.9 CI/CD GitHub Actions

- **PR 触发**：ruff lint + pytest + 前端 build 检查
- **main 推送**：build 镜像 → push ECR → App Runner 自动拉取新镜像部署
- AWS 凭证走 GitHub OIDC，不存长期 key

### 4.10 Agent 评测（Day 6+，不在 demo 范围）

- **评测集**：20～30 条 query，每条标注期望的工具调用序列与期望命中的圣地
- **指标**：工具选择准确率、圣地召回率、LLM-as-judge 评回答质量
- **工具**：Langfuse Datasets + Experiments，结果与 trace 关联，改 prompt 后可对比回归
- 评测框架搭好后，harness 的每次改动都有量化反馈——这是评测的真正价值

## 五、五天计划

| Day | 主线 | 产出 | 验收 |
|---|---|---|---|
| **1** ✅ | 数据 + 三个 MCP | ~~验证数据源~~ → Anitabi 不可用，已切 mock；圣地/天气/路线三个 MCP server 全部跑通 | ✅ 5 部作品 22 个地标（坐标已用 Nominatim 校正）；天气、路线实测调通真实 API |
| **2** ✅ | Agent loop + 后端 | LLM 客户端（流式）、MCP 客户端、loop + harness、system prompt、内存 SessionStore、FastAPI SSE、Langfuse、CLI | ✅ curl 流式拿到带 3 次工具调用的回答（首字 0.8s）、多轮续聊正常；✅ Langfuse 里看到完整 trace 树 |
| **3** ✅ | React 前端 | 聊天 + 地图双面板；SSE 消费；工具过程可视化；打点；会话侧栏 | ✅ 无头浏览器实测：点击提问后流式输出、并行工具转圈、地图打点与路线；刷新后原样恢复；侧栏切换会话。前端 17 个单元测试 |
| **4** | Docker + 持久化 + CI | 多阶段 Dockerfile；compose 起 app + postgres；`SessionStore` 换 Postgres 实现；GitHub Actions PR 检查 + 镜像推 ECR | `docker compose up` 一键可用，重启容器会话还在；PR 有绿勾；ECR 有镜像 |
| **5** | AWS 部署 + 缓冲 | App Runner 上线；域名/HTTPS；**留半天修 bug** | 公网 URL 可访问，三个场景可演示 |

**Langfuse 在 Day 2 就接**，不要留到最后——它是 debug agent loop 最好用的工具，早接早受益。

## 六、明确不做

- 用户系统 / 登录 / 鉴权（会话持久化但不区分用户，所有会话全局可见）
- 数据库迁移工具（Alembic），`create_all` 够用
- 多语言 UI（界面中文，agent 回答跟随用户语言）
- 移动端适配（响应式做到手机能看即可）
- 语音
- 自托管 Langfuse
- 评测（Day 6+）

## 七、风险与预案

| 风险 | 影响 | 预案 |
|---|---|---|
| ~~圣地数据源不可用~~ **已发生、已解决** | Anitabi 被 Cloudflare 拦截 + 公开 API 不返回地标坐标 | ✅ 先切 mock 保住进度；后来找到网站自用的数据文件，改为本地快照。网站改版导致快照无法更新时，旧快照照常可用，没有快照则退回 mock |
| AWS 部署踩坑 | Day 5 交不出公网 URL | 本地 compose + 录屏演示兜底；App Runner 不行切 EC2 |
| ~~社区 MCP server 质量参差~~ | — | ✅ 已自建，天气/路线实测可用 |
| 公共路线/地理编码服务不稳定 | 规划路线失败 | ✅ OSRM 不可用时降级为估算；Nominatim 限速 + 缓存 |
| ~~LLM tool calling 不稳定~~ | — | ✅ DeepSeek 实测表现良好，见「待定决策」 |
| 流式 + tool calls 的解析 | loop 实现复杂度被低估 | Day 2 先做非流式版本跑通逻辑，再加流式 |

## 八、待定决策

- ~~**LLM**：继续 DeepSeek 还是换模型~~ → **继续用 DeepSeek**（2026-10-01）。三个场景实测：工具选择与调用顺序正确、会并行调用、工具失败后能自行重试、能按注入的日期换算「这周末」。问题都能靠 prompt 解决（英文过渡语、抄写坐标）。每个问题约 6600~8000 tokens
- **生产 Postgres 托管**：RDS（AWS 原生，但要配 VPC / 安全组，吃掉 Day 5 半天）vs Neon / Supabase 这类托管免费层（拿到连接串就能用）——倾向后者，Day 5 的时间留给部署本身
- ~~**何时切回真实 Anitabi 数据**~~ → **2026-10-21 已切换**为本地快照（网站地图数据文件里有全部坐标，不需要地理编码补全），见 4.1

## 九、目标目录结构

```
Aoki-Agent/
├── backend/
│   ├── app/              FastAPI 入口、路由、SSE
│   ├── agent/            自建 loop、harness、MCP client
│   ├── store/            SessionStore 接口 + 内存 / Postgres 实现
│   ├── cli.py            命令行入口（与 FastAPI 共用组件）
│   └── mcp_servers/
│       ├── seichi/       圣地巡礼 MCP server（本地数据）
│       ├── weather/      天气 MCP server（Open-Meteo）
│       └── route/        路线 MCP server（OSRM + Nominatim）
├── frontend/             React (Vite + TS)
├── evals/                评测（Day 6+）
├── docs/                 学习手册：计划、架构、逐模块讲解、踩坑日志
├── learn/                学习期代码（原 core/ agent/ tools/ tests/）
├── CLAUDE.md             项目约定（含「代码与文档同步」规则）
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/
```

## 十、参考

- MCP Python SDK 2.x（注意：`FastMCP` 已改名 `MCPServer`）
- Langfuse Python SDK 文档
- Open-Meteo API
- OSRM / Nominatim
- Anitabi 开放 API（实测结论见 4.1）
- Bangumi API `api.bgm.tv/v0/search/subjects`
