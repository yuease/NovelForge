import { forwardRef } from 'react'
import type { HTMLAttributes, ReactNode } from 'react'
import { cn } from '../../lib/cn'

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** `glow` adds the animated Gemini border – use only for "AI is working" surfaces. */
  variant?: 'default' | 'flat' | 'tinted' | 'glow'
  padding?: 'none' | 'sm' | 'md' | 'lg'
  interactive?: boolean
  selected?: boolean
  as?: 'div' | 'section' | 'article' | 'aside'
}

const pad = { none: '', sm: 'p-4', md: 'p-5', lg: 'p-6 md:p-7' }

export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { variant = 'default', padding = 'md', interactive, selected, as: Tag = 'div', className, ...rest },
  ref,
) {
  return (
    <Tag
      ref={ref}
      className={cn(
        'relative min-w-0 rounded-lg transition-[border-color,box-shadow,background-color,transform] duration-[180ms] ease-[var(--ease-standard)]',
        variant === 'default' && 'border border-line bg-surface shadow-card',
        variant === 'flat' && 'bg-surface-2',
        variant === 'tinted' && 'border border-primary/15 bg-[image:var(--nf-gradient-soft)] bg-surface',
        variant === 'glow' && 'nf-glow-border border border-line bg-surface',
        interactive && 'nf-card-lift cursor-pointer hover:border-line-strong hover:shadow-raised active:scale-[.994]',
        selected && 'border-primary! bg-primary-soft/60 ring-1 ring-primary',
        pad[padding],
        className,
      )}
      {...rest}
    />
  )
})

export function CardHeader({
  title, description, icon, actions, className,
}: { title: ReactNode; description?: ReactNode; icon?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <div className={cn('mb-4 flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="flex min-w-0 items-start gap-3">
        {icon && (
          <span className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-md bg-primary-soft text-primary [&_svg]:size-[18px]">
            {icon}
          </span>
        )}
        <div className="min-w-0">
          <h3 className="text-title font-medium text-ink">{title}</h3>
          {description && <p className="mt-0.5 text-caption text-muted">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

/** Section label inside a card – replaces nested bordered boxes. */
export function SectionLabel({ children, className, actions }: { children: ReactNode; className?: string; actions?: ReactNode }) {
  return (
    <div className={cn('mb-3 flex items-center justify-between gap-2', className)}>
      <h4 className="text-caption font-medium tracking-wide text-muted">{children}</h4>
      {actions}
    </div>
  )
}

export function Divider({ className }: { className?: string }) {
  return <hr className={cn('my-5 border-0 border-t border-line', className)} />
}
