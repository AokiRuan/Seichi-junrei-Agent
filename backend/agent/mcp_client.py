"""MCP 客户端聚合层。讲解见 docs/07-mcp-client.md。

职责：
1. 把每个 MCP server 拉成 stdio 子进程，完成握手与工具发现
2. 把所有 server 的工具汇总成一份 OpenAI 格式的 tool schema 给 LLM
3. 按工具名把调用分发到对应的 server，并把结果整理成 LLM 能读的文本

对 loop 来说，工具来自几个 server、怎么通信都是透明的：它只调用
tool_schemas() 和 call_tool()。
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# stdio 子进程默认只继承一小撮系统环境变量（PATH、APPDATA 等），
# 代理设置不在其中——不显式传入的话，天气/路线 server 在代理环境下会连不上外网
PASSTHROUGH_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


@dataclass(frozen=True)
class ServerSpec:
    name: str
    module: str  # 以 python -m 启动的模块路径


DEFAULT_SERVERS = (
    ServerSpec("seichi", "backend.mcp_servers.seichi.server"),
    ServerSpec("weather", "backend.mcp_servers.weather.server"),
    ServerSpec("route", "backend.mcp_servers.route.server"),
)


@dataclass
class ToolOutcome:
    """一次工具调用的结果。"""

    data: Any  # 结构化结果（已解包），供前端展示 / 地图打点
    is_error: bool  # 协议层面的失败：工具不存在、参数校验失败、工具内部异常、进程通信失败
    text: str  # 回填给 LLM 的文本


class ToolExecutor(Protocol):
    """loop 对工具层的全部要求。测试时可以用一个假实现替换。"""

    def tool_schemas(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome: ...


class MCPClientPool:
    def __init__(self, servers: tuple[ServerSpec, ...] | list[ServerSpec] = DEFAULT_SERVERS) -> None:
        self._servers = list(servers)
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}  # 工具名 -> 所属 server 的会话
        self._owner: dict[str, str] = {}  # 工具名 -> server 名，用于报错与排查
        self._schemas: list[dict[str, Any]] = []

    async def connect_all(self) -> None:
        """启动所有 server 并完成工具发现。应在应用启动时调用一次。

        中途失败时会关掉已经启动的子进程再抛出异常，不留孤儿进程。
        """
        try:
            await self._connect_all()
        except BaseException:
            await self.close()
            raise

    async def _connect_all(self) -> None:
        for spec in self._servers:
            session = await self._stack.enter_async_context(_open_session(spec))
            result = await session.list_tools()
            for tool in result.tools:
                if tool.name in self._sessions:
                    # 启动时就失败，而不是运行时悄悄调到另一个 server 的同名工具
                    raise ValueError(
                        f"工具名冲突：{tool.name} 同时存在于 "
                        f"{self._owner[tool.name]} 和 {spec.name}"
                    )
                self._sessions[tool.name] = session
                self._owner[tool.name] = spec.name
                self._schemas.append(_to_openai_schema(tool))

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def tool_owner(self, name: str) -> str | None:
        return self._owner.get(name)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        session = self._sessions.get(name)
        if session is None:
            msg = f"不存在名为 {name} 的工具。可用工具：{', '.join(self._sessions)}"
            return ToolOutcome(data=None, is_error=True, text=msg)

        try:
            result = await session.call_tool(name, arguments)
        except Exception as e:  # noqa: BLE001 —— 子进程崩溃、管道断开等，不能让它打断对话
            msg = f"工具 {name} 调用失败（{type(e).__name__}）"
            return ToolOutcome(data=None, is_error=True, text=msg)

        # 跨进程调用时，工具出错不会抛异常，而是 is_error=True —— 必须检查这个字段
        if result.is_error:
            text = _content_text(result) or f"工具 {name} 执行出错"
            return ToolOutcome(data=None, is_error=True, text=text)

        data = _unwrap(result.structured_content)
        if data is None:
            data = _content_text(result)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return ToolOutcome(data=data, is_error=False, text=text)

    async def close(self) -> None:
        """关闭所有会话并结束子进程。"""
        await self._stack.aclose()
        self._sessions.clear()
        self._owner.clear()
        self._schemas.clear()

    # 推荐用法：async with MCPClientPool() as pool: ...
    # MCP SDK 基于 anyio，要求连接在**同一个 task** 里打开和关闭。
    # async with 天然保证这一点；手动调用 connect_all / close 时要自己保证。
    async def __aenter__(self) -> MCPClientPool:
        await self.connect_all()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


def _open_session(spec: ServerSpec):
    env = {"PYTHONIOENCODING": "utf-8"}
    env.update({k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ})
    params = StdioServerParameters(
        command=sys.executable,  # 用当前 venv 的 Python，保证子进程能 import 到同样的依赖
        args=["-m", spec.module],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    return _SessionContext(params)


class _SessionContext:
    """把 stdio_client 与 ClientSession 两层 async with 合成一层，并完成 initialize。"""

    def __init__(self, params: StdioServerParameters) -> None:
        self._params = params
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> ClientSession:
        try:
            read, write = await self._stack.enter_async_context(stdio_client(self._params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return session
        except BaseException:
            await self._stack.aclose()
            raise

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()


def _to_openai_schema(tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            # docstring 里的缩进会原样带进描述，每次调用都白白消耗 token，清理掉
            "description": inspect.cleandoc(tool.description or ""),
            "parameters": tool.input_schema,
        },
    }


def _unwrap(structured: Any) -> Any:
    """返回 list 等非对象值的工具，结构化结果会被 SDK 包成 {"result": ...}，这里拆掉。
    set()把字典的所有键收成一个集合"""
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


def _content_text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


async def _print_tools() -> None:
    async with MCPClientPool() as pool:
        for schema in pool.tool_schemas():
            fn = schema["function"]
            params = ", ".join(fn["parameters"].get("properties", {}))
            print(f"{pool.tool_owner(fn['name']):8} {fn['name']}({params})")


if __name__ == "__main__":
    # 调试用：列出 LLM 实际能看到的全部工具。python -m backend.agent.mcp_client
    import asyncio

    asyncio.run(_print_tools())
