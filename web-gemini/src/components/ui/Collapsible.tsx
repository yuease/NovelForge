import { useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { ChevronDown } from 'lucide-react'
import { cn } from '../../lib/cn'
import { Card } from './Card'

/** Card with a clickable header that expands its body (height animation). */
export function Collapsible({
  icon, title, subtitle, children, onToggle, defaultOpen = false, className,
}: { icon?: ReactNode; title: ReactNode; subtitle?: ReactNode; children: ReactNode; onToggle?: (open: boolean) => void; defaultOpen?: boolean; className?: string }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <Card padding="none" className={cn('self-start', className)}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => { setOpen(!open); onToggle?.(!open) }}
        className="flex w-full items-center gap-3 rounded-lg px-5 py-4 text-left transition-colors hover:bg-surface-2/60"
      >
        {icon && <span className="grid size-9 shrink-0 place-items-center rounded-md bg-primary-soft text-primary [&_svg]:size-[18px]">{icon}</span>}
        <span className="min-w-0 flex-1">
          <span className="block text-title font-medium text-ink">{title}</span>
          {subtitle && <span className="block truncate text-caption text-muted">{subtitle}</span>}
        </span>
        <ChevronDown className={cn('size-5 shrink-0 text-muted transition-transform duration-300', open && 'rotate-180')} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.28, ease: [0.2, 0, 0, 1] }}
            className="overflow-hidden"
          >
            <div className="border-t border-line px-5 py-5">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  )
}

/** Inline disclosure without a card – for "高级设置" inside a card. */
export function Disclosure({ title, children, defaultOpen = false }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1.5 rounded-full py-1 pr-2 text-body font-medium text-primary hover:text-primary-strong"
      >
        <ChevronDown className={cn('size-4 transition-transform duration-300', open && 'rotate-180')} />
        {title}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.28, ease: [0.2, 0, 0, 1] }}
            className="overflow-hidden"
          >
            <div className="pt-4">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
