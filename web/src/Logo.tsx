// 产品标识：橙色渐变圆角方块上一枚白色图钉，图钉头里是一只冒热气的碗（"这里有好吃的"）。
// 顶栏、首页、回答头像、favicon（public/favicon.svg，同一份图形）共用。
type Props = { size?: number; radius?: number; className?: string }

export default function Logo({ size = 32, radius = 0.3, className = '' }: Props) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 64 64" aria-label="本地生活推荐助手" role="img">
      <defs>
        <linearGradient id="lg-bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#ff7a45" />
          <stop offset="1" stopColor="#ff4d4f" />
        </linearGradient>
        <linearGradient id="lg-pin" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffffff" />
          <stop offset="1" stopColor="#ffe9e1" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx={64 * radius} fill="url(#lg-bg)" />
      <path d="M32 56C32 56 14.5 41.5 14.5 27.5a17.5 17.5 0 0 1 35 0C49.5 41.5 32 56 32 56z" fill="url(#lg-pin)" />
      <path d="M21.5 28.5h21a10.5 10.5 0 0 1-21 0z" fill="url(#lg-bg)" />
      <path d="M26.5 26.5h11" stroke="url(#lg-bg)" strokeWidth="2" strokeLinecap="round" />
      <path d="M28.2 14.5q-2.6 3.6 0 7.2M35.8 14.5q-2.6 3.6 0 7.2" fill="none" stroke="url(#lg-bg)" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  )
}
