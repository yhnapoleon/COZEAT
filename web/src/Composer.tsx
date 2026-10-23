import { useEffect, useRef, useState } from 'react'
import { AREAS, BUDGETS, DISTS, SCENES, type Settings } from './settings'

type Opt = { v: string | number; t: string }
type ChipProps = {
  icon: string
  label: string
  value: string | number
  opts: Opt[]
  flash: boolean
  disabled: boolean
  onChange: (v: string | number) => void
}

function Chip({ icon, label, value, opts, flash, disabled, onChange }: ChipProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  const cur = opts.find((o) => o.v === value)
  const on = Boolean(value)
  return (
    <div className="chipbox" ref={ref}>
      <button type="button" className={`qchip ${on ? 'on' : ''} ${flash ? 'flash' : ''}`} disabled={disabled} onClick={() => setOpen((o) => !o)}>
        <span className="qi">{icon}</span>
        {on ? cur?.t : label}
        {on ? (
          <span
            className="qx"
            onClick={(e) => {
              e.stopPropagation()
              onChange(typeof value === 'number' ? 0 : '')
            }}
          >
            ×
          </span>
        ) : (
          <span className="qv">▾</span>
        )}
      </button>
      {open && (
        <div className="pop">
          <div className="pop-h">{label}</div>
          {opts.map((o) => (
            <button
              type="button"
              key={o.v}
              className={o.v === value ? 'sel' : ''}
              onClick={() => {
                onChange(o.v === value ? (typeof o.v === 'number' ? 0 : '') : o.v)
                setOpen(false)
              }}
            >
              {o.t}
              {o.v === value && <span>✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

type Props = {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  settings: Settings
  setSettings: (f: (s: Settings) => Settings) => void
  flash: keyof Settings | null
  demo: boolean
  busy: boolean
}

export default function Composer({ value, onChange, onSubmit, settings, setSettings, flash, demo, busy }: Props) {
  const set = (k: keyof Settings) => (v: string | number) => setSettings((s) => ({ ...s, [k]: v }))
  const canSend = demo ? Boolean(value) : Boolean(value.trim()) && !busy
  const any = Object.values(settings).some(Boolean)
  return (
    <form
      className={`composer ${value ? 'typing' : ''}`}
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit()
      }}
    >
      <input
        value={value}
        readOnly={demo}
        onChange={(e) => onChange(e.target.value)}
        placeholder={demo ? '' : '说说在哪、想吃什么、预算多少…'}
      />
      <div className="toolbar">
        <Chip icon="📍" label="位置" value={settings.area} flash={flash === 'area'} disabled={demo}
          opts={AREAS.map((a) => ({ v: a.name, t: a.name }))} onChange={set('area')} />
        <Chip icon="¥" label="预算" value={settings.budget} flash={flash === 'budget'} disabled={demo}
          opts={BUDGETS.map((b) => ({ v: b, t: `人均 ${b} 以内` }))} onChange={set('budget')} />
        <Chip icon="🚶" label="距离" value={settings.dist} flash={flash === 'dist'} disabled={demo}
          opts={DISTS.map((d) => ({ v: d, t: d }))} onChange={set('dist')} />
        <Chip icon="✨" label="场景" value={settings.scene} flash={flash === 'scene'} disabled={demo}
          opts={SCENES.map((s) => ({ v: s, t: s }))} onChange={set('scene')} />
        {any && !demo && (
          <button type="button" className="qclear" onClick={() => setSettings(() => ({ area: '', budget: 0, dist: '', scene: '' }))}>
            清空
          </button>
        )}
        <span className="spacer" />
        <button type="submit" className={`send ${canSend ? 'ready' : ''}`} disabled={!canSend} aria-label="发送">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </form>
  )
}
