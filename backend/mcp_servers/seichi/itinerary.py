"""生成一份巡礼行程：把数据、步行时间、电车换乘和规划算法串起来。讲解见 docs/11-pilgrimage-planner.md。

分工：
- planner.py：纯算法（挑片区、在预算内挑点排序），不碰网络
- 这里：查数据、调 OSRM 步行矩阵和 Transit 换乘（客户端由参数注入，测试时换成假的）、组装输出

输出会原样交给 LLM（写导语和每站看点）和前端（画攻略卡片和地图），
所以既要信息完整，又不能太长——每个字段都是 token。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from . import planner
from .anitabi import distance_m
from .repository import ANITABI_MAP_URL, SeichiRepository

JST = timezone(timedelta(hours=9))


class MatrixClient(Protocol):
    async def matrix(self, points: list[tuple[float, float]], mode: str) -> tuple[list[list[float]], list[list[float]], bool]: ...


class TransitPlanner(Protocol):
    async def plan(self, origin, dest, date=None, time=None) -> dict[str, Any] | None: ...


async def make_itinerary(
    repo: SeichiRepository,
    route: MatrixClient,
    transit: TransitPlanner,
    anime_id: int,
    start: tuple[float, float] | None = None,
    start_name: str | None = None,
    budget_min: int | None = None,
    date: str | None = None,
    start_time: str | None = None,
    area_id: str | None = None,
) -> dict[str, Any]:
    anime = repo.get_anime(anime_id)
    if anime is None:
        return {"error": f"没有 id 为 {anime_id} 的作品，请先用 search_anime 查 anime_id"}
    if not anime["areas"]:
        return {"error": "这部作品没有可用的圣地坐标"}

    assumptions = []
    if budget_min is None:
        budget_min = planner.DEFAULT_BUDGET_MIN
        assumptions.append(f"用户没给时间预算，按半天（{budget_min} 分钟）规划")
    locations = {loc["id"]: loc for loc in anime["locations"]}

    # 1. 选片区
    ranked = planner.rank_areas(anime["areas"], locations, start, budget_min)
    if area_id:
        chosen = next((a for a in ranked if a["id"] == area_id), None)
        if chosen is None:
            return {"error": f"没有片区 {area_id}，可选的见 other_areas"}
    else:
        chosen = ranked[0]
    cands = planner.candidates_for(chosen, anime["areas"], locations)

    # 2. 怎么到达：起点离最近的候选点够近就走过去，否则查电车
    access = None
    origin_idx = None
    points = [(c["lat"], c["lng"]) for c in cands]
    if start is not None:
        entry = min(range(len(cands)), key=lambda i: distance_m(start, points[i]))
        gap = distance_m(start, points[entry])
        if gap <= planner.WALK_ACCESS_MAX_M:
            points.append(start)  # 起点作为步行矩阵的最后一个点，路线从它出发
            origin_idx = len(cands)
        else:
            access = await _access_leg(transit, start, start_name, cands[entry], date, start_time)
            # 坐车到达的那个点作为路线起点（它在候选里，停留时间照算）
            origin_idx = None
            cands.insert(0, cands.pop(entry))
            points = [(c["lat"], c["lng"]) for c in cands]

    # 3. 步行时间矩阵（分钟）
    secs, meters, walk_estimated = await route.matrix(points, "walking")
    minutes = [[s / 60 for s in row] for row in secs]

    # 4. 在剩余时间内挑点排序
    walking_budget = budget_min - (access["duration_min"] if access else 0)
    values = [planner.spot_value(c["scenes_count"]) for c in cands]
    dwell = [planner.dwell_minutes(c["scenes_count"]) for c in cands]
    if access:
        # 坐车到达的点必须是第一站：把它固定成起点，停留时间单独扣掉
        walking_budget -= dwell[0]
        if walking_budget < 0:
            assumptions.append(
                f"单程交通约 {access['duration_min']} 分钟，到达后几乎没有剩余时间；只安排了到达的第一处"
            )
        plan = planner.plan_route(values, dwell, minutes, walking_budget, origin=0)
        order = [0, *plan.order]
        plan_travel, plan_dwell = plan.travel_min, plan.dwell_min + dwell[0]
    else:
        plan = planner.plan_route(values, dwell, minutes, walking_budget, origin=origin_idx)
        order = plan.order
        plan_travel, plan_dwell = plan.travel_min, plan.dwell_min

    if not order:
        return {"error": "时间预算太少，一个地点都去不了。可以放宽时间预算，或者换一个离得近的片区"}

    # 5. 组装：每一站、到下一站的步行时间、到达时刻
    clock = _parse_clock(start_time)
    if clock and access:
        # 用换乘结果里的到达时刻，而不是「出发时间 + 时长」：中间还有等车时间。
        # 实测 10:00 出发、车程 24 分钟，但 10:04 才有车，10:27 到——相加会算成 10:24
        clock = _parse_clock(access["arrive"]) if access.get("arrive") else clock + timedelta(minutes=access["duration_min"])
    stops = []
    prev = origin_idx
    walk_m = 0.0
    for n, i in enumerate(order, start=1):
        walk = 0.0 if prev is None else minutes[prev][i]
        walk_m += 0.0 if prev is None else meters[prev][i]
        if clock and prev is not None:
            clock += timedelta(minutes=walk)
        stops.append(_stop_out(n, cands[i], anime, dwell[i], round(walk), clock))
        if clock:
            clock += timedelta(minutes=dwell[i])
        prev = i

    route_points = ([start] if origin_idx is not None else []) + [points[i] for i in order]
    total = round(plan_travel + plan_dwell + (access["duration_min"] if access else 0))
    assumptions.append("每处停留 10~20 分钟（镜头越多停留越久），用于找机位拍照")
    if access:
        # 实测 LLM 看到「总时长」后自己补了一句「含往返电车」——要把边界写明，不能指望它猜对
        assumptions.append("总时长含去程交通，不含返程")
    if walk_estimated:
        assumptions.append("步行路线服务不可用，步行时间是直线距离估算")
    return {
        "anime_id": anime["id"],
        "anime_title": anime.get("cn") or anime.get("title"),
        "area": {"id": chosen["id"], "name": chosen["name"], "spots_in_area": len(chosen["location_ids"])},
        "start": {"name": start_name, "lat": start[0], "lng": start[1]} if start else None,
        "budget_min": budget_min,
        "total_min": total,
        "walk_km": round(walk_m / 1000, 1),
        # 同理：步行时间来自哪里要写明，否则 LLM 会一律说成「估算值」
        "walk_source": "直线距离估算" if walk_estimated else "OSRM 按实际路网计算",
        "access": access,
        "stops": stops,
        "omitted": [
            {"spot_id": cands[i]["id"], "name": cands[i].get("cn") or cands[i].get("name"), "reason": why}
            for i, why in sorted(plan.omitted.items(), key=lambda kv: -cands[kv[0]]["scenes_count"])
            if not (access and i == 0)
        ][:8],
        "other_areas": [
            {"area_id": a["id"], "name": a["name"], "spots": len(a["location_ids"]),
             "scenes": a["scenes_count"], **({"access_est_min": a["_access_est_min"]} if start else {})}
            for a in ranked if a["id"] != chosen["id"]
        ][:4],
        "walking_route_url": planner.google_maps_url(route_points, "walking") if len(route_points) > 1 else None,
        "assumptions": assumptions,
        "source": "圣地数据：Anitabi（CC BY-NC-SA 4.0）；步行：OSRM / OpenStreetMap",
    }


async def _access_leg(transit, start, start_name, target, date, start_time) -> dict[str, Any]:
    dest = (target["lat"], target["lng"])
    gmaps = planner.google_maps_url([start, dest], "transit")
    found = await transit.plan(start, dest, date=date, time=start_time)
    if found:
        return {**found, "estimated": False, "to_spot": target.get("cn") or target.get("name"),
                "google_maps_url": gmaps, "source": "Transit API（非官方，以运营商官方信息为准）"}
    # 换乘服务不可用：按直线距离粗估，明确标注
    minutes = planner.estimate_transit_min(distance_m(start, dest))
    return {"duration_min": minutes, "estimated": True, "to_spot": target.get("cn") or target.get("name"),
            "note": "换乘查询暂时不可用，时间是按直线距离粗估的，请点链接查实际换乘",
            "google_maps_url": gmaps}


def _stop_out(n, loc, anime, dwell, walk_from_prev, clock) -> dict[str, Any]:
    out = {
        "order": n,
        "spot_id": loc["id"],
        "name_ja": loc.get("name"),
        "name_cn": loc.get("cn"),
        "lat": loc["lat"],
        "lng": loc["lng"],
        "episodes": loc.get("episodes") or ([loc["ep"]] if loc.get("ep") else []),
        "scenes_count": loc.get("scenes_count", 1),
        "image": loc.get("image"),
        # 备注最多两条、每条截短：给 LLM 写注意事项用，太长会撑大上下文
        "notes": [t if len(t) <= 120 else t[:119] + "…" for t in (loc.get("notes") or [])[:2]],
        "dwell_min": dwell,
        "walk_from_prev_min": walk_from_prev,
        "origin_url": loc.get("origin_url") or ANITABI_MAP_URL.format(anime_id=anime["id"], spot_id=loc["id"]),
    }
    if clock:
        out["arrive_at"] = clock.strftime("%H:%M")
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _parse_clock(hhmm: str | None) -> datetime | None:
    if not hhmm:
        return None
    try:
        h, m = (int(x) for x in hhmm.split(":")[:2])
    except ValueError:
        return None
    return datetime.now(JST).replace(hour=h, minute=m, second=0, microsecond=0)
