// 右侧地图。讲解见 docs/10-frontend.md「地图：只认结构化数据」
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useEffect, useMemo } from 'react'
import { CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, useMap } from 'react-leaflet'
import type { MapData, Spot } from '../map/extract'
import { focusPoints } from '../map/extract'

const JAPAN: [number, number] = [36.2, 138.25]

// 不用 Leaflet 的默认 Marker 图标：它通过 CSS 里的相对路径找图片，经过 Vite 打包后路径会断，
// 显示成一个破图。圣地用 CircleMarker（矢量圆点，不需要图片），路线站点用 divIcon（一段 HTML）
function stopIcon(order: number) {
  return L.divIcon({
    className: 'stop-icon',
    html: `<span>${order}</span>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  })
}

// 样式直接写进 pathOptions，不用 className + CSS：react-leaflet 是在元素创建之后用 setStyle() 应用
// pathOptions 的，而 Leaflet 只在创建 SVG 元素时读 className，之后 setStyle 不会再加类名——实测
// 写了 className 的圆点仍是默认的蓝色空心圈。
// 放在模块级常量里：react-leaflet 按引用比较 pathOptions，每次渲染传一个新对象字面量就会每次都 setStyle
const SPOT_STYLE: L.PathOptions = { color: '#ffffff', weight: 2, fillColor: '#c2410c', fillOpacity: 0.9 }
const ROUTE_STYLE: L.PathOptions = { color: '#c2410c', weight: 3, dashArray: '6 6' }

const placeIcon = L.divIcon({ className: 'place-icon', html: '<span>◆</span>', iconSize: [20, 20], iconAnchor: [10, 10] })

/**
 * 点集变化时自动缩放到能看见全部点。
 *
 * 依赖是点集拼成的字符串 key，而不是 points 数组本身：流式输出时每来一个 token 整个页面都会
 * 重渲染、points 都是一个新数组，拿数组当依赖会导致地图每个字都重新飞一次。
 * 字符串按值比较，只有点真的变了才触发
 */
function FitBounds({ points, reserveRight }: { points: [number, number][]; reserveRight: boolean }) {
  const map = useMap()
  const key = points.map((p) => p.join(',')).join(';')
  useEffect(() => {
    if (!key) return
    const pts = key.split(';').map((p) => p.split(',').map(Number) as [number, number])
    if (pts.length === 1) map.flyTo(pts[0], 15, { duration: 0.8 })
    else
      map.flyToBounds(L.latLngBounds(pts), {
        paddingTopLeft: [40, 40],
        // 右下角有路线卡片时多留出它的宽度，免得卡片盖住路线。窄屏上卡片最多占地图宽度的一半多，按比例留
        paddingBottomRight: reserveRight ? [Math.min(340, map.getSize().x * 0.55), 40] : [40, 40],
        maxZoom: 16,
        duration: 0.8,
      })
  }, [key, map, reserveRight])
  return null
}

function formatTime(sec?: number) {
  if (sec == null) return ''
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

function SpotPopup({ s }: { s: Spot }) {
  return (
    <div className="spot-popup">
      {s.image && <img src={s.image} alt={s.name} loading="lazy" />}
      <strong>{s.name}</strong>
      {s.nameJa && s.nameJa !== s.name && <div className="muted">{s.nameJa}</div>}
      <div className="muted">
        {s.anime}
        {s.episodes && s.episodes.length > 1
          ? ` · 第 ${s.episodes.slice(0, 6).join('、')}${s.episodes.length > 6 ? ' 等' : ''} 集`
          : s.episode != null && ` · 第 ${s.episode} 集 ${formatTime(s.timestampSec)}`}
      </div>
      {s.scenesCount != null && s.scenesCount > 1 && <div className="muted">共 {s.scenesCount} 个镜头</div>}
      {s.originUrl && (
        <a href={s.originUrl} target="_blank" rel="noreferrer">
          在 Anitabi 查看
        </a>
      )}
    </div>
  )
}

export function MapPanel({ data }: { data: MapData }) {
  const points = useMemo(() => focusPoints(data), [data])
  const routeLine = data.route?.stops.map((s) => [s.lat, s.lng] as [number, number]) ?? []

  return (
    <section className="map-panel">
      <MapContainer center={JAPAN} zoom={5} className="map" scrollWheelZoom>
        <TileLayer
          // 圣地数据遵循 CC BY-NC-SA 4.0，要求署名：和地图底图的版权信息放在一起
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors | 圣地数据 &copy; <a href="https://www.anitabi.cn" target="_blank" rel="noreferrer">Anitabi</a> (CC BY-NC-SA 4.0)'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        {data.spots.map((s) => (
          <CircleMarker
            key={s.id}
            center={[s.lat, s.lng]}
            radius={8}
            pathOptions={SPOT_STYLE}
          >
            <Tooltip direction="top" offset={[0, -6]}>{s.name}</Tooltip>
            <Popup>
              <SpotPopup s={s} />
            </Popup>
          </CircleMarker>
        ))}
        {data.places.map((p) => (
          <Marker key={p.name} position={[p.lat, p.lng]} icon={placeIcon}>
            <Tooltip direction="top">{p.name}</Tooltip>
          </Marker>
        ))}
        {data.route && (
          <>
            {/* plan_route 只返回站点顺序、没有道路几何，所以画的是站点之间的直线示意 */}
            <Polyline positions={routeLine} pathOptions={ROUTE_STYLE} />
            {data.route.stops.map((s) => (
              <Marker key={`${s.order}-${s.name}`} position={[s.lat, s.lng]} icon={stopIcon(s.order)}>
                <Tooltip direction="top" offset={[0, -10]}>
                  {s.order}. {s.name}
                </Tooltip>
              </Marker>
            ))}
          </>
        )}
        <FitBounds points={points} reserveRight={data.route != null} />
      </MapContainer>

      {data.route && (
        <div className="route-card">
          <strong>
            路线 · {data.route.mode === 'driving' ? '驾车' : '步行'}
            {data.route.totalKm != null && ` · ${data.route.totalKm} km`}
            {data.route.totalMin != null && ` · 约 ${data.route.totalMin} 分钟`}
          </strong>
          <ol>
            {data.route.stops.map((s, i) => {
              const leg = data.route!.legs[i]
              return (
                <li key={`${s.order}-${s.name}`}>
                  {s.name}
                  {leg && (
                    <span className="muted">
                      {' '}↓ {leg.duration_min} 分钟{leg.suggest_transit ? '（建议乘车）' : ''}
                    </span>
                  )}
                </li>
              )
            })}
          </ol>
          {data.route.estimated && <div className="muted">路线服务不可用，时间为直线距离估算</div>}
        </div>
      )}
      {points.length === 0 && <div className="map-hint">问我一部作品的圣地，地点会出现在这里</div>}
    </section>
  )
}
