import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Composer from './Composer'
import Logo from './Logo'
import MapView from './MapView'
import ProfileDrawer from './ProfileDrawer'
import ReplyView from './ReplyView'
import Welcome from './Welcome'
import { sceneOf } from './scene'
import { AREA_SCENE, compose, ctxOf, EMPTY, type Settings } from './settings'
import type { MapScene, Msg, Profile, Reply, Script } from './types'

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const params = new URLSearchParams(location.search)

function store(k: string, v?: string): string | null {
  try {
    if (v !== undefined) localStorage.setItem(k, v)
    return localStorage.getItem(k)
  } catch {
    return v ?? null
  }
}

// 实时模式：每个浏览器一个 user_id（长期记忆按它隔离），"新对话"换会话名
const USER_ID = store('la-uid') || store('la-uid', 'web-' + Math.random().toString(36).slice(2, 10))!

// 等待时的提示，按回答类型轮换（演示模式知道下一条是什么类型，实时模式用通用的）
const HINTS: Record<string, string[]> = {
  shop_list: ['正在理解你的需求…', '在附近找符合条件的店…', '翻小红书上的评价…', '对照你的偏好挑店…', '写推荐理由…'],
  compare: ['找出你说的是哪几家…', '翻两家的评价…', '整理对比…'],
  route: ['查路线…', '算步行、公交和打车的时间…'],
  deals: ['查这家的团购…'],
  any: ['正在理解你的需求…', '查数据…', '组织回答…', '快好了…'],
}

function quickActions(last: Reply | null): string[] {
  if (!last) return []
  const c = last.cards[0]
  if (c?.type === 'shop_list' && c.items.length >= 2) return ['第一家和第二家哪个好', '第一家怎么去', '第一家有团购吗', '换一批']
  if (/就定 \*\*/.test(last.markdown)) return ['从科技园过去怎么走', '有团购吗', '它怎么样']
  return []
}

