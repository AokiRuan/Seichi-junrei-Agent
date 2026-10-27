// plan_pilgrimage 工具结果 → 攻略卡片的数据。讲解见 docs/11-pilgrimage-planner.md「攻略卡片」
// 纯函数：卡片（GuideCard.tsx）和地图（map/extract.ts）都从这里拿，字段不对就返回 null 不渲染
import type { ToolPart } from './model'

export interface GuideRide {
  line: string
  headsign?: string
  from: string
  to: string
  depart?: string
  arrive?: string
}

export interface GuideAccess {
  durationMin: number
  estimated: boolean
  depart?: string
  arrive?: string
  transfers?: number
  rides: GuideRide[]
  toSpot?: string
  note?: string
  googleMapsUrl?: string
  source?: string
}

export interface GuideStop {
  order: number
  spotId: string
  name: string
  nameJa?: string
  lat: number
  lng: number
  episodes: number[]
  scenesCount: number
  image?: string
  notes: string[]
  dwellMin: number
  walkFromPrevMin: number
  arriveAt?: string
  originUrl?: string
}

export interface Guide {
  animeTitle: string
  areaName: string
  start?: { name?: string; lat: number; lng: number }
  budgetMin: number
  totalMin: number
  walkKm: number
  access?: GuideAccess
  stops: GuideStop[]
  omitted: { name: string; reason: string }[]
  otherAreas: { areaId: string; name: string; spots: number }[]
  walkingRouteUrl?: string
  assumptions: string[]
  source?: string
}

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

function toStop(v: unknown): GuideStop | null {
  if (!isObj(v)) return null
  const lat = num(v.lat), lng = num(v.lng), order = num(v.order), spotId = str(v.spot_id)
  if (lat == null || lng == null || order == null || !spotId) return null
  return {
    order,
    spotId,
    name: str(v.name_cn) ?? str(v.name_ja) ?? spotId,
    nameJa: str(v.name_ja),
    lat,
    lng,
    episodes: arr(v.episodes).filter((e): e is number => typeof e === 'number'),
    scenesCount: num(v.scenes_count) ?? 1,
    image: str(v.image),
    notes: arr(v.notes).filter((n): n is string => typeof n === 'string'),
    dwellMin: num(v.dwell_min) ?? 0,
    walkFromPrevMin: num(v.walk_from_prev_min) ?? 0,
    arriveAt: str(v.arrive_at),
    originUrl: str(v.origin_url),
  }
}

function toAccess(v: unknown): GuideAccess | undefined {
  if (!isObj(v) || num(v.duration_min) == null) return undefined
  return {
    durationMin: num(v.duration_min)!,
    estimated: v.estimated === true,
    depart: str(v.depart),
    arrive: str(v.arrive),
    transfers: num(v.transfers),
    rides: arr(v.rides).filter(isObj).map((r) => ({
      line: str(r.line) ?? '',
      headsign: str(r.headsign),
      from: str(r.from) ?? '',
      to: str(r.to) ?? '',
      depart: str(r.depart),
      arrive: str(r.arrive),
    })),
    toSpot: str(v.to_spot),
    note: str(v.note),
    googleMapsUrl: str(v.google_maps_url),
    source: str(v.source),
  }
}

export function parseGuide(data: unknown): Guide | null {
  if (!isObj(data) || 'error' in data) return null
  const stops = arr(data.stops).map(toStop).filter((s): s is GuideStop => s !== null)
  if (stops.length === 0) return null
  const area = isObj(data.area) ? data.area : {}
  const start = isObj(data.start) && num(data.start.lat) != null && num(data.start.lng) != null
    ? { name: str(data.start.name), lat: num(data.start.lat)!, lng: num(data.start.lng)! }
    : undefined
  return {
    animeTitle: str(data.anime_title) ?? '',
    areaName: str(area.name) ?? '',
    start,
    budgetMin: num(data.budget_min) ?? 0,
    totalMin: num(data.total_min) ?? 0,
    walkKm: num(data.walk_km) ?? 0,
    access: toAccess(data.access),
    stops: stops.sort((a, b) => a.order - b.order),
    omitted: arr(data.omitted).filter(isObj).map((o) => ({ name: str(o.name) ?? '', reason: str(o.reason) ?? '' })),
    otherAreas: arr(data.other_areas).filter(isObj).map((o) => ({
      areaId: str(o.area_id) ?? '',
      name: str(o.name) ?? '',
      spots: num(o.spots) ?? 0,
    })),
    walkingRouteUrl: str(data.walking_route_url),
    assumptions: arr(data.assumptions).filter((a): a is string => typeof a === 'string'),
    source: str(data.source),
  }
}

/** 这个工具片段是一份成功的攻略吗？是就解析出来 */
export function guideOf(part: ToolPart): Guide | null {
  return part.name === 'plan_pilgrimage' && part.status === 'ok' ? parseGuide(part.data) : null
}

export function formatDuration(min: number): string {
  const h = Math.floor(min / 60), m = Math.round(min % 60)
  return h ? (m ? `${h} 小时 ${m} 分` : `${h} 小时`) : `${m} 分钟`
}
