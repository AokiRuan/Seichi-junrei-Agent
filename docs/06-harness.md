# 06 · Harness

## 本章目标

读完能回答：

- harness 和 loop 的分工是什么？为什么要分开？
- 一个被所有请求共享的对象，为什么不能保存"本次运行"的状态？
- LLM 一直调工具停不下来，怎么办？直接截断有什么问题？
- LLM 用完全相同的参数反复调同一个工具，说明什么？怎么处理？换着参数调一个已经挂掉的工具呢？
- 对话历史太长要裁剪时，为什么不能随便从中间切开？

## 核心概念

[05 章](05-agent-loop.md)的 loop 只写了"正常情况"：问 LLM、调工具、回填、再问。但 LLM 是个不可控的决策者，它可能：

- 一直调工具，永远不给最终回答
- 用同样的参数反复调同一个工具（它卡住了），或者换着参数反复调一个已经挂掉的工具
- 生成格式错误的参数
- 让对话历史长到超出上下文窗口

**harness 就是处理这些"不正常情况"的策略集合**。把它从 loop 里拆出来，是 [01 章原则三](01-architecture.md#原则三loop-与-harness-分离)：loop 保持简短可读，策略可以单独调整、单独测试。

### harness 必须是无状态的

在 FastAPI 里，harness 会在应用启动时创建**一个**实例，被所有请求共享。假设它这样写：

```python
# ❌ 反例
class Harness:
    def __init__(self):
        self.seen_calls = Counter()   # 记录调用过什么

    def is_repeat(self, key):
        self.seen_calls[key] += 1
        return self.seen_calls[key] > 2
```

用户 A 和用户 B 同时在聊天，两个人的调用记录会混在同一个 `Counter` 里：A 查了两次莉可丽丝，B 第一次查就被判定为"重复调用"。

所以 [harness.py](../backend/agent/harness.py) 只保存**配置**（上限是多少），**每次运行的状态由 loop 在局部变量里维护，作为参数传给 harness**：

```python
# loop.py
seen_calls: Counter[str] = Counter()     # 局部变量，每次 run() 都是新的
...
self.harness.repeated_call_message(tc.name, tc.arguments, seen_calls)
```

判断标准：**会被多个请求共享的对象，只能持有不随请求变化的东西**。这条规则在 Web 开发里到处适用。

## 代码走读

[harness.py](../backend/agent/harness.py) 四块策略：

### 迭代上限

```python
def should_continue(self, iteration: int) -> bool:
    return iteration <= self.max_iterations       # 默认 8，可通过 MAX_ITERATIONS 配置

def force_final_message(self) -> dict:
    return {"role": "user", "content": FORCE_FINAL_INSTRUCTION}
```

超过上限时，loop 不是直接结束，而是**再问 LLM 最后一次**：追加一条"不能再调用工具了，请根据已有信息直接回答"的指令，并且**不提供工具**（`tools=None`），LLM 在技术上就没法再调工具了。

两种处理方式的对比：

| | 用户得到的 |
|---|---|
| 超限直接截断 | 什么都没有，或者一句"出错了" |
| 超限后强制收尾 | 一个基于已有信息的回答，缺的部分如实说明 |

已经调了 8 轮工具，通常已经拿到了不少信息，浪费掉很可惜。

指令用的是 `role: "user"` 而不是 `role: "system"`：对话中间插入 system 消息，不是所有 LLM 服务都支持；user 消息所有服务都支持。这条指令只在最后一次请求里使用，**不存进历史**（原因见 [05 章设计取舍](05-agent-loop.md#设计取舍)）。

### 重复调用检测

```python
def repeated_call_message(self, name, arguments, seen) -> str | None:
    key = self.call_key(name, arguments)       # "search_anime:{\"title\": \"莉可丽丝\"}"
    if seen[key] >= self.max_repeat_calls:     # 默认 2
        return "你已经用相同的参数调用过 search_anime 2 次，结果不会变化，见上文。……"
    seen[key] += 1
    return None
```

LLM 用完全相同的参数反复调同一个工具，通常说明它卡在了某种循环里。再执行一次只会得到同样的结果，浪费时间，还会把历史撑大。所以第三次起不再真正执行，而是把一段提示作为工具结果返回给 LLM，引导它换个思路。

`call_key` 里用了 `json.dumps(arguments, sort_keys=True)`，这样 `{"x": 1, "y": 2}` 和 `{"y": 2, "x": 1}` 会被视为相同的调用。

为什么阈值是 2 而不是 1：同样的参数调两次有合理的情况，比如第一次网络超时、LLM 重试（05 章实测就发生过）。

**并发安全**：一轮里的多个工具调用是用 `asyncio.gather` 并行执行的，它们共享同一个 `seen_calls`。会不会出现竞争？不会：`repeated_call_message` 里"检查"和"计数"之间没有 `await`。asyncio 是单线程的，只在 `await` 处切换任务，没有 `await` 的一段代码不会被打断。

### 工具熔断（评测发现后加的）

重复调用检测有个漏洞：它只认"参数完全相同"。[16 章](16-evaluation.md)的故障注入评测里，天气工具一直超时，LLM 的应对是：

```
get_weather_forecast {"lat": 35.319003, "lng": 139.5506805, "days": 5}  → 超时
get_weather_forecast {"lat": 35.319,    "lng": 139.5507,    "days": 5}  → 超时
get_weather_forecast {"lat": 35.32,     "lng": 139.55,      "days": 5}  → 超时
get_weather_forecast {"lat": 35.319,    "lng": 139.551,     "days": 4}  → 超时
```

每次把坐标改一点点，对重复检测来说就是"新"调用。工具本身挂了的时候，换参数没有意义。所以加了第二道检测：**按工具名计数失败次数**，不管参数。

```python
# loop._execute 里，真正执行工具之后
if outcome.is_error:
    failures[tc.name] += 1
    note = self.harness.tool_failure_note(tc.name, failures)   # 刚到上限：在这次错误后面附一句「本轮不要再调用它」
    ...
# 执行之前
unavailable = self.harness.unavailable_tool_message(tc.name, failures)  # 已到上限：不执行，直接返回提示
```

两步都要：

- **刚到上限时附提示**：LLM 在下一次尝试**之前**就知道该停了，省掉一轮 LLM 调用
- **超过上限时拦截**：LLM 无视提示继续调时的硬保证

只数工具真正执行后的 `is_error`。harness 自己拦下的（坏 JSON、重复调用）不算，那是 LLM 的问题，不说明工具不可用；业务上的"查不到"（`data` 里的 `error`，比如"没有这部作品"）也不算，那是正常结果。

默认上限 2：超时后重试一次是合理的（网络偶尔抖动），第二次还失败，大概率是服务真的挂了。`Harness(max_tool_failures=0)` 关闭熔断，用来做消融对比。

### 参数错误转译

LLM 生成的参数不是合法 JSON 时，loop 不会执行工具，而是把错误信息作为工具结果交给 LLM：

```python
def argument_error_message(name, error) -> str:
    return f"调用 {name} 失败：{error}。请修正参数后重试。"
```

同时，写回历史的是 LLM 给出的**原文**参数（`raw_arguments`），这样 LLM 下一轮能看到自己写错了什么。

> **Python 知识点：`@staticmethod`**
>
> 源码里 `call_key` 和 `argument_error_message` 上面有 `@staticmethod`，表示这个方法**不接收 `self`**——它不读也不改实例状态，只是逻辑上属于这个类的普通函数。三种方法对比：
>
> | 写法 | 第一个参数 | 能访问 | 本项目例子 |
> |---|---|---|---|
> | 普通方法 | `self`（实例） | 实例属性 + 类 | `trim` 要读 `self.max_history_messages` |
> | `@classmethod` | `cls`（类本身） | 类属性，常用于"另一种构造方式" | `Tracing.from_settings` |
> | `@staticmethod` | 无 | 只有传进来的参数 | `call_key`、`argument_error_message` |
>
> 实例和类都能调用：`Harness.argument_error_message("x", "e")` 与 `h.argument_error_message("x", "e")` 结果相同。loop 里仍写 `self.harness.argument_error_message(...)`，调用方不需要关心它是不是静态的。
>
> **为什么不写成模块级函数？** 也可以，功能上没区别。放在类里是因为它属于 harness 的策略——以后想换一套错误提示措辞，写个 `Harness` 子类覆盖这个方法就行，loop 不用改。加 `@staticmethod` 而不是留着没用的 `self`，是为了让读者一眼看出"这个方法不依赖任何配置"。

### 上下文裁剪

```python
def trim(self, messages):
    system = [m for m in messages if m["role"] == "system"]
    rest   = [m for m in messages if m["role"] != "system"]
    if len(rest) <= self.max_history_messages:
        return system + rest
    tail = rest[-self.max_history_messages:]
    # 从最近的消息里，找第一条 user 消息作为起点
    for i, m in enumerate(tail):
        if m["role"] == "user":
            return system + tail[i:]
    ...
```

两条规则：

1. **system 消息永远保留**，它是 agent 的"人设"和规则
2. **裁剪的起点必须落在 user 消息上**

第 2 条是这段代码的关键。一轮完整的交互是 `user → assistant(tool_calls) → tool → assistant`。如果按条数从中间随便切：

```
... assistant(tool_calls: [t3]) | tool(t3) → assistant → user → ...
                                ↑ 从这里切
```

保留下来的第一条是 `tool(t3)`，但它对应的、带 `tool_calls` 的 assistant 消息被切掉了。**LLM 服务遇到"没有对应调用的 tool 消息"会直接拒绝请求**。实测 DeepSeek 返回：

```
400 - Messages with role 'tool' must be a response to a preceding message with 'tool_calls'
```

从 user 消息开始切，保证每一轮都是完整的。

[test_harness.py](../backend/tests/test_harness.py) 的 `test_trim_never_leaves_orphan_tool_message` 把上限从 1 到 20 全部试一遍，确认任何情况下都不会留下孤立的 tool 消息。

## 设计取舍

**按消息条数裁剪，还是按 token 数裁剪？**

真正的限制是 LLM 的上下文窗口，单位是 token。一条 `list_spots` 的结果可能有上千 token，一条"好的"只有几个 token，按条数裁剪很粗糙。按 token 裁剪需要一个 tokenizer 来计算每条消息的长度，而不同模型的 tokenizer 不同。demo 阶段对话轮数少，按条数够用；这是一个明确的待改进点。

**超限时的阈值该设多少？**

默认 8 轮。三个 demo 场景实测用了 2～3 轮。设得太小，复杂问题会被提前截断；设得太大，LLM 卡住时要浪费很多轮才能收尾。8 轮留了充足的余量。这类参数最终应该由评测数据决定。

**还有哪些策略没做？**

- **单个工具调用的超时**：现在依赖工具自己的超时（天气和路线的 httpx 设了 10 秒）。如果某个工具卡住不返回，整个 loop 会一直等
- **总 token 预算**：一次回答最多花多少 token
- **敏感操作的确认**：本项目的工具都是只读查询，没有需要用户确认的操作（比如下单、发邮件）

## 动手验证

```powershell
python -m pytest -v backend/tests/test_harness.py backend/tests/test_loop.py
```

重点看这几个测试，它们用假 LLM 制造了真实 LLM 很难稳定复现的情况：

- `test_iteration_limit_forces_final_answer_without_tools` —— LLM 一直调工具停不下来
- `test_repeated_identical_calls_are_short_circuited` —— LLM 反复发出完全相同的调用
- `test_invalid_json_arguments_are_not_executed` —— LLM 生成了坏 JSON
- `test_failing_tool_is_cut_off_even_with_varied_arguments` —— 工具一直失败，LLM 换着参数重试（评测里发现的真实行为）

## 延伸思考

1. 超限后的收尾指令是追加一条 user 消息。如果 LLM 无视它，仍然想调工具，会发生什么？（提示：看 `tools=None`）
2. 重复调用检测只看"工具名 + 参数完全相同"。LLM 用 `{"title": "莉可丽丝"}` 和 `{"title": "莉可麗絲"}` 交替搜索，算不算卡住了？该怎么识别？（工具**失败**时的版本已经由工具熔断解决；工具**成功**但结果没用时，LLM 换着说法反复搜，熔断管不到——怎么办？）
3. 如果要按 token 数裁剪，被裁掉的早期对话里有用户说过的重要信息（比如"我不能爬山"），怎么办？
