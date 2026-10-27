// 图文巡礼攻略卡片：plan_pilgrimage 的结果。讲解见 docs/11-pilgrimage-planner.md「攻略卡片」
//
// 图片、集数、时间、链接全部来自工具返回的结构化数据，不让 LLM 抄写——
// 和地图不解析 LLM 文字是同一个原则（LLM 抄坐标会抄错，抄 URL 一样会错）。LLM 只写导语和看点
import { type Guide, type GuideAccess, formatDuration } from '../chat/guide'

function AccessBlock({ a, startName }: { a: GuideAccess; startName?: string }) {
  return (
    <div className="guide-access">
      <div className="guide-access-head">
        <span className="guide-icon" aria-hidden>🚃</span>
        <strong>
          {startName ?? '出发地'} → {a.toSpot ?? '第一站'}
        </strong>
        <span className="muted">
          {a.depart && a.arrive ? `${a.depart} 出发 · ${a.arrive} 到 · ` : ''}约 {a.durationMin} 分钟
          {a.transfers != null && a.rides.length > 0 ? ` · 换乘 ${a.transfers} 次` : ''}
        </span>
        {a.estimated && <span className="tag tag-warn">估算</span>}
      </div>
      {a.rides.length > 0 && (
        <ol className="guide-rides">
          {a.rides.map((r, i) => (
            <li key={i}>
              <span className="ride-line">{r.line}</span>
              {r.headsign && <span className="muted">（{r.headsign} 方向）</span>} {r.from} {r.depart} → {r.to} {r.arrive}
            </li>
          ))}
        </ol>
      )}
      {a.note && <div className="muted small">{a.note}</div>}
      <div className="small">
        {a.googleMapsUrl && (
          <a href={a.googleMapsUrl} target="_blank" rel="noreferrer">
            在 Google Maps 查实时换乘
          </a>
        )}
        {a.source && <span className="muted"> · {a.source}</span>}
      </div>
    </div>
  )
}

export function GuideCard({ guide }: { guide: Guide }) {
  const g = guide
  return (
    <section className="guide">
      <header className="guide-head">
        <div className="guide-title">
          《{g.animeTitle}》巡礼 · {g.areaName}一带
        </div>
        <div className="muted small">
          {g.stops.length} 处 · 共 {formatDuration(g.totalMin)}
          {g.budgetMin ? `（预算 ${formatDuration(g.budgetMin)}）` : ''} · 步行 {g.walkKm} km
        </div>
      </header>

      {g.access && <AccessBlock a={g.access} startName={g.start?.name} />}

      <ol className="guide-stops">
        {g.stops.map((s, i) => (
          <li key={s.spotId} className="guide-stop">
            {i > 0 && s.walkFromPrevMin > 0 && <div className="guide-walk">🚶 步行约 {s.walkFromPrevMin} 分钟</div>}
            <div className="guide-stop-body">
              <span className="stop-badge">{s.order}</span>
              {s.image && <img src={s.image} alt={s.name} loading="lazy" />}
              <div className="guide-stop-text">
                <div className="guide-stop-name">
                  <strong>{s.name}</strong>
                  {s.nameJa && s.nameJa !== s.name && <span className="muted small"> {s.nameJa}</span>}
                </div>
                <div className="muted small">
                  {s.arriveAt && `${s.arriveAt} 到 · `}停留约 {s.dwellMin} 分钟
                  {s.episodes.length > 0 &&
                    ` · 第 ${s.episodes.slice(0, 5).join('、')}${s.episodes.length > 5 ? ' 等' : ''} 集`}
                  {s.scenesCount > 1 && ` · ${s.scenesCount} 个镜头`}
                </div>
                {s.notes.map((n, j) => (
                  <div className="guide-note" key={j}>
                    {n}
                  </div>
                ))}
                {s.originUrl && (
                  <a className="small" href={s.originUrl} target="_blank" rel="noreferrer">
                    在 Anitabi 看所有镜头
                  </a>
                )}
              </div>
            </div>
          </li>
        ))}
      </ol>

      <footer className="guide-foot small">
        {g.walkingRouteUrl && (
          <a href={g.walkingRouteUrl} target="_blank" rel="noreferrer">
            在 Google Maps 打开步行路线
          </a>
        )}
        {g.omitted.length > 0 && (
          <details>
            <summary>没有安排的 {g.omitted.length} 处</summary>
            <ul>
              {g.omitted.map((o, i) => (
                <li key={i}>
                  {o.name}：<span className="muted">{o.reason}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
        {g.otherAreas.length > 0 && (
          <div className="muted">
            这部作品的其他巡礼片区：{g.otherAreas.map((o) => `${o.name}一带（${o.spots} 处）`).join('、')}
          </div>
        )}
        {g.assumptions.map((a, i) => (
          <div className="muted" key={i}>
            ※ {a}
          </div>
        ))}
        <div className="muted">
          {g.source ?? '圣地数据：Anitabi（CC BY-NC-SA 4.0）'} · 时间均为参考，出发前请以官方信息为准
        </div>
      </footer>
    </section>
  )
}
