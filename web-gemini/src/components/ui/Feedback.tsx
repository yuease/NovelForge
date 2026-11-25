import { useEffect, useId, useRef } from 'react'
import type { ReactNode } from 'react'
import { AlertCircle, AlertTriangle, CheckCircle2, Info, X } from 'lucide-react'
import { AnimatePresence, animate, motion, useInView, useMotionValue, useReducedMotion, useTransform } from 'motion/react'
import { cn } from '../../lib/cn'
import type { Tone } from '../../lib/format'
import { jobStatusMeta } from '../../lib/format'
import { DURATION, EASE_EXIT, EASE_OUT, ERROR_SHAKE_ANIMATE, SPRING_TACTILE } from '../../lib/motion'
import { GeminiStar } from '../brand/GeminiStar'

/* ---------------- Badge ---------------- */

const toneClass: Record<Tone, string> = {
  neutral: 'bg-surface-2 text-body',
  primary: 'bg-primary-soft text-primary',
  success: 'bg-success-soft text-success',
  warning: 'bg-warning-soft text-warning',
  danger: 'bg-danger-soft text-danger',
}
const dotClass: Record<Tone, string> = {
  neutral: 'bg-muted',
  primary: 'bg-g-blue',
  success: 'bg-g-green',
  warning: 'bg-g-yellow',
  danger: 'bg-g-red',
}

export function Badge({
  tone = 'neutral', children, dot, live, icon, className,
}: { tone?: Tone; children: ReactNode; dot?: boolean; live?: boolean; icon?: ReactNode; className?: string }) {
  return (
    <span className={cn('inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-caption font-medium transition-colors duration-[180ms] ease-[var(--ease-standard)] [&_svg]:size-3.5', toneClass[tone], className)}>
      {(dot || live) && (
        <span className="relative flex size-2">
          {live && <span className={cn('absolute inline-flex size-full animate-ping rounded-full opacity-55', dotClass[tone])} />}
          <span className={cn('relative inline-flex size-2 rounded-full transition-colors duration-[180ms]', dotClass[tone])} />
        </span>
      )}
      {icon}
      {children}
    </span>
  )
}

export function StatusBadge({ status, className }: { status?: string; className?: string }) {
  const meta = jobStatusMeta(status)
  return (
    <Badge tone={meta.tone} dot live={meta.live} className={className}>
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={meta.label}
          initial={{ opacity: 0, y: 3 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -3 }}
          transition={{ duration: 0.14, ease: EASE_OUT }}
          className="inline-block"
        >
          {meta.label}
        </motion.span>
      </AnimatePresence>
    </Badge>
  )
}

export function Tag({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('inline-flex h-6 items-center rounded-sm bg-surface-2 px-2 text-caption text-body transition-colors duration-[160ms]', className)}>{children}</span>
}

/* ---------------- Progress ---------------- */

export function Progress({
  value, max = 100, tone = 'gradient', size = 'md', animated, className, label,
}: { value: number; max?: number; tone?: 'gradient' | Tone; size?: 'sm' | 'md' | 'lg'; animated?: boolean; className?: string; label?: string }) {
  const ratio = Math.max(0, Math.min(1, value / (max || 1)))
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(ratio * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
      className={cn('w-full overflow-hidden rounded-full bg-surface-3', size === 'sm' ? 'h-1' : size === 'lg' ? 'h-2.5' : 'h-1.5', className)}
    >
      <motion.div
        className={cn('h-full w-full origin-left rounded-full will-change-transform', tone === 'gradient' ? cn('nf-gradient-bg', animated && 'is-animated') : dotClass[tone])}
        initial={{ scaleX: 0 }}
        animate={{ scaleX: ratio }}
        transition={{ duration: DURATION.data, ease: EASE_OUT }}
      />
    </div>
  )
}

/** Circular progress ring with gradient stroke. */
export function ProgressRing({ value, size = 64, stroke = 6, children }: { value: number; size?: number; stroke?: number; children?: ReactNode }) {
  const reduced = useReducedMotion()
  const drawn = useRef(false)
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const pct = Math.max(0, Math.min(100, value))
  const gid = `ring-${useId().replace(/:/g, '')}`

  useEffect(() => {
    drawn.current = true
  }, [])

  return (
    <div className="relative inline-grid place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#4285f4" />
            <stop offset="50%" stopColor="#9b72cb" />
            <stop offset="100%" stopColor="#ea4335" />
          </linearGradient>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--nf-surface-3)" strokeWidth={stroke} />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={`url(#${gid})`}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          initial={{ strokeDashoffset: reduced ? c * (1 - pct / 100) : c }}
          animate={{ strokeDashoffset: c * (1 - pct / 100) }}
          transition={{ duration: reduced ? 0 : drawn.current ? 0.36 : 0.58, ease: EASE_OUT }}
        />
      </svg>
      <div className="absolute inset-0 grid place-items-center">{children}</div>
    </div>
  )
}

/* ---------------- Loading ---------------- */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('nf-skeleton h-4', className)} />
}

