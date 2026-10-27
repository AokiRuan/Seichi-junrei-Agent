// 从对话里的工具结果中提取地图要画的东西。讲解见 docs/10-frontend.md「地图：只认结构化数据」
//
// 地图不解析 LLM 写的文字（它可能抄错坐标，05 章实测踩过），只看工具返回的结构化 data。
// 这也是后端把 data 存进会话的原因：刷新页面后地图能原样恢复
import type { ChatItem, ToolPart } from '../chat/model'

export interface Spot {
  id: string
  name: string
  nameJa?: string
  lat: number
  lng: number
  anime?: string
  episode?: number // 最早出场的一集与时间点
  timestampSec?: number
  episodes?: number[] // 出现过的所有集
  scenesCount?: number // 同一地点合并了几个镜头
  image?: string
  originUrl?: string
}

export interface RouteStop {
  order: number
  name: string
  lat: number
  lng: number
}

export interface RouteLeg {
  from: string
  to: string
  distance_km: number
  duration_min: number
  suggest_transit?: boolean
}

export interface Route {
  mode: string
  stops: RouteStop[]
  legs: RouteLeg[]
  totalKm?: number
  totalMin?: number
  estimated?: boolean
}

export interface Place {
  name: string
  lat: number
  lng: number
}

export interface MapData {
  spots: Spot[]
  route: Route | null // 只画最近一次规划的路线
  places: Place[] // geocode 查到的地点（车站、酒店等起终点）
}

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const str = (v: unknown) => (typeof v === 'string' ? v : undefined)
const num = (v: unknown) => (isNum(v) ? v : undefined)
const hasLatLng = (v: unknown): v is Obj => isObj(v) && isNum(v.lat) && isNum(v.lng)

/**
 * parent：list_spots 的顶层对象。为了省 LLM 的 token，作品名和作品 id 只在顶层写一次、
 * 来源链接也不逐个写（见 backend/mcp_servers/seichi/repository.py 的 _spot_brief），这里补回来
 */
function toSpot(v: Obj, parent?: Obj): Spot | null {
  const id = str(v.spot_id)
  if (!id || !hasLatLng(v)) return null
  const animeId = num(v.anime_id) ?? num(parent?.anime_id)
  return {
    id,
    name: str(v.name_cn) ?? str(v.name_ja) ?? id,
    nameJa: str(v.name_ja),
    lat: v.lat as number,
    lng: v.lng as number,
    anime: str(v.anime_title) ?? str(parent?.anime_title),
    episode: num(v.episode),
    timestampSec: num(v.timestamp_sec),
    episodes: Array.isArray(v.episodes) ? v.episodes.filter(isNum) : undefined,
    scenesCount: num(v.scenes_count),
    image: str(v.image),
    originUrl:
      str(v.origin_url) ??
      (animeId != null ? `https://www.anitabi.cn/map?bangumiId=${animeId}&pid=${encodeURIComponent(id)}` : undefined),
  }
}

function toRoute(v: unknown): Route | null {
  if (!isObj(v) || !Array.isArray(v.stops)) return null
  const stops = v.stops.filter(hasLatLng).map((s, i) => ({
    order: num(s.order) ?? i + 1,
    name: str(s.name) ?? `第 ${i + 1} 站`,
    lat: s.lat as number,
    lng: s.lng as number,
  }))
  if (stops.length < 2) return null
  return {
    mode: str(v.mode) ?? 'walking',
    stops: stops.sort((a, b) => a.order - b.order),
    legs: Array.isArray(v.legs) ? (v.legs.filter(isObj) as unknown as RouteLeg[]) : [],
    totalKm: num(v.total_distance_km),
    totalMin: num(v.total_duration_min),
    estimated: v.estimated === true,
  }
}

export function extractMapData(items: ChatItem[]): MapData {
  const spots = new Map<string, Spot>() // 按 spot_id 去重：多轮里同一个地点可能被查多次
  const places = new Map<string, Place>()
  let route: Route | null = null

  const tools: ToolPart[] = items.flatMap((it) =>
    it.role === 'assistant' ? it.parts.filter((p): p is ToolPart => p.kind === 'tool') : [],
  )

  for (const t of tools) {
    if (t.status !== 'ok') continue
    const d = t.data
    switch (t.name) {
      case 'list_spots': {
        // 现在的格式是 {total_spots, shown, spots: [...]}；早期是直接返回数组，兼容一下
        const list = Array.isArray(d) ? d : isObj(d) && Array.isArray(d.spots) ? d.spots : []
        for (const v of list) {
          const s = isObj(v) ? toSpot(v, isObj(d) ? d : undefined) : null
          if (s) spots.set(s.id, s)
        }
        break
      }
      case 'get_spot': {
        const s = isObj(d) ? toSpot(d) : null
        if (s) spots.set(s.id, s)
        break
      }
      case 'plan_route':
        route = toRoute(d) ?? route
        break
      case 'geocode':
        // 只取第一个候选：LLM 通常也是用第一个
        if (isObj(d) && Array.isArray(d.candidates) && hasLatLng(d.candidates[0])) {
          const c = d.candidates[0]
          const name = str(c.name) ?? str(d.query) ?? '地点'
          places.set(name, { name, lat: c.lat as number, lng: c.lng as number })
        }
        break
    }
  }
  // geocode 查的往往就是路线起点（如「京阪宇治駅」），它和路线第 1 站重合时只画站点编号。
  // 按距离判断而不是按名字：LLM 传给 plan_route 的名字可能是「京阪宇治站」，和 geocode 结果差一个字
  const stops = (route as Route | null)?.stops ?? []
  const visiblePlaces = [...places.values()].filter((p) => !stops.some((s) => isNear(p, s)))
  return { spots: [...spots.values()], route, places: visiblePlaces }
}

/** 两点相距约 100 米以内（纬度 0.001° ≈ 111 米；日本所在纬度上经度 0.001° ≈ 90 米） */
function isNear(a: { lat: number; lng: number }, b: { lat: number; lng: number }) {
  return Math.abs(a.lat - b.lat) < 0.001 && Math.abs(a.lng - b.lng) < 0.001
}

/**
 * 地图自动缩放时要框进视野的点。
 * 有路线时只框路线：用户此时关心的是怎么走，而作品的圣地可能分散在别的城市
 * （实测：《上低音号》有一个圣地在京都市区，全框进来宇治的路线就缩成一团）
 */
export function focusPoints(m: MapData): [number, number][] {
  if (m.route) return m.route.stops.map((s) => [s.lat, s.lng])
  return [
    ...m.spots.map((s) => [s.lat, s.lng] as [number, number]),
    ...m.places.map((p) => [p.lat, p.lng] as [number, number]),
  ]
}