export default function App() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [focus, setFocus] = useState<string | null>(null)
  const [convNo, setConvNo] = useState(1)
  const [mode, setMode] = useState<'live' | 'demo'>(params.get('mode') === 'live' ? 'live' : 'demo')
  const [scripts, setScripts] = useState<{ name: string; title: string }[]>([])
  const [scriptName, setScriptName] = useState(params.get('script') || 'default')
  const [script, setScript] = useState<Script | null>(null)
  const [step, setStep] = useState(0)
  const [waitMode, setWaitMode] = useState<'fast' | 'real'>(params.get('wait') === 'real' ? 'real' : 'fast')
  const [pressing, setPressing] = useState<string | null>(null)
  const [settings, setSettings] = useState<Settings>(EMPTY)
  const [flash, setFlash] = useState<keyof Settings | null>(null)
  const [panel, setPanel] = useState(false)
  const [hint, setHint] = useState(0)
  const [drawer, setDrawer] = useState(params.get('profile') === '1')
  const [liveProf, setLiveProf] = useState<Profile | null>(null)
  const [profLoading, setProfLoading] = useState(false)
  const idRef = useRef(1)
  const listRef = useRef<HTMLDivElement>(null)
  const busyRef = useRef(false)
  const demo = mode === 'demo'

  const nid = () => idRef.current++
  const lastBot = useMemo(() => {
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      if (m.role === 'bot') return m
      if (m.role === 'sys') return null
    }
    return null
  }, [msgs])
  // 地图跟着最近一条带坐标的回答走；还没开始聊时显示三个商圈
  const scene: MapScene | null = useMemo(() => {
    if (!msgs.length) return AREA_SCENE
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      if (m.role === 'sys') return null
      if (m.role === 'bot') {
        const s = sceneOf(m.reply, String(m.id))
        if (s) return s
      }
    }
    return null
  }, [msgs])
  const mapFocus = msgs.length ? focus : settings.area ? 'area:' + settings.area : null
  const focusName = msgs.length && focus ? scene?.points.find((p) => p.id === focus && p.kind === 'shop')?.name : ''
  const waiting = msgs.find((m) => m.role === 'wait') as Extract<Msg, { role: 'wait' }> | undefined
  // 演示模式的画像：录制时每一步之后存的快照，取已经放完的最后一步
  const doneSteps = busy ? step - 1 : step
  const demoProf = demo && script && doneSteps > 0 ? script.steps[doneSteps - 1]?.profile ?? null : null
  const loadProfile = useCallback(async (consolidate = false) => {
    setProfLoading(true)
    try {
      const r = consolidate
        ? await fetch(`/api/profile/consolidate?uid=${encodeURIComponent(USER_ID)}`, { method: 'POST' }).then((x) => x.json())
        : await fetch(`/api/profile?uid=${encodeURIComponent(USER_ID)}`).then((x) => x.json())
      setLiveProf(consolidate ? r.profile : r)
    } catch {
      setLiveProf(null)
    }
    setProfLoading(false)
  }, [])
  useEffect(() => {
    // 实时模式下抽屉开着时，每次回答完刷新一次画像
    if (drawer && !demo && !busy) loadProfile()
  }, [drawer, demo, busy, loadProfile])

  useEffect(() => {
    fetch('/api/scripts')
      .then((r) => r.json())
      .then(setScripts)
      .catch(() => setScripts([]))
  }, [])
  useEffect(() => {
    if (!demo) return
    fetch(`/api/scripts/${encodeURIComponent(scriptName)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((s: Script | null) => {
        setScript(s)
        // ?from=N：前 N 步直接铺好，从第 N+1 步接着放（录屏中途出错时从断点续录）
        const from = Math.min(parseInt(params.get('from') || '0') || 0, s?.steps.length || 0)
        if (!s || !from) return
        const ms: Msg[] = []
        let st0: Settings = EMPTY
        s.steps.slice(0, from).forEach((st, i) => {
          if (st.new_conv && i > 0) ms.push({ id: nid(), role: 'sys', text: '新对话 · 长期记忆保留' })
          if (st.set) st0 = { ...st0, ...st.set }
          ms.push({ id: nid(), role: 'user', text: st.say, ctx: st.ctx }, { id: nid(), role: 'bot', reply: st.resp })
        })
        setSettings(st0)
        setMsgs(ms)
        setStep(from)
      })
      .catch(() => setScript(null))
  }, [demo, scriptName])
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [msgs, pressing])
  useEffect(() => {
    if (!waiting) return
    setHint(0)
    const t = setInterval(() => setHint((h) => h + 1), 2600)
    return () => clearInterval(t)
  }, [waiting?.id])
  useEffect(() => {
    // 新回答出来时，默认选中第一家
    const c = lastBot?.reply.cards[0]
    setFocus(c && 'items' in c && c.items.length ? c.items[0].amap_id : null)
  }, [lastBot?.id])

  const newConv = useCallback(() => {
    setConvNo((n) => n + 1)
    setMsgs((ms) => (ms.length ? [...ms, { id: nid(), role: 'sys', text: '新对话 · 长期记忆保留' }] : ms))
    setFocus(null)
  }, [])

  const reset = useCallback(() => {
    setMsgs([])
    setStep(0)
    setFocus(null)
    setConvNo(1)
    setInput('')
    setSettings(EMPTY)
  }, [])

  // 发一句：先显示用户消息和等待，再换成回答。replay 是演示回放的录好的回答，没有就实时调接口。
  // 快捷设置补成句末的条件（ctx），用户消息下方用小标签显示
  const send = useCallback(
    async (text: string, replay?: { reply: Reply; waitMs: number; ctx?: string[] }) => {
      if (busyRef.current || !text.trim()) return
      busyRef.current = true
      setBusy(true)
      const wid = nid()
      const ctx = replay ? replay.ctx || [] : ctxOf(text, settings)
      const kind = replay?.reply.cards[0]?.type || 'any'
      setMsgs((ms) => [...ms, { id: nid(), role: 'user', text, ctx }, { id: wid, role: 'wait', kind }])
      let reply: Reply
      if (replay) {
        await sleep(replay.waitMs)
        reply = replay.reply
      } else {
        try {
          const r = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: compose(text, ctx), user_id: USER_ID, conv: `${USER_ID}-${convNo}` }),
          })
          reply = r.ok ? await r.json() : { markdown: '', cards: [], options: [], error: `HTTP ${r.status}` }
        } catch (e) {
          reply = { markdown: '', cards: [], options: [], error: String(e) }
        }
      }
      setMsgs((ms) => ms.map((m) => (m.id === wid ? { id: nid(), role: 'bot', reply } : m)))
      busyRef.current = false
      setBusy(false)
    },
    [convNo, settings],
  )

  // 演示模式：按一下放一步。先点快捷设置（set），打字的句子逐字输入后发送；点选项的先高亮按钮再发送
  const next = useCallback(async () => {
    if (!script || busyRef.current || step >= script.steps.length) return
    const st = script.steps[step]
    setStep((s) => s + 1)
    busyRef.current = true
    if (st.new_conv) {
      newConv()
      await sleep(900)
    }
    for (const [k, v] of Object.entries(st.set || {})) {
      setFlash(k as keyof Settings)
      setSettings((s) => ({ ...s, [k]: v }))
      await sleep(700)
      setFlash(null)
      await sleep(200)
    }
    if (st.via === 'click') {
      setPressing(st.say)
      await sleep(750)
      setPressing(null)
    } else {
      for (let i = 1; i <= st.say.length; i++) {
        setInput(st.say.slice(0, i))
        await sleep(70 + Math.random() * 70)
      }
      await sleep(450)
      setInput('')
    }
    busyRef.current = false
    const real = (st.resp.elapsed_s || 5) * 1000
    const waitMs = waitMode === 'real' ? real : Math.min(Math.max(real * 0.15, 1800), 4200)
    await send(st.say, { reply: st.resp, waitMs, ctx: st.ctx })
  }, [script, step, waitMode, send, newConv])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement)?.tagName === 'INPUT' && !demo
      if (typing) return
      if (demo && (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ')) {
        e.preventDefault()
        next()
      } else if (e.key === 'Escape') setPanel((p) => !p)
      else if (demo && (e.key === 'r' || e.key === 'R') && !busyRef.current) reset()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [demo, next, reset])

  const submit = () => {
    if (demo || busy) return
    const t = input.trim()
    setInput('')
    send(t)
  }
  const pick = (text: string) => (demo ? next() : send(text))
  const onMapSelect = useCallback((id: string) => {
    if (id.startsWith('area:')) {
      const a = id.slice(5)
      setSettings((s) => ({ ...s, area: s.area === a ? '' : a }))
    } else setFocus(id)
  }, [])
  const quick = !demo && !busy ? quickActions(lastBot?.reply ?? null) : []
  const hints = HINTS[waiting?.kind || 'any'] || HINTS.any

  return (
    <div className="app">
      <header>
        <Logo size={34} className="logo" />
        <div className="title">
          <div className="t1">本地生活推荐助手</div>
          <div className="t2">
            <span>深圳南山 · 海岸城 / 深圳湾万象城 / 科技园</span>
            <span className={`status ${demo ? 'demo' : ''}`}>{demo ? '演示回放' : 'DeepSeek V4.1 Flash'}</span>
          </div>
        </div>
        <div className="spacer" />
        {(
          <button className={`ghost ${drawer ? 'active' : ''}`} onClick={() => setDrawer((d) => !d)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 21s-7-4.4-9.3-9A5.3 5.3 0 0 1 12 6.6a5.3 5.3 0 0 1 9.3 5.4C19 16.6 12 21 12 21z" />
            </svg>
            我的口味
          </button>
        )}
        <button className="ghost" onClick={demo ? reset : newConv} disabled={busy}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            {demo ? <path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6" /> : <path d="M12 5v14M5 12h14" />}
          </svg>
          {demo ? '重新开始' : '新对话'}
        </button>
        <button className="ghost icon" title="设置（Esc）" onClick={() => setPanel((p) => !p)}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
          </svg>
        </button>
      </header>

      {panel && (
        <div className="panel">
          <div className="panel-h">设置</div>
          <label>
            模式
            <select value={mode} onChange={(e) => (reset(), setMode(e.target.value as 'live' | 'demo'))}>
              <option value="demo">演示回放（按 → 或空格放下一句）</option>
              <option value="live">实时对话</option>
            </select>
          </label>
          {demo && (
            <>
              <label>
                脚本
                <select value={scriptName} onChange={(e) => (reset(), setScriptName(e.target.value))}>
                  {scripts.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}：{s.title}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                等待
                <select value={waitMode} onChange={(e) => setWaitMode(e.target.value as 'fast' | 'real')}>
                  <option value="fast">压缩（2–4 秒）</option>
                  <option value="real">按录制时的真实耗时</option>
                </select>
              </label>
              <div className="pinfo">
                进度 {step}/{script?.steps.length ?? 0}
                {script && ` · 录制于 ${script.recorded_at}`}
                <br />
                <span className="kbd">→</span>/<span className="kbd">空格</span> 下一句 · <span className="kbd">R</span> 重新开始 ·{' '}
                <span className="kbd">Esc</span> 收起
              </div>
            </>
          )}
        </div>
      )}

      <main>
        <section className="chat">
          <div className="list" ref={listRef}>
            {msgs.length === 0 && <Welcome onPick={pick} />}
            {msgs.map((m) => {
              if (m.role === 'user')
                return (
                  <div key={m.id} className="msg me">
                    <div className="bubble">{m.text}</div>
                    {m.ctx && m.ctx.length > 0 && (
                      <div className="ctx">
                        {m.ctx.map((c) => (
                          <span key={c}>{c}</span>
                        ))}
                      </div>
                    )}
                  </div>
                )
              if (m.role === 'sys')
                return (
                  <div key={m.id} className="sys">
                    <span>{m.text}</span>
                  </div>
                )
              if (m.role === 'wait')
                return (
                  <div key={m.id} className="msg bot">
                    <Logo size={30} radius={0.32} className="avatar" />
                    <div className="bubble wait">
                      <div className="wait-top">
                        <span className="dots">
                          <i />
                          <i />
                          <i />
                        </span>
                        <span className="hint" key={hint}>
                          {hints[hint % hints.length]}
                        </span>
                      </div>
                      <div className="skel">
                        <i />
                        <i />
                        <i />
                      </div>
                    </div>
                  </div>
                )
              const isLast = lastBot?.id === m.id
              return (
                <div key={m.id} className="msg bot">
                  <Logo size={30} radius={0.32} className="avatar" />
                  <div className="bubble">
                    <ReplyView
                      reply={m.reply}
                      focus={isLast ? focus : null}
                      onFocus={isLast ? setFocus : () => {}}
                      onOption={isLast && !busy ? pick : undefined}
                      pressing={isLast ? pressing : null}
                      onProfile={() => setDrawer(true)}
                    />
                  </div>
                </div>
              )
            })}
          </div>
          {quick.length > 0 && (
            <div className="quick">
              {quick.map((q) => (
                <button key={q} onClick={() => send(q)}>
                  {q}
                </button>
              ))}
            </div>
          )}
          <Composer
            value={input}
            onChange={setInput}
            onSubmit={submit}
            settings={settings}
            setSettings={setSettings}
            flash={flash}
            demo={demo}
            busy={busy}
          />
          <div className="foot">推荐依据高德门店数据和小红书笔记；团购含示例数据，以店内为准</div>
        </section>
        <section className="side">
          {drawer && (
            <ProfileDrawer
              profile={demo ? demoProf : liveProf}
              loading={!demo && profLoading}
              demo={demo}
              onClose={() => setDrawer(false)}
              onRefresh={() => loadProfile()}
              onConsolidate={() => loadProfile(true)}
              onForget={(t) => send(t)}
            />
          )}
          <MapView scene={scene} focus={mapFocus} onSelect={onMapSelect}>
            {msgs.length === 0 && (
              <div className="cover">
                <div className="cover-h">覆盖范围 · 深圳南山</div>
                <div className="cover-n">
                  <div>
                    <b>338</b>
                    <span>家门店</span>
                  </div>
                  <div>
                    <b>200</b>
                    <span>篇小红书笔记</span>
                  </div>
                  <div>
                    <b>3</b>
                    <span>个商圈</span>
                  </div>
                </div>
                <div className="cover-s">海岸城、深圳湾万象城、南山科技园。点地图上的商圈可以设为位置。</div>
              </div>
            )}
            {focusName && (
              <div className="map-tip">
                <b>{focusName}</b>
                <span>· 点其他标记或卡片可切换</span>
              </div>
            )}
          </MapView>
        </section>
      </main>
    </div>
  )
}
