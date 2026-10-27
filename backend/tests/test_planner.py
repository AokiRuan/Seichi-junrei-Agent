"""巡礼规划：片区划分、预算内选点排序、换乘客户端、整条编排。讲解见 docs/11-pilgrimage-planner.md。

全部不连网络：算法用手写的小矩阵，换乘用 MockTransport，编排用 mock 数据 + 假客户端。
"""
import httpx

from backend.mcp_servers.route.client import haversine_km
from backend.mcp_servers.route.transit import TransitClient, summarize_journey
from backend.mcp_servers.seichi import planner
from backend.mcp_servers.seichi.anitabi import build_areas
from backend.mcp_servers.seichi.itinerary import make_itinerary
from backend.mcp_servers.seichi.repository import MockRepository

# ---------------------------------------------------------------- 片区


def loc(i, lat, lng, scenes=1):
    return {"id": f"p{i}", "name": f"P{i}", "cn": None, "lat": lat, "lng": lng, "scenes_count": scenes}


def test_areas_cut_long_links_and_split_long_chains():
    # 一串每隔 ~500 米一个的点（总长约 5 km），外加 10 km 外的一个孤点
    chain = [loc(i, 35.0 + i * 0.0045, 135.0) for i in range(11)]
    far = loc(99, 35.1, 135.1)
    areas = build_areas([*chain, far], anime_id=1)
    sizes = sorted(len(a["location_ids"]) for a in areas)
    assert sizes[0] == 1  # 孤点自成一片（超过 1.4 km 的边被剪断）
    assert sum(sizes) == 12
    # 5 km 的长条被切成直径不超过 2.6 km 的几段，而不是一整片
    assert all(a["diameter_m"] <= 2600 for a in areas)
    assert len(areas) >= 3


def test_area_named_after_most_scenes_and_sorted():
    areas = build_areas([loc(1, 35.0, 135.0, 3), loc(2, 35.001, 135.0, 40), loc(3, 36.0, 135.0, 5)], 7)
    assert areas[0]["name"] == "P2" and areas[0]["id"] == "7-1"
    assert areas[0]["scenes_count"] == 43


# ---------------------------------------------------------------- 预算内选点


def test_greedy_respects_budget_and_is_not_hijacked_by_a_remote_top_spot():
    # 0 单个价值最高但很偏（离谁都要走 50 分钟）；1、2 价值中等、彼此很近，加起来比 0 高
    values = [6.0, 4.0, 4.0]
    dwell = [10, 10, 10]
    m = [[0, 50, 50], [50, 0, 5], [50, 5, 0]]
    plan = planner.plan_route(values, dwell, m, budget_min=40)
    # 拿价值最高的 0 开头只能去它一个（6 分）；试遍每个起点后选 1+2（8 分，用时 25 分钟）
    assert sorted(plan.order) == [1, 2]
    assert plan.total_min == 25 and plan.omitted[0] == "离路线上其他地点太远（最近的也要步行约 50 分钟）"
    # 预算够大时三个都去
    assert len(planner.plan_route(values, dwell, m, budget_min=200).order) == 3


def test_origin_stays_first_and_far_points_explained():
    # 3 是起点（矩阵最后一行）；2 离谁都远
    values = [3.0, 3.0, 9.0]
    dwell = [10, 10, 10]
    m = [
        [0, 5, 60, 5],
        [5, 0, 60, 8],
        [60, 60, 0, 60],
        [5, 8, 60, 0],
    ]
    plan = planner.plan_route(values, dwell, m, budget_min=60, origin=3)
    assert plan.order and 3 not in plan.order
    assert 2 in plan.omitted and "太远" in plan.omitted[2]


def test_two_opt_removes_crossing():
    # 一条直线上的 4 个点，故意给一个交叉的顺序
    pos = [0, 1, 2, 3]
    m = [[abs(a - b) * 10 for b in pos] for a in pos]
    assert planner._two_opt([0, 2, 1, 3], m, fixed_first=True) == [0, 1, 2, 3]


def test_rank_areas_trades_value_against_access_time():
    locations = {f"a{i}": {"scenes_count": 50} for i in range(10)} | {f"b{i}": {"scenes_count": 40} for i in range(10)}
    far = {"id": "far", "location_ids": [f"a{i}" for i in range(10)], "lat": 35.0, "lng": 139.0}  # 价值稍高但远
    near = {"id": "near", "location_ids": [f"b{i}" for i in range(10)], "lat": 35.68, "lng": 139.70}
    start = (35.69, 139.70)
    assert planner.rank_areas([far, near], locations, None, 240)[0]["id"] == "far"
    assert planner.rank_areas([far, near], locations, start, 240)[0]["id"] == "near"


def test_google_maps_url():
    url = planner.google_maps_url([(35.0, 139.0), (35.1, 139.1), (35.2, 139.2)], "walking")
    assert "origin=35.000000,139.000000" in url and "destination=35.200000,139.200000" in url
    assert "waypoints=35.100000,139.100000" in url and "travelmode=walking" in url


