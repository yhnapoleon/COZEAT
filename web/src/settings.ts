import type { MapScene } from './types'

// 快捷设置：发送时补在句子后面，由槽位提取当普通条件读（和 demo/record.py 的 compose 保持一致）
export type Settings = { area: string; budget: number; dist: string; scene: string }
export const EMPTY: Settings = { area: '', budget: 0, dist: '', scene: '' }

export const AREAS = [
  { name: '海岸城', lng: 113.937635, lat: 22.518308 },
  { name: '深圳湾万象城', lng: 113.946529, lat: 22.51695 },
  { name: '南山科技园', lng: 113.94604, lat: 22.54461 },
]
export const BUDGETS = [50, 100, 200, 300]
export const DISTS = ['步行10分钟内', '1公里内']
export const SCENES = ['一人食', '约会', '朋友聚餐', '带娃', '带老人', '商务宴请']

// 记偏好、问路线、问规则、针对上轮某家店的话不补条件，补了会被当成新的找店条件或被记进长期偏好
const SKIP = /记住|忘掉|忘记|偏好|团购券|退款|退吗|过期|发票|预约|怎么去|怎么走|路线|第[一二三123]家|哪个好|就它|就定|这家|那家|它怎么样|换一批/

export function ctxOf(text: string, s: Settings): string[] {
  if (SKIP.test(text)) return []
  const parts: string[] = []
  const hasPlace = /附近|周边|一带|[城园湾区路]/.test(text)
  if (s.area && !hasPlace) parts.push(`在${s.area}附近`)
  if (s.budget && !/人均|预算|以内|块|元/.test(text)) parts.push(`人均${s.budget}以内`)
  if (s.dist && !/步行|走路|公里|米/.test(text)) parts.push(s.dist)
  if (s.scene && !text.includes(s.scene)) parts.push(s.scene)
  return parts
}

export const compose = (text: string, ctx: string[]) => (ctx.length ? `${text}（${ctx.join('，')}）` : text)

// 首页地图：三个商圈，点一下设为位置
export const AREA_SCENE: MapScene = {
  key: 'areas',
  points: AREAS.map((a) => ({ id: 'area:' + a.name, name: a.name, lng: a.lng, lat: a.lat, label: a.name, kind: 'area' })),
}
