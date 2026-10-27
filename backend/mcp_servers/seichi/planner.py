"""巡礼规划算法：挑片区、在时间预算内挑点并排顺序。讲解见 docs/11-pilgrimage-planner.md。

全部是纯函数，不发请求：步行时间矩阵、电车时间由调用方（server.py 的 plan_pilgrimage）查好传进来。
这样算法可以用固定输入做确定性测试，也方便以后做评测（同样的输入一定得到同样的路线）。

为什么不让 LLM 自己挑点排路线：它不擅长组合优化和算距离，结果不稳定；而且要把几十个地点的数据
全塞进上下文，一轮要上万 token。参考的几个开源巡礼规划项目也是同样的分工——
「距离、时间的计算由确定性代码负责，LLM 负责理解需求和写攻略」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .anitabi import distance_m

# 默认时间预算：用户没说的话按半天算
DEFAULT_BUDGET_MIN = 240
# 起点离第一个地点在这个距离以内就直接走过去，否则需要坐车
WALK_ACCESS_MAX_M = 1500
# 相邻片区的中心离主片区中心在这个距离以内，就一起作为候选（时间够的话可以顺路去）
NEARBY_AREA_M = 2500
# 送进步行时间矩阵的候选上限：OSRM 公共实例对一次 table 请求的坐标数有限制，候选太多也没意义
MAX_CANDIDATES = 20
# 单段步行超过这么久，就不值得为它绕路
MAX_WALK_LEG_MIN = 30


def dwell_minutes(scenes_count: int) -> int:
    """在一个地点停留多久（找机位、拍照）。镜头越多的地点，值得拍的角度越多。"""
    if scenes_count >= 20:
        return 20
    if scenes_count >= 5:
        return 15
    return 10


def spot_value(scenes_count: int) -> float:
    """一个地点的「价值」。用对数而不是镜头数本身：

    北宇治高校一处就有 214 个镜头，按镜头数算它一个点抵得上其他二十个点，
    规划器会为了它牺牲其他所有点。对数让价值随镜头数增长但递减。
    """
    return 1 + math.log2(max(scenes_count, 1))


def estimate_transit_min(meters: float) -> int:
    """坐电车的粗估（查不到真实换乘时用）：等车 + 换乘 15 分钟，加上平均 35 km/h 的行驶时间。

    只用于挑片区时比较远近，以及换乘接口不可用时兜底。结果里一定标注 estimated。
    """
    if meters <= WALK_ACCESS_MAX_M:
        return round(meters * 1.25 / 4500 * 60)  # 1.25：直线 → 实际步行距离的经验系数
    return round(15 + meters / 1000 / 35 * 60)


# ---------------------------------------------------------------- 选片区


def rank_areas(
    areas: list[dict[str, Any]],
    locations: dict[str, dict[str, Any]],
    start: tuple[float, float] | None,
    budget_min: int,
) -> list[dict[str, Any]]:
    """给片区排序，第一个就是推荐的。

    - 价值：片区里最热门的 10 个地点的价值之和（不是全部：去不完的点不该加分）
    - 没给起点：只看价值
    - 给了起点：路上花的时间越多，留给巡礼的时间越少，价值按「剩余时间比例」打折；
      单程超过预算一半的片区直接排到最后
    """
    ranked = []
    for area in areas:
        top = [locations[i] for i in area["location_ids"][:10]]
        value = sum(spot_value(loc["scenes_count"]) for loc in top)
        access = estimate_transit_min(distance_m(start, (area["lat"], area["lng"]))) if start else 0
        feasible = access <= budget_min / 2
        score = value * max(0.0, 1 - access / budget_min) if feasible else -access
        ranked.append({**area, "_value": value, "_access_est_min": access, "_score": score})
    ranked.sort(key=lambda a: -a["_score"])
    return ranked


def candidates_for(
    area: dict[str, Any], areas: list[dict[str, Any]], locations: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """候选地点：主片区的全部，加上中心离得不远的相邻片区。按价值从高到低，最多 MAX_CANDIDATES 个。"""
    ids = list(area["location_ids"])
    for other in areas:
        if other["id"] != area["id"] and distance_m((area["lat"], area["lng"]), (other["lat"], other["lng"])) <= NEARBY_AREA_M:
            ids.extend(other["location_ids"])
    locs = sorted((locations[i] for i in dict.fromkeys(ids)), key=lambda loc: -loc["scenes_count"])
    return locs[:MAX_CANDIDATES]


# ---------------------------------------------------------------- 在预算内挑点、排顺序


@dataclass
class RoutePlan:
    order: list[int]  # 候选下标的访问顺序（不含起点）
    travel_min: float
    dwell_min: float
    omitted: dict[int, str] = field(default_factory=dict)  # 候选下标 → 没去的原因

    @property
    def total_min(self) -> float:
        return self.travel_min + self.dwell_min


def plan_route(
    values: list[float],
    dwell: list[int],
    minutes: list[list[float]],
    budget_min: float,
    origin: int | None = None,
) -> RoutePlan:
    """在时间预算内挑点并排顺序（一条不回头的路线）。

    这是「定向越野问题」（Orienteering Problem）：每个点有价值，走路和停留都花时间，
    在总时间不超过预算的前提下让价值之和最大。精确求解是 NP 难的，这里用两步启发式：

    1. 贪心插入：每一轮试遍「哪个点 × 插在哪个位置」，选「价值 ÷ 新增时间」最高、且不超预算的那个
    2. 2-opt：把路线中间的一段反过来，如果总时间变短就保留，直到改不动为止

    参数：
    - values / dwell：每个候选点的价值、停留分钟
    - minutes：步行时间矩阵（分钟）。有起点时，最后一行/列是起点
    - origin：起点在矩阵里的下标；None 表示从某个地点出发（由算法选价值最高的点开头）
    """
    n = len(values)
    if origin is None:
        # 没有起点：每个点都试一次当第一站，取总价值最高的方案（同价值取用时短的）。
        # 不能简单地拿价值最高的点开头——它要是很偏（单程走 50 分钟），整条路线都会被它绑架
        plans = [_greedy(values, dwell, minutes, budget_min, [s]) for s in range(n) if dwell[s] <= budget_min]
        if not plans:
            return RoutePlan(order=[], travel_min=0, dwell_min=0, omitted={i: "时间预算不够" for i in range(n)})
        return max(plans, key=lambda p: (sum(values[i] for i in p.order), -p.total_min))
    return _greedy(values, dwell, minutes, budget_min, [origin], origin=origin)


def _greedy(values, dwell, minutes, budget_min, path, origin=None) -> RoutePlan:
    n = len(values)
    path = list(path)

    def cost(p: list[int]) -> float:
        travel = sum(minutes[a][b] for a, b in zip(p, p[1:]))
        return travel + sum(dwell[i] for i in p if i != origin)

    while True:
        best = None  # (比值, 点, 位置)
        current = cost(path)
        for c in range(n):
            if c in path:
                continue
            # 有起点时起点固定在最前面，不能插到它前面
            for pos in range(1 if origin is not None else 0, len(path) + 1):
                trial = path[:pos] + [c] + path[pos:]
                added = cost(trial) - current
                if current + added > budget_min:
                    continue
                ratio = values[c] / max(added, 1e-6)
                if best is None or ratio > best[0]:
                    best = (ratio, c, pos)
        if best is None:
            break
        path.insert(best[2], best[1])

    path = _two_opt(path, minutes, fixed_first=origin is not None)

    visited = [i for i in path if i != origin]
    omitted = {}
    for c in range(n):
        if c in visited:
            continue
        nearest = min((minutes[c][v] for v in path), default=math.inf)
        if nearest > MAX_WALK_LEG_MIN:
            omitted[c] = f"离路线上其他地点太远（最近的也要步行约 {round(nearest)} 分钟）"
        else:
            omitted[c] = "时间预算不够"
    travel = sum(minutes[a][b] for a, b in zip(path, path[1:]))
    return RoutePlan(order=visited, travel_min=travel, dwell_min=sum(dwell[i] for i in visited), omitted=omitted)


def _two_opt(path: list[int], minutes: list[list[float]], fixed_first: bool) -> list[int]:
    """反转路线中的一段，能缩短就保留。开放路径（不回起点）版本。"""

    def length(p):
        return sum(minutes[a][b] for a, b in zip(p, p[1:]))

    best = path[:]
    start = 1 if fixed_first else 0
    improved = True
    while improved:
        improved = False
        for i in range(start, len(best) - 1):
            for j in range(i + 1, len(best)):
                trial = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                if length(trial) < length(best) - 1e-9:
                    best, improved = trial, True
    return best


# ---------------------------------------------------------------- 导航链接


def google_maps_url(points: list[tuple[float, float]], mode: str) -> str:
    """Google Maps 的路线链接（不需要 API key）。点开就能在手机上导航，公交模式会显示实时换乘。

    URL 规范：https://developers.google.com/maps/documentation/urls/get-started
    中途点最多 9 个（URL 方案的限制），超出的截掉——链接只是方便导航，完整顺序以攻略为准
    """
    fmt = lambda p: f"{p[0]:.6f},{p[1]:.6f}"  # noqa: E731
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={fmt(points[0])}&destination={fmt(points[-1])}&travelmode={mode}"
    )
    middle = points[1:-1][:9]
    if middle:
        url += "&waypoints=" + "%7C".join(fmt(p) for p in middle)
    return url
