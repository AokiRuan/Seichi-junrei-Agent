import { describe, expect, it } from 'vitest'
import { extractMapData } from '../map/extract'
import { formatDuration, parseGuide } from './guide'
import type { AssistantItem } from './model'

// 与 plan_pilgrimage 实测返回的结构一致（字段节选）
const DATA = {
  anime_title: '孤独摇滚！',
  area: { id: '328609-1', name: '下北沢SHELTER', spots_in_area: 12 },
  start: { name: '新宿駅', lat: 35.690921, lng: 139.700258 },
  budget_min: 240,
  total_min: 225,
  walk_km: 3.4,
  access: {
    depart: '10:04', arrive: '10:27', duration_min: 24, transfers: 1, walk_min: 6, estimated: false,
    rides: [{ line: '山手線（内回り）', headsign: '大崎', from: '新宿', to: '渋谷', depart: '10:04', arrive: '10:11' }],
    to_spot: 'ADRIFT', google_maps_url: 'https://www.google.com/maps/dir/?api=1&x', source: 'Transit API',
  },
  stops: [
    { order: 2, spot_id: 'b', name_ja: 'Times下北沢第8', lat: 35.661, lng: 139.668, scenes_count: 33, episodes: [3, 4],
      dwell_min: 20, walk_from_prev_min: 3, arrive_at: '10:40', notes: ['停车场'] },
    { order: 1, spot_id: 'a', name_ja: 'ADRIFT', lat: 35.660, lng: 139.669, scenes_count: 4, dwell_min: 10,
      walk_from_prev_min: 0, arrive_at: '10:27' },
  ],
  omitted: [{ spot_id: 'z', name: '远处', reason: '时间预算不够' }],
  other_areas: [{ area_id: '328609-2', name: '金沢八景', spots: 8, scenes: 82 }],
  assumptions: ['每处停留 10~20 分钟'],
}

describe('parseGuide', () => {
  it('解析行程，站点按 order 排序', () => {
    const g = parseGuide(DATA)!
    expect(g.stops.map((s) => s.spotId)).toEqual(['a', 'b'])
    expect(g.stops[1]).toMatchObject({ name: 'Times下北沢第8', episodes: [3, 4], notes: ['停车场'], arriveAt: '10:40' })
    expect(g.access).toMatchObject({ durationMin: 24, estimated: false, arrive: '10:27' })
    expect(g.access!.rides[0].line).toBe('山手線（内回り）')
    expect(g.otherAreas[0]).toEqual({ areaId: '328609-2', name: '金沢八景', spots: 8 })
  })

  it('错误结果、没有站点时返回 null（不渲染卡片）', () => {
    expect(parseGuide({ error: '没有这部作品' })).toBeNull()
    expect(parseGuide({ ...DATA, stops: [] })).toBeNull()
    expect(parseGuide(null)).toBeNull()
  })

  it('地图：站点上图、连成路线，出发地画成地点', () => {
    const item: AssistantItem = {
      role: 'assistant', id: 'x', status: 'done',
      parts: [{ kind: 'tool', id: 't', name: 'plan_pilgrimage', args: {}, status: 'ok', data: DATA }],
    }
    const m = extractMapData([item])
    expect(m.spots.map((s) => s.id).sort()).toEqual(['a', 'b'])
    expect(m.route?.stops.map((s) => s.name)).toEqual(['ADRIFT', 'Times下北沢第8'])
    expect(m.places).toEqual([{ name: '新宿駅', lat: 35.690921, lng: 139.700258 }])
  })
})

it('formatDuration', () => {
  expect(formatDuration(225)).toBe('3 小时 45 分')
  expect(formatDuration(240)).toBe('4 小时')
  expect(formatDuration(25)).toBe('25 分钟')
})
