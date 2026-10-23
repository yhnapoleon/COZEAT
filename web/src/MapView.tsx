import React, { useEffect, useRef, useState } from 'react'
import type { MapScene } from './types'
import { brand } from './scene'

/* eslint-disable @typescript-eslint/no-explicit-any */
declare global {
  interface Window {
    AMap: any
    _AMapSecurityConfig: { securityJsCode: string }
  }
}

let loading: Promise<any> | null = null

// 高德 JS API 2.0：加载前要先设安全密钥
function loadAMap(key: string, code: string): Promise<any> {
  if (loading) return loading
  window._AMapSecurityConfig = { securityJsCode: code }
  loading = new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}&plugin=AMap.Scale`
    s.onload = () => (window.AMap ? resolve(window.AMap) : reject(new Error('AMap 未加载')))
    s.onerror = () => reject(new Error('高德地图脚本加载失败'))
    document.head.appendChild(s)
  })
  return loading
}

const HOME: [number, number] = [113.9376, 22.5183] // 海岸城

function markerHtml(kind: string, label: string, name: string, active: boolean) {
  const cls = `mk mk-${kind}${active ? ' on' : ''}`
  const text = kind === 'anchor' || kind === 'area' ? '' : `<span class="mk-n">${label}</span>`
  const tip = kind === 'anchor' || kind === 'area' ? label : brand(name)
  return `<div class="${cls}">${text}<span class="mk-l">${tip.replace(/</g, '&lt;')}</span></div>`
}

type Props = { scene: MapScene | null; focus: string | null; onSelect: (id: string) => void; children?: React.ReactNode }

export default function MapView({ scene, focus, onSelect, children }: Props) {
  const el = useRef<HTMLDivElement>(null)
  const map = useRef<any>(null)
  const markers = useRef<Record<string, { m: any; kind: string; label: string; name: string }>>({})
  const [err, setErr] = useState('')
  const [ready, setReady] = useState(false)
  const pick = useRef(onSelect)
  pick.current = onSelect

  useEffect(() => {
    let dead = false
    fetch('/api/config')
      .then((r) => r.json())
      .then((c) => {
        if (!c.amap_js_key) throw new Error('没有配置高德 JS key')
        return loadAMap(c.amap_js_key, c.amap_security_code)
      })
      .then((AMap) => {
        if (dead || !el.current) return
        map.current = new AMap.Map(el.current, { zoom: 15, center: HOME, viewMode: '2D', resizeEnable: true })
        map.current.addControl(new AMap.Scale())
        ;(window as any).__laMap = map.current // 调试用
        setReady(true)
      })
      .catch((e) => setErr(String(e.message || e)))
    return () => {
      dead = true
      map.current?.destroy()
      map.current = null
    }
  }, [])

  // 场景变了：清掉旧覆盖物，画新的点和路线，自动缩放到全部可见
  useEffect(() => {
    const m = map.current
    const AMap = window.AMap
    if (!ready || !m || !AMap) return
    m.clearMap()
    markers.current = {}
    if (!scene) {
      m.setZoomAndCenter(15, HOME)
      return
    }
    const all: any[] = []
    if (scene.path) {
      const line = new AMap.Polyline({
        path: scene.path,
        strokeColor: scene.mode === 'walk' ? '#16a085' : '#2f6fde',
        strokeWeight: 7,
        strokeOpacity: 0.9,
        lineJoin: 'round',
        lineCap: 'round',
        showDir: true,
        isOutline: true,
        outlineColor: '#ffffff',
        borderWeight: 2,
      })
      m.add(line)
      all.push(line)
    }
    for (const p of scene.points) {
      const mk = new AMap.Marker({
        position: [p.lng, p.lat],
        content: markerHtml(p.kind, p.label, p.name, false),
        anchor: p.kind === 'anchor' || p.kind === 'area' ? 'bottom-center' : 'top-left',
        offset: p.kind === 'anchor' || p.kind === 'area' ? new AMap.Pixel(0, 0) : new AMap.Pixel(-14, -14), // 圆点圆心对准坐标
        zIndex: p.kind === 'shop' || p.kind === 'area' ? 120 : 110,
      })
      if (p.kind === 'shop' || p.kind === 'area') mk.on('click', () => pick.current(p.id))
      m.add(mk)
      all.push(mk)
      markers.current[p.id] = { m: mk, kind: p.kind, label: p.label, name: p.name }
    }
    // 自己算范围：setFitView 对自定义内容的标记算不准，路线起点和 1 公里外的店会落在视野外
    const xs = scene.points.map((p) => p.lng).concat((scene.path || []).map((p) => p[0]))
    const ys = scene.points.map((p) => p.lat).concat((scene.path || []).map((p) => p[1]))
    if (xs.length === 1) m.setZoomAndCenter(17, [xs[0], ys[0]])
    else {
      const pad = 0.0006
      const bounds = new AMap.Bounds([Math.min(...xs) - pad, Math.min(...ys) - pad], [Math.max(...xs) + pad, Math.max(...ys) + pad])
      m.setBounds(bounds, true, [80, 80, 60, 140]) // 带动画的缩放会被随后的标记更新打断，路线停在终点附近
      if (m.getZoom() > 17) m.setZoom(17, true) // 几家店挨得很近时不要放得太大，标签挤不下
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene, ready])

  // 选中某家：高亮并移过去
  useEffect(() => {
    for (const [id, x] of Object.entries(markers.current)) {
      x.m.setContent(markerHtml(x.kind, x.label, x.name, id === focus))
      x.m.setzIndex(id === focus ? 200 : x.kind === 'shop' ? 120 : 110)
    }
    const f = focus ? markers.current[focus] : null
    // 选中的店已经在视野里（留一圈边距）就不平移，免得把其他店挤出画面
    if (f && f.kind !== 'area' && map.current) {
      const m = map.current
      const pos = f.m.getPosition()
      const px = m.lngLatToContainer(pos)
      const size = m.getSize()
      const inView = px.x > 80 && px.y > 80 && px.x < size.width - 160 && px.y < size.height - 80
      if (!inView) m.panTo(pos)
    }
  }, [focus, scene])

  return (
    <div className="map-wrap">
      <div ref={el} className="map" />
      {err && <div className="map-err">地图没加载出来：{err}</div>}
      {children}
      {scene?.path && (
        <div className="map-legend">
          <span className={`dot ${scene.mode === 'walk' ? 'walk' : 'drive'}`} />
          {scene.mode === 'walk' ? '步行路线' : '驾车路线'}
        </div>
      )}
    </div>
  )
}
