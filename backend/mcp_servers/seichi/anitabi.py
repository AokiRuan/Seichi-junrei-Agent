"""下载 Anitabi 地图数据，生成本地快照。讲解见 docs/03-mcp-server.md「真实数据：Anitabi 快照」。

    python -m backend.mcp_servers.seichi.anitabi        # 约 8 个请求、十几 MB，生成 data/anitabi_snapshot.json

数据来源是 anitabi.cn 地图页自己用的两类文件（不是公开 API，格式按位置排列、随时可能变）：

- /d/g.json      全部作品 + 每个地标的 [id, 纬度, 经度, 优先级] 扁平数组
- /d/g{n}.json   第 n 片作品（每片 g.json[1] 部）的地标详情：名称、图片、集数、秒数……不含坐标

按地标 id 把两者拼起来，再把同一地点的多个镜头合并成一个「地点」。格式知识参考了开源客户端
boiboif/anitabi-app（GPL-3.0）的文档，代码是自己写的。

数据遵循 CC BY-NC-SA 4.0：仅非商业使用、展示时署名并提供跳转链接。
所以这里是「低频下载一次、存成快照」，而不是每次对话都去请求。
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://www.anitabi.cn"
IMAGE_HOST = "https://image.anitabi.cn"  # 实测 img-tc.anitabi.cn 的地标图片连不上，image. 可以
DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT_FILE = DATA_DIR / "anitabi_snapshot.json"
# 下载下来的原始文件。调整合并 / 分区算法后用 --offline 重建快照，不必再请求 Anitabi
RAW_DIR = DATA_DIR / "anitabi_raw"
SNAPSHOT_VERSION = 2  # v2：加入地点的社区备注（notes）与巡礼片区（areas）

# 同一文件夹、或彼此相距 100 米以内的地标算同一个地点。
# 实测《上低音号》582 个地标 → 52 个地点；50 米切得太碎（78），200 米会把相邻的两站并在一起
MERGE_RADIUS_M = 100

# 巡礼片区：步行可以串起来的一片地点。参数取自开源项目 MiriaGo 路线规划 Skill 的 balanced 档
# （见 docs/11-pilgrimage-planner.md）：相邻地点超过 1.4 km 就不算一片；
# 一片直径超过 2.6 km 或超过 25 处，就继续切开
AREA_HARD_LINK_M = 1400
AREA_MAX_DIAMETER_M = 2600
AREA_MAX_LOCATIONS = 25

# 社区备注：每个地点最多保留几条、每条最多多长（有的备注是几百字的注意事项）
NOTES_PER_LOCATION = 3
NOTE_MAX_CHARS = 200

# g.json 里每部作品的字段位置
G_ID, G_CN, G_EN, G_TITLE, G_CITY, G_COLOR, G_COVER, _, G_CAT, G_LAT, G_LNG, G_ZOOM, G_POINTS = range(13)
# g{n}.json 里每个地标的字段位置
P_FIELDS = ("id", "name", "cn", "is_folder", "mid", "uid", "image", "fid", "ep", "s",
            "mark", "origin", "origin_link", "folder", "density")


# ---------------------------------------------------------------- 下载


def day_param(now: float | None = None) -> str:
    """URL 上的 ?d= 参数：距 1970 年的天数 + 6，转 36 进制。每天变一次，让 CDN 回源拿新数据。"""
    n = int((time.time() if now is None else now) // 86400) + 6
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def download(client: httpx.Client | None = None, pause_s: float = 1.0) -> tuple[list, list]:
    """返回 (g.json 的作品列表, 全部分片拼成的详情列表)。

    请求逐个发、中间停顿：Anitabi 前面有 Cloudflare，之前探测时连发约 25 个请求就被 403 了
    """
    own = client is None
    client = client or httpx.Client(
        timeout=120, headers={"User-Agent": "Aoki-Agent/0.1 (learning project; snapshot builder)"}
    )
    try:
        d = day_param()
        g = _get_json(client, f"{BASE_URL}/d/g.json?d={d}")
        anime, shard_size = g[0], g[1]
        details: list = []
        for i in range(math.ceil(len(anime) / shard_size)):
            time.sleep(pause_s)
            details.extend(_get_json(client, f"{BASE_URL}/d/g{i}.json?d={d}"))
        return anime, details
    finally:
        if own:
            client.close()


def save_raw(anime: list, details: list) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "g.json").write_text(json.dumps(anime, ensure_ascii=False), encoding="utf-8")
    (RAW_DIR / "details.json").write_text(json.dumps(details, ensure_ascii=False), encoding="utf-8")


def load_raw() -> tuple[list, list]:
    if not (RAW_DIR / "g.json").exists():
        raise FileNotFoundError(f"没有原始数据缓存 {RAW_DIR}，先不带 --offline 运行一次")
    return (
        json.loads((RAW_DIR / "g.json").read_text(encoding="utf-8")),
        json.loads((RAW_DIR / "details.json").read_text(encoding="utf-8")),
    )


def _get_json(client: httpx.Client, url: str) -> Any:
    for attempt in range(3):
        try:
            r = client.get(url)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            if attempt == 2:
                raise
            print(f"  {url} 失败（{type(e).__name__}），5 秒后重试", file=sys.stderr)
            time.sleep(5)


# ---------------------------------------------------------------- 解析与合并（纯函数，测试见 test_anitabi.py）


def clean(text: Any) -> str | None:
    """名称清洗：去掉零宽空格等不可见字符，合并连续空白（含全角空格、不换行空格）。

    实测地标名里混着 \\u200b（零宽空格）：「京都\\u200bコンサート\\u200bホール」，
    不清洗的话搜索匹配不上、显示也可能出怪字符。
    不在这里做 NFKC（全角转半角）：实测它把标题里的「！」变成了「!」。NFKC 只在搜索比较时用，见 repository.py
    """
    if not isinstance(text, str):  # 原始数据里「空」用 0 表示
        return None
    text = re.sub("[\u200b-\u200f\u2060\ufeff]", "", text)  # 零宽空格、零宽连接符、BOM 等
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def image_url(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    if path.startswith("http"):
        return path
    return IMAGE_HOST + re.sub(r"^/images", "", path)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点间距离（米）。几公里范围内用平面近似就够了，不需要球面公式"""
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(lat)
    return math.hypot(dx, dy)


