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
SNAPSHOT_FILE = Path(__file__).parent / "data" / "anitabi_snapshot.json"
SNAPSHOT_VERSION = 1

# 同一文件夹、或彼此相距 100 米以内的地标算同一个地点。
# 实测《上低音号》582 个地标 → 52 个地点；50 米切得太碎（78），200 米会把相邻的两站并在一起
MERGE_RADIUS_M = 100

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
    text = re.sub(r"[​-‏⁠﻿]", "", text)
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
        "priority": sum(m["priority"] for m in members),
        "scenes": [
            {"id": m["id"], "name": m["name"], "cn": m["cn"], "ep": m["ep"], "s": m["s"], "image": m["image"]}
            for m in sorted(scenes, key=lambda m: (m["ep"] or 999, m["s"] or 0))
        ],
        "scene_ids": [m["id"] for m in members],
    }


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
        })
    return {
        "_meta": {
            "version": SNAPSHOT_VERSION,
            "source": "anitabi.cn（网站地图数据文件 /d/g.json + /d/g{n}.json）",
            "license": "CC BY-NC-SA 4.0 —— 仅非商业使用，展示时需署名 Anitabi 并提供跳转链接",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "merge_radius_m": MERGE_RADIUS_M,
        },
        "anime": anime,
    }


def main() -> None:
    t0 = time.time()
    print(f"下载 {BASE_URL}/d/g.json 及分片（?d={day_param()}）…", file=sys.stderr)
    raw_anime, raw_details = download()
    snapshot = build_snapshot(raw_anime, raw_details)
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    anime = snapshot["anime"]
    scenes = sum(a["scenes_count"] for a in anime)
    locs = sum(len(a["locations"]) for a in anime)
    size_mb = SNAPSHOT_FILE.stat().st_size / 1e6
    print(
        f"完成：{len(anime)} 部作品、{scenes} 个镜头 → {locs} 个地点，"
        f"{size_mb:.1f} MB，用时 {time.time() - t0:.0f}s → {SNAPSHOT_FILE}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
