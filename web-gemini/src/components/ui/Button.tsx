import { forwardRef } from 'react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '../../lib/cn'

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
  'transition-[background-color,box-shadow,color,transform,border-color] duration-200 ease-[var(--ease-standard)] ' +
  'active:scale-[.97] disabled:pointer-events-none disabled:opacity-45'

const variants: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-on-primary hover:bg-primary-strong hover:shadow-raised',
  gradient:
    'nf-gradient-bg text-white shadow-[0_6px_20px_-6px_rgb(66_133_244/.55)] hover:shadow-[0_10px_28px_-6px_rgb(155_114_203/.6)] hover:[background-position:100%_0] [transition:background-position_.6s_var(--ease-standard),box-shadow_.2s,transform_.2s]',
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
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(base, variants[variant], sizes[size], block && 'w-full', className)}
      {...rest}
    >
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
      {iconRight && <span className="transition-transform duration-200 group-hover/btn:translate-x-0.5">{iconRight}</span>}
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
        'inline-flex shrink-0 items-center justify-center rounded-full text-body transition-colors duration-200',
        'hover:bg-surface-2 hover:text-ink active:scale-95 disabled:opacity-40',
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
