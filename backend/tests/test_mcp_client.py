"""MCP 客户端测试：真实拉起 server 子进程（只调不联网的工具）。讲解见 docs/07-mcp-client.md。

注意：这里没有用 async fixture（yield 前后打开/关闭连接）。pytest-asyncio 会在
不同的 task 里执行 fixture 的 setup 和 teardown，而 MCP SDK（anyio）要求连接
在同一个 task 里打开和关闭，否则 teardown 时报 "Attempted to exit cancel scope
in a different task"。所以每个测试自己用 async with。
"""
import pytest

from backend.agent.mcp_client import DEFAULT_SERVERS, MCPClientPool, ServerSpec

SEICHI_ONLY = [DEFAULT_SERVERS[0]]  # 只起一个子进程，测试更快


async def test_aggregates_tools_from_all_servers():
    async with MCPClientPool() as pool:
        names = [s["function"]["name"] for s in pool.tool_schemas()]
        assert sorted(names) == ["geocode", "get_spot", "get_weather_forecast", "list_spots", "plan_pilgrimage", "plan_route", "search_anime"]
        assert pool.tool_owner("plan_route") == "route"
        assert pool.tool_owner("search_anime") == "seichi"
        for s in pool.tool_schemas():
            assert "\n    " not in s["function"]["description"], "docstring 缩进应已清理"


async def test_list_result_is_unwrapped():
    async with MCPClientPool(SEICHI_ONLY) as pool:
        out = await pool.call_tool("search_anime", {"title": "莉可丽丝"})
    assert out.is_error is False
    assert isinstance(out.data, list) and out.data[0]["anime_id"] == 364450
    assert '"anime_id": 364450' in out.text  # 给 LLM 的是 JSON 文本，中文不转义


async def test_error_kinds():
    async with MCPClientPool(SEICHI_ONLY) as pool:
        invalid = await pool.call_tool("list_spots", {"anime_id": "not-a-number"})
        unknown = await pool.call_tool("no_such_tool", {})
        not_found = await pool.call_tool("get_spot", {"spot_id": "bad-id"})

    # 参数校验失败：协议层错误，校验详情传给了 LLM，它可以据此修正
    assert invalid.is_error is True and "valid integer" in invalid.text
    # 工具不存在：告诉 LLM 有哪些工具可用
    assert unknown.is_error is True and "search_anime" in unknown.text
    # 业务上的「查不到」：工具正常返回了数据，不是协议错误
    assert not_found.is_error is False and "error" in not_found.data


async def test_duplicate_tool_names_fail_fast():
    seichi = DEFAULT_SERVERS[0]
    p = MCPClientPool([seichi, ServerSpec("seichi-copy", seichi.module)])
    with pytest.raises(ValueError, match="工具名冲突"):
        await p.connect_all()
    assert p.tool_schemas() == []  # 失败时已自行清理，没有留下子进程
