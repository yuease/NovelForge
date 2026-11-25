import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactNode, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'
import { cn } from '../../lib/cn'
import { EASE_EXIT, EASE_OUT, popoverMotion, SPRING_SHEET, SPRING_TACTILE } from '../../lib/motion'
import type { PopoverOrigin } from '../../lib/motion'

/* ---------------- PopoverSurface ---------------- */

/** Unified anchor-aware popover surface used by all menus & comboboxes. */
export function PopoverSurface({
  open, origin = 'top-left', className, role = 'dialog', ariaLabel, id, children,
}: {
  open: boolean
  origin?: PopoverOrigin
  className?: string
  role?: string
  ariaLabel?: string
  id?: string
  children: ReactNode
}) {
  const m = popoverMotion(origin)
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          id={id}
          role={role}
          aria-label={ariaLabel}
          style={m.style}
          initial={m.initial}
          animate={m.animate}
          exit={m.exit}
          className={className}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

interface PortalCoords {
  left: number
  width: number
  top?: number
  bottom?: number
  maxHeight: number
  origin: 'top' | 'bottom'
}

/**
 * Viewport-aware portaled popover anchored to a trigger element.
 * Escapes overflow-hidden ancestors (Disclosure, Collapsible, Drawers, Cards)
 * and automatically flips above the trigger when near the bottom of the screen.
 */
export function PortalPopover({
  open,
  anchorRef,
  popoverRef,
  minWidth = 0,
  maxHeight = 320,
  gap = 6,
  className,
  role = 'dialog',
  ariaLabel,
  id,
  children,
}: {
  open: boolean
  anchorRef: RefObject<HTMLElement | null>
  popoverRef?: RefObject<HTMLDivElement | null>
  minWidth?: number
  maxHeight?: number
  gap?: number
  className?: string
  role?: string
  ariaLabel?: string
  id?: string
  children: ReactNode
}) {
  const [coords, setCoords] = useState<PortalCoords | null>(null)

  const measure = useCallback(() => {
    const el = anchorRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight
    const pad = 10
    const spaceBelow = vh - rect.bottom - gap - pad
    const spaceAbove = rect.top - gap - pad
    const openBelow = spaceBelow >= Math.min(maxHeight, 220) || spaceBelow >= spaceAbove
    const width = Math.min(Math.max(rect.width, minWidth), vw - 16)
    const left = Math.max(8, Math.min(rect.left, vw - width - 8))
    const availHeight = openBelow ? spaceBelow : spaceAbove
    setCoords({
      left,
      width,
      ...(openBelow ? { top: rect.bottom + gap } : { bottom: vh - rect.top + gap }),
      maxHeight: Math.min(maxHeight, Math.max(140, Math.floor(availHeight))),
      origin: openBelow ? 'top' : 'bottom',
    })
  }, [anchorRef, gap, maxHeight, minWidth])

  useLayoutEffect(() => {
    if (!open) return
    measure()
    let raf = 0
    const schedule = () => {
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        measure()
      })
    }
    window.addEventListener('scroll', schedule, true)
    window.addEventListener('resize', schedule)
    return () => {
      if (raf) cancelAnimationFrame(raf)
      window.removeEventListener('scroll', schedule, true)
      window.removeEventListener('resize', schedule)
    }
  }, [open, measure])

  const m = popoverMotion(coords?.origin ?? 'top')

  return createPortal(
    <AnimatePresence>
      {open && coords && (
        <motion.div
          ref={popoverRef}
          id={id}
          role={role}
          aria-label={ariaLabel}
          style={{
            ...m.style,
            position: 'fixed',
            left: coords.left,
            width: coords.width,
            maxHeight: coords.maxHeight,
            ...(coords.top !== undefined ? { top: coords.top } : { bottom: coords.bottom }),
            zIndex: 95,
          }}
          initial={m.initial}
          animate={m.animate}
          exit={m.exit}
          className={className}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}

/* ---------------- Toast ---------------- */

type ToastTone = 'success' | 'danger' | 'info'
interface ToastItem { id: number; tone: ToastTone; message: ReactNode }
const ToastCtx = createContext<(message: ReactNode, tone?: ToastTone) => void>(() => {})

/** `const toast = useToast(); toast('已保存', 'success')` */
export const useToast = () => useContext(ToastCtx)

function ToastPill({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  const duration = item.tone === 'danger' ? 5800 : 4000
  const [paused, setPaused] = useState(false)
  const remaining = useRef(duration)
  const started = useRef(Date.now())

  useEffect(() => {
    if (paused) return
    started.current = Date.now()
    const timer = window.setTimeout(() => onDismiss(item.id), remaining.current)
    return () => {
      window.clearTimeout(timer)
      remaining.current = Math.max(0, remaining.current - (Date.now() - started.current))
    }
  }, [paused, item.id, onDismiss])

  const Icon = item.tone === 'success' ? CheckCircle2 : item.tone === 'danger' ? AlertCircle : Info
  return (
    <motion.div
      layout="position"
      role={item.tone === 'danger' ? 'alert' : 'status'}
      initial={{ opacity: 0, y: 18, scale: 0.94 }}
      animate={
        item.tone === 'danger'
          ? {
            opacity: 1,
            y: 0,
            scale: 1,
            x: [0, -3.5, 3.5, -2, 1, 0],
            transition: { ...SPRING_SHEET, x: { duration: 0.32, ease: EASE_OUT } },
          }
          : { opacity: 1, y: 0, scale: 1, transition: SPRING_SHEET }
      }
      exit={{ opacity: 0, y: 8, scale: 0.96, transition: { duration: 0.14, ease: EASE_EXIT } }}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      className="pointer-events-auto flex max-w-md items-center gap-3 rounded-full bg-[#1f1f1f]/95 py-2.5 pl-4 pr-2 text-body text-white shadow-float backdrop-blur-md dark:bg-[#e3e3e3]/95 dark:text-[#1f1f1f]"
    >
      <motion.span
        initial={{ scale: 0.72, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={SPRING_TACTILE}
        className="flex shrink-0"
      >
        <Icon className={cn('size-[18px]', item.tone === 'success' && 'text-g-green', item.tone === 'danger' && 'text-g-red', item.tone === 'info' && 'text-g-blue')} />
      </motion.span>
      <span className="min-w-0 flex-1">{item.message}</span>
      <button
        type="button"
        aria-label="关闭"
        onClick={() => onDismiss(item.id)}
        className="rounded-full p-1.5 opacity-60 transition-[opacity,transform] duration-[140ms] hover:opacity-100 active:scale-90"
      >
        <X className="size-3.5" />
      </button>
    </motion.div>
  )
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id))
  }, [])
  const push = useCallback((message: ReactNode, tone: ToastTone = 'info') => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev.slice(-3), { id, tone, message }])
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      {createPortal(
        <div className="pointer-events-none fixed inset-x-0 bottom-6 z-[100] flex flex-col items-center gap-2 px-4">
          <AnimatePresence mode="popLayout">
            {items.map((t) => <ToastPill key={t.id} item={t} onDismiss={dismiss} />)}
          </AnimatePresence>
        </div>,
        document.body,
      )}
    </ToastCtx.Provider>
  )
}