# ---------------------------------------------------------------- 换乘客户端

JOURNEY = {
    "departureSecs": 36240, "arrivalSecs": 37620, "durationSecs": 1380, "transferCount": 1,
    "accessWalkSecs": 60, "egressWalkSecs": 300,
    "legs": [
        {"kind": "transit", "routeName": "山手線（内回り）", "headsign": "大崎", "from": {"name": "新宿"},
         "to": {"name": "渋谷"}, "departureSecs": 36240, "arrivalSecs": 36660},
        {"kind": "walk", "from": {"name": "渋谷"}, "to": {"name": "渋谷"}, "departureSecs": 36660, "arrivalSecs": 36780},
        {"kind": "transit", "routeName": "IN", "headsign": "吉祥寺", "from": {"name": "渋谷"},
         "to": {"name": "池ノ上"}, "departureSecs": 36900, "arrivalSecs": 37320},
    ],
}


def test_summarize_journey():
    s = summarize_journey(JOURNEY)
    assert (s["depart"], s["arrive"], s["duration_min"], s["transfers"]) == ("10:04", "10:27", 23, 1)
    assert [r["line"] for r in s["rides"]] == ["山手線（内回り）", "IN"]
    assert s["walk_min"] == 8  # 换乘步行 2 分 + 进站 1 分 + 出站 5 分


async def test_transit_client_caches_and_degrades():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if "fail" in str(req.url):
            return httpx.Response(503)
        return httpx.Response(200, json={"journeys": [JOURNEY, {**JOURNEY, "arrivalSecs": 39000}]})

    client = TransitClient(transport=httpx.MockTransport(handler))
    a = await client.plan((35.69, 139.70), (35.66, 139.67), date="20261031", time="10:00")
    b = await client.plan((35.69, 139.70), (35.66, 139.67), date="20261031", time="10:00")
    assert a == b and a["arrive"] == "10:27"  # 取最早到达的方案
    assert len(calls) == 1  # 第二次走缓存
    assert calls[0].url.params["from"] == "geo:35.690000,139.700000"

    # 服务出错：返回 None（由调用方退回估算），不抛异常
    client._transport = httpx.MockTransport(lambda r: httpx.Response(503))
    assert await client.plan((1.0, 1.0), (2.0, 2.0)) is None


# ---------------------------------------------------------------- 整条编排（mock 数据 + 假客户端）


class FakeRoute:
    """按直线距离算步行时间，代替 OSRM"""

    async def matrix(self, points, mode):
        secs = [[haversine_km(*a, *b) / 4.5 * 3600 for b in points] for a in points]
        meters = [[haversine_km(*a, *b) * 1000 for b in points] for a in points]
        return secs, meters, False


class FakeTransit:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def plan(self, origin, dest, date=None, time=None):
        self.calls.append((origin, dest, date, time))
        return self.result


async def test_itinerary_without_start_uses_best_area():
    r = await make_itinerary(MockRepository(), FakeRoute(), FakeTransit(None), anime_id=115908)
    assert r["area"]["id"] == "115908-1" and r["access"] is None
    assert [s["order"] for s in r["stops"]] == list(range(1, len(r["stops"]) + 1))
    assert r["total_min"] <= r["budget_min"] == 240
    assert "半天" in r["assumptions"][0]
    assert r["walking_route_url"].startswith("https://www.google.com/maps/dir/")


async def test_itinerary_from_far_start_takes_train_with_real_arrival_time():
    transit = FakeTransit({"depart": "09:34", "arrive": "10:06", "duration_min": 32, "transfers": 2,
                           "walk_min": 6, "rides": []})
    kyoto = (34.985849, 135.758767)
    r = await make_itinerary(MockRepository(), FakeRoute(), transit, anime_id=115908, start=kyoto,
                             start_name="京都駅", budget_min=180, date="20261031", start_time="09:30")
    assert transit.calls and transit.calls[0][2:] == ("20261031", "09:30")
    assert r["access"]["estimated"] is False and r["access"]["google_maps_url"]
    assert r["stops"][0]["arrive_at"] == "10:06"  # 用换乘结果的到达时刻，不是 09:30 + 32 分
    assert r["total_min"] <= 180


async def test_itinerary_falls_back_to_estimate_when_transit_down():
    r = await make_itinerary(MockRepository(), FakeRoute(), FakeTransit(None), anime_id=115908,
                             start=(34.985849, 135.758767), budget_min=180)
    assert r["access"]["estimated"] is True and "粗估" in r["access"]["note"]


async def test_itinerary_errors_are_data():
    assert "error" in await make_itinerary(MockRepository(), FakeRoute(), FakeTransit(None), anime_id=999)
    assert "error" in await make_itinerary(MockRepository(), FakeRoute(), FakeTransit(None), anime_id=115908,
                                           area_id="nope")
