import { describe, expect, it } from 'vitest'
import type { AssistantItem, ToolPart } from '../chat/model'
import { extractMapData, focusPoints } from './extract'

const tool = (name: string, data: unknown, status: ToolPart['status'] = 'ok'): ToolPart => ({
  kind: 'tool',
  id: `${name}-${Math.random()}`,
  name,
  args: {},
  status,
  data,
})

const turn = (...parts: ToolPart[]): AssistantItem => ({ role: 'assistant', id: 'a', parts, status: 'done' })

// 字段与 list_spots 实测返回一致（见 docs/10-frontend.md）
const SPOT = {
  spot_id: 'lyco01',
  name_ja: '浅草寺 雷門',
  name_cn: '浅草寺 雷门',
  lat: 35.7108,
  lng: 139.7967,
  episode: 1,
  timestamp_sec: 210,
  anime_title: '莉可丽丝',
}

const ROUTE = {
  mode: 'walking',
  estimated: false,
  stops: [
    { order: 2, name: 'B', lat: 35.71, lng: 139.8 },
    { order: 1, name: 'A', lat: 35.7, lng: 139.79 },
  ],
  legs: [{ from: 'A', to: 'B', distance_km: 0.7, duration_min: 9 }],
  total_distance_km: 0.7,
  total_duration_min: 9,
}

describe('extractMapData', () => {
  it('圣地按 spot_id 去重，list_spots 与 get_spot 都算', () => {
    const m = extractMapData([turn(tool('list_spots', [SPOT, { ...SPOT, spot_id: 'lyco02' }])), turn(tool('get_spot', SPOT))])
    expect(m.spots.map((s) => s.id)).toEqual(['lyco01', 'lyco02'])
    expect(m.spots[0]).toMatchObject({ name: '浅草寺 雷门', nameJa: '浅草寺 雷門', episode: 1, anime: '莉可丽丝' })
  })

  it('list_spots 的新格式 {total_spots, spots}，多集与镜头数也带上', () => {
    // 作品名、作品 id 只在顶层；来源链接由前端拼
    const { anime_title: _, ...brief } = SPOT
    const data = { anime_id: 364450, anime_title: '莉可丽丝', total_spots: 40, shown: 1, spots: [{ ...brief, episodes: [1, 3], scenes_count: 57 }] }
    const m = extractMapData([turn(tool('list_spots', data))])
    expect(m.spots[0]).toMatchObject({
      id: 'lyco01',
      anime: '莉可丽丝',
      episodes: [1, 3],
      scenesCount: 57,
      originUrl: 'https://www.anitabi.cn/map?bangumiId=364450&pid=lyco01',
    })
  })

  it('路线取最近一次，站点按 order 排序', () => {
    const m = extractMapData([turn(tool('plan_route', { ...ROUTE, mode: 'driving' })), turn(tool('plan_route', ROUTE))])
    expect(m.route?.mode).toBe('walking')
    expect(m.route?.stops.map((s) => s.name)).toEqual(['A', 'B'])
  })

  it('失败的、降级返回 {error} 的、缺坐标的结果都不上图', () => {
    const m = extractMapData([
      turn(
        tool('list_spots', [SPOT], 'error'),
        tool('get_spot', { error: '未找到圣地 x' }),
        tool('plan_route', { error: '路线服务不可用' }),
        tool('list_spots', [{ spot_id: 'nolat', name_cn: '?' }]),
      ),
    ])
    expect(m).toEqual({ spots: [], route: null, places: [] })
  })

  it('geocode 取第一个候选；天气等其他工具不影响地图', () => {
    const m = extractMapData([
      turn(
        tool('geocode', { query: '京阪宇治駅', candidates: [{ name: '宇治駅', lat: 34.89, lng: 135.81 }, { name: 'x', lat: 0, lng: 0 }] }),
        tool('get_weather_forecast', { lat: 35, lng: 135, days: [] }),
      ),
    ])
    expect(m.places).toEqual([{ name: '宇治駅', lat: 34.89, lng: 135.81 }])
    expect(focusPoints(m)).toEqual([[34.89, 135.81]])
  })

  it('有路线时视野只框路线站点', () => {
    const m = extractMapData([turn(tool('list_spots', [{ ...SPOT, lat: 40, lng: 140 }]), tool('plan_route', ROUTE))])
    expect(m.spots).toHaveLength(1) // 圣地照样画
    expect(focusPoints(m)).toEqual([
      [35.7, 139.79],
      [35.71, 139.8],
    ])
  })

  it('与路线站点重合的 geocode 地点不重复画（名字不同也算）', () => {
    const geo = tool('geocode', { query: 'x', candidates: [{ name: 'A駅', lat: 35.7003, lng: 139.7904 }] })
    const m = extractMapData([turn(geo, tool('plan_route', ROUTE))])
    expect(m.places).toEqual([])
  })
})
