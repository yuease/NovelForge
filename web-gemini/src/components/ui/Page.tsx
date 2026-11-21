import type { ReactNode } from 'react'
import { motion } from 'motion/react'
import type { HTMLMotionProps, Variants } from 'motion/react'
import { cn } from '../../lib/cn'

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

const ease = [0.2, 0, 0, 1] as const

export const staggerParent: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.045, delayChildren: 0.02 } },
}
export const staggerChild: Variants = {
  hidden: { opacity: 0, y: 10 },
  show: { opacity: 1, y: 0, transition: { duration: 0.35, ease } },
}

/** Wrap a list; children should be <StaggerItem>. Items fade-and-rise in sequence. */
export function Stagger({ children, className, ...rest }: HTMLMotionProps<'div'>) {
  return (
    <motion.div variants={staggerParent} initial="hidden" animate="show" className={className} {...rest}>
      {children}
    </motion.div>
  )
}

export function StaggerItem({ children, className, ...rest }: HTMLMotionProps<'div'>) {
  return (
    <motion.div variants={staggerChild} className={className} {...rest}>
      {children}
    </motion.div>
  )
}

/** Single element fade-in-up. */
export function Reveal({ children, delay = 0, className, ...rest }: HTMLMotionProps<'div'> & { delay?: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay, ease }}
      className={className}
      {...rest}
    >
      {children}
    </motion.div>
  )
}
