"""电车换乘查询客户端。讲解见 docs/11-pilgrimage-planner.md「电车：Transit API」。

用的是 Transit API（https://api.transit.ls8h.com/）：免费、不需要 key 的日本换乘搜索。
数据来自 GTFS、ODPT、各运营商等（1100 多个数据源，含 JR、东京 Metro、私铁、京阪……）。

它是**个人维护的非官方服务**，没有可用性承诺（实测单次 2~30 秒），条款要求：
不要大量请求、标注非官方、提醒用户以官方信息为准。所以这里：

- 同样的查询只发一次（内存缓存）；两次请求至少间隔 1 秒
- 超时、出错一律返回 None，由调用方退回估算——它挂了不能让整个规划失败
- 结果里带上 source，前端据此标注「非官方，以官方信息为准」

为什么不用 ODPT（公共交通开放数据中心，官方、免费注册）：它只提供时刻表等原始数据，
不提供「A 到 B 怎么坐」的路线搜索，要自己搭 OpenTripPlanner 这样的引擎。
"""
from __future__ import annotations

from typing import Any

import httpx

from .client import USER_AGENT, _Throttle

TRANSIT_BASE = "https://api.transit.ls8h.com"
SOURCE = "Transit API（api.transit.ls8h.com，非官方，以运营商官方信息为准）"


def _hhmm(secs: int) -> str:
    # 时间是「距当天 0 点的秒数」，跨过午夜的末班车会超过 86400
    h, m = divmod(secs // 60, 60)
    return f"{h % 24:02d}:{m:02d}"


def summarize_journey(j: dict[str, Any]) -> dict[str, Any]:
    """把一个换乘方案压缩成攻略需要的样子：几点出发、几点到、坐哪几条线、在哪站上下车。"""
    rides = [leg for leg in j.get("legs", []) if leg.get("kind") == "transit"]
    walk_secs = sum(
        leg["arrivalSecs"] - leg["departureSecs"] for leg in j.get("legs", []) if leg.get("kind") == "walk"
    ) + j.get("accessWalkSecs", 0) + j.get("egressWalkSecs", 0)
    return {
        "depart": _hhmm(j["departureSecs"]),
        "arrive": _hhmm(j["arrivalSecs"]),
        "duration_min": round(j["durationSecs"] / 60),
        "transfers": j.get("transferCount", max(len(rides) - 1, 0)),
        "walk_min": round(walk_secs / 60),
        "rides": [
            {
                # routeName 有时只是线路代码（如京王的「IN」），headsign（方向）更好懂，一起给
                "line": leg.get("routeName"),
                "headsign": leg.get("headsign"),
                "from": leg["from"]["name"],
                "to": leg["to"]["name"],
                "depart": _hhmm(leg["departureSecs"]),
                "arrive": _hhmm(leg["arrivalSecs"]),
            }
            for leg in rides
        ],
    }


class TransitClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0) -> None:
        self._transport = transport
        self._timeout = timeout
        self._throttle = _Throttle(1.0)
        self._cache: dict[tuple, dict[str, Any] | None] = {}

    async def plan(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
        date: str | None = None,
        time: str | None = None,
    ) -> dict[str, Any] | None:
        """最快到达的一个方案；查不到或服务不可用时返回 None。

        date：YYYYMMDD；time：HH:MM（出发时间）。不传时由服务按当前时间算。
        """
        # 坐标保留 4 位小数（约 10 米）作缓存 key：同一个起点稍微偏一点不必重新查
        key = (round(origin[0], 4), round(origin[1], 4), round(dest[0], 4), round(dest[1], 4), date, time)
        if key in self._cache:
            return self._cache[key]

        params = {"from": f"geo:{origin[0]:.6f},{origin[1]:.6f}", "to": f"geo:{dest[0]:.6f},{dest[1]:.6f}"}
        if date:
            params["date"] = date
        if time:
            params["time"] = time
        await self._throttle.wait()
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout, headers={"User-Agent": USER_AGENT}
            ) as http:
                resp = await http.get(f"{TRANSIT_BASE}/api/v1/plan", params=params)
                resp.raise_for_status()
                journeys = resp.json().get("journeys") or []
        except (httpx.HTTPError, ValueError):
            return None  # 不缓存失败：下次也许就好了

        result = None
        if journeys:
            best = min(journeys, key=lambda j: j["arrivalSecs"])
            result = summarize_journey(best)
        self._cache[key] = result
        return result
