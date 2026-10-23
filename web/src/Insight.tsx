import { useEffect, useState } from 'react'
import { brand } from './scene'
import type { Explain, ShopItem, ShopNotes } from './types'

const SRC_CLS: Record<string, string> = { 本轮: 'now', 本会话: 'sess', 长期偏好: 'long', 默认假设: 'def' }

// 评分标准细则：和 CODE_TRIM 的 dims() 一一对应
export const RULES: Record<string, string> = {
  口碑评分: '高德评分：3.5 分及以下 0 分，5.0 分 10 分，中间线性',
  笔记口碑: '小红书笔记里口味、环境、服务、性价比的均分（1–5 分换算到 0–10），疑似推广的笔记降权',
  距离: '说了范围时：范围内 10 分，超出按比例扣；没说时：0 米 10 分，每远 200 米扣 1 分',
  预算: '在预算范围内 10 分，超出按超出比例扣（超 50% 为 0），人均未知 4 分',
  品类: '店名或推荐菜里有你点的菜 10 分，同品类 8 分，不是这个品类 0 分',
  场景: '门店有这个场景标签 10 分，评价里写到 8 分，都没有 3 分',
  口味: '符合你的口味 10 分，中性 6 分，相反（比如不吃辣却是川湘菜）0 分',
  评价命中: '按你的要求在全部评价里检索，前 3 名 10 分，4–6 名 7 分，再往后 5 分，没命中 2 分',
  热度: '小红书笔记数，每篇 2 分，封顶 10 分',
  可信度: '参考笔记里不像推广的比例（模型按 CHASM 线索判断，仅供参考）',
}

export function ExplainPanel({ ex, onProfile }: { ex: Explain; onProfile?: () => void }) {
  const [open, setOpen] = useState(true)
  const f = ex.funnel || {}
  const steps: { t: string; n?: number; sub?: string }[] = []
  if (f.total != null) steps.push({ t: '5 公里内', n: f.total })
  for (const c of f.conds || []) steps.push({ t: c.k, n: c.ok, sub: c.v })
  if (f.after != null) steps.push({ t: '进入候选', n: f.after, sub: f.relaxed ? '已放宽条件' : undefined })
  if (f.scene?.filtered) steps.push({ t: '场景对得上', n: f.scene.ok, sub: f.scene.v })
  if (f.to_llm != null) steps.push({ t: '交给推荐生成', n: f.to_llm })
  if (f.picked != null) steps.push({ t: '推荐', n: f.picked })
  const longs = ex.conds.filter((c) => c.src === '长期偏好')
  return (
    <div className={`explain ${open ? 'open' : ''}`}>
      <button className="ex-h" onClick={() => setOpen((v) => !v)}>
        <span className="ex-i">?</span>为什么是这几家
        {longs.length > 0 && <span className="ex-tip">用了 {longs.length} 条你的长期偏好</span>}
        <span className="ex-arrow">{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <div className="ex-b">
          <div className="ex-row">
            <span className="ex-lab">条件</span>
            <div className="conds">
              {ex.conds.map((c, i) => (
                <span key={i} className={`cond ${SRC_CLS[c.src] || ''}`} onClick={c.src === '长期偏好' ? onProfile : undefined}>
                  <b>{c.k}</b>
                  {c.v}
                  <em>{c.src}</em>
                </span>
              ))}
            </div>
          </div>
          {steps.length > 1 && (
            <div className="ex-row">
              <span className="ex-lab">筛选</span>
              <div className="funnel">
                {steps.map((s, i) => (
                  <div className="fstep" key={i}>
                    <div className="fn">{s.n}</div>
                    <div className="ft">{s.t}</div>
                    {s.sub && <div className="fs">{s.sub}</div>}
                    <div className="fbar" style={{ height: `${Math.max(6, ((s.n || 0) / Math.max(1, f.total || 1)) * 34)}px` }} />
                  </div>
                ))}
              </div>
            </div>
          )}
          {f.taste && (f.taste.down.length > 0 || f.taste.up.length > 0) && (
            <div className="ex-row">
              <span className="ex-lab">口味</span>
              <div className="ex-taste">
                按「{f.taste.pref}」
                {f.taste.up.length > 0 && (
                  <>
                    ，<span className="up">加分 {f.taste.up.length} 家</span>（{f.taste.up.slice(0, 3).map(brand).join('、')}）
                  </>
                )}
                {f.taste.down.length > 0 && (
                  <>
                    ，<span className="down">往后排 {f.taste.down.length} 家</span>（{f.taste.down.slice(0, 3).map(brand).join('、')}）
                  </>
                )}
              </div>
            </div>
          )}
          {f.relaxed && <div className="ex-note">{f.relaxed}</div>}
          <div className="ex-foot">综合分决定哪几家交给推荐生成；最后几家由推荐生成对照评价原文、场景和你的要求挑出来，所以不一定按综合分排。</div>
        </div>
      )}
    </div>
  )
}

