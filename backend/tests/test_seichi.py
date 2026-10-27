"""圣地巡礼 MCP server 测试。讲解见 docs/03-mcp-server.md。"""
from backend.mcp_servers.seichi import server
from backend.mcp_servers.seichi.repository import MockRepository
from backend.tests.stdio_helper import stdio_session


async def test_stdio_roundtrip_as_subprocess():
    """走真实协议：把 server 拉成子进程，经 stdin/stdout 的 JSON-RPC 调用工具。
    这正是 Day 2 的 mcp_client.py 要做的事。"""
    async with stdio_session("backend.mcp_servers.seichi.server") as session:
        result = await session.call_tool("search_anime", {"title": "莉可丽丝"})

    assert result.is_error is False
    # 返回 list 的工具，结构化结果会被包成 {"result": [...]}
    assert result.structured_content["result"][0]["anime_id"] == 364450


async def test_three_tools_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == {"search_anime", "list_spots", "get_spot"}


async def test_tool_schema_generated_from_type_hints():
    # MCP SDK 从函数签名自动生成 JSON Schema —— 这就是 learn/tools/registry.py 手工拼的那部分
    tools = {t.name: t for t in await server.mcp.list_tools()}
    schema = tools["list_spots"].input_schema
    assert schema["properties"]["anime_id"]["type"] == "integer"
    assert schema["required"] == ["anime_id"]


def test_search_by_chinese_japanese_and_city():
    assert server.search_anime("莉可丽丝")[0]["anime_id"] == 364450
    assert server.search_anime("ユーフォニアム")[0]["anime_id"] == 115908
    assert server.search_anime("宇治")[0]["anime_id"] == 115908


def test_search_miss_and_blank_return_empty():
    assert server.search_anime("不存在的作品") == []
    assert server.search_anime("   ") == []


def test_search_result_excludes_points():
    # 摘要不带地标列表，避免一次把 LLM 上下文塞满
    hit = server.search_anime("莉可丽丝")[0]
    assert "points" not in hit
    assert hit["spots_count"] == 5


def test_list_spots_shape():
    result = server.list_spots(364450)
    # 返回总数与本次返回数：LLM 据此知道「还有没列出的」
    assert result["total_spots"] == 5 and result["shown"] == 5
    assert result["anime_title"] == "莉可丽丝"  # 作品级字段只在顶层写一次
    first = result["spots"][0]
    for key in ("spot_id", "name_ja", "lat", "lng", "episode", "scenes_count"):
        assert key in first
    # 为省 token，空值和作品级字段不逐个输出
    assert "anime_title" not in first and None not in first.values()


def test_list_spots_limit_and_near_filter():
    assert server.list_spots(364450, limit=2)["shown"] == 2
    # 浅草寺雷门附近 1 公里：晴空塔约 1.3km 不在内，雷门本身在内
    near = server.list_spots(364450, near_lat=35.7108, near_lng=139.7967, radius_km=1)
    names = [s["name_cn"] for s in near["spots"]]
    assert "浅草寺 雷门" in names and "东京晴空塔" not in names
    assert near["total_spots"] == len(near["spots"])
    assert all(s["distance_km"] <= 1 for s in near["spots"])


def test_list_spots_unknown_anime_returns_error():
    assert "error" in server.list_spots(999999)


def test_search_tolerates_a_wrong_character():
    # docs/10 实测：LLM 写成「吹响！上低音号」（少了「吧」），旧的子串匹配搜不到
    assert server.search_anime("吹响！上低音号")[0]["anime_id"] == 115908


def test_get_spot_includes_scenes():
    spot_id = server.list_spots(364450)["spots"][0]["spot_id"]
    detail = server.get_spot(spot_id)
    assert detail["spot_id"] == spot_id
    assert len(detail["scenes"]) == 1 and "episode" in detail["scenes"][0]


def test_unknown_spot_returns_error_instead_of_raising():
    # 返回 error 而不是抛异常：让 LLM 自己决定重试还是告诉用户
    assert "error" in server.get_spot("bad-id")


def test_tests_use_mock_data():
    # conftest.py 固定了 SEICHI_DATA_SOURCE=mock：本地有没有快照都不影响测试
    assert isinstance(server.repo, MockRepository)


def test_every_mock_spot_has_coordinates():
    repo = MockRepository()
    for anime in repo._anime():
        for spot in repo.list_spots(anime["id"], limit=50)["spots"]:
            assert spot["lat"] is not None and spot["lng"] is not None, spot["spot_id"]
