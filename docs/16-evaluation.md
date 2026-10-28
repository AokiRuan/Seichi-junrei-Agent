# 16 · Agent 评测

## 本章目标

读完能回答：

- 改了一行 prompt，怎么知道 agent 是变好了还是变坏了？只靠"手动问几句看看"为什么不够？
- 别人（TravelPlanner、ChinaTravel、τ-bench）是怎么评旅行规划 / 工具调用 agent 的？借鉴了什么？
- 一条评测用例长什么样？"通过"到底指什么？
- 天气、换乘这些外部服务每天的返回都不一样，评测结果怎么才能前后可比？
- 怎么测"工具坏了 agent 还能不能体面收场"？
- pass^1 和 pass^k 有什么区别，为什么要跑不止一次？

## 核心概念

### 为什么要评测

到上一章为止，验证 agent 的方式都是"在浏览器里问几句，看着不错"。这有三个问题：

- **覆盖不全**：每次只试自己想到的那两三种问法，改 prompt 时修好了一种、弄坏了另一种也不知道
- **不可比**：今天"看着不错"和昨天"看着不错"没法比较哪个更好
- **LLM 有随机性**：同一个问题问一次对了，不代表每次都对

评测要做的是：**固定一组任务 → 每次改动后都跑一遍 → 用确定的规则打分 → 和上一次比**。有了它，prompt、工具描述、harness 的每次改动都有量化反馈。

### 参考了什么

