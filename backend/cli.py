"""命令行调试入口：不经过 HTTP，直接在终端里和 agent 对话。讲解见 docs/05-agent-loop.md。

    python -m backend.cli "莉可丽丝的圣地在哪？"     # 单次提问
    python -m backend.cli                           # 交互模式，支持多轮对话，输入 exit 退出

和 FastAPI 共用同一套组件（backend/app/components.py）与同一个 ChatService，
所以终端里看到的行为就是 HTTP 接口的行为，只是把 SSE 换成了打印。
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from backend.agent.events import AgentEvent
from backend.app.chat_service import ChatService
from backend.app.components import default_components
from backend.app.config import settings

# CLI 的会话都记在这个固定访客名下（浏览器访客的 id 是随机串，不会和它重名）
CLI_OWNER = "cli"


class Renderer:
    """把事件流画到终端上。流式文字逐片打印，不换行。"""

    def __init__(self) -> None:
        self._mid_line = False

    def __call__(self, event: AgentEvent) -> None:
        if event.type == "token":
            print(event.content, end="", flush=True)
            self._mid_line = True
            return
        if self._mid_line:  # 文字流结束、开始打印工具信息前先换行
            print()
            self._mid_line = False
        if event.type == "tool_call":
            print(f"  🔧 {event.tool_name}({json.dumps(event.payload, ensure_ascii=False)})")
        elif event.type == "tool_result":
            mark = "❌" if event.payload["is_error"] else "✅"
            print(f"  {mark} {event.tool_name} → {summarize(event.payload['data'])}")
        elif event.type == "done":
            p = event.payload
            print(f"  —— {p['iterations']} 轮 LLM 调用，{p['usage'].get('total_tokens', '?')} tokens")
        elif event.type == "error":
            print(f"  ⚠️ {event.message}")


def summarize(data: Any) -> str:
    if isinstance(data, list):
        return f"{len(data)} 条结果"
    if isinstance(data, dict) and "error" in data:
        return f"错误：{data['error']}"
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return text if len(text) <= 80 else text[:80] + "…"


async def main(argv: list[str]) -> None:
    async with default_components(settings) as components:
        service = ChatService(components)
        session = None

        async def ask(question: str) -> None:
            nonlocal session
            # 第一轮新建会话，之后沿用：多轮对话的历史由 store 负责
            session = session or await service.open_session(CLI_OWNER, None, question)
            render = Renderer()
            async for event in service.run_turn(session, question):
                render(event)
            # 不在每轮后 flush：Langfuse 不可达时 flush 会阻塞约 5 秒（实测），
            # 每轮都卡一下。退出时 default_components 会调用 shutdown()，那里会把缓冲的数据发完

        if argv:
            await ask(" ".join(argv))
            return
        print("圣地巡礼助手（输入 exit 退出）")
        while True:
            # input() 会阻塞，放到线程里执行，避免卡住事件循环
            question = (await asyncio.to_thread(input, "\n你> ")).strip()
            if question.lower() in ("exit", "quit"):
                break
            if question:
                await ask(question)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