def parse_points(raw_anime: list, raw_detail: list | None) -> list[dict[str, Any]]:
    """把一部作品的坐标表（来自 g.json）与详情（来自 g{n}.json）按地标 id 拼起来。"""
    flat = raw_anime[G_POINTS] if isinstance(raw_anime[G_POINTS], list) else []
    geo = {flat[i]: (flat[i + 1], flat[i + 2], flat[i + 3]) for i in range(0, len(flat) - 3, 4)}
    rows = raw_detail[2] if raw_detail and isinstance(raw_detail[2], list) else []

    points = []
    for row in rows:
        p = dict(zip(P_FIELDS, row))
        g = geo.get(p["id"])
        if g is None or not all(isinstance(v, (int, float)) for v in g[:2]):
            continue  # 没有坐标的地标上不了地图，丢掉（实测 0 条）
        points.append({
            "id": p["id"],
            "name": clean(p["name"]),
            "cn": clean(p["cn"]),
            "is_folder": bool(p["is_folder"]),
            "fid": p["fid"] or None,
            "ep": p["ep"] if isinstance(p["ep"], int) and p["ep"] > 0 else None,
            "s": int(p["s"]) if isinstance(p["s"], (int, float)) or (isinstance(p["s"], str) and p["s"].isdigit()) else None,
            "image": image_url(p["image"]),
            "origin": clean(p["origin"]),
            # 社区备注：交通方式、能否进入、现状变化等（实测 38% 的地标有）。攻略里最有用的信息之一
            "mark": clean(p["mark"]),
            "lat": g[0],
            "lng": g[1],
            "priority": g[2] if isinstance(g[2], (int, float)) else 0,
        })
    return points


def group_locations(points: list[dict[str, Any]], radius_m: float = MERGE_RADIUS_M) -> list[dict[str, Any]]:
    """把同一地点的多个镜头合并成一个地点。

    1. 同一文件夹（fid 指向 is_folder 的地标）的先归在一起——这是数据维护者手工分的组
    2. 剩下的按距离贪心合并：按优先级从高到低，离已有地点中心 radius_m 以内就并进去

    为什么不按名字合并：实测名字里常带镜头描述（「宇治桥 不甘心 全景2」），同一地点名字各不相同
    """
    by_id = {p["id"]: p for p in points}
    seeds: dict[str, list[dict]] = {}  # 组的代表 id → 成员
    for p in sorted(points, key=lambda p: -p["priority"]):
        key = p["fid"] if p["fid"] in by_id else p["id"]
        seeds.setdefault(key, []).append(p)

    locations: list[dict[str, Any]] = []
    for key, members in seeds.items():
        head = by_id[key]
        center = (head["lat"], head["lng"])
        for loc in locations:
            if distance_m(loc["_center"], center) <= radius_m:
                loc["_members"].extend(members)
                break
        else:
            locations.append({"_key": key, "_center": center, "_members": members})

    return [_summarize(loc, by_id) for loc in locations]


