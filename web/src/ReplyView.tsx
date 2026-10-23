import { useState } from 'react'
import Markdown from 'react-markdown'
import { ExplainPanel, ScoreDetail } from './Insight'
import remarkGfm from 'remark-gfm'
import type { Deal, DealsCard, Reply, RouteCard, ShopItem, ShopListCard } from './types'

const md = (s: string) => <Markdown remarkPlugins={[remarkGfm]}>{s}</Markdown>

function costText(c: ShopItem['cost']) {
  const v = typeof c === 'string' ? parseFloat(c) : c
  return v && v > 0 ? `¥${Math.round(v)}/人` : '人均未知'
}

function distText(m?: number) {
  if (m == null) return ''
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${m} m`
}

// 推荐格式化（CODE_FORMAT）每家店一段："**1. 店名**（品类）"、评分行，后面是"标签：内容"的几行
function splitShops(text: string) {
  const pre: string[] = []
  const secs: string[][] = []
  for (const line of text.split('\n')) {
    if (/^\*\*\d+\.\s/.test(line)) {
      secs.push([])
      continue
    }
    if (secs.length) secs[secs.length - 1].push(line)
    else pre.push(line)
  }
  return { pre: pre.join('\n').trim(), secs }
}

function Row({ line }: { line: string }) {
  const m = line.match(/^([^：]{1,32})：(.*)$/)
  if (!m) return line.trim() ? <div className="row plain">{line}</div> : null
  let [, label, body] = m
  let note = ''
  const g = label.match(/^团购（(.+)）$/)
  if (g) {
    label = '团购'
    note = g[1]
  }
  if (label === '符合') {
    return (
      <div className="row">
        <span className="lab">符合</span>
        <span className="chips">
          {body.split('、').map((x, i) => (
            <span className="chip ok" key={i}>
              {x.replace(/^✓/, '')}
            </span>
          ))}
        </span>
      </div>
    )
  }
  return (
    <div className={`row r-${label}`}>
      <span className="lab">{label}</span>
      <span className="val">
        {body}
        {note && <span className={`tag ${note.includes('示例') ? 'demo' : 'src'}`}>{note}</span>}
      </span>
    </div>
  )
}

type ShopListProps = { card: ShopListCard; text: string; focus: string | null; onFocus: (id: string) => void; onProfile?: () => void }

function ShopList({ card, text, focus, onFocus, onProfile }: ShopListProps) {
  const { pre, secs } = splitShops(text)
  const [open, setOpen] = useState<string | null>(null)
  return (
    <div className="shoplist">
      {pre && <div className="md">{md(pre)}</div>}
      {card.explain && <ExplainPanel ex={card.explain} onProfile={onProfile} />}
      {card.items.map((s, i) => {
        const lines = (secs[i] || []).slice(1) // 第一行是评分、人均、距离，用卡片数据显示
        return (
          <div key={s.amap_id} className={`shop ${focus === s.amap_id ? 'on' : ''}`} onClick={() => onFocus(s.amap_id)}>
            <div className="shop-h">
              <span className="no">{i + 1}</span>
              <div className="shop-t">
                <div className="nm">{s.name}</div>
                <div className="meta">
                  {s.cate && <span className="cate">{s.cate}</span>}
                  <span className="star">
                    <svg viewBox="0 0 24 24">
                      <path d="M12 2.5l2.9 6.1 6.6.8-4.9 4.6 1.3 6.6L12 17.3l-5.9 3.3 1.3-6.6L2.5 9.4l6.6-.8z" />
                    </svg>
                    {s.rating}
                  </span>
                  <span>{costText(s.cost)}</span>
                  <span>距{card.anchor.replace(/^南山区/, '')} {distText(s.dist_m)}</span>
                </div>
              </div>
              {s.score != null && (
                <div className="score" title="综合分（0–10），排序用的就是它">
                  <b>{s.score.toFixed(1)}</b>
                  <span>综合</span>
                </div>
              )}
            </div>
            <div className="shop-b">
              {lines.map((l, k) => (
                <Row key={k} line={l} />
              ))}
            </div>
            <button
              className={`more ${open === s.amap_id ? 'on' : ''}`}
              onClick={(e) => {
                e.stopPropagation()
                onFocus(s.amap_id)
                setOpen((o) => (o === s.amap_id ? null : s.amap_id))
              }}
            >
              {open === s.amap_id ? '收起' : '评分明细 · 评价来源'}
              <span>{open === s.amap_id ? '▴' : '▾'}</span>
            </button>
            {open === s.amap_id && <ScoreDetail s={s} weights={card.explain?.funnel.weights} />}
          </div>
        )
      })}
    </div>
  )
}

function RouteView({ card, text }: { card: RouteCard; text: string }) {
  const rest = text
    .split('\n')
    .filter((l) => !l.startsWith('- ') && !/^从.*到 \*\*|^到 \*\*/.test(l))
    .join('\n')
    .trim()
  const ICON: Record<string, string> = {
    walk: 'M13 4a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm-3 3.5L7 22h2.2l1.8-7 2 2v5h2v-6.5l-2-2 .6-3A7 7 0 0 0 18 13v-2a5 5 0 0 1-4-2l-1-1.6c-.4-.6-1-.9-1.7-.9-.3 0-.5 0-.8.1L6 8.3V13h2V9.6l2-.8',
    transit: 'M4 16a2 2 0 0 0 2 2l-1 1v1h2l2-2h6l2 2h2v-1l-1-1a2 2 0 0 0 2-2V6c0-3.5-3.6-4-8-4S4 2.5 4 6v10zm2-9h12v5H6V7zm1.5 9a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm9 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3z',
    drive: 'M5 11l1.5-4.5h11L19 11m-1 6H6a1 1 0 0 1-1-1v-5h14v5a1 1 0 0 1-1 1zM7 17v2H5v-2m14 0v2h-2v-2M7.5 13.5h.01M16.5 13.5h.01',
  }
  const modes = [
    card.walk && { k: 'walk', t: '步行', v: card.walk.min, s: distText(card.walk.m) },
    card.transit && { k: 'transit', t: '公交地铁', v: card.transit.min, s: card.transit.lines || '' },
    card.drive && { k: 'drive', t: '打车', v: card.drive.min, s: card.drive.taxi_yuan ? `约 ${card.drive.taxi_yuan} 元` : distText(card.drive.m) },
  ].filter(Boolean) as { k: string; t: string; v: number; s: string }[]
  return (
    <div className="route">
      <div className="route-h">
        <span className="pin o">起</span>
        <span className="rn">{card.origin.name}</span>
        <span className="arrow">
          <span>→</span>
        </span>
        <span className="pin d">终</span>
        <span className="rn">{card.dest.name}</span>
      </div>
      <div className="modes">
        {modes.map((m) => (
          <div key={m.k} className={`mode ${card.mode === m.k ? 'on' : ''}`}>
            <div className="mt">
              <svg viewBox="0 0 24 24" fill={m.k === 'walk' ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth={m.k === 'walk' ? 0 : 1.8} strokeLinecap="round" strokeLinejoin="round">
                <path d={ICON[m.k]} />
              </svg>
              {m.t}
            </div>
            <div className="mv">
              {m.v}
              <small>分钟</small>
            </div>
            <div className="ms">{m.s}</div>
          </div>
        ))}
      </div>
      {rest && <div className="md">{md(rest)}</div>}
    </div>
  )
}

function DealView({ d }: { d: Deal }) {
  return (
    <div className="deal">
      <div className="deal-l">
        <div className="dn">{d.name}</div>
        {d.content && <div className="dc">{d.content}</div>}
        <span className={`tag ${d.synthetic ? 'demo' : 'src'}`}>{d.synthetic ? '示例数据，非真实优惠' : `${d.src || '笔记'} 提到，以店内为准`}</span>
      </div>
      <div className="deal-r">
        {d.price != null && (
          <div className="dp">
            <small>¥</small>
            {d.price}
          </div>
        )}
        {d.orig_price != null && <div className="do">¥{d.orig_price}</div>}
      </div>
    </div>
  )
}

function DealsView({ card }: { card: DealsCard }) {
  return (
    <div className="deals">
      {card.items.map((s) => (
        <div key={s.amap_id}>
          <div className="deals-h">{s.name}</div>
          {s.deals.length ? s.deals.map((d, i) => <DealView key={i} d={d} />) : <div className="row plain">没查到这家的团购信息。</div>}
        </div>
      ))}
    </div>
  )
}

type Props = {
  reply: Reply
  focus: string | null
  onFocus: (id: string) => void
  onProfile?: () => void
  onOption?: (text: string) => void
  pressing?: string | null
}

export default function ReplyView({ reply, focus, onFocus, onOption, pressing, onProfile }: Props) {
  const card = reply.cards[0]
  let body
  if (reply.error && !reply.markdown) body = <div className="err">出错了：{reply.error}</div>
  else if (card?.type === 'shop_list') body = <ShopList card={card} text={reply.markdown} focus={focus} onFocus={onFocus} onProfile={onProfile} />
  else if (card?.type === 'route') body = <RouteView card={card} text={reply.markdown} />
  else if (card?.type === 'deals') body = <DealsView card={card} />
  else body = <div className={`md ${card?.type === 'compare' ? 'cmp' : ''}`}>{md(reply.markdown)}</div>
  return (
    <>
      {body}
      {reply.options.length > 0 && (
        <div className="opts">
          {reply.options.map((o) => (
            <button key={o} className={`opt ${pressing === o ? 'press' : ''}`} disabled={!onOption} onClick={() => onOption?.(o)}>
              {o}
            </button>
          ))}
        </div>
      )}
    </>
  )
}
