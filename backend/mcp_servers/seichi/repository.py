"""圣地数据访问层。讲解见 docs/03-mcp-server.md。

把「数据从哪来」和「MCP 工具怎么用」隔开：server.py 只依赖 SeichiRepository 接口。

两个实现共用同一套查询逻辑（LocationRepository），只是加载数据的方式不同：

- AnitabiRepository：读 anitabi.py 生成的本地快照（1552 部作品、2 万多个地点）
- MockRepository：读手写的 mock_spots.json（5 部作品），测试和没有快照时用

用哪个由环境变量 SEICHI_DATA_SOURCE 决定：auto（默认，有快照用快照）/ anitabi / mock。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any

from .anitabi import SNAPSHOT_FILE, build_areas, distance_m

MOCK_FILE = Path(__file__).parent / "data" / "mock_spots.json"
ANITABI_MAP_URL = "https://www.anitabi.cn/map?bangumiId={anime_id}&pid={spot_id}"


class SeichiRepository(ABC):
    @abstractmethod
    def search_anime(self, title: str, limit: int = 5) -> list[dict[str, Any]]:
        """按标题模糊搜索作品。"""

    @abstractmethod
    def list_spots(
        self,
        anime_id: int,
        limit: int = 15,
        near: tuple[float, float] | None = None,
        radius_km: float = 3,
    ) -> dict[str, Any] | None:
        """列出某作品的圣地（按镜头数从多到少），可限定在某点附近。作品不存在返回 None。"""

    @abstractmethod
    def get_spot(self, spot_id: str) -> dict[str, Any] | None:
        """单个圣地详情，含各个镜头。"""

    @abstractmethod
    def get_anime(self, anime_id: int) -> dict[str, Any] | None:
        """作品的内部数据（含 locations 与 areas），给规划器用。只读，不要修改。"""


class LocationRepository(SeichiRepository):
    """查询逻辑。数据格式就是快照的格式：anime[] → locations[] → scenes[]。"""

    @abstractmethod
    def _anime(self) -> list[dict[str, Any]]:
        """全部作品。加载结果有缓存、返回的是同一份数据：只能读，不能修改，否则会污染后续所有调用"""

    def search_anime(self, title: str, limit: int = 5) -> list[dict[str, Any]]:
        q = _norm(title)
        if not q:
            return []
        scored = []
        for a in self._anime():
            score = _match_score(q, a)
            if score > 0:
                scored.append((score, a["scenes_count"], a))
        # 匹配度优先；同样匹配度时，地标多的作品（更可能是用户想找的那部）排前面
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [_anime_brief(a) for _, _, a in scored[:limit]]

    def list_spots(self, anime_id, limit=15, near=None, radius_km=3):
        a = self._index()["anime"].get(anime_id)
        if a is None:
            return None
        locations = a["locations"]
        if near is not None:
            with_d = [(distance_m(near, (loc["lat"], loc["lng"])) / 1000, loc) for loc in locations]
            locations = [loc for d, loc in with_d if d <= radius_km]
            dist = {loc["id"]: round(d, 2) for d, loc in with_d if d <= radius_km}
        shown = locations[:limit]
        spots = [_spot_brief(loc) for loc in shown]
        if near is not None:
            for s in spots:
                s["distance_km"] = dist[s["spot_id"]]
        return {
            # 作品级字段只写一次，不在每个地点里重复（实测 limit=50 时重复字段占了结果的一半）
            "anime_id": a["id"],
            "anime_title": _title(a),
            "city": a.get("city"),
            "source": "Anitabi（anitabi.cn），CC BY-NC-SA 4.0",
            # 告诉 LLM 总数：「共 40 处，这里列出最热门的 15 处」，它才能如实转告用户、或决定要不要缩小范围
            "total_spots": len(locations),
            "shown": len(spots),
            "sorted_by": "镜头数（同一地点在动画里出现的次数）从多到少",
            "spots": spots,
        }

    def get_anime(self, anime_id: int) -> dict[str, Any] | None:
        return self._index()["anime"].get(anime_id)

    def get_spot(self, spot_id: str) -> dict[str, Any] | None:
        # 也接受镜头的 id：前端或 LLM 拿到的可能是某个镜头的 id，而不是地点的
        hit = self._index()["spot"].get(spot_id)
        if hit is None:
            return None
        a, loc = hit
        out = _spot_out(loc, a)
        out["notes"] = loc.get("notes", [])  # 社区备注：交通、能否进入、现状变化等
        out["scenes"] = [
            {
                "name_ja": s["name"],
                "name_cn": s["cn"],
                "episode": s["ep"],
                "timestamp_sec": s["s"],
                "image": s["image"],
            }
            for s in loc["scenes"][:20]  # 北宇治高校有 214 个镜头，全给 LLM 没有意义
        ]
        return out

    def _index(self) -> dict[str, dict]:
        # 第一次用到时才建，之后缓存在实例上（server.py 里只有一个 repo 实例）
        if getattr(self, "_idx", None) is None:
            self._idx = _build_index(self._anime())
        return self._idx


class AnitabiRepository(LocationRepository):
    def __init__(self, snapshot_file: Path = SNAPSHOT_FILE) -> None:
        self._file = snapshot_file

    def _anime(self) -> list[dict[str, Any]]:
        return _load_json(self._file)["anime"]

    @property
    def meta(self) -> dict[str, Any]:
        return _load_json(self._file)["_meta"]


class MockRepository(LocationRepository):
    """读手写的 mock 数据。零网络依赖，测试结果不随外部数据变化。"""

    def __init__(self, data_file: Path = MOCK_FILE) -> None:
        self._file = data_file

    def _anime(self) -> list[dict[str, Any]]:
        return _mock_as_locations(self._file)


# ---------------------------------------------------------------- 加载与索引


@lru_cache(maxsize=4)
def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=4)
def _mock_as_locations(path: Path) -> list[dict[str, Any]]:
    """把 mock 的「一部作品一串地标」转成快照格式：每个地标就是一个只有一个镜头的地点。"""
    anime = []
    for a in _load_json(path)["anime"]:
        locations = []
        for p in a["points"]:
            scene = {"id": p["id"], "name": p.get("name"), "cn": p.get("cn"),
                     "ep": p.get("ep"), "s": p.get("s"), "image": p.get("image")}
            locations.append({
                "id": p["id"], "name": p.get("name"), "cn": p.get("cn"),
                "lat": p["geo"][0], "lng": p["geo"][1],
                "scenes_count": 1, "episodes": [p["ep"]] if p.get("ep") else [],
                "ep": p.get("ep"), "s": p.get("s"), "image": p.get("image"),
                "origin": p.get("origin"), "origin_url": p.get("originURL"),
                "scenes": [scene], "scene_ids": [p["id"]], "notes": [],
            })
        anime.append({**{k: a.get(k) for k in ("id", "cn", "title", "city", "cover", "geo")},
                      "en": None, "scenes_count": len(locations), "locations": locations,
                      # 片区用和快照完全相同的函数算，规划器在 mock 和真实数据上走同一条路
                      "areas": build_areas(locations, a["id"])})
    return anime


def _build_index(anime: list[dict[str, Any]]) -> dict[str, dict]:
    """按 id 查作品、按地点或镜头 id 查地点。2 万多个地点，每次线性扫描太慢，建一次字典。"""
    index: dict[str, dict] = {"anime": {}, "spot": {}}
    for a in anime:
        index["anime"][a["id"]] = a
        for loc in a["locations"]:
            for sid in loc["scene_ids"]:
                index["spot"].setdefault(sid, (a, loc))
            index["spot"][loc["id"]] = (a, loc)  # 地点 id 优先于同名的镜头 id
    return index


# ---------------------------------------------------------------- 搜索


def _norm(text: Any) -> str:
    """搜索用的归一化：全角转半角（NFKC）、小写、去掉标点和空白。

    「吹响！上低音号」和「吹响吧!上低音号」归一化后是「吹响上低音号」「吹响吧上低音号」
    """
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\W_]+", "", text)


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _match_score(q: str, a: dict[str, Any]) -> float:
    """0 = 不匹配。完全相同 3 > 互相包含 2 > 二元组重合度高 1~0.6。

    二元组重合是为了容忍 LLM 写错一两个字：实测 LLM 把「吹响吧！上低音号」写成「吹响！上低音号」，
    旧的子串匹配直接搜不到，LLM 只好换三种写法重试（docs/10 的实测）
    """
    best = 0.0
    for field in ("cn", "title", "en"):
        t = _norm(a.get(field))
        if not t:
            continue
        if q == t:
            return 3.0
        if q in t or (len(t) >= 2 and t in q):
            best = max(best, 2.0)
        else:
            qb, tb = _bigrams(q), _bigrams(t)
            overlap = len(qb & tb) / len(qb)
            if overlap >= 0.6:
                best = max(best, overlap)
    city = _norm(a.get("city"))
    if city and (q in city or (len(city) >= 2 and city in q)):
        best = max(best, 1.5)  # 按城市搜到的排在按标题搜到的后面
    return best


# ---------------------------------------------------------------- 输出格式


def _title(a: dict[str, Any]) -> str | None:
    return a.get("cn") or a.get("title")


def _anime_brief(a: dict[str, Any]) -> dict[str, Any]:
    """作品摘要。不含地点列表，避免一次塞爆 LLM 上下文。"""
    return {
        "anime_id": a["id"],
        "title_cn": a.get("cn"),
        "title_ja": a.get("title"),
        "title_en": a.get("en"),
        "city": a.get("city"),
        "cover": a.get("cover"),
        "center": a.get("geo"),
        "spots_count": len(a["locations"]),
    }


def _spot_brief(loc: dict[str, Any]) -> dict[str, Any]:
    """list_spots 里的地点：只放 LLM 和地图打点需要的字段，空值不输出。

    这个结果会原样进入 LLM 的上下文，每个字段都是 token。来源链接可以由前端用
    anime_id + spot_id 拼出来，就不逐个写了；只有地点自带的外部来源（mock 里的 Google Maps）才保留
    """
    out = {
        "spot_id": loc["id"],
        "name_ja": loc.get("name"),
        "name_cn": loc.get("cn"),
        "lat": loc["lat"],
        "lng": loc["lng"],
        "episode": loc.get("ep"),
        "timestamp_sec": loc.get("s"),
        "episodes": loc.get("episodes") if len(loc.get("episodes") or []) > 1 else None,
        "scenes_count": loc.get("scenes_count", 1),
        "image": loc.get("image"),
        "origin": loc.get("origin"),
        "origin_url": loc.get("origin_url"),
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _spot_out(loc: dict[str, Any], a: dict[str, Any]) -> dict[str, Any]:
    """get_spot 的输出格式（单个地点，字段给全）。字段名对 LLM 友好，坐标拆成 lat/lng 便于前端直接打点。"""
    return {
        "spot_id": loc["id"],
        "name_ja": loc.get("name"),
        "name_cn": loc.get("cn"),
        "lat": loc["lat"],
        "lng": loc["lng"],
        "episode": loc.get("ep"),  # 最早出场的一集与时间点
        "timestamp_sec": loc.get("s"),
        "episodes": loc.get("episodes", []),
        "scenes_count": loc.get("scenes_count", 1),
        "image": loc.get("image"),
        "anime_id": a["id"],
        "anime_title": _title(a),
        "city": a.get("city"),
        # 展示时需标注来源并支持跳转（Anitabi 数据遵循 CC BY-NC-SA 4.0）
        "origin": loc.get("origin") or "Anitabi",
        "origin_url": loc.get("origin_url") or ANITABI_MAP_URL.format(anime_id=a["id"], spot_id=loc["id"]),
    }


# ---------------------------------------------------------------- 选择数据源


def get_repository() -> SeichiRepository:
    """按 SEICHI_DATA_SOURCE 选数据源。

    auto（默认）：有快照就用快照，没有就退回 mock——没下载快照也能跑起来
    测试里 conftest.py 固定成 mock，测试结果不随快照内容变化
    """
    source = os.environ.get("SEICHI_DATA_SOURCE", "auto").lower()
    if source == "mock":
        return MockRepository()
    if source == "anitabi" or (source == "auto" and SNAPSHOT_FILE.exists()):
        if not SNAPSHOT_FILE.exists():
            raise FileNotFoundError(
                f"SEICHI_DATA_SOURCE=anitabi 但快照不存在：{SNAPSHOT_FILE}。"
                "先执行 python -m backend.mcp_servers.seichi.anitabi"
            )
        return AnitabiRepository()
    return MockRepository()
