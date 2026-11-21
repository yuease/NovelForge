import { useId } from 'react'
import type { ReactNode } from 'react'
import { motion } from 'motion/react'
import { cn } from '../../lib/cn'

export interface SegmentOption<T extends string> {
  value: T
  label: ReactNode
  icon?: ReactNode
  count?: number
}

/** Pill segmented control with a sliding indicator. Use for 2–6 mutually exclusive views. */
export function Segmented<T extends string>({
  value, onChange, options, size = 'md', className, ariaLabel,
}: {
  value: T
  onChange: (value: T) => void
  options: ReadonlyArray<SegmentOption<T>>
  size?: 'sm' | 'md'
  className?: string
  ariaLabel?: string
}) {
  const layoutId = useId()
  return (
    <div role="tablist" aria-label={ariaLabel} className={cn('inline-flex max-w-full gap-1 overflow-x-auto rounded-full bg-surface-2 p-1', className)}>
      {options.map((opt) => {
        const active = opt.value === value
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              'relative inline-flex shrink-0 items-center gap-1.5 rounded-full font-medium transition-colors duration-200',
              size === 'sm' ? 'h-7 px-3 text-caption' : 'h-8 px-4 text-body',
              active ? 'text-primary' : 'text-body hover:text-ink',
            )}
          >
            {active && (
              <motion.span
                layoutId={layoutId}
                className="absolute inset-0 rounded-full bg-surface shadow-card"
                transition={{ type: 'spring', stiffness: 500, damping: 38 }}
              />
            )}
            <span className="relative z-10 inline-flex items-center gap-1.5 [&_svg]:size-4">
              {opt.icon}
              {opt.label}
              {opt.count !== undefined && (
                <span className={cn('rounded-full px-1.5 text-[11px] leading-4 tabular-nums', active ? 'bg-primary-soft' : 'bg-surface-3 text-muted')}>
                  {opt.count}
                </span>
              )}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** Large selectable option cards (e.g. POV, style intensity). */
export function ChoiceCards<T extends string>({
  value, onChange, options, columns = 3,
}: {
  value: T
  onChange: (value: T) => void
  options: ReadonlyArray<{ value: T; label: ReactNode; description?: ReactNode; icon?: ReactNode; badge?: ReactNode }>
  columns?: 2 | 3 | 4
}) {
  return (
    <div className={cn('grid gap-3', columns === 2 && 'sm:grid-cols-2', columns === 3 && 'sm:grid-cols-3', columns === 4 && 'sm:grid-cols-2 lg:grid-cols-4')}>
      {options.map((opt) => {
        const active = opt.value === value
        return (
          <motion.button
            key={opt.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(opt.value)}
            whileTap={{ scale: 0.98 }}
            className={cn(
              'relative rounded-md border p-4 text-left transition-[border-color,background-color,box-shadow] duration-200',
              active
                ? 'border-primary bg-primary-soft/70 shadow-[0_0_0_1px_var(--nf-primary)]'
                : 'border-line bg-surface hover:border-line-strong hover:bg-surface-2/60',
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <span className={cn('flex items-center gap-2 text-body font-medium [&_svg]:size-4', active ? 'text-primary' : 'text-ink')}>
                {opt.icon}
                {opt.label}
              </span>
              {opt.badge}
            </div>
            {opt.description && <p className="mt-1.5 text-caption leading-relaxed text-muted">{opt.description}</p>}
          </motion.button>
        )
      })}
    </div>
  )
}
