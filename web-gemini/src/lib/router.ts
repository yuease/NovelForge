import { useEffect, useState } from 'react'

export type PageKey =
  | 'monitor'
  | 'create'
  | 'hotspots'
  | 'reader'
  | 'facts'
  | 'evaluation'
  | 'models'
  | 'acceptance'

export const PAGE_KEYS: PageKey[] = ['monitor', 'create', 'hotspots', 'reader', 'facts', 'evaluation', 'models', 'acceptance']

/** Legacy hash aliases from the old UI. */
const ALIASES: Record<string, PageKey> = { generator: 'create' }

export function readPage(): PageKey {
  const raw = window.location.hash.replace(/^#\/?/, '').split('?')[0]
  const key = (ALIASES[raw] ?? raw) as PageKey
  return PAGE_KEYS.includes(key) ? key : 'monitor'
}

export function navigate(page: PageKey) {
  if (readPage() === page) return
  window.location.hash = `/${page}`
}

export function usePage(): PageKey {
  const [page, setPage] = useState<PageKey>(readPage)
  useEffect(() => {
    const onChange = () => setPage(readPage())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return page
}
