export type Deal = {
  name: string
  price?: number | null
  orig_price?: number | null
  content?: string | null
  src?: string | null
  synthetic?: boolean
}

export type ShopItem = {
  amap_id: string
  name: string
  cate?: string
  rating?: number | string
  cost?: number | string | null
  dist_m?: number
  reason?: string
  matched?: string[]
  lng?: number | string
  lat?: number | string
  addr?: string
  deals?: Deal[]
  score?: number
  dims?: Record<string, number>
  scene_ok?: boolean
  need_hit?: boolean
}

export type Place = { name: string; lng: number | string; lat: number | string }

export type ShopListCard = {
  type: 'shop_list'
  anchor: string
  anchor_lng?: number | null
  anchor_lat?: number | null
  items: ShopItem[]
  explain?: Explain
}
export type CompareCard = { type: 'compare'; items: ShopItem[] }
export type RouteCard = {
  type: 'route'
  origin: Place
  dest: Place
  walk?: { m: number; min: number } | null
  drive?: { m: number; min: number; taxi_yuan?: string } | null
  transit?: { min: number; lines?: string } | null
  mode: string
  poly: string
}
export type DealsCard = {
  type: 'deals'
  items: { amap_id: string; name: string; lng?: number | string; lat?: number | string; deals: Deal[] }[]
}
export type Card = ShopListCard | CompareCard | RouteCard | DealsCard

export type Reply = {
  markdown: string
  cards: Card[]
  options: string[]
  elapsed_s?: number
  error?: string
}

export type Msg =
  | { id: number; role: 'user'; text: string; ctx?: string[] }
  | { id: number; role: 'bot'; reply: Reply }
  | { id: number; role: 'wait'; kind: string }
  | { id: number; role: 'sys'; text: string }

export type ScriptStep = {
  say: string
  via: 'type' | 'click'
  new_conv: boolean
  set?: Partial<{ area: string; budget: number; dist: string; scene: string }>
  ctx?: string[]
  resp: Reply
  profile?: Profile
}
export type Script = { title: string; user_id: string; recorded_at: string; steps: ScriptStep[] }

export type MapPoint = {
  id: string
  name: string
  lng: number
  lat: number
  label: string
  kind: 'shop' | 'anchor' | 'origin' | 'dest' | 'area'
}
export type MapScene = { key: string; points: MapPoint[]; path?: [number, number][]; mode?: string }

// 解释和评分（CF-main v0.0.19）
export type Cond = { k: string; v: string; src: string }
export type Funnel = {
  total?: number
  conds?: { k: string; v: string; ok: number }[]
  after?: number
  relaxed?: string
  taste?: { pref: string; down: string[]; up: string[] }
  weights?: Record<string, number>
  top?: number
  evid?: number
  scene?: { v: string; ok: number; filtered: boolean }
  kb?: number
  to_llm?: number
  picked?: number
}
export type Explain = { conds: Cond[]; funnel: Funnel }

// "我的口味"页（demo/memview.py）
export type PrefItem = {
  id: number
  key: string
  name: string
  value: string
  status: string
  explicit: boolean
  support?: number
  consistency?: number
  confidence?: number
  text: string
  conv: string
  time?: string
}
export type PendingItem = { key: string; name: string; value: string; texts: string[]; conf: number; sessions: number; need: number; op: string }
export type FeedbackItem = { id: number; label: string; shop: string; cate: string; cost?: number; others: string[]; reason: string; time?: string }
export type Profile = {
  uid: string
  active: PrefItem[]
  pending: PendingItem[]
  feedback: FeedbackItem[]
  strategy: { text: string; updated_at?: string } | null
  history: PrefItem[]
  rule: { support: number; consistency: number; stable: number }
}

// 评价来源（/api/shop/{id}/notes）
export type NoteRef = {
  note_id: string
  title: string
  ts?: string
  likes?: string
  collects?: string
  ncomments?: string
  ad: number
  label: string
  cues: string[]
  rule_cues: string[]
  url: string
  quote?: string
  cons?: string
}
export type ShopNotes = {
  aspects: Record<string, { score: number; n: number }>
  ugc_score: number | null
  cred: number | null
  n_notes: number
  n_ad: number
  notes: NoteRef[]
}
