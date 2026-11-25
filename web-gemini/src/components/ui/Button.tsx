import { forwardRef } from 'react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Loader2 } from 'lucide-react'
import { cn } from '../../lib/cn'
import { EASE_OUT } from '../../lib/motion'

export type ButtonVariant = 'primary' | 'gradient' | 'secondary' | 'tonal' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  icon?: ReactNode
  iconRight?: ReactNode
  block?: boolean
}

const base =
  'group/btn relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap rounded-full font-medium ' +
  'transition-[background-color,box-shadow,color,transform,border-color,opacity] duration-[150ms] ease-[var(--ease-standard)] ' +
  'active:scale-[.968] disabled:pointer-events-none disabled:opacity-45'

const variants: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-on-primary hover:bg-primary-strong hover:shadow-raised',
  gradient:
    'nf-gradient-bg text-white shadow-[0_6px_20px_-6px_rgb(66_133_244/.55)] hover:shadow-[0_10px_28px_-6px_rgb(155_114_203/.6)] hover:[background-position:100%_0] [transition:background-position_.55s_var(--ease-standard),box-shadow_.18s_var(--ease-standard),transform_.14s_var(--ease-standard),opacity_.15s]',
  secondary: 'border border-line-strong bg-surface text-primary hover:bg-primary-soft hover:border-primary/40',
  tonal: 'bg-primary-soft text-primary hover:bg-primary/15',
  ghost: 'text-body hover:bg-surface-2 hover:text-ink',
  danger: 'bg-danger-soft text-danger hover:bg-danger hover:text-white',
}

const sizes: Record<ButtonSize, string> = {
  sm: 'h-8 px-3.5 text-caption',
  md: 'h-10 px-5 text-body',
  lg: 'h-12 px-7 text-title',
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', loading, icon, iconRight, block, className, children, disabled, type = 'button', ...rest },
  ref,
) {
  const showLeading = Boolean(loading || icon)
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(base, variants[variant], sizes[size], block && 'w-full', className)}
      {...rest}
    >
      {showLeading && (
        <AnimatePresence mode="wait" initial={false}>
          {loading ? (
            <motion.span
              key="spin"
              initial={{ opacity: 0, scale: 0.72 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.72 }}
              transition={{ duration: 0.12, ease: EASE_OUT }}
              className="inline-flex shrink-0 items-center"
            >
              <Loader2 className="size-4 animate-spin" aria-hidden />
            </motion.span>
          ) : (
            <motion.span
              key="icon"
              initial={{ opacity: 0, scale: 0.8 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.8 }}
              transition={{ duration: 0.12, ease: EASE_OUT }}
              className="inline-flex shrink-0 items-center"
            >
              {icon}
            </motion.span>
          )}
        </AnimatePresence>
      )}
      {children}
      {iconRight && (
        <span className="inline-flex shrink-0 items-center transition-transform duration-[180ms] ease-[var(--ease-standard)] group-hover/btn:translate-x-0.5">
          {iconRight}
        </span>
      )}
    </button>
  )
})

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string
  size?: 'sm' | 'md'
  active?: boolean
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, size = 'md', active, className, children, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={label}
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-full text-body transition-[background-color,color,transform,opacity] duration-[140ms] ease-[var(--ease-standard)]',
        'hover:bg-surface-2 hover:text-ink active:scale-[.92] disabled:opacity-40',
        size === 'sm' ? 'size-8' : 'size-10',
        active && 'bg-primary-soft text-primary hover:bg-primary-soft',
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  )
})
