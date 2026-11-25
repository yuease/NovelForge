import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { navigate } from '../lib/router'
import type { EditorModelSelection, ModelCatalog, ProviderCatalogEntry } from '../lib/types'
import { PopoverSurface } from '../components/ui'

export default function EditorModelMenu({ catalog, selected, disabled, choose }: {
  catalog: ModelCatalog | null
  selected: EditorModelSelection | null
  disabled: boolean
  choose: (model: EditorModelSelection) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [discovered, setDiscovered] = useState<Record<string, ProviderCatalogEntry>>({})
  const wrapper = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const refreshed = useRef<{ catalog: ModelCatalog | null; at: number }>({ catalog: null, at: 0 })
  const providers = (catalog?.providers ?? []).filter((provider) => provider.configured && provider.providerId !== 'mock')
  const activeProvider = providers.find((provider) => provider.providerId === selected?.provider)
  const filter = query.trim().toLocaleLowerCase()
  const groups = providers.map((saved) => {
    const found = discovered[saved.providerId]
    const provider = found?.profileId === saved.profileId && found ? found : saved
    const models = [provider.selectedModel, ...(provider.models ?? [])]
    if (selected?.provider === provider.providerId && provider.providerId !== 'openai-siwc') models.push(selected.model)
    return { provider, models: [...new Set(models.filter(Boolean))].filter((model) =>
      (provider.label + ' ' + model + ' ' + (provider.modelLabels?.[model] || '')).toLocaleLowerCase().includes(filter)) }
  }).filter((group) => group.models.length > 0)

  useEffect(() => {
    if (!open) return
    requestAnimationFrame(() => search.current?.focus())
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !wrapper.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  const toggle = () => {
    if (open) { setOpen(false); return }
    setQuery('')
    setOpen(true)
    if (!catalog || (refreshed.current.catalog === catalog && Date.now() - refreshed.current.at < 120_000)) return
    if (refreshed.current.catalog !== catalog) setDiscovered({})
    const request = { catalog, at: Date.now() }
    refreshed.current = request
    for (const provider of providers.filter((item) => item.discoverySupported)) {
      void api.discoverProviderModels(provider.providerId).then(({ provider: entry }) => {
        if (refreshed.current === request) setDiscovered((current) => ({ ...current, [entry.providerId]: entry }))
      }).catch(() => {})
    }
  }

  return <div ref={wrapper} className="relative min-w-0" onKeyDown={(event) => {
    if (open && event.key === 'Escape') {
      event.stopPropagation()
      setOpen(false)
      trigger.current?.focus()
    }
  }}>
    <button type="button" ref={trigger} onClick={toggle} aria-expanded={open}
      aria-controls="chief-editor-models" aria-haspopup="dialog" disabled={disabled}
      title={selected ? (activeProvider?.label || selected.provider) + ' · ' + selected.model : undefined} aria-label="选择总编模型"
      className="flex max-w-full items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-caption font-medium text-body transition-[background-color,transform] duration-[140ms] hover:bg-surface-2 active:scale-[.97] disabled:opacity-50">
      <span className="truncate">{selected ? activeProvider?.modelLabels?.[selected.model] || selected.model : '选择模型'}</span>
      <ChevronDown className={cn('size-3.5 shrink-0 text-muted transition-transform duration-[180ms] ease-[var(--ease-standard)]', open && 'rotate-180')} />
    </button>
    <PopoverSurface
      open={open}
      origin="bottom-left"
      id="chief-editor-models"
      role="dialog"
      ariaLabel="选择总编模型"
      className="absolute bottom-full left-0 z-20 mb-2 flex max-h-[min(55dvh,24rem)] w-[min(19rem,calc(100vw-3rem))] flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-float"
    >
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2.5 text-muted">
        <Search className="size-4 shrink-0" aria-hidden />
        <input ref={search} value={query} onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') event.preventDefault() }}
          type="search" aria-label="搜索模型" placeholder="搜索模型或供应商" className="min-w-0 flex-1 bg-transparent text-body text-ink outline-none" />
      </div>
      <div className="min-h-0 overflow-y-auto overscroll-contain px-1.5 py-1.5">
        {groups.map(({ provider, models }) => <div key={provider.providerId} className="pb-1 last:pb-0">
          <p className="px-2 py-1.5 text-caption font-medium text-muted">{provider.label}</p>
          {models.map((model) => {
            const active = selected?.provider === provider.providerId && selected.model === model
              && (provider.providerId !== 'openai-siwc' || selected.siwcProfileId === provider.profileId)
            return <button key={model} type="button" aria-pressed={active} disabled={disabled}
              className={cn(
                'flex w-full items-center justify-between gap-2 rounded-md px-2 py-2 text-left text-body transition-[background-color,transform] duration-[140ms] hover:bg-surface-2 active:scale-[.985] disabled:opacity-50',
                active ? 'bg-primary-soft/65 font-medium text-primary' : 'text-ink',
              )}
              onClick={() => void choose({ provider: provider.providerId, model, siwcProfileId: provider.profileId }).then((saved) => {
                if (saved) { setOpen(false); trigger.current?.focus() }
              })}>
              <span className="break-all">{provider.modelLabels?.[model] || model}</span>{active && <Check className="size-4 shrink-0 text-primary" aria-hidden />}
            </button>
          })}
        </div>)}
        {!groups.length && <p className="px-2 py-3 text-caption text-muted">{filter ? '没有匹配的模型。' : '还没有可选模型。'}</p>}
      </div>
      {!filter && !groups.length && <button type="button" className="shrink-0 border-t border-line px-3 py-2.5 text-left text-caption text-primary transition-colors duration-[140ms] hover:bg-surface-2"
        onClick={() => { setOpen(false); navigate('models') }}>前往模型管理</button>}
    </PopoverSurface>
  </div>
}
