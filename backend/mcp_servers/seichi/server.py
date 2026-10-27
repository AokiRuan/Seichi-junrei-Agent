"""圣地巡礼 MCP Server（自建）。

用 MCP SDK 2.x + stdio 传输，由后端作为子进程拉起。讲解见 docs/03-mcp-server.md。

必须以模块方式启动（本文件用了相对导入，按文件路径启动会报错）：

    python -m backend.mcp_servers.seichi.server

stdio 模式下 stdout 是 JSON-RPC 通道——本文件里任何 print() 都会破坏协议，调试信息请写 stderr。

数据源见 repository.py：默认读 Anitabi 本地快照（python -m backend.mcp_servers.seichi.anitabi 生成），
没有快照时退回 mock 数据。
"""
from typing import Annotated, Any

# 注意：MCP SDK 2.x 把 FastMCP 改名为 MCPServer，网上多数教程还是 1.x 的写法
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .repository import get_repository

mcp = MCPServer("seichi")
repo = get_repository()


@mcp.tool()
def search_anime(title: str) -> list[dict[str, Any]]:
    """按作品名搜索动画，返回作品 id 与基本信息。

    拿到 anime_id 后用 list_spots 查该作品的圣地。
    支持中文名、日文原名、英文名、城市名，能容忍个别字写错。
    结果按匹配度排序；返回多部时先看第一部是否就是用户说的那部。
    """
    return repo.search_anime(title)


@mcp.tool()
def list_spots(
    anime_id: int,
    limit: Annotated[int, Field(ge=1, le=50, description="最多返回几处")] = 15,
    near_lat: Annotated[float | None, Field(ge=-90, le=90, description="只看这个点附近：纬度")] = None,
    near_lng: Annotated[float | None, Field(ge=-180, le=180, description="只看这个点附近：经度")] = None,
    radius_km: Annotated[float, Field(gt=0, le=50, description="附近的范围，公里")] = 3,
) -> dict[str, Any]:
    """列出某作品的圣地：名称、经纬度、出场的集数、镜头数。

    同一地点的多个镜头已合并成一处，按镜头数从多到少排序（镜头越多通常越有代表性）。
    结果里 total_spots 是总数、shown 是本次返回数——热门作品可能有几十处，只返回前 limit 处。
    用户问「某个城市 / 车站附近的圣地」时，先用 geocode 查到那里的坐标，
    再传 near_lat / near_lng 只看附近的，结果会带 distance_km。
    anime_id 来自 search_anime 的返回结果。
    """
    near = (near_lat, near_lng) if near_lat is not None and near_lng is not None else None
    result = repo.list_spots(anime_id, limit=limit, near=near, radius_km=radius_km)
    if result is None:
        return {"error": f"没有 id 为 {anime_id} 的作品，请先用 search_anime 查 anime_id"}
    return result


@mcp.tool()
def get_spot(spot_id: str) -> dict[str, Any]:
    """查询单个圣地的详情，包括它在动画里的各个镜头（集数、时间点、截图）。

    spot_id 来自 list_spots 的返回结果。
    """
    spot = repo.get_spot(spot_id)
    if spot is None:
        # 返回而不是抛异常——让 LLM 自己决定是换个 id 重试还是告诉用户查不到
        return {"error": f"未找到圣地 {spot_id}"}
    return spot


if __name__ == "__main__":
    mcp.run(transport="stdio")