function ago(ts?: string) {
  const n = Number(ts || 0)
  if (!n) return ''
  const d = new Date(n > 1e12 ? n : n * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

const LABEL_CLS: Record<string, string> = { 真实体验: 'real', 存疑: 'unsure', 疑似推广: 'ad', 明确合作: 'ad' }

export function ScoreDetail({ s, weights }: { s: ShopItem; weights?: Record<string, number> }) {
  const [notes, setNotes] = useState<ShopNotes | null>(null)
  const [showRules, setShowRules] = useState(false)
  useEffect(() => {
    fetch(`/api/shop/${encodeURIComponent(s.amap_id)}/notes`)
      .then((r) => r.json())
      .then(setNotes)
      .catch(() => setNotes(null))
  }, [s.amap_id])
  const dims = Object.entries(s.dims || {})
  const tw = dims.reduce((a, [k]) => a + (weights?.[k] || 0), 0) || 1
  return (
    <div className="detail" onClick={(e) => e.stopPropagation()}>
      {dims.length > 0 && (
        <div className="dsec">
          <div className="dsec-h">
            评分明细 <span>综合 {s.score?.toFixed(1)} = 各项按权重加权平均</span>
            <button onClick={() => setShowRules((v) => !v)}>{showRules ? '收起标准' : '评分标准'}</button>
          </div>
          <div className="dims">
            {dims.map(([k, v]) => (
              <div className="dim" key={k} title={RULES[k]}>
                <span className="dk">{k}</span>
                <span className="dw">{Math.round(((weights?.[k] || 0) / tw) * 100)}%</span>
                <span className="db">
                  <i className={v >= 8 ? 'hi' : v >= 5 ? 'mid' : 'lo'} style={{ width: `${v * 10}%` }} />
                </span>
                <span className="dv">{v.toFixed(1)}</span>
                {showRules && <span className="dr">{RULES[k]}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="dsec">
        <div className="dsec-h">
          小红书口碑
          {notes && notes.n_notes > 0 && (
            <span>
              {notes.n_notes} 篇笔记{notes.n_ad > 0 ? `，${notes.n_ad} 篇疑似推广已降权` : ''}
            </span>
          )}
        </div>
        {!notes && <div className="dempty-s">读取中…</div>}
        {notes && notes.n_notes === 0 && <div className="dempty-s">这家店没有对得上的小红书笔记，口碑只看高德评分。</div>}
        {notes && Object.keys(notes.aspects).length > 0 && (
          <div className="aspects">
            {Object.entries(notes.aspects).map(([k, a]) => (
              <div className="asp" key={k}>
                <div className="av">{a.score.toFixed(1)}</div>
                <div className="ak">{k}</div>
                <div className="an">{a.n} 条</div>
              </div>
            ))}
          </div>
        )}
        {notes &&
          notes.notes.map((n) => (
            <a className="note" key={n.note_id} href={n.url} target="_blank" rel="noreferrer">
              <div className="note-h">
                <span className={`cred ${LABEL_CLS[n.label] || ''}`}>{n.label}</span>
                <span className="nt">{n.title || '（无标题）'}</span>
                <span className="nm2">
                  {ago(n.ts)} · ♥ {n.likes || 0} · 💬 {n.ncomments || 0}
                </span>
              </div>
              {n.quote && <div className="nq">“{n.quote.slice(0, 80)}{n.quote.length > 80 ? '…' : ''}”</div>}
              {(n.cues.length > 0 || n.rule_cues.length > 0) && (
                <div className="ncues">
                  判断依据：{[...n.rule_cues, ...n.cues].slice(0, 4).join(' · ')}
                </div>
              )}
            </a>
          ))}
        {notes && notes.n_notes > 0 && (
          <div className="disclaim">可信度由模型参照小红书隐蔽广告研究（CHASM）的线索判断，同类方法零样本准确率约六成，仅供参考。</div>
        )}
      </div>
    </div>
  )
}
