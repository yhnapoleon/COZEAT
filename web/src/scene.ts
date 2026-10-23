import type { Card, MapPoint, MapScene, Reply } from './types'

const num = (x: unknown): number | null => {
  const v = typeof x === 'string' ? parseFloat(x) : typeof x === 'number' ? x : NaN
  return Number.isFinite(v) && v !== 0 ? v : null
}

// 店名去掉括号里的分店说明，地图标签用
export const brand = (name: string) => name.split(/[(（]/)[0].trim()

function point(id: string, name: string, lng: unknown, lat: unknown, label: string, kind: MapPoint['kind']): MapPoint | null {
  const x = num(lng)
  const y = num(lat)
  return x && y ? { id, name, lng: x, lat: y, label, kind } : null
}

function fromCard(c: Card): Omit<MapScene, 'key'> | null {
  const pts: (MapPoint | null)[] = []
  if (c.type === 'shop_list') {
    pts.push(point('anchor', c.anchor, c.anchor_lng, c.anchor_lat, c.anchor.replace(/^南山区/, ''), 'anchor'))
    c.items.forEach((s, i) => pts.push(point(s.amap_id, s.name, s.lng, s.lat, String(i + 1), 'shop')))
  } else if (c.type === 'compare' || c.type === 'deals') {
    c.items.forEach((s, i) => pts.push(point(s.amap_id, s.name, s.lng, s.lat, String(i + 1), 'shop')))
  } else if (c.type === 'route') {
    pts.push(point('origin', c.origin.name, c.origin.lng, c.origin.lat, '起', 'origin'))
    pts.push(point('dest', c.dest.name, c.dest.lng, c.dest.lat, '终', 'dest'))
    const path = (c.poly || '')
      .split(';')
      .map((xy) => xy.split(',').map(Number) as [number, number])
      .filter((p) => p.length === 2 && p.every(Number.isFinite))
    const ok = pts.filter(Boolean) as MapPoint[]
    return ok.length ? { points: ok, path: path.length > 1 ? path : undefined, mode: c.mode } : null
  }
  const ok = pts.filter(Boolean) as MapPoint[]
  return ok.length ? { points: ok } : null
}

export function sceneOf(reply: Reply, key: string): MapScene | null {
  for (const c of reply.cards) {
    const s = fromCard(c)
    if (s) return { key, ...s }
  }
  return null
}
