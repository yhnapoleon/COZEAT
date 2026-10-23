import { useState } from 'react'
import Logo from './Logo'

const PRESETS = [
  { icon: '🍲', tag: '找店', text: '海岸城附近人均100以内的火锅', bg: '#fff1ec' },
  { icon: '🌹', tag: '约会', text: '深圳湾万象城约会，想吃西餐，最好能拍照', bg: '#fdeef4' },
  { icon: '🍜', tag: '一人食', text: '科技园一个人吃饭，步行10分钟内，清淡点', bg: '#eef6ee' },
  { icon: '🏆', tag: '榜单', text: '海岸城有什么热门的店', bg: '#fff7e0' },
  { icon: '🎫', tag: '团购规则', text: '团购券过期了还能退吗', bg: '#eef3ff' },
  { icon: '🧠', tag: '记住口味', text: '记住我不吃香菜', bg: '#f3efff' },
  { icon: '👨‍👩‍👧', tag: '家庭', text: '带爸妈在海岸城吃粤菜，人均150左右', bg: '#eef6ee' },
  { icon: '🍣', tag: '要求', text: '万象城附近有没有不用排队的日料', bg: '#eef3ff' },
  { icon: '🎉', tag: '聚餐', text: '6个人聚餐，海岸城，人均一两百', bg: '#fff1ec' },
  { icon: '🍰', tag: '下午茶', text: '深圳湾万象城附近适合拍照的下午茶', bg: '#fdeef4' },
  { icon: '🌶️', tag: '口味', text: '想吃辣的，科技园附近', bg: '#fff1ec' },
  { icon: '🎲', tag: '随便', text: '都行，你看着办', bg: '#f3efff' },
]

const CAPS = ['找店推荐', '口碑对比', '路线规划', '团购套餐', '热门榜单', '记住口味']

function greet() {
  const h = new Date().getHours()
  return h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好'
}

export default function Welcome({ onPick }: { onPick: (text: string) => void }) {
  const [page, setPage] = useState(0)
  const n = PRESETS.length / 6
  const shown = PRESETS.slice((page % n) * 6, (page % n) * 6 + 6)
  return (
    <div className="welcome">
      <div className="hero">
        <Logo size={64} className="hero-logo" />
        <h1>
          {greet()}，<em>想吃点什么？</em>
        </h1>
        <p>结合小红书上的真实评价、你的口味和预算，在南山三个商圈里帮你挑店，还能对比口碑、规划路线、查团购。</p>
        <div className="caps">
          {CAPS.map((c) => (
            <span key={c}>
              <i />
              {c}
            </span>
          ))}
        </div>
      </div>
      <div className="presets-h">
        <span>
          <b>试试这样问</b> · 点一下直接发送
        </span>
        <button onClick={() => setPage((p) => p + 1)}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6" />
          </svg>
          换一批
        </button>
      </div>
      <div className="presets" key={page}>
        {shown.map((p, i) => (
          <button key={p.text} className="preset" style={{ animationDelay: `${0.22 + i * 0.05}s` }} onClick={() => onPick(p.text)}>
            <span className="pi" style={{ background: p.bg }}>
              {p.icon}
            </span>
            <span className="pt">
              <span className="ptag">{p.tag}</span>
              <span className="ptext">{p.text}</span>
            </span>
            <span className="parrow">→</span>
          </button>
        ))}
      </div>
    </div>
  )
}
