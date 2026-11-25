import { useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { ChevronDown } from 'lucide-react'
import { cn } from '../../lib/cn'
import { COLLAPSE_MOTION, EASE_OUT } from '../../lib/motion'
import { Card } from './Card'

/** Card with a clickable header that expands its body cleanly and interruptibly. */
export function Collapsible({
  icon, title, subtitle, children, onToggle, defaultOpen = false, className,
}: { icon?: ReactNode; title: ReactNode; subtitle?: ReactNode; children: ReactNode; onToggle?: (open: boolean) => void; defaultOpen?: boolean; className?: string }) {
  const [open, setOpen] = useState(defaultOpen)
  const [overflowVisible, setOverflowVisible] = useState(defaultOpen)
  const toggle = () => {
    const next = !open
    if (!next) setOverflowVisible(false)
    setOpen(next)
    onToggle?.(next)
  }
  return (
    <Card padding="none" className={cn('self-start', className)}>
      <button
        type="button"
        aria-expanded={open}
        onClick={toggle}
        className="flex w-full items-center gap-3 rounded-lg px-5 py-4 text-left transition-colors duration-[160ms] ease-[var(--ease-standard)] hover:bg-surface-2/60 active:bg-surface-2"
      >
        {icon && <span className="grid size-9 shrink-0 place-items-center rounded-md bg-primary-soft text-primary [&_svg]:size-[18px]">{icon}</span>}
        <span className="min-w-0 flex-1">
          <span className="block text-title font-medium text-ink">{title}</span>
          {subtitle && <span className="block truncate text-caption text-muted">{subtitle}</span>}
        </span>
        <ChevronDown className={cn('size-5 shrink-0 text-muted transition-transform duration-[220ms] ease-[var(--ease-standard)]', open && 'rotate-180')} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={COLLAPSE_MOTION.initial}
            animate={COLLAPSE_MOTION.animate}
            exit={COLLAPSE_MOTION.exit}
            onAnimationStart={() => setOverflowVisible(false)}
            onAnimationComplete={() => { if (open) setOverflowVisible(true) }}
            className={overflowVisible ? 'overflow-visible' : 'overflow-hidden'}
          >
            <motion.div
              initial={{ y: -4 }}
              animate={{ y: 0 }}
              transition={{ duration: 0.22, ease: EASE_OUT }}
              className="border-t border-line px-5 py-5"
            >
              {children}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  )
}

/** Inline disclosure without a card – for "高级设置" / "模型设置" inside a card. */
export function Disclosure({ title, children, defaultOpen = false }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const [overflowVisible, setOverflowVisible] = useState(defaultOpen)
  const toggle = () => {
    const next = !open
    if (!next) setOverflowVisible(false)
    setOpen(next)
  }
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={toggle}
        className="inline-flex items-center gap-1.5 rounded-full py-1 pr-2 text-body font-medium text-primary transition-[color,transform] duration-[150ms] ease-[var(--ease-standard)] hover:text-primary-strong active:scale-[.98]"
      >
        <ChevronDown className={cn('size-4 transition-transform duration-[220ms] ease-[var(--ease-standard)]', open && 'rotate-180')} />
        {title}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={COLLAPSE_MOTION.initial}
            animate={COLLAPSE_MOTION.animate}
            exit={COLLAPSE_MOTION.exit}
            onAnimationStart={() => setOverflowVisible(false)}
            onAnimationComplete={() => { if (open) setOverflowVisible(true) }}
            className={overflowVisible ? 'overflow-visible' : 'overflow-hidden'}
          >
            <motion.div
              initial={{ y: -4 }}
              animate={{ y: 0 }}
              transition={{ duration: 0.2, ease: EASE_OUT }}
              className="pt-4"
            >
              {children}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
