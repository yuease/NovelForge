import type { ReactNode } from 'react'
import { motion } from 'motion/react'
import type { HTMLMotionProps } from 'motion/react'
import { cn } from '../../lib/cn'
import { boundedDelay, EASE_OUT, STAGGER_CHILD, STAGGER_PARENT } from '../../lib/motion'

/** Every page starts with this: title (22px) + one-line description + primary actions on the right. */
export function PageHeader({
  title, description, actions, eyebrow, className,
}: { title: ReactNode; description?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode; className?: string }) {
  return (
    <header className={cn('mb-7 flex flex-wrap items-end justify-between gap-4', className)}>
      <div className="min-w-0">
        {eyebrow && <div className="mb-1.5 text-caption font-medium text-primary">{eyebrow}</div>}
        <h1 className="font-display text-headline font-normal tracking-tight text-ink md:text-[28px]">{title}</h1>
        {description && <p className="mt-1.5 max-w-2xl text-body text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

export const staggerParent = STAGGER_PARENT
export const staggerChild = STAGGER_CHILD

/** Wrap a list; children should be <StaggerItem>. Fast, restrained stagger. */
export function Stagger({ children, className, ...rest }: HTMLMotionProps<'div'>) {
  return (
    <motion.div variants={STAGGER_PARENT} initial="hidden" animate="show" className={className} {...rest}>
      {children}
    </motion.div>
  )
}

/** Pass `index` for long lists (>8 items) so late items never queue multi-second delays. */
export function StaggerItem({ children, index, className, ...rest }: HTMLMotionProps<'div'> & { index?: number }) {
  if (typeof index === 'number') {
    return (
      <motion.div
        initial={{ opacity: 0, y: 7 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.22, delay: boundedDelay(index), ease: EASE_OUT }}
        className={className}
        {...rest}
      >
        {children}
      </motion.div>
    )
  }
  return (
    <motion.div variants={STAGGER_CHILD} className={className} {...rest}>
      {children}
    </motion.div>
  )
}

/** Single element fade-in-up. */
export function Reveal({ children, delay = 0, className, ...rest }: HTMLMotionProps<'div'> & { delay?: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, delay, ease: EASE_OUT }}
      className={className}
      {...rest}
    >
      {children}
    </motion.div>
  )
}
