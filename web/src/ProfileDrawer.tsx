import { useState } from 'react'
import type { Profile } from './types'

// 每类偏好在推荐里怎么用（对应 CODE_MERGE、CODE_TRIM、recommend.v8 的规则）
const HOW: Record<string, string> = {
  avoid: '硬条件：主打这类口味的店不推，偏辣的店往后排',
  taste: '没说口味时默认带上，按推荐菜和评价软加减分',
  cuisine_like: '推荐生成时参考，同等条件下优先',
  cuisine_dislike: '推荐生成时参考，同等条件下靠后',
  budget: '这次没说预算时，默认按它筛',
  area: '这次没说地点时，默认在这里找',
  scene: '推荐生成时参考',
}

const KEY_ICON: Record<string, string> = {
  avoid: '🚫', taste: '🌶️', cuisine_like: '❤️', cuisine_dislike: '👎', budget: '¥', area: '📍', scene: '✨',
}

function ago(t?: string) {
  if (!t) return ''
  const d = new Date(t.replace(' ', 'T'))
  const s = (Date.now() - d.getTime()) / 1000
  if (!isFinite(s)) return ''
  if (s < 90) return '刚刚'
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`
  return `${Math.round(s / 86400)} 天前`
}

type Props = {
  profile: Profile | null
  loading: boolean
  demo: boolean
  onClose: () => void
  onRefresh: () => void
  onConsolidate: () => void
  onForget: (text: string) => void
}

export default function ProfileDrawer({ profile, loading, demo, onClose, onRefresh, onConsolidate, onForget }: Props) {
  const [showHist, setShowHist] = useState(false)
  const p = profile
  const empty = p && !p.active.length && !p.pending.length && !p.feedback.length && !p.strategy
  return (
    <aside className="drawer">
      <div className="drawer-h">
        <div>
          <div className="dh1">我的口味画像</div>
          <div className="dh2">{demo ? '演示用户 · 跟着对话实时更新' : '你和助手聊天时，它记下的偏好'}</div>
        </div>
        <div className="spacer" />
        {!demo && (
          <>
            <button className="ghost sm" onClick={onRefresh} disabled={loading}>
              刷新
            </button>
            <button className="ghost sm" onClick={onConsolidate} disabled={loading} title="平时在你停止聊天 2 分钟后自动整理">
              立即整理
            </button>
          </>
        )}
        <button className="ghost icon sm" onClick={onClose} aria-label="关闭">
          ✕
        </button>
      </div>

      <div className="drawer-b">
        {loading && !p && <div className="dempty">读取中…</div>}
        {empty && <div className="dempty">还没有记住你的偏好。可以直接说"记住我不吃香菜"，或者多聊几次，助手会从对话里归纳。</div>}

        {p && p.active.length > 0 && (
          <section>
            <div className="sec-h">
              生效中 <span className="cnt">{p.active.length}</span>
              <span className="sec-s">推荐时会用到</span>
            </div>
            <div className="prefs">
              {p.active.map((a) => (
                <div className="pref" key={a.id}>
                  <div className="pref-top">
                    <span className="pk">
                      <i>{KEY_ICON[a.key] || '•'}</i>
                      {a.name}
                    </span>
                    <span className="pv">{a.key === 'avoid' ? `不吃${a.value}` : a.value}</span>
                    <span className={`psrc ${a.explicit ? 'ex' : 'ind'}`}>{a.explicit ? '你明确说的' : `从 ${a.support || 2} 个会话归纳`}</span>
                    {!demo && (
                      <button className="pforget" onClick={() => onForget(`忘掉${a.key === 'avoid' ? '我不吃' : a.name}${a.value}`)}>
                        忘掉
                      </button>
                    )}
                  </div>
                  <div className="pref-how">→ {HOW[a.key] || '推荐时参考'}</div>
                  {a.text && <div className="pref-q">“{a.text}” · {ago(a.time)}</div>}
                </div>
              ))}
            </div>
          </section>
        )}

        {p && p.pending.length > 0 && (
          <section>
            <div className="sec-h">
              待确认 <span className="cnt">{p.pending.length}</span>
              <span className="sec-s">在 {p.rule.support} 个不同会话里出现过，或判断很稳定，才会记上</span>
            </div>
            <div className="pends">
              {p.pending.map((x) => (
                <div className="pend" key={x.key + x.value}>
                  <div className="pend-top">
                    <span className="pk">
                      <i>{KEY_ICON[x.key] || '•'}</i>
                      {x.name}
                    </span>
                    <span className="pv">{x.value}</span>
                    <span className="pend-n">
                      {x.sessions}/{p.rule.support} 个会话
                    </span>
                  </div>
                  <div className="bar">
                    <i style={{ width: `${Math.min(100, (x.sessions / p.rule.support) * 100)}%` }} />
                  </div>
                  <div className="pref-q">
                    {x.texts[0] ? `“${x.texts[0]}”` : ''}
                    {x.need > 0 ? ` · 再出现 ${x.need} 次就记上` : ' · 下次整理时会生效'}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {p?.strategy && (
          <section>
            <div className="sec-h">
              推荐策略 <span className="sec-s">根据你的选择总结，推荐生成时参考</span>
            </div>
            <div className="strategy">{p.strategy.text}</div>
          </section>
        )}

        {p && p.feedback.length > 0 && (
          <section>
            <div className="sec-h">
              最近的选择 <span className="sec-s">用来总结推荐策略</span>
            </div>
            <div className="fbs">
              {p.feedback.slice(0, 6).map((f) => (
                <div className={`fb ${f.label}`} key={f.id}>
                  <span className="fbi">{f.label === 'accept' ? '✓' : '✕'}</span>
                  <div>
                    <div>
                      {f.label === 'accept' ? '选了' : '没要'} <b>{f.shop}</b>
                      {f.cate && <span className="fbc">{f.cate}</span>}
                    </div>
                    {f.others.length > 0 && <div className="fbo">同屏还有：{f.others.join('、')}</div>}
                    {f.reason && <div className="fbo">“{f.reason}” · {ago(f.time)}</div>}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        <section>
          <div className="sec-h">记忆怎么维护</div>
          <div className="flow">
            <div className="fl">
              <b>1</b>每轮对话抽取偏好和你对推荐的反应
            </div>
            <div className="fl">
              <b>2</b>你明确说"记住 / 忘掉"的，当轮生效
            </div>
            <div className="fl">
              <b>3</b>"今天想吃辣"这种只管这一次的，不进长期记忆
            </div>
            <div className="fl">
              <b>4</b>其余先放进待确认；你停止聊天 2 分钟后整理一次
            </div>
            <div className="fl">
              <b>5</b>在 {p?.rule.support ?? 2} 个不同会话出现、前后一致（≥{Math.round((p?.rule.consistency ?? 0.67) * 100)}%）或判断稳定（≥
              {Math.round((p?.rule.stable ?? 0.8) * 100)}%）才生效；30 天没再出现就过期
            </div>
            <div className="fl">
              <b>6</b>这次说的和长期偏好冲突时，以这次为准，本会话内不再用旧的
            </div>
          </div>
        </section>

        {p && p.history.length > 0 && (
          <section>
            <button className="sec-h toggle" onClick={() => setShowHist((v) => !v)}>
              历史 <span className="cnt">{p.history.length}</span>
              <span className="sec-s">已删除、被替换、过期的 {showHist ? '▴' : '▾'}</span>
            </button>
            {showHist &&
              p.history.map((h) => (
                <div className="hist" key={h.id}>
                  <span className="pk">{h.name}</span> {h.value} <span className="hs">{h.status}</span>
                </div>
              ))}
          </section>
        )}
      </div>
    </aside>
  )
}
