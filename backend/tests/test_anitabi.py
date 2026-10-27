"""Anitabi 快照的解析、合并与查询。讲解见 docs/03-mcp-server.md「真实数据：Anitabi 快照」。

用手写的原始数组（格式与线上文件一致）测试，不连网络。
最后一个测试读本地真实快照，没有快照时跳过。
"""
import json
from datetime import datetime, timezone

import pytest

from backend.mcp_servers.seichi import anitabi
from backend.mcp_servers.seichi.anitabi import (
    build_snapshot,
    clean,
    day_param,
    group_locations,
    image_url,
    parse_points,
)
from backend.mcp_servers.seichi.repository import AnitabiRepository


def raw_anime(aid, flat_points, cn="作品", title="作品", city="宇治市"):
    """g.json 里的一部作品：18 个字段，第 13 个是 [id, 纬度, 经度, 优先级, ...] 扁平数组"""
    return [aid, cn, 0, title, city, "#000", f"/images/bangumi/{aid}.jpg", 8, "TV",
            34.9, 135.8, 12, flat_points, 0, 0, None, "/images/icon/x.jpg", 0]


def raw_point(pid, name, cn=0, is_folder=0, fid=0, ep=None, s=""):
    """g{n}.json 里的一个地标：15 个字段，没有坐标"""
    return [pid, name, cn, is_folder, 0, 1, f"/images/points/1/{pid}.jpg", fid, ep, s, 0, 0, 0, 0, 10]


# 一个文件夹（北宇治高校，带 2 个镜头）、宇治桥的 2 个镜头（相距约 30 米）、远处一个车站
FLAT = [
    "f", 34.9090, 135.8157, 1,
    "f1", 34.9091, 135.8158, 5,
    "f2", 34.9300, 135.8000, 3,  # 文件夹成员，即使离得远也归到文件夹
    "b1", 34.8936, 135.8068, 9,
    "b2", 34.8938, 135.8070, 2,
    "st", 34.9500, 135.7500, 1,
    "nodetail", 35.0, 135.0, 1,  # 有坐标没详情：丢掉
]
DETAIL = [1, [], [
    raw_point("f", "莵道高", is_folder=1),
    raw_point("f1", "校门", cn="校门", fid="f", ep=1, s=164),
    raw_point("f2", "京阪黄檗站", cn="京阪黄檗站", fid="f", ep=2, s=10),
    raw_point("b1", "宇治橋​ 不甘心", cn="宇治桥", ep=8, s=1212),
    raw_point("b2", "宇治橋", cn="宇治桥", ep=1, s="242"),
    raw_point("st", "京阪宇治駅", ep=1, s=95),
    raw_point("nogeo", "没有坐标"),
], 0]


def test_day_param_matches_site_formula():
    # 实测 2026-10-21 当天网站请求的是 ?d=g0h
    assert day_param(datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()) == "g0h"


def test_clean_removes_zero_width_but_keeps_fullwidth_punctuation():
    assert clean("京都​コンサート​ホール") == "京都コンサートホール"
    assert clean("吹响吧！上低音号") == "吹响吧！上低音号"  # 不做 NFKC：「！」不能变成「!」
    assert clean("Potato\xa0淀川店") == "Potato 淀川店"
    assert clean(0) is None and clean("  ") is None


def test_image_url():
    assert image_url("/images/points/1/a.jpg") == "https://image.anitabi.cn/points/1/a.jpg"
    assert image_url(0) is None


def test_parse_points_joins_geo_by_id_and_drops_unmatched():
    points = parse_points(raw_anime(1, FLAT), DETAIL)
    ids = [p["id"] for p in points]
    assert "nogeo" not in ids and "nodetail" not in ids
    b2 = next(p for p in points if p["id"] == "b2")
    assert (b2["lat"], b2["lng"], b2["s"]) == (34.8938, 135.8070, 242)  # 秒数字符串也转成 int


def test_group_by_folder_then_distance():
    locs = {loc["id"]: loc for loc in group_locations(parse_points(raw_anime(1, FLAT), DETAIL))}
    assert set(locs) == {"f", "b1", "st"}

    school = locs["f"]
    assert school["name"] == "莵道高" and school["scenes_count"] == 2  # 文件夹本身不算镜头
    # 文件夹没有中文名就留空，不借成员的（成员里有一个是别的车站）
    assert school["cn"] is None

    bridge = locs["b1"]
    assert bridge["scenes_count"] == 2 and bridge["episodes"] == [1, 8]
    assert (bridge["ep"], bridge["s"]) == (1, 242)  # 最早出场的镜头
    assert bridge["cn"] == "宇治桥"


def test_snapshot_sorts_locations_by_scene_count():
    snap = build_snapshot([raw_anime(1, FLAT)], [DETAIL])
    a = snap["anime"][0]
    assert [loc["id"] for loc in a["locations"]][:2] in (["f", "b1"], ["b1", "f"])
    assert a["locations"][-1]["id"] == "st"
    assert a["cover"] == "https://image.anitabi.cn/bangumi/1.jpg"


@pytest.fixture
def repo(tmp_path):
    snap = build_snapshot(
        [raw_anime(1, FLAT, cn="吹响吧！上低音号", title="響け！ユーフォニアム")], [DETAIL]
    )
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return AnitabiRepository(path)


def test_repository_over_snapshot(repo):
    assert repo.search_anime("吹响！上低音号")[0]["anime_id"] == 1
    assert repo.search_anime("ユーフォニアム")[0]["spots_count"] == 3

    result = repo.list_spots(1, near=(34.8936, 135.8068), radius_km=0.5)
    assert [s["spot_id"] for s in result["spots"]] == ["b1"]
    # 列表里不逐个写来源链接（省 token），由前端用 anime_id + spot_id 拼；顶层写一次来源
    assert "origin_url" not in result["spots"][0] and "Anitabi" in result["source"]
    # 单个地点的详情里有完整链接（CC BY-NC-SA 要求署名与跳转）
    assert repo.get_spot("b1")["origin_url"] == "https://www.anitabi.cn/map?bangumiId=1&pid=b1"

    # 用镜头 id 也能查到它所在的地点
    assert repo.get_spot("b2")["spot_id"] == "b1"
    assert len(repo.get_spot("f")["scenes"]) == 2


@pytest.mark.skipif(not anitabi.SNAPSHOT_FILE.exists(), reason="本地没有快照")
def test_real_snapshot_sanity():
    """真实快照的粗略检查。数据会更新，只断言不会轻易变的性质。"""
    repo = AnitabiRepository()
    hit = repo.search_anime("吹响吧！上低音号")[0]
    assert hit["anime_id"] == 115908 and hit["spots_count"] > 20
    spots = repo.list_spots(115908, limit=50)["spots"]
    assert all(s["lat"] and s["lng"] for s in spots)
    assert repo.search_anime("莉可丽丝")[0]["anime_id"] == 364450
