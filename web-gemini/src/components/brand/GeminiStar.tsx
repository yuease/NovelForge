import { useId } from 'react'
import { cn } from '../../lib/cn'

const STAR = 'M12 0C12.6 6.4 17.6 11.4 24 12C17.6 12.6 12.6 17.6 12 24C11.4 17.6 6.4 12.6 0 12C6.4 11.4 11.4 6.4 12 0Z'

/**
 * The Gemini-style four-point spark, filled with the blue/red/yellow/green aura.
 * `spinning` slowly rotates it (thinking state); `pulse` breathes it.
 */
export function GeminiStar({
  size = 24, spinning, pulse, mono, className,
}: { size?: number; spinning?: boolean; pulse?: boolean; mono?: boolean; className?: string }) {
  const id = useId().replace(/:/g, '')
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden
      className={cn('shrink-0', spinning && 'animate-spark-spin', pulse && 'animate-breathe', className)}
    >
      {mono ? (
        <path d={STAR} fill="currentColor" />
      ) : (
        <>
          <defs>
            <clipPath id={`c${id}`}><path d={STAR} /></clipPath>
            <filter id={`b${id}`} x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="2.6" />
            </filter>
          </defs>
          <g clipPath={`url(#c${id})`}>
            <rect width="24" height="24" fill="#4285f4" />
            <g filter={`url(#b${id})`}>
              <circle cx="12" cy="1" r="7" fill="#ea4335" />
              <circle cx="2" cy="12" r="6.5" fill="#fbbc04" />
              <circle cx="12" cy="23.5" r="7" fill="#34a853" />
              <circle cx="22" cy="12" r="7" fill="#4285f4" />
              <circle cx="13.5" cy="11.5" r="4" fill="#4f8df5" />
            </g>
          </g>
        </>
      )}
    </svg>
  )
}

const ORBIT_PATH = 'M3 24A17 5.6 0 0 1 37 24A17 5.6 0 0 1 3 24'

/**
 * 墨枢品牌标：一滴墨（墨）+ 环绕轨道与卫星（枢）+ 墨滴中的星芒（智能）。
 */
export function InkDropLogo({ size = 36, className }: { size?: number; className?: string }) {
  const id = useId().replace(/:/g, '')
  const inkId = `nf-ink-${id}`
  const orbitId = `nf-orbit-${id}`
  const shineId = `nf-shine-${id}`
  return (
    <span className={cn('nf-logo', className)} style={{ width: size, height: size }} aria-hidden>
      <span className="nf-logo-glow" />
      <svg width={size} height={size} viewBox="0 0 40 40" fill="none">
        <defs>
          <linearGradient id={inkId} x1="9" y1="5" x2="31" y2="35" gradientUnits="userSpaceOnUse">
            <stop stopColor="#4285f4" />
            <stop offset=".42" stopColor="#9b72cb" />
            <stop offset=".76" stopColor="#d96570" />
            <stop offset="1" stopColor="#fbbc04" />
            <animateTransform attributeName="gradientTransform" type="rotate" values="0 20 20;360 20 20" dur="14s" repeatCount="indefinite" />
          </linearGradient>
          <linearGradient id={orbitId} x1="3" y1="24" x2="37" y2="24" gradientUnits="userSpaceOnUse">
            <stop stopColor="#4285f4" stopOpacity=".15" />
            <stop offset=".35" stopColor="#4285f4" />
            <stop offset=".7" stopColor="#9b72cb" />
            <stop offset="1" stopColor="#fbbc04" stopOpacity=".35" />
          </linearGradient>
          <radialGradient id={shineId} cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(15.5 15) rotate(60) scale(12)">
            <stop stopColor="#fff" stopOpacity=".55" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </radialGradient>
        </defs>
        <g transform="rotate(-18 20 24)">
          <path d="M3 24A17 5.6 0 0 1 37 24" stroke={`url(#${orbitId})`} strokeWidth="1.4" strokeLinecap="round" opacity=".45" />
        </g>
        <g className="nf-logo-drop">
          <path d="M20 4.2C20 4.2 9.4 15.9 9.4 22.9a10.6 10.6 0 0 0 21.2 0C30.6 15.9 20 4.2 20 4.2Z" fill={`url(#${inkId})`} />
          <path d="M20 4.2C20 4.2 9.4 15.9 9.4 22.9a10.6 10.6 0 0 0 21.2 0C30.6 15.9 20 4.2 20 4.2Z" fill={`url(#${shineId})`} />
          <path d="M14.1 22.4c.2-2.5 1.6-5.2 3.4-7.6" stroke="#fff" strokeOpacity=".6" strokeWidth="1.5" strokeLinecap="round" />
          <path className="nf-logo-spark" d="M20.8 16.9c.36 2.7 1.94 4.28 4.64 4.64-2.7.36-4.28 1.94-4.64 4.64-.36-2.7-1.94-4.28-4.64-4.64 2.7-.36 4.28-1.94 4.64-4.64Z" fill="#fff" />
        </g>
        <g transform="rotate(-18 20 24)">
          <path d="M37 24A17 5.6 0 0 1 3 24" stroke={`url(#${orbitId})`} strokeWidth="1.6" strokeLinecap="round" />
          <circle r="1.7" fill="#fbbc04">
            <animateMotion dur="6s" repeatCount="indefinite" path={ORBIT_PATH} />
            <animate attributeName="opacity" values="0.25;0.25;1;1;0.25" keyTimes="0;0.42;0.5;0.94;1" dur="6s" repeatCount="indefinite" />
          </circle>
        </g>
      </svg>
    </span>
  )
}

export function BrandMark({ collapsed, badge }: { collapsed?: boolean; badge?: string }) {
  return (
    <span className="nf-brand-mark flex items-center gap-2.5">
      <InkDropLogo size={36} />
      {!collapsed && (
        <span className="leading-none">
          <span className="flex items-center gap-1.5 font-serif text-[20px] font-bold leading-[1.18] tracking-[.14em] text-ink">
            墨枢
            {badge && (
              <span className="rounded-full bg-[image:var(--nf-gradient)] px-1.5 py-0.5 font-sans text-[8px] font-bold leading-none tracking-[.08em] text-white">
                {badge}
              </span>
            )}
          </span>
          <span className="mt-1 block text-[9px] font-semibold tracking-[.26em] text-muted">NOVEL · FORGE</span>
        </span>
      )}
    </span>
  )
}
