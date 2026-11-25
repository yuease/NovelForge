import { useCallback, useEffect, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { flushSync } from 'react-dom'

export type Theme = 'light' | 'dark'
const KEY = 'novel_forge_theme'

function initial(): Theme {
  const saved = localStorage.getItem(KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(initial)
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#131314' : '#f8fafd')
  }, [theme])

  const toggle = useCallback((event?: ReactMouseEvent<HTMLElement>) => {
    const next: Theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'
    localStorage.setItem(KEY, next)

    const doc = document as Document & {
      startViewTransition?: (update: () => void) => { ready: Promise<void> }
    }
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (!doc.startViewTransition || reduced) {
      setTheme(next)
      return
    }

    // Take reveal center from the toggle button rect so keyboard activation works cleanly too.
    const rect = event?.currentTarget?.getBoundingClientRect()
    const x = rect ? rect.left + rect.width / 2 : window.innerWidth - 48
    const y = rect ? rect.top + rect.height / 2 : 32
    const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y))

    const transition = doc.startViewTransition(() => {
      flushSync(() => setTheme(next))
      document.documentElement.dataset.theme = next
    })
    transition.ready
      .then(() => {
        document.documentElement.animate(
          { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
          { duration: 480, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', pseudoElement: '::view-transition-new(root)' },
        )
      })
      .catch(() => { /* Theme has already switched even if transition was interrupted */ })
  }, [])

  return { theme, toggle }
}

/** Persist a piece of UI state in sessionStorage so drafts survive page switches. */
export function useSessionState<T>(key: string, init: T | (() => T)) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(key)
      if (raw != null) return JSON.parse(raw) as T
    } catch { /* ignore */ }
    return typeof init === 'function' ? (init as () => T)() : init
  })
  useEffect(() => {
    try { sessionStorage.setItem(key, JSON.stringify(value)) } catch { /* ignore */ }
  }, [key, value])
  return [value, setValue] as const
}
