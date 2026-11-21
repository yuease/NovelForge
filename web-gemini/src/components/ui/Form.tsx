import { forwardRef, useId } from 'react'
import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'
import { ChevronDown } from 'lucide-react'
import { motion } from 'motion/react'
import { cn } from '../../lib/cn'

const control =
  'w-full rounded-md border border-line bg-surface px-3.5 text-body text-ink transition-[border-color,box-shadow,background-color] duration-200 ' +
  'hover:border-line-strong focus:border-primary focus:outline-none focus:ring-4 focus:ring-primary/12 ' +
  'disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-muted'

export interface FieldProps {
  label?: ReactNode
  hint?: ReactNode
  error?: ReactNode
  /** Right side of the label row, e.g. a small link button. */
  aside?: ReactNode
  children: (id: string) => ReactNode
  className?: string
}

/** Label + control + help text. Keep labels 2–6 characters; put details in `hint`. */
export function Field({ label, hint, error, aside, children, className }: FieldProps) {
  const id = useId()
  return (
    <div className={cn('grid gap-1.5', className)}>
      {(label || aside) && (
        <div className="flex items-center justify-between gap-2">
          {label && <label htmlFor={id} className="text-caption font-medium text-body">{label}</label>}
          {aside}
        </div>
      )}
      {children(id)}
      {error ? (
        <p className="text-caption text-danger">{error}</p>
      ) : hint ? (
        <p className="text-caption text-muted">{hint}</p>
      ) : null}
    </div>
  )
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { leading?: ReactNode; trailing?: ReactNode }>(
  function Input({ className, leading, trailing, ...rest }, ref) {
    if (!leading && !trailing) return <input ref={ref} className={cn(control, 'h-10', className)} {...rest} />
    return (
      <div className="relative">
        {leading && <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted [&_svg]:size-4">{leading}</span>}
        <input ref={ref} className={cn(control, 'h-10', leading && 'pl-9', trailing && 'pr-10', className)} {...rest} />
        {trailing && <span className="absolute right-2 top-1/2 -translate-y-1/2">{trailing}</span>}
      </div>
    )
  },
)

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return <textarea ref={ref} className={cn(control, 'min-h-24 resize-y py-2.5 leading-relaxed', className)} {...rest} />
  },
)

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...rest }, ref) {
    return (
      <div className="relative">
        <select ref={ref} className={cn(control, 'h-10 appearance-none pr-9', className)} {...rest}>
          {children}
        </select>
        <ChevronDown className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
      </div>
    )
  },
)

export function Checkbox({
  checked, onChange, label, description, disabled, className,
}: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; description?: ReactNode; disabled?: boolean; className?: string }) {
  return (
    <label className={cn('group flex cursor-pointer items-start gap-3', disabled && 'cursor-not-allowed opacity-50', className)}>
      <input
        type="checkbox"
        className="peer sr-only"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span
        aria-hidden
        className={cn(
          'mt-0.5 grid size-[18px] shrink-0 place-items-center rounded-[5px] border-2 transition-all duration-200',
          'peer-focus-visible:ring-4 peer-focus-visible:ring-primary/20',
          checked ? 'border-primary bg-primary' : 'border-line-strong bg-surface group-hover:border-primary/60',
        )}
      >
        <svg viewBox="0 0 12 12" className="size-3 text-on-primary">
          <motion.path
            d="M2.5 6.2 5 8.6 9.6 3.6"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            initial={false}
            animate={{ pathLength: checked ? 1 : 0 }}
            transition={{ duration: 0.2 }}
          />
        </svg>
      </span>
      <span className="min-w-0">
        <span className="block text-body text-ink">{label}</span>
        {description && <span className="mt-0.5 block text-caption text-muted">{description}</span>}
      </span>
    </label>
  )
}

export function Switch({
  checked, onChange, label, description, disabled, className,
}: { checked: boolean; onChange: (v: boolean) => void; label?: ReactNode; description?: ReactNode; disabled?: boolean; className?: string }) {
  return (
    <label className={cn('flex cursor-pointer items-center justify-between gap-4', disabled && 'cursor-not-allowed opacity-50', className)}>
      {(label || description) && (
        <span className="min-w-0">
          {label && <span className="block text-body text-ink">{label}</span>}
          {description && <span className="mt-0.5 block text-caption text-muted">{description}</span>}
        </span>
      )}
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border-2 transition-colors duration-200',
          checked ? 'border-primary bg-primary' : 'border-line-strong bg-surface-3',
        )}
      >
        <motion.span
          layout
          transition={{ type: 'spring', stiffness: 600, damping: 34 }}
          className={cn('block rounded-full shadow-sm', checked ? 'ml-[22px] size-4 bg-on-primary' : 'ml-1 size-3 bg-muted')}
        />
      </button>
    </label>
  )
}

export function Slider({
  value, min, max, step, onChange, label, format, disabled,
}: { value: number; min: number; max: number; step?: number; onChange: (v: number) => void; label?: ReactNode; format?: (v: number) => ReactNode; disabled?: boolean }) {
  const pct = ((value - min) / (max - min)) * 100
  return (
    <div className="grid gap-2">
      {label && (
        <div className="flex items-center justify-between text-caption">
          <span className="font-medium text-body">{label}</span>
          <span className="font-mono text-primary tabular-nums">{format ? format(value) : value}</span>
        </div>
      )}
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        className="nf-range h-1.5 w-full cursor-pointer appearance-none rounded-full bg-surface-3"
        style={{ background: `linear-gradient(90deg, var(--nf-primary) ${pct}%, var(--nf-surface-3) ${pct}%)` }}
      />
    </div>
  )
}
