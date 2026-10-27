// 工具调用在界面上怎么描述。讲解见 docs/10-frontend.md「工具过程可视化」
// 工具名是给 LLM 看的英文标识；用户看到的是这里的中文动作 + 从参数 / 结果里摘出的一句话
import type { ToolPart } from './model'

const LABELS: Record<string, string> = {
  search_anime: '查询作品',
  list_spots: '查询圣地',
  get_spot: '查询圣地详情',
  get_weather_forecast: '获取天气预报',
  geocode: '查找地点',
  plan_route: '规划路线',
}

export const toolLabel = (name: string) => LABELS[name] ?? name

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)

/** 调用时的参数摘要，如「莉可丽丝」「3 个地点 · 步行」 */
export function describeArgs(t: ToolPart): string {
  const a = t.args
  switch (t.name) {
    case 'search_anime':
      return String(a.title ?? '')
    case 'geocode':
      return String(a.query ?? '')
    case 'get_spot':
      return String(a.spot_id ?? '')
    case 'list_spots': {
      const parts = a.anime_id != null ? [`作品 #${a.anime_id}`] : []
      if (a.near_lat != null) parts.push(`附近 ${a.radius_km ?? 3} km`)
      return parts.join(' · ')
    }
    case 'get_weather_forecast': {
      const lat = Number(a.lat), lng = Number(a.lng)
      const where = Number.isFinite(lat) && Number.isFinite(lng) ? `${lat.toFixed(2)}, ${lng.toFixed(2)}` : ''
      return a.days != null ? `${where} · ${a.days} 天` : where
    }
    case 'plan_route': {
      const n = Array.isArray(a.stops) ? a.stops.length : 0
      return `${n} 个地点 · ${a.mode === 'driving' ? '驾车' : '步行'}`
    }
    default:
      return ''
  }
}

/**
 * 工具成功时结果的摘要。
 * 注意天气 / 路线 server 在外部 API 失败时返回的是 {"error": "..."}，is_error 却是 false
 * （server 主动降级，见 04 章），所以这里单独识别出来，返回 warn
 */
export function describeResult(t: ToolPart): { text: string; warn?: boolean } {
  const d = t.data
  if (isObj(d) && typeof d.error === 'string') return { text: d.error, warn: true }

  switch (t.name) {
    case 'search_anime':
      if (Array.isArray(d)) {
        if (d.length === 0) return { text: '没有找到作品', warn: true }
        const first = isObj(d[0]) ? String(d[0].title_cn ?? d[0].title_ja ?? '') : ''
        return { text: d.length === 1 ? first : `${d.length} 部作品：${first} 等` }
      }
      break
    case 'list_spots':
      if (Array.isArray(d)) return { text: `${d.length} 个圣地` }
      if (isObj(d) && Array.isArray(d.spots)) {
        const total = Number(d.total_spots ?? d.spots.length)
        if (total === 0) return { text: '这个范围内没有圣地', warn: true }
        return { text: total > d.spots.length ? `共 ${total} 处，列出 ${d.spots.length} 处` : `${total} 个圣地` }
      }
      break
    case 'get_spot':
      if (isObj(d)) return { text: String(d.name_cn ?? d.name_ja ?? '') }
      break
    case 'get_weather_forecast':
      if (isObj(d) && Array.isArray(d.days) && isObj(d.days[0])) {
        const day = d.days[0]
        return { text: `${d.days.length} 天预报 · 今天${day.weather ?? ''} ${day.temp_min_c}~${day.temp_max_c}°C` }
      }
      break
    case 'geocode':
      if (isObj(d) && Array.isArray(d.candidates)) {
        const first = isObj(d.candidates[0]) ? String(d.candidates[0].name ?? '') : ''
        return d.candidates.length ? { text: first } : { text: '没有找到该地点', warn: true }
      }
      break
    case 'plan_route':
      if (isObj(d)) {
        const n = Array.isArray(d.stops) ? d.stops.length : 0
        return { text: `${n} 站 · ${d.total_distance_km} km · 约 ${d.total_duration_min} 分钟` }
      }
      break
  }
  return { text: '完成' }
}