def _summarize(loc: dict, by_id: dict[str, dict]) -> dict[str, Any]:
    members = loc["_members"]
    head = by_id[loc["_key"]]
    scenes = [m for m in members if not m["is_folder"]] or members  # 文件夹本身不是一个镜头

    def most_common(field: str) -> str | None:
        # 文件夹名是人工起的地点名，直接用；文件夹没有这个字段就留空，不去借成员的——
        # 实测文件夹「京坂木幡站」的成员里有「京阪黄檗站」的镜头，借过来就张冠李戴了。
        # 不是文件夹的，取成员里出现最多的名字
        if head["is_folder"]:
            return head[field]
        names = Counter(m[field] for m in members if m[field])
        return names.most_common(1)[0][0] if names else None

    episodes = sorted({m["ep"] for m in scenes if m["ep"]})
    first = min(scenes, key=lambda m: (m["ep"] or 999, m["s"] or 0))  # 最早出场的镜头
    return {
        "id": loc["_key"],
        "name": most_common("name"),
        "cn": most_common("cn"),
        "lat": round(loc["_center"][0], 6),
        "lng": round(loc["_center"][1], 6),
        "scenes_count": len(scenes),
        "episodes": episodes,
        "ep": first["ep"],
        "s": first["s"],
        "image": first["image"] or next((m["image"] for m in scenes if m["image"]), None),
        "origin": next((m["origin"] for m in members if m["origin"]), None),
        "notes": _notes(members),
        "priority": sum(m["priority"] for m in members),
        "scenes": [
            {"id": m["id"], "name": m["name"], "cn": m["cn"], "ep": m["ep"], "s": m["s"], "image": m["image"]}
            for m in sorted(scenes, key=lambda m: (m["ep"] or 999, m["s"] or 0))
        ],
        "scene_ids": [m["id"] for m in members],
    }


def _notes(members: list[dict]) -> list[str]:
    """同一地点各镜头的备注去重，出现次数多的在前（实测同一条注意事项常被复制到多个镜头上）。"""
    counts = Counter(m["mark"] for m in members if m.get("mark"))
    notes = []
    for text, _ in counts.most_common(NOTES_PER_LOCATION):
        notes.append(text if len(text) <= NOTE_MAX_CHARS else text[: NOTE_MAX_CHARS - 1] + "…")
    return notes


# ---------------------------------------------------------------- 巡礼片区


def build_areas(locations: list[dict[str, Any]], anime_id: Any) -> list[dict[str, Any]]:
    """把地点分成「步行可以串起来的一片」，供规划器挑选。

    做法参考开源项目 MiriaGo 路线规划 Skill（docs/11 有对比），这里是简化版：

    1. 在所有地点上建最小生成树（MST）：用最短的边把所有点连起来
    2. 剪掉长于 AREA_HARD_LINK_M 的边 → 树断成几块，每块是一个候选片区
    3. 某块直径太大或点太多，就剪掉它内部最长的一条边，递归切分

    为什么不用固定半径连片：实测 1.5 km 连片时，《上低音号》宇治的 33 处连成了一个 7.7 km 的长条
    （地点一个挨一个，链式地连了下去）。MST 加直径上限能把长条切成几段，同时城市和郊区都适用。
    MiriaGo 还会按局部密度找断点，这里没做——demo 规模下直径上限已经够用。
    """
    n = len(locations)
    if n == 0:
        return []
    pts = [(loc["lat"], loc["lng"]) for loc in locations]
    edges = _mst_edges(pts)  # [(长度, i, j)]
    kept = [e for e in edges if e[0] <= AREA_HARD_LINK_M]

    groups: list[list[int]] = []
    for comp in _components(n, kept):
        groups.extend(_split(comp, [e for e in kept if e[1] in comp and e[2] in comp], pts))

    areas = []
    for members in groups:
        locs = sorted((locations[i] for i in members), key=lambda loc: -loc["scenes_count"])
        weight = sum(loc["scenes_count"] for loc in locs)
        areas.append({
            "location_ids": [loc["id"] for loc in locs],
            "name": locs[0].get("cn") or locs[0].get("name"),  # 用片区里镜头最多的地点命名
            "lat": round(sum(loc["lat"] * loc["scenes_count"] for loc in locs) / weight, 6),
            "lng": round(sum(loc["lng"] * loc["scenes_count"] for loc in locs) / weight, 6),
            "scenes_count": weight,
            "diameter_m": round(_diameter([pts[i] for i in members])),
        })
    areas.sort(key=lambda a: -a["scenes_count"])
    for i, a in enumerate(areas):
        a["id"] = f"{anime_id}-{i + 1}"
    return areas


