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

export function BrandMark({ collapsed }: { collapsed?: boolean }) {
  return (
    <span className="flex items-center gap-3">
      <span className="relative grid size-9 place-items-center">
        <span className="absolute inset-0 rounded-full bg-[image:var(--nf-gradient-spark)] opacity-20 blur-md" />
        <GeminiStar size={28} className="relative transition-transform duration-500 group-hover:rotate-90" />
      </span>
      {!collapsed && (
        <span className="leading-tight">
          <span className="block font-display text-title font-medium tracking-wide text-ink">墨枢</span>
          <span className="block text-[11px] tracking-[.14em] text-muted">NOVEL FORGE</span>
        </span>
      )}
    </span>
  )
}