| 项目 | 是什么 | 借鉴的点 |
|---|---|---|
| [TravelPlanner](https://arxiv.org/abs/2402.01622)（OSU，ICML 2024） | 旅行规划 agent 基准：1225 个问题，agent 在沙箱里用工具查航班、酒店、景点，交出多日行程 | **交付率**（在步数上限内交出方案，失败包括死循环、反复失败、超步数）；**常识约束 vs 硬约束**两类检查（前者是任何行程都不该违反的，后者是用户这次提的要求）；**micro / macro 通过率**；**Final Pass Rate**：所有约束同时满足才算过 |
| [ChinaTravel](https://arxiv.org/abs/2412.13682)（南大 LAMDA，ICLR 2026） | 中国旅行规划基准，需求来自真实用户 | 约束检查写成**可组合的 DSL**：通用的逻辑 / 算术 / 时间算子 + 可插拔的领域谓词。新增一种约束只要加一个谓词 |
| [τ-bench](https://arxiv.org/abs/2406.12045)（Sierra，ICLR 2025） | 客服 agent 基准：模拟用户和 agent 多轮对话，agent 调工具改数据库 | **看终态，不看文字**：对话结束后比较数据库状态和标注的目标状态；**pass^k**：同一任务跑 k 次全成功的概率，衡量稳定性 |

公开报告里，这些基准上的真实数字都不好看：TravelPlanner 原论文里 GPT-4 的 Final Pass Rate 只有 0.6%；τ-bench 里最强的模型 pass^1 不到 50%，零售场景的 pass^8 不到 25%。**"看着不错"和"每次都满足全部约束"之间差得很远**，这正是要评测的原因。

我们的场景比这些基准小得多（25 条用例、一个 agent），但思路照搬：

```
TravelPlanner 的常识约束  →  通用检查：每条用例都做（能交付、不写坐标、语言对、行程自洽）
TravelPlanner 的硬约束    →  用例检查：cases.yaml 里每条的 expect
ChinaTravel 的 DSL        →  expect 是一组可组合的小检查（must_call / args / plan / answer）
τ-bench 的终态判定         →  优先检查工具调用参数和 plan_pilgrimage 返回的结构化行程，而不是回答文字
τ-bench 的 pass^k          →  --repeat k，报告 pass^k
```

### 一条用例长什么样

```yaml
- id: plan-bocchi-shinjuku
  category: plan_full
  turns: ["我周六上午 10 点从新宿出发，有 4 个小时，想去孤独摇滚的圣地巡礼"]
  expect:
    tools:
      must_call: [search_anime, geocode, plan_pilgrimage]
      must_not_call: [list_spots, plan_route]      # 不要自己拼行程
      order: [geocode, plan_pilgrimage]            # 先查出发地坐标再规划
    args:
      plan_pilgrimage: {anime_id: 328609, time_budget_min: 240, date: "@date:sat", start_time: "10:00"}
    plan: {ok: true, start_near: [35.69, 139.70, 3], min_stops: 3}
    answer: {not_contains: ["含往返", "包括返程", "含返程"], mentions_stops: 2}
```

逐项看它在检查什么：

- **tools**：工具选对了吗？这条要求走 `plan_pilgrimage` 而不是 `list_spots + plan_route` 自己拼——这是 [11 章](11-pilgrimage-planner.md)的核心设计，prompt 里写了，但 LLM 照不照做要测
- **args**：需求理解对了吗？"4 个小时"要换算成 240，"周六"要换算成具体日期，"上午 10 点"要变成 `10:00`
- **plan**：结果对吗？`start_near` 检查行程起点在新宿 3 公里内——如果 geocode 查成了别的"新宿"，这里会抓到
- **answer**：话说对了吗？只检查确实只能从文字里看的东西，比如不能把"不含返程"说成"含往返"（[11 章](11-pilgrimage-planner.md#让-llm-说对边界要写在数据里)实测出过这个问题）

25 条用例分 8 类：

| 类别 | 条数 | 测什么 |
|---|---|---|
| lookup | 5 | 查圣地：中 / 日 / 英文提问、按车站附近过滤、续作较多时选对作品 |
| weather | 2 | 用对坐标查天气 |
| plan_full | 7 | 信息完整的规划：出发地、时间、日期都要正确传给工具 |
| plan_minimal | 2 | 信息不全时直接规划（prompt 要求不先追问），并在回答里说明假设 |
| clarify | 1 | 连作品都没说时，应该追问而不是乱规划 |
| not_found | 2 | 查不到的作品不编造；超出能力（订票）如实说明 |
| multi_turn | 3 | 多轮：换片区、追问天气、改时间预算 |
| fault | 3 | 故障注入：geocode / 规划 / 天气工具失败时的表现 |

### 通用检查：任何回答都不该违反的

每条用例都会自动做（[evals/checks.py](../evals/checks.py) 的 `_generic`）：

| 检查 | 内容 |
|---|---|
| `delivery` | 在迭代上限内交出了非空回答，没有报错。失败时后面的检查都不做 |
| `answer.no_coordinates` | 回答里没有经纬度（prompt 规定坐标交给地图显示） |
| `answer.language` | 用提问的语言回答（默认中文，用例可以指定 ja / en） |
| `plan.*`（规划成功时） | 行程自洽：不超预算、站点不重复、序号连续、到达时刻不倒退、单段步行不超过 30 分钟、各段时间加起来等于总时长 |

`plan.*` 这组检查的对象是确定性代码（planner / itinerary），不是 LLM。它们失败说明算法有 bug。单元测试也测算法，但只在几组手造的数据上；评测是在真实快照、真实步行矩阵上跑，能发现手造数据里没有的情况。

### 生成和打分分开

```
run.py    驱动 AgentLoop → 事件流 → Trial（每轮的工具调用、参数、结果、回答、token、耗时）
checks.py 只读 Trial → CheckResult 列表
```

Trial 完整存进结果文件。这样**改了检查规则，可以对旧结果重新打分**（`--rescore`），不用再花 token 重跑。实际开发中这个功能第一次试跑就用上了，见下文「检查规则自己也会错」。

## 代码走读

### [evals/cases.py](../evals/cases.py)：严格校验 + 固定时间

两件事：

1. **写错的键直接报错**。`must_cal`（少个 l）如果被静默忽略，这条检查就永远不执行，用例永远"通过"——评测自己的 bug 是最难发现的，因为它表现为"一切正常"
2. **固定"当前时间"**：`EVAL_NOW = 2026-10-28 09:00 JST（周三）`。system prompt 里有今天的日期，LLM 据此把"周六"换算成 `20261031`。不固定的话同一条用例每天传的参数都不一样。用例里写 `"@date:sat"`，加载时换算成具体日期，以后改 `EVAL_NOW` 不用逐条改用例

### [evals/tools.py](../evals/tools.py)：缓存与故障注入

两个装饰器，和 [09 章](09-langfuse.md)的 `TracedTools` 一样实现 `ToolExecutor` 接口，loop 看不出区别：

```
loop → TracedTools → FaultyTools → CachedTools → MCPClientPool
```

**CachedTools**：同样的工具 + 同样的参数 → 直接返回上次的结果。

- 为什么要缓存：天气、geocode、换乘都是外部服务，今天和明天返回不一样，还偶尔超时。不缓存的话分数变了，分不清是 agent 变了还是外部世界变了。顺带也少给公共服务发请求
- 只缓存成功结果：超时是偶发的，不该被固定下来
- **指纹**：缓存条目带上"所有 MCP server 源码 + 快照文件"的哈希。改了规划算法、指纹就变，旧缓存整体作废——否则评测测的是旧代码的缓存结果。粒度粗（改天气 server 也会让圣地缓存失效），但不会出错
- 局限：缓存的键是参数。LLM 换个说法（`新宿` vs `新宿駅`）就是新的键，会实时调用。所以缓存降低了外部波动，但消除不了 LLM 本身的随机性——后者要靠 pass^k 来衡量

**FaultyTools**：让指定工具一律返回错误，形状和 `MCPClientPool` 真实出错时一致（`is_error=True`、`data=None`、`text` 是给 LLM 的说明）。用来测：

- geocode 挂了 → 应该不带起点继续规划，而不是卡住或编一个坐标
- 规划工具挂了 → 应该如实说明，而不是自己编一份行程
- 天气挂了 → 不能编出"晴，18℃"

### [evals/run.py](../evals/run.py)：组装与驱动

和 [components.py](../backend/app/components.py) 用的是同一套真实组件（真实 LLM、真实 MCP 子进程、同一个 `Harness`），区别只有三点：固定时间、工具包装、默认不上报 Langfuse（`--trace` 打开，每次 trial 一个 session，失败时可以去 Langfuse 里看完整 trace）。

几个细节：

- **不经过 ChatService**：评测不需要会话存储，直接构造 messages 调 `AgentLoop.run()`。多轮用例把上一轮 `done` 里的 `new_messages` 追加进去，和 ChatService 的行为一样
- **回答取最后一条有文字的 assistant 消息**，不是把所有 token 拼起来——token 流里还有工具调用前的"我来查一下"
- **并发**：`asyncio.Semaphore` 限制同时跑的用例数（默认 4）。所有 trial 共用一个 `MCPClientPool`（MCP 会话支持并发请求，和 FastAPI 里多个请求共用一样）
- **元数据**：结果里记下模型、git commit（带 `-dirty` 标记）、system prompt 的哈希、工具 schema 的哈希、harness 参数。两次结果对不上时，先看这些是不是变了。工具 schema 也要记——[07 章](07-mcp-client.md)讲过工具的 docstring 就是给 LLM 看的 prompt
- **必须用真实快照**：启动 MCP 子进程前设 `SEICHI_DATA_SOURCE=anitabi`（经 `PASSTHROUGH_ENV` 传进去）。mock 只有 5 部作品，用例里的 `anime_id` 都对不上；没快照时直接退出，而不是悄悄退回 mock 跑出一堆失败

### [evals/checks.py](../evals/checks.py)：规则检查

检查名的前缀就是失败分类：`delivery` / `tools` / `args` / `plan` / `answer`。报告里按分类计数，一眼看出问题集中在"工具没选对"还是"话没说对"。

参数匹配（`match`）支持四种写法：具体值、`one_of`、`range`、`present`。`range` 用于"一整天"这种没有唯一正确答案的换算（360~600 分钟都算对）；`present: false` 用于"没给出发地就不该传起点"——注意参数值为 `null` 也算没传。

### [evals/report.py](../evals/report.py)：指标

| 指标 | 定义 | 来源 |
|---|---|---|
| 交付率 | `delivery` 检查通过的比例 | TravelPlanner Delivery Rate |
| pass^1（pass_rate） | 一次运行所有检查全过的比例 | TravelPlanner Final Pass Rate |
| pass^k | 每条用例跑 n 次、成功 c 次，C(c,k)/C(n,k)，再对用例平均 | τ-bench |
| 检查通过率 | 单项检查通过的比例，比 pass^1 细 | TravelPlanner micro pass rate |
| 无效工具调用率 | 出错的调用 + 同一轮参数完全相同的重复调用，占全部调用的比例。注入的故障不算 | gpt-advice 里的建议 |
| 成本 | 平均 token、延迟（均值、p90）、工具调用次数、LLM 轮数 | — |

**pass^k 为什么重要**：假设一条用例的成功率是 70%，pass^1 = 0.7，但 pass^3 = 0.7³ ≈ 0.34。用户不会因为"十次里对了七次"满意，他只会碰到错的那三次。pass^1 和 pass^k 差得越多，说明 agent 越不稳定——这是"再加一条 prompt 规则"和"把这件事交给确定性代码"之间做选择的依据。

## 设计取舍

**为什么不用 LLM-as-judge 评回答质量？** 最初计划（[00 章 4.10](00-project-plan.md)）里有这一项，暂时没做：

- 规则检查是确定的、免费的、可以 `--rescore`；judge 本身有随机性和偏好（偏爱长回答、偏爱和自己风格像的回答），还要再校准 judge
- 我们最关心的错误（选错工具、参数换算错、编造天气、把"不含返程"说反）都能用规则抓到
- 规则检查都通过、但还是觉得回答不好时，再加 judge。到那时要先用人工标注的少量样本检验 judge 的判断和人是否一致

**为什么结果存本地 JSON，不用 Langfuse Datasets + Experiments？** 也是计划里的，暂时没用：

- 本地文件可以 diff、可以 `--rescore`、不依赖网络，以后接 CI 也简单
- trace 仍然可以上报 Langfuse（`--trace`），需要看某次失败的完整过程时打开
- Langfuse Experiments 的优势是界面上对比和多人协作；单人学习项目里，`python -m evals.report A B` 够用

**为什么用规则、不要求精确的工具调用序列？** 计划里原本是"每条标注期望的工具调用序列"。实际做下来发现精确序列太脆：LLM 先 geocode 还是先 search_anime 都对，并行调用的顺序也不确定。改成 `must_call` / `must_not_call` / `order`（只约束真正有先后依赖的那几个），检查的是"该做的做了、不该做的没做"。

**为什么温度不调成 0？** 评测要测的是用户实际碰到的行为。生产环境用默认温度，评测也用默认温度，随机性交给 pass^k 衡量。如果为了分数稳定把评测的温度调成 0，就是在测一个用户用不到的系统。

**缓存会不会掩盖问题？** 会掩盖外部服务的波动——这正是目的。但它也意味着"换乘 API 今天挂了"这种情况评测发现不了，那要靠故障注入用例主动测、以及线上的 Langfuse 监控。`--no-cache` 可以完全实时地跑一次。

## 基线结果

2026-10-27 实测，模型 deepseek-chat，25 条用例。网络条件不好（DeepSeek 接口多次超时），下面的数字要结合说明看。

### 第一次基线（25 条 × 2 次）

| 指标 | 原始打分 | 修正检查规则后重新打分 |
|---|---|---|
| 交付率 | 100% | 100% |
| pass^1 | 84.0% | **90.0%** |
| pass^2 | 76.0% | **84.0%** |
| 检查通过率 | 98.5% | 99.0% |
| 平均 token / 延迟 | 15.8k / 9.4s | — |

两列的差别全部来自**检查规则自己的错误**，见下一节。规则修正之后剩下的 5 条失败，逐条看过原文，都是 agent 真实的问题：

| 失败 | 原因 |
|---|---|
| `plan-en-tokyo-station` 1/2 | 用英文提问，用中文回答。prompt 写了"用提问的语言回答"，但工具结果全是中文时 LLM 有时会跟着工具走 |
| `fault-weather-down` / `fault-geocode-down` / `fault-plan-down` 共 4 次 | 工具挂了之后反复重试。天气工具被调了 4 次，每次把坐标**微调一点**（35.319003 → 35.319 → 35.32），恰好绕过 harness"参数完全相同"的重复检测 |

### 检查规则自己也会错

每条失败都打开原文确认，结果发现 6 条是规则误判，而不是 agent 错了：

| 误判 | 回答原文 | 修正 |
|---|---|---|
| `not_contains: 含返程` | "总时长包含去程交通，**不含返程**" | 前两个字有否定词的不算（见[踩坑日志](pitfalls.md)） |
| `language: en` | 英文回答里每个地点都括注日文原名，假名比例很高 | 先判英文再判日文 |
| `contains_any` | "这个我帮不上忙" / "这个我帮不了" / "连续两次超时" | 补关键词 |
| `asks_question` | "还需要知道你想巡礼哪部作品……告诉我就好。"（没有问号） | 加入"告诉我""哪部"等追问线索 |

修正后都用 `--rescore` 重新打分，没有重跑。

这一节有两条结论：

- **错误方向不只是"太松"**。太严的规则会让你去修一个不存在的问题。所以每条失败都要看原文
- **回答文本的规则检查是最脆的**：语言检测、关键词，三轮下来每轮都有新的误判；工具、参数、行程这几类检查一次都没误判过。这印证了"优先看终态"的原则，也说明以后加 LLM-as-judge 最该用在哪里——判断"有没有拒绝 / 有没有追问"这类语义问题

### 评测驱动的改进：工具熔断

针对上面第二类失败，harness 加了**按工具名计数**的熔断：同一个工具在一次回答里失败 2 次后，附提示"视为暂时不可用"，之后再调直接拦下（见 [06 章](06-harness.md#工具熔断评测发现后加的)）。

| 运行 | 故障类通过率 | pass^1（全部） | pass^2（全部） | 说明 |
|---|---|---|---|---|
| 基线（重新打分后） | 33%（2/6） | 90.0% | 84.0% | |
| 加熔断，第 1 次 | 100%（6/6） | 84.0% | 76.0% | 4 次 DeepSeek 超时 + 规则误判，总分反而降了 |
| 加熔断 + 超时重跑 + 规则修正 | 100%（6/6） | 92.0% | 88.0% | 仍有 3 次重跑 2 次都超时（交付率 94%） |

第一次加熔断后的对比是个反面教材：故障类明明从 33% 升到 100%，总分却降了。拆开看，掉分来自两件和改动无关的事：网络超时，以及熔断根本不会触发的用例上的随机波动（每条只跑 2 次，一次波动就是 50 个百分点）。之后做了两件事：LLM 接口出错时整条重跑、重跑次数单独报告（`--infra-retries`）；评估改动时对受影响的类别多跑、并且开关各跑一组做对照（[踩坑日志](pitfalls.md)有记录）。

### 消融：熔断开 / 关，故障类 × 5 次（🚧 结论待补）

| 配置 | 故障类 pass^1 | pass^5 | 故障工具被调次数（每次 trial） |
|---|---|---|---|
| 关（`--max-tool-failures 0`） | 86.7% | 66.7% | geocode：2, 3, 0, 2, 2；规划 / 天气：都是 2 |
| 开（默认 2） | 93.3% | 66.7% | 全部 2 |

这组数据**还不能下结论**：

- 关掉熔断时 LLM 这次大多也只试了 2 次，和基线里重试 3~4 次的表现不一样——LLM 的重试行为本身就不稳定，5 次样本看不出差别
- 两组各有一次失败与熔断无关（一次 LLM 超时、一次回答里写了坐标）
- 两组 infra_retries 分别是 6 和 3，网络状况在变

熔断"不让重试超过 2 次"是由代码保证的（单元测试 `test_failing_tool_is_cut_off_even_with_varied_arguments`），这一点不依赖评测。评测要回答的是"它对通过率、成本有多大影响"，这需要网络稳定时每组跑 10 次以上再看。

### 待查

- `lookup-lycoris`、`lookup-ja` 在两次运行里都超时，而且**重跑 3 次都失败**，每次约 16 秒就报错，不像随机波动。这两条恰好是最先开始的用例，怀疑和启动时并发发出的头几个请求有关（连接建立？），需要单独跑这两条、再调并发确认
- 换乘查询没给出发时间时，Transit API 按**真实的当前时刻**查（实测出现"按 19:57 发的车查的"）。评测固定了 system prompt 里的时间，但固定不了外部服务看到的时间

## 动手验证

```powershell
$env:PYTHONIOENCODING = "utf-8"
# 框架自身的单元测试（不花 token）
python -m pytest backend/tests/test_eval.py

# 只跑几条（约 1 分钟，几万 token）
python -m evals.run --only plan-bocchi-shinjuku notfound-fictional fault-geocode-down --label smoke

# 完整基线：25 条 × 2 次
python -m evals.run --repeat 2 --label baseline

# 改了检查规则后，对上次的结果重新打分
python -m evals.run --rescore evals/results/<文件名>.json

# 消融：把迭代上限降到 3，和基线对比
python -m evals.run --repeat 2 --max-iterations 3 --label iter3
python -m evals.report evals/results/<基线>.json evals/results/<iter3>.json
```

## 延伸思考

- 现在 25 条用例都是我写的，覆盖的是我想得到的问法。怎么从真实用户的对话（Langfuse 里的 trace）里挖出新用例？哪些失败的 trace 值得变成回归用例？
- `plan.*` 检查发现算法问题时，要不要把同样的检查加进 planner 的单元测试（用一个随机生成数据的 property-based 测试）？两者各自适合发现什么样的 bug？
- 故障注入现在是"一律失败"。真实世界更常见的是"偶尔超时"或"返回了但数据不对"（比如 geocode 查到了同名的另一个地方）。怎么注入这类故障？
- gpt-advice 里建议做消融实验：去掉重复调用检测、去掉强制收尾，看指标怎么变。先想一想：哪些用例会受影响？如果某个 harness 机制在 25 条用例里一次都没触发，这说明机制没用，还是用例不够？
- 如果换一个模型（比如更便宜的），pass^1 掉了 10 个点。怎么判断是模型理解不了工具描述，还是 prompt 写得太依赖某个模型的习惯？