def _mst_edges(pts: list[tuple[float, float]]) -> list[tuple[float, int, int]]:
    """Prim 算法，O(n²)。一部作品最多几百个地点，离线算一次，够快。"""
    n = len(pts)
    in_tree = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0
    edges = []
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i])
        in_tree[u] = True
        if parent[u] >= 0:
            edges.append((best[u], parent[u], u))
        for v in range(n):
            if not in_tree[v]:
                d = distance_m(pts[u], pts[v])
                if d < best[v]:
                    best[v], parent[v] = d, u
    return edges


def _components(n_or_nodes, edges) -> list[set[int]]:
    nodes = range(n_or_nodes) if isinstance(n_or_nodes, int) else n_or_nodes
    parent = {i: i for i in nodes}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for _, a, b in edges:
        parent[find(a)] = find(b)
    comps: dict[int, set[int]] = {}
    for i in nodes:
        comps.setdefault(find(i), set()).add(i)
    return list(comps.values())


def _split(comp: set[int], edges: list, pts) -> list[list[int]]:
    too_big = len(comp) > AREA_MAX_LOCATIONS or _diameter([pts[i] for i in comp]) > AREA_MAX_DIAMETER_M
    if not too_big or not edges:
        return [sorted(comp)]
    longest = max(edges)
    rest = [e for e in edges if e is not longest]
    out = []
    for sub in _components(comp, rest):
        out.extend(_split(sub, [e for e in rest if e[1] in sub and e[2] in sub], pts))
    return out


def _diameter(pts: list[tuple[float, float]]) -> float:
    return max((distance_m(a, b) for a in pts for b in pts), default=0.0)


def build_snapshot(raw_anime: list, raw_details: list) -> dict[str, Any]:
    detail_by_id = {d[0]: d for d in raw_details}
    anime = []
    for a in raw_anime:
        points = parse_points(a, detail_by_id.get(a[G_ID]))
        if not points:
            continue
        locations = group_locations(points)
        locations.sort(key=lambda loc: (-loc["scenes_count"], -loc["priority"]))
        anime.append({
            "id": a[G_ID],
            "cn": clean(a[G_CN]),
            "en": clean(a[G_EN]),
            "title": clean(a[G_TITLE]),
            "city": clean(a[G_CITY]),
            "cat": clean(a[G_CAT]),
            "cover": image_url(a[G_COVER]),
            "geo": [a[G_LAT], a[G_LNG]],
            "scenes_count": len([p for p in points if not p["is_folder"]]),
            "locations": locations,
            "areas": build_areas(locations, a[G_ID]),
        })
    return {
        "_meta": {
            "version": SNAPSHOT_VERSION,
            "source": "anitabi.cn（网站地图数据文件 /d/g.json + /d/g{n}.json）",
            "license": "CC BY-NC-SA 4.0 —— 仅非商业使用，展示时需署名 Anitabi 并提供跳转链接",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "merge_radius_m": MERGE_RADIUS_M,
            "area_limits": {"hard_link_m": AREA_HARD_LINK_M, "max_diameter_m": AREA_MAX_DIAMETER_M,
                            "max_locations": AREA_MAX_LOCATIONS},
        },
        "anime": anime,
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="生成 Anitabi 本地快照")
    ap.add_argument("--offline", action="store_true", help="用上次下载的原始文件重建，不发请求（调算法时用）")
    args = ap.parse_args()

    t0 = time.time()
    if args.offline:
        raw_anime, raw_details = load_raw()
        print(f"用缓存的原始数据重建（{RAW_DIR}）…", file=sys.stderr)
    else:
        print(f"下载 {BASE_URL}/d/g.json 及分片（?d={day_param()}）…", file=sys.stderr)
        raw_anime, raw_details = download()
        save_raw(raw_anime, raw_details)
    snapshot = build_snapshot(raw_anime, raw_details)
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    anime = snapshot["anime"]
    scenes = sum(a["scenes_count"] for a in anime)
    locs = sum(len(a["locations"]) for a in anime)
    areas = sum(len(a["areas"]) for a in anime)
    size_mb = SNAPSHOT_FILE.stat().st_size / 1e6
    print(
        f"完成：{len(anime)} 部作品、{scenes} 个镜头 → {locs} 个地点 → {areas} 个片区，"
        f"{size_mb:.1f} MB，用时 {time.time() - t0:.0f}s → {SNAPSHOT_FILE}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