/** Gemini-style "thinking" indicator. */
export function Thinking({ label = '思考中', className }: { label?: ReactNode; className?: string }) {
  return (
    <motion.span
      initial={{ opacity: 0, y: 3 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -3 }}
      transition={{ duration: 0.16, ease: EASE_OUT }}
      className={cn('inline-flex items-center gap-2 text-body text-muted', className)}
      role="status"
    >
      <GeminiStar size={18} spinning />
      <span className="nf-gradient-text is-animated font-medium">{label}</span>
    </motion.span>
  )
}

/* ---------------- Empty / Alert ---------------- */

export function EmptyState({
  icon, title, description, action, className,
}: { icon?: ReactNode; title: ReactNode; description?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: EASE_OUT }}
      className={cn('flex flex-col items-center justify-center rounded-lg border border-dashed border-line px-6 py-14 text-center', className)}
    >
      <div className="relative mb-4 grid size-14 place-items-center rounded-full bg-surface-2 text-muted [&_svg]:size-6">
        {icon ?? <GeminiStar size={26} />}
      </div>
      <h3 className="text-title font-medium text-ink">{title}</h3>
      {description && <p className="mt-1 max-w-sm text-body text-muted">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </motion.div>
  )
}

const alertIcon = { neutral: Info, primary: Info, success: CheckCircle2, warning: AlertTriangle, danger: AlertCircle }

export function Alert({
  tone = 'primary', title, children, action, onClose, className,
}: { tone?: Tone; title?: ReactNode; children?: ReactNode; action?: ReactNode; onClose?: () => void; className?: string }) {
  const Icon = alertIcon[tone]
  return (
    <motion.div
      initial={{ opacity: 0, y: -5 }}
      animate={tone === 'danger' ? ERROR_SHAKE_ANIMATE : { opacity: 1, y: 0, transition: { duration: 0.2, ease: EASE_OUT } }}
      exit={{ opacity: 0, y: -4, transition: { duration: 0.13, ease: EASE_EXIT } }}
      role={tone === 'danger' ? 'alert' : 'status'}
      className={cn('flex items-start gap-3 rounded-md px-4 py-3', toneClass[tone], className)}
    >
      <motion.span
        initial={tone === 'success' ? { scale: 0.72, opacity: 0 } : false}
        animate={{ scale: 1, opacity: 1 }}
        transition={SPRING_TACTILE}
        className="mt-0.5 flex shrink-0"
      >
        <Icon className="size-[18px]" aria-hidden />
      </motion.span>
      <div className="min-w-0 flex-1 text-body">
        {title && <div className="font-medium">{title}</div>}
        {children && <div className={cn('break-words', title && 'mt-0.5 opacity-90')}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
      {onClose && (
        <button type="button" onClick={onClose} aria-label="关闭" className="shrink-0 rounded-full p-1 opacity-70 transition-[opacity,background-color,transform] duration-[140ms] hover:bg-black/5 hover:opacity-100 active:scale-90">
          <X className="size-4" />
        </button>
      )}
    </motion.div>
  )
}

/* ---------------- Numbers ---------------- */

/** Smoothly interpolates numeric changes: poised on first reveal, brisk on live updates. */
export function AnimatedNumber({ value, format = (v) => Math.round(v).toLocaleString('zh-CN'), className }: { value: number; format?: (v: number) => string; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null)
  const inView = useInView(ref, { once: true })
  const reduced = useReducedMotion()
  const hasAnimated = useRef(false)
  const mv = useMotionValue(reduced ? value : 0)
  const text = useTransform(mv, (v) => format(v))

  useEffect(() => {
    if (!inView) return
    if (reduced) {
      mv.set(value)
      hasAnimated.current = true
      return
    }
    const duration = hasAnimated.current ? 0.28 : 0.48
    hasAnimated.current = true
    const controls = animate(mv, value, { duration, ease: EASE_OUT })
    return () => controls.stop()
  }, [inView, value, mv, reduced])

  return <motion.span ref={ref} className={cn('tabular-nums', className)}>{text}</motion.span>
}

export function Stat({
  label, value, hint, icon, tone = 'primary', className,
}: { label: ReactNode; value: ReactNode; hint?: ReactNode; icon?: ReactNode; tone?: Tone; className?: string }) {
  return (
    <div className={cn('rounded-md bg-surface-2 p-4', className)}>
      <div className="flex items-center gap-2 text-caption text-muted">
        {icon && <span className={cn('grid size-6 place-items-center rounded-full [&_svg]:size-3.5', toneClass[tone])}>{icon}</span>}
        {label}
      </div>
      <div className="mt-2 font-display text-headline font-medium text-ink tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-caption text-muted">{hint}</div>}
    </div>
  )
}

/** Key–value row list. */
export function KeyValue({ items, className }: { items: Array<{ label: ReactNode; value: ReactNode }>; className?: string }) {
  return (
    <dl className={cn('grid gap-2.5', className)}>
      {items.map((it, i) => (
        <div key={i} className="flex items-baseline justify-between gap-4 text-body">
          <dt className="shrink-0 text-muted">{it.label}</dt>
          <dd className="min-w-0 truncate text-right tabular-nums text-ink">{it.value}</dd>
        </div>
      ))}
    </dl>
  )
}
