import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'motion/react'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'
import { cn } from '../../lib/cn'

/* ---------------- Toast ---------------- */

type ToastTone = 'success' | 'danger' | 'info'
interface ToastItem { id: number; tone: ToastTone; message: ReactNode }
const ToastCtx = createContext<(message: ReactNode, tone?: ToastTone) => void>(() => {})

/** `const toast = useToast(); toast('已保存', 'success')` */
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const push = useCallback((message: ReactNode, tone: ToastTone = 'info') => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev.slice(-3), { id, tone, message }])
    window.setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 4200)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      {createPortal(
        <div className="pointer-events-none fixed inset-x-0 bottom-6 z-[100] flex flex-col items-center gap-2 px-4">
          <AnimatePresence>
            {items.map((t) => {
              const Icon = t.tone === 'success' ? CheckCircle2 : t.tone === 'danger' ? AlertCircle : Info
              return (
                <motion.div
                  key={t.id}
                  layout
                  initial={{ opacity: 0, y: 24, scale: 0.95 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: 12, scale: 0.95 }}
                  transition={{ type: 'spring', stiffness: 420, damping: 32 }}
                  className="pointer-events-auto flex max-w-md items-center gap-3 rounded-full bg-[#1f1f1f] py-2.5 pl-4 pr-2 text-body text-white shadow-float dark:bg-[#e3e3e3] dark:text-[#1f1f1f]"
                >
                  <Icon className={cn('size-[18px] shrink-0', t.tone === 'success' && 'text-g-green', t.tone === 'danger' && 'text-g-red', t.tone === 'info' && 'text-g-blue')} />
                  <span className="min-w-0 flex-1">{t.message}</span>
                  <button type="button" aria-label="关闭" onClick={() => setItems((p) => p.filter((x) => x.id !== t.id))} className="rounded-full p-1.5 opacity-60 hover:opacity-100">
                    <X className="size-3.5" />
                  </button>
                </motion.div>
              )
            })}
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
          <motion.div className="absolute inset-0 bg-black/30 backdrop-blur-[2px]" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} />
          <motion.div
            role="dialog"
            aria-modal="true"
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ type: 'spring', stiffness: 400, damping: 34 }}
            className="relative max-h-[88vh] w-full overflow-y-auto rounded-xl bg-surface p-6 shadow-float"
            style={{ maxWidth: width }}
          >
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h2 className="text-headline font-normal text-ink">{title}</h2>
                {description && <p className="mt-1 text-body text-muted">{description}</p>}
              </div>
              <button type="button" onClick={onClose} aria-label="关闭" className="rounded-full p-2 text-muted hover:bg-surface-2"><X className="size-5" /></button>
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
          <motion.div className="absolute inset-0 bg-black/25" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} />
          <motion.aside
            role="dialog"
            aria-modal="true"
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', stiffness: 380, damping: 40 }}
            className="absolute inset-y-0 right-0 flex w-full flex-col bg-surface shadow-float"
            style={{ maxWidth: width }}
          >
            <div className="flex items-start justify-between gap-4 border-b border-line px-6 py-5">
              <div>
                <h2 className="text-headline font-normal text-ink">{title}</h2>
                {description && <p className="mt-1 text-body text-muted">{description}</p>}
              </div>
              <button type="button" onClick={onClose} aria-label="关闭" className="rounded-full p-2 text-muted hover:bg-surface-2"><X className="size-5" /></button>
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
