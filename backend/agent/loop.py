"""自建 Agent Loop —— 本项目的核心。讲解见 docs/05-agent-loop.md。

核心循环：

    messages → LLM(tools) → 有 tool_calls?
       ├─ 是 → 执行工具（可并行）→ 结果追加到 messages → 回到开头
       └─ 否 → 输出最终回答

loop 对外只 yield AgentEvent，不写 HTTP 响应、不 print、不碰数据库。
它依赖的三样东西（LLM、工具、策略）都从构造函数传入。

流式：LLM 每吐出一小段文字就转发成一个 token 事件；工具调用要等这一轮
流式结束、参数拼完整之后才执行。
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any, AsyncIterator

from .events import AgentEvent
from .harness import Harness
from .llm import ChatModel, LLMResponse, TextDelta, ToolCall
from .mcp_client import ToolExecutor, ToolOutcome

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


class AgentLoop:
    def __init__(self, llm: ChatModel, tools: ToolExecutor, harness: Harness) -> None:
        self.llm = llm
        self.tools = tools
        self.harness = harness

    async def run(self, messages: list[dict[str, Any]]) -> AsyncIterator[AgentEvent]:
        """执行一次完整的回答（可能包含多轮工具调用），产出事件流。

        messages：system prompt + 会话历史 + 本轮用户输入。不会被修改。
        最后一个 done 事件的 payload["new_messages"] 是本次新产生的消息
        （assistant / tool），由调用方决定是否写回会话存储。
        """
        history = self.harness.trim(list(messages))
        start = len(history)
        schemas = self.tools.tool_schemas()
        # 本次运行的状态放在局部变量里：harness 被所有请求共享，不能存状态
        # Counter 是标准库 collections 里的计数器。它本质上就是一个 dict，只多了一个关键特性：读取不存在的键时返回 0，而不是报错。
        seen_calls: Counter[str] = Counter()
        failures: Counter[str] = Counter()  # 工具名 → 本次 run 里失败的次数（熔断用）
        usage: Counter[str] = Counter()

        iteration = 0
        while True:
            iteration += 1
            final_round = not self.harness.should_continue(iteration)

            if final_round:
                # 收尾指令只发给 LLM 这一次，不进入 history —— 它不是用户说的话，不该被存进会话
                request = history + [self.harness.force_final_message()]
                tools = None
            else:
                request, tools = history, schemas

            resp: LLMResponse | None = None
            try:
                async for item in self.llm.stream(request, tools):
                    if isinstance(item, TextDelta):
                        # 文字一到就转发，用户不用等整段生成完
                        yield AgentEvent(type="token", content=item.text)
                    else:
                        resp = item
            except Exception as e:  # noqa: BLE001
                # LLM 本身挂了，没法「把错误交给 LLM 处理」，只能告诉调用方。
                # 注意：流式中途断开时，前面的 token 已经发出去了
                yield AgentEvent(type="error", message=f"LLM 调用失败：{type(e).__name__}: {e}")
                return
            if resp is None:
                yield AgentEvent(type="error", message="LLM 流式响应意外结束，没有给出完整回复")
                return

            _accumulate(usage, resp.usage)
            if final_round:
                resp = LLMResponse(content=resp.content, finish_reason=resp.finish_reason)
            history.append(resp.to_message())

            if not resp.tool_calls:
                yield AgentEvent(
                    type="done",
                    payload={
                        "new_messages": history[start:],
                        "iterations": iteration,
                        "usage": dict(usage),
                    },
                )
                return

            for tc in resp.tool_calls:
                yield AgentEvent(
                    type="tool_call", tool_name=tc.name, tool_call_id=tc.id, payload=tc.arguments
                )

            # 同一轮的多个工具调用并行执行（比如同时查京都和东京的天气）
            outcomes = await asyncio.gather(
                *(self._execute(tc, seen_calls, failures) for tc in resp.tool_calls)
            )

            # 按 tool_calls 的原始顺序追加结果；每条 tool 消息用 tool_call_id 对应到调用
            # zip() 会把两个序列中相同位置的元素配对
            for tc, outcome in zip(resp.tool_calls, outcomes):
                history.append({"role": "tool", "tool_call_id": tc.id, "content": outcome.text})
                payload: dict[str, Any] = {"is_error": outcome.is_error, "data": outcome.data}
                if outcome.is_error:
                    # 出错时 data 为空，把给 LLM 看的错误说明也给前端，界面上才能显示失败原因
                    payload["error"] = outcome.text
                yield AgentEvent(
                    type="tool_result", tool_name=tc.name, tool_call_id=tc.id, payload=payload
                )

    async def _execute(self, tc: ToolCall, seen_calls: Counter[str], failures: Counter[str]) -> ToolOutcome:
        if tc.parse_error:
            text = self.harness.argument_error_message(tc.name, tc.parse_error)
            return ToolOutcome(data=None, is_error=True, text=text)

        unavailable = self.harness.unavailable_tool_message(tc.name, failures)
        if unavailable:
            return ToolOutcome(data=None, is_error=True, text=unavailable)

        # 检查与计数之间没有 await，所以并行执行时也不会出现竞争
        repeat = self.harness.repeated_call_message(tc.name, tc.arguments, seen_calls)
        if repeat:
            return ToolOutcome(data=None, is_error=True, text=repeat)

        outcome = await self.tools.call_tool(tc.name, tc.arguments)
        # 只数工具真正执行后的失败；上面 harness 自己拦下的（坏 JSON、重复调用）不算——
        # 那是 LLM 的问题，不说明工具不可用。业务上的「查不到」（data 里的 error）也不算
        if outcome.is_error:
            failures[tc.name] += 1
            note = self.harness.tool_failure_note(tc.name, failures)
            if note:
                outcome = ToolOutcome(data=outcome.data, is_error=True, text=outcome.text + note)
        return outcome


def _accumulate(total: Counter[str], usage: dict[str, Any] | None) -> None:
    for key in USAGE_FIELDS:
        value = (usage or {}).get(key)
        if isinstance(value, int):
            total[key] += value