/* ---------------- Dialog / Drawer ---------------- */

function useEscape(open: boolean, onClose: () => void) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = prev }
  }, [open, onClose])
}

export function Dialog({
  open, onClose, title, description, children, footer, width = 520,
}: { open: boolean; onClose: () => void; title: ReactNode; description?: ReactNode; children?: ReactNode; footer?: ReactNode; width?: number }) {
  useEscape(open, onClose)
  return createPortal(
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[90] grid place-items-center p-4">
          <motion.div
            className="absolute inset-0 bg-black/32 backdrop-blur-[2px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1, transition: { duration: 0.18, ease: EASE_OUT } }}
            exit={{ opacity: 0, transition: { duration: 0.14, ease: EASE_EXIT } }}
            onClick={onClose}
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            initial={{ opacity: 0, y: 10, scale: 0.965 }}
            animate={{ opacity: 1, y: 0, scale: 1, transition: SPRING_SHEET }}
            exit={{ opacity: 0, y: 6, scale: 0.975, transition: { duration: 0.14, ease: EASE_EXIT } }}
            className="relative max-h-[88vh] w-full overflow-y-auto rounded-xl border border-line/70 bg-surface p-6 shadow-float"
            style={{ maxWidth: width }}
          >
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h2 className="text-headline font-normal text-ink">{title}</h2>
                {description && <p className="mt-1 text-body text-muted">{description}</p>}
              </div>
              <button type="button" onClick={onClose} aria-label="关闭" className="rounded-full p-2 text-muted transition-[background-color,color,transform] duration-[140ms] hover:bg-surface-2 hover:text-ink active:scale-90"><X className="size-5" /></button>
            </div>
            {children}
            {footer && <div className="mt-6 flex justify-end gap-2">{footer}</div>}
          </motion.div>
        </div>
      )}
    </AnimatePresence>,
    document.body,
  )
}

export function Drawer({
  open, onClose, title, description, children, footer, width = 480,
}: { open: boolean; onClose: () => void; title: ReactNode; description?: ReactNode; children?: ReactNode; footer?: ReactNode; width?: number }) {
  useEscape(open, onClose)
  return createPortal(
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[90]">
          <motion.div
            className="absolute inset-0 bg-black/26 backdrop-blur-[1.5px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1, transition: { duration: 0.2, ease: EASE_OUT } }}
            exit={{ opacity: 0, transition: { duration: 0.16, ease: EASE_EXIT } }}
            onClick={onClose}
          />
          <motion.aside
            role="dialog"
            aria-modal="true"
            initial={{ x: '100%' }}
            animate={{ x: 0, transition: SPRING_SHEET }}
            exit={{ x: '100%', transition: { duration: 0.2, ease: EASE_EXIT } }}
            className="absolute inset-y-0 right-0 flex w-full flex-col border-l border-line/70 bg-surface shadow-float"
            style={{ maxWidth: width }}
          >
            <div className="flex items-start justify-between gap-4 border-b border-line px-6 py-5">
              <div>
                <h2 className="text-headline font-normal text-ink">{title}</h2>
                {description && <p className="mt-1 text-body text-muted">{description}</p>}
              </div>
              <button type="button" onClick={onClose} aria-label="关闭" className="rounded-full p-2 text-muted transition-[background-color,color,transform] duration-[140ms] hover:bg-surface-2 hover:text-ink active:scale-90"><X className="size-5" /></button>
            </div>
            <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>
            {footer && <div className="flex justify-end gap-2 border-t border-line px-6 py-4">{footer}</div>}
          </motion.aside>
        </div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
