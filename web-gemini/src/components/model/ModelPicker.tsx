import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { ArrowUpRight, Check, ChevronDown, RotateCw, Search } from 'lucide-react'
import { api } from '../../lib/api'
import { navigate } from '../../lib/router'
import type { LLMProvider, ModelCatalog, ProviderCatalogEntry } from '../../lib/types'
import { cn } from '../../lib/cn'
import { PortalPopover } from '../ui/Overlay'

/* Unified searchable model combobox & shared catalog cache across the studio. */

const MODEL_CATALOG_EVENT = 'novelforge:model-catalog-change'

export const DEMO_PROVIDER: ProviderCatalogEntry = {
  providerId: 'mock',
  label: '模拟模型',
  configured: true,
  baseUrl: '',
  defaultBaseUrl: '',
  apiKeyMasked: '',
  requiresKey: false,
  selectedModel: 'mock-novel-v1',
  models: ['mock-novel-v1'],
  discoverySupported: false,
  modelSource: 'preset',
  description: '使用本地模拟响应预览工作流。',
}

type ModelRequest =
  | { status: 'loading' }
  | { status: 'ready'; entry: ProviderCatalogEntry; until: number }
  | { status: 'fallback'; until: number }

const providerRequests = new Map<string, ModelRequest>()
const listeners = new Set<() => void>()
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }
const publish = () => listeners.forEach((l) => l())

function requestKeyOf(entry: ProviderCatalogEntry | undefined) {
  return entry?.configured && entry.discoverySupported
    ? JSON.stringify([entry.providerId, entry.baseUrl, entry.apiKeyMasked, entry.profileId, entry.catalogVersion]) : ''
}

function requestProviderModels(key: string, provider: LLMProvider, force = false) {
  if (!key) return
  const prev = providerRequests.get(key)
  if (prev?.status === 'loading' || (!force && prev && prev.until > Date.now())) return
  const pending = { status: 'loading' } as const
  providerRequests.set(key, pending)
  publish()
  const settle = (r: ModelRequest) => {
    if (providerRequests.get(key) !== pending) return
    providerRequests.set(key, r)
    publish()
  }
  void api.discoverProviderModels(provider).then(
    ({ provider: entry }) => settle({ status: 'ready', entry, until: Date.now() + 5 * 60_000 }),
    () => settle({ status: 'fallback', until: Date.now() + 30_000 }),
  )
}

/** Call after saving provider settings so every picker refreshes. */
export function notifyCatalogChanged() {
  providerRequests.clear()
  publish()
  window.dispatchEvent(new Event(MODEL_CATALOG_EVENT))
}

export function useModelCatalog() {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const gen = useRef(0)

  const refresh = useCallback(async (opts?: { quiet?: boolean }) => {
    const req = ++gen.current
    if (!opts?.quiet) setLoading(true)
    setError(null)
    try {
      const r = await api.getModelCatalog()
      if (req === gen.current) setCatalog(r)
    } catch (err) {
      if (req === gen.current) setError(err instanceof Error ? err.message : '无法读取模型目录')
    } finally {
      if (req === gen.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
    const onChanged = () => { void refresh({ quiet: true }) }
    window.addEventListener(MODEL_CATALOG_EVENT, onChanged)
    return () => { gen.current += 1; window.removeEventListener(MODEL_CATALOG_EVENT, onChanged) }
  }, [refresh])

  return { catalog, loading, error, refresh }
}

export function findProvider(catalog: ModelCatalog | null | undefined, provider: LLMProvider) {
  return catalog?.providers.find((e) => e.providerId === provider && (e.providerId !== 'openai-siwc' || e.allStages))
    ?? (provider === 'mock' ? DEMO_PROVIDER : undefined)
}

export function resolveSelectedModel(catalog: ModelCatalog | null | undefined, provider: LLMProvider, model: string): string {
  const entry = findProvider(catalog, provider)
  if (provider === 'openai-siwc') {
    if (!entry?.configured) return ''
    return model && entry.models.includes(model) ? model : entry.selectedModel
  }
  return model || entry?.selectedModel || entry?.models[0] || ''
}

export function isModelChoiceReady(catalog: ModelCatalog | null | undefined, provider: LLMProvider, model: string): boolean {
  return Boolean(findProvider(catalog, provider)?.configured && resolveSelectedModel(catalog, provider, model))
}

/** Picks the best configured real provider & model, falling back to mock only if none are configured. */
export function resolveDefaultRealModel(catalog: ModelCatalog | null | undefined): { provider: LLMProvider; model: string } {
  if (!catalog) return { provider: 'mock', model: DEMO_PROVIDER.selectedModel }
  const eligible = catalog.providers.filter((p) => p.configured && p.providerId !== 'mock' && (p.providerId !== 'openai-siwc' || p.allStages))
  const active = catalog.activeProvider && catalog.activeProvider !== 'mock'
    ? eligible.find((p) => p.providerId === catalog.activeProvider)
    : undefined
  const picked = active ?? eligible[0]
  if (picked) {
    return { provider: picked.providerId, model: resolveSelectedModel(catalog, picked.providerId, '') }
  }
  return { provider: 'mock', model: DEMO_PROVIDER.selectedModel }
}

const openSettings = () => navigate('models')

function usePopoverDismiss(
  open: boolean,
  wrapper: React.RefObject<HTMLElement | null>,
  popover: React.RefObject<HTMLElement | null>,
  trigger: React.RefObject<HTMLElement | null>,
  onClose: () => void,
) {
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {
      if (!(event.target instanceof Node)) return
      if (wrapper.current?.contains(event.target) || popover.current?.contains(event.target)) return
      onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      onClose()
      trigger.current?.focus()
    }
    document.addEventListener('pointerdown', outside)
    window.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('pointerdown', outside)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [open, wrapper, popover, trigger, onClose])
}

/* ---------------- ModelSelect (single-provider searchable combobox) ---------------- */

export interface ModelSelectProps {
  provider: LLMProvider
  model: string
  onChange: (model: string) => void
  catalog?: ModelCatalog | null
  label?: string
  disabled?: boolean
  /** When set, adds an empty option with this label (e.g. “与主模型相同”). */
  emptyLabel?: string
  includeLegacy?: boolean
  hint?: string
}

export function ModelSelect({
  provider, model, onChange, catalog, label = '模型', disabled = false, emptyLabel, includeLegacy = true, hint,
}: ModelSelectProps) {
  const id = useId()
  const saved = findProvider(catalog, provider)
  const key = requestKeyOf(saved)
  const live = useSyncExternalStore(subscribe, () => providerRequests.get(key))
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const wrapper = useRef<HTMLDivElement>(null)
  const popover = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  useEffect(() => { if (!disabled) requestProviderModels(key, provider) }, [key, provider, disabled, saved])
  usePopoverDismiss(open, wrapper, popover, trigger, useCallback(() => setOpen(false), []))
  useEffect(() => {
    if (!open) return
    const raf = requestAnimationFrame(() => searchRef.current?.focus())
    return () => cancelAnimationFrame(raf)
  }, [open])

  const waiting = Boolean(!disabled && key && (!live || live.status === 'loading'))
  const entry = live?.status === 'ready' ? live.entry : saved
  const models = entry?.models ?? []
  const value = emptyLabel ? model : model || entry?.selectedModel || models[0] || ''
  const isLegacy = Boolean(includeLegacy && value && !models.includes(value))
  const allChoices = isLegacy ? [value, ...models] : models
  const hasChoices = allChoices.length > 0 || Boolean(emptyLabel)
  const fallback = !disabled && live?.status === 'fallback'
  const q = query.trim().toLowerCase()
  const shown = allChoices.filter((m) => !q || `${m} ${entry?.modelLabels?.[m] || ''}`.toLowerCase().includes(q))

  const displayText = waiting
    ? '正在获取模型列表…'
    : !value
      ? (emptyLabel || '暂无可选模型')
      : (entry?.modelLabels?.[value] || value)

  return (
    <div className="grid gap-1.5">
      {label && <label htmlFor={id} className="text-caption font-medium text-body">{label}</label>}
      <div
        ref={wrapper}
        className="relative"
        onKeyDown={(e) => {
          if (open && e.key === 'Escape') {
            e.stopPropagation()
            setOpen(false)
            trigger.current?.focus()
          }
        }}
      >
        <button
          id={id}
          ref={trigger}
          type="button"
          disabled={disabled || waiting || !hasChoices}
          aria-expanded={open}
          aria-haspopup="listbox"
          onClick={() => {
            if (open) { setOpen(false); return }
            setQuery('')
            requestProviderModels(key, provider)
            setOpen(true)
          }}
          className={cn(
            'flex h-10 w-full items-center justify-between gap-2 rounded-md border border-line bg-surface px-3.5 text-left text-body text-ink transition-[border-color,background-color,box-shadow,transform] duration-[150ms] ease-[var(--ease-standard)] hover:border-line-strong focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20 active:scale-[.99] disabled:cursor-not-allowed disabled:opacity-55',
            waiting && 'nf-skeleton text-muted',
          )}
        >
          <span className={cn('min-w-0 flex-1 truncate', (!value && emptyLabel) && 'text-muted')}>{displayText}</span>
          <ChevronDown className={cn('size-4 shrink-0 text-muted transition-transform duration-[180ms] ease-[var(--ease-standard)]', open && 'rotate-180')} />
        </button>

        <PortalPopover
          open={open}
          anchorRef={trigger}
          popoverRef={popover}
          maxHeight={288}
          role="listbox"
          ariaLabel={label}
          className="flex flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-float"
        >
          {allChoices.length > 5 && (
            <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2 text-muted">
              <Search className="size-3.5 shrink-0" aria-hidden />
              <input
                ref={searchRef}
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault() }}
                placeholder="搜索模型名称"
                className="min-w-0 flex-1 bg-transparent text-caption text-ink outline-none placeholder:text-muted"
              />
            </div>
          )}
          <div className="min-h-0 overflow-y-auto p-1.5">
            {emptyLabel && (!q || emptyLabel.toLowerCase().includes(q)) && (
              <button
                type="button"
                role="option"
                aria-selected={!value}
                onClick={() => { onChange(''); setOpen(false); trigger.current?.focus() }}
                className={cn(
                  'flex w-full items-center justify-between gap-2 rounded-md px-2.5 py-2 text-left text-body transition-[background-color,transform] duration-[140ms] hover:bg-surface-2 active:scale-[.985]',
                  !value ? 'bg-primary-soft/60 font-medium text-primary' : 'text-muted',
                )}
              >
                <span className="truncate">{emptyLabel}</span>
                {!value && <Check className="size-4 shrink-0 text-primary" />}
              </button>
            )}
            {shown.map((m) => {
              const active = m === value
              return (
                <button
                  key={m}
                  type="button"
                  role="option"
                  aria-selected={active}
                  onClick={() => { onChange(m); setOpen(false); trigger.current?.focus() }}
                  className={cn(
                    'flex w-full items-center justify-between gap-2 rounded-md px-2.5 py-2 text-left text-body transition-[background-color,transform] duration-[140ms] hover:bg-surface-2 active:scale-[.985]',
                    active ? 'bg-primary-soft/60 font-medium text-primary' : 'text-ink',
                  )}
                >
                  <span className="min-w-0 flex-1 break-all">
                    {entry?.modelLabels?.[m] || m}
                    {isLegacy && m === value ? '（已有）' : ''}
                  </span>
                  {active && <Check className="size-4 shrink-0 text-primary" />}
                </button>
              )
            })}
            {shown.length === 0 && !emptyLabel && (
              <p className="px-2.5 py-3 text-center text-caption text-muted">没有匹配的模型</p>
            )}
          </div>
        </PortalPopover>
      </div>
      {fallback ? (
        <span className="inline-flex items-center gap-2 text-caption text-warning">
          未能取得最新模型列表，当前显示已保存目录。
          <button type="button" className="inline-flex items-center gap-1 font-medium text-primary hover:underline" onClick={() => requestProviderModels(key, provider, true)}>
            <RotateCw className="size-3" />重试
          </button>
        </span>
      ) : hint ? (
        <span className="text-caption text-muted">{hint}</span>
      ) : null}
    </div>
  )
}

/* ---------------- ModelPicker (unified searchable provider + model combobox) ---------------- */

export interface ModelPickerProps {
  provider: LLMProvider
  model: string
  onChange: (provider: LLMProvider, model: string) => void
  catalog?: ModelCatalog | null
  label?: string
  disabled?: boolean
  includeLegacy?: boolean
  className?: string
}

export function ModelPicker({
  provider, model, onChange, catalog, label = '模型', disabled = false, includeLegacy = true, className,
}: ModelPickerProps) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const wrapper = useRef<HTMLDivElement>(null)
  const popover = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  const entry = findProvider(catalog, provider)
  const liveSnapshot = useSyncExternalStore(subscribe, () => {
    const key = requestKeyOf(entry)
    return key ? providerRequests.get(key) : undefined
  })
  // Subscribe to all provider request updates while popover is open
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!open) return
    const bump = () => setTick((t) => t + 1)
    listeners.add(bump)
    return () => { listeners.delete(bump) }
  }, [open])

  usePopoverDismiss(open, wrapper, popover, trigger, useCallback(() => setOpen(false), []))
  useEffect(() => {
    if (!open) return
    const raf = requestAnimationFrame(() => searchRef.current?.focus())
    return () => cancelAnimationFrame(raf)
  }, [open])

  const all = catalog?.providers.filter((p) => p.providerId !== 'openai-siwc' || p.allStages) ?? [DEMO_PROVIDER]
  const configuredProviders = all.filter((p) => p.configured || (includeLegacy && p.providerId === provider))

  const openPopover = () => {
    if (disabled) return
    if (open) { setOpen(false); return }
    setQuery('')
    setOpen(true)
    for (const p of configuredProviders) {
      const k = requestKeyOf(p)
      if (k) requestProviderModels(k, p.providerId)
    }
  }

  const effectiveEntry = liveSnapshot?.status === 'ready' ? liveSnapshot.entry : entry
  const resolvedModel = resolveSelectedModel(catalog, provider, model) || effectiveEntry?.selectedModel || effectiveEntry?.models[0] || ''
  const needsSetup = !entry?.configured
  const lacksModels = Boolean(entry?.configured && !resolvedModel)
  const vertexKeyOnly = Boolean(entry?.configured && entry.discoveryBlock === 'vertex-oauth')

  const q = query.trim().toLowerCase()
  const groups = configuredProviders
    .map((saved) => {
      const k = requestKeyOf(saved)
      const req = k ? providerRequests.get(k) : undefined
      const current = req?.status === 'ready' ? req.entry : saved
      const baseModels = [current.selectedModel, ...(current.models ?? [])]
      if (includeLegacy && provider === current.providerId && model) baseModels.push(model)
      const dedup = [...new Set(baseModels.filter(Boolean))]
      const filtered = dedup.filter((m) => !q || `${current.label} ${current.providerId} ${m} ${current.modelLabels?.[m] || ''}`.toLowerCase().includes(q))
      return { provider: current, loading: req?.status === 'loading', models: filtered }
    })
    .filter((g) => g.models.length > 0 || g.loading)

  const displayLabel = resolvedModel
    ? (effectiveEntry?.modelLabels?.[resolvedModel] || resolvedModel)
    : '选择模型'

  return (
    <div className={cn('grid gap-2', className)}>
      {label && <label htmlFor={id} className="text-caption font-medium text-body">{label}</label>}
      <div
        ref={wrapper}
        className="relative"
        onKeyDown={(e) => {
          if (open && e.key === 'Escape') {
            e.stopPropagation()
            setOpen(false)
            trigger.current?.focus()
          }
        }}
      >
        <button
          id={id}
          ref={trigger}
          type="button"
          disabled={disabled}
          aria-expanded={open}
          aria-haspopup="dialog"
          onClick={openPopover}
          title={entry ? `${entry.label} · ${displayLabel}` : displayLabel}
          className="flex h-10 w-full items-center justify-between gap-2 rounded-md border border-line bg-surface px-3.5 text-left text-body text-ink transition-[border-color,background-color,box-shadow,transform] duration-[150ms] ease-[var(--ease-standard)] hover:border-line-strong focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/20 active:scale-[.99] disabled:cursor-not-allowed disabled:opacity-55"
        >
          <span className="flex min-w-0 flex-1 items-center gap-2">
            <span className="shrink-0 rounded-sm bg-surface-2 px-1.5 py-0.5 text-[11px] font-medium text-muted">
              {entry?.label || provider}
            </span>
            <span className="truncate font-medium text-ink">{displayLabel}</span>
          </span>
          <ChevronDown className={cn('size-4 shrink-0 text-muted transition-transform duration-[180ms] ease-[var(--ease-standard)]', open && 'rotate-180')} />
        </button>

        <PortalPopover
          open={open}
          anchorRef={trigger}
          popoverRef={popover}
          minWidth={260}
          maxHeight={320}
          role="dialog"
          ariaLabel={`选择${label}`}
          className="flex flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-float"
        >
          <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2.5 text-muted">
            <Search className="size-4 shrink-0" aria-hidden />
            <input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault() }}
              placeholder="搜索模型或供应商"
              className="min-w-0 flex-1 bg-transparent text-body text-ink outline-none placeholder:text-muted"
            />
          </div>

          <div className="min-h-0 overflow-y-auto p-1.5">
            {groups.map(({ provider: p, loading: syncing, models: list }) => (
              <div key={p.providerId} className="pb-1.5 last:pb-0">
                <div className="flex items-center justify-between px-2.5 py-1 text-caption font-medium text-muted">
                  <span>{p.label}</span>
                  {syncing && <span className="text-[11px] text-primary">同步中…</span>}
                </div>
                {list.map((m) => {
                  const active = p.providerId === provider && m === resolvedModel
                  return (
                    <button
                      key={`${p.providerId}:${m}`}
                      type="button"
                      aria-pressed={active}
                      onClick={() => {
                        onChange(p.providerId, m)
                        setOpen(false)
                        trigger.current?.focus()
                      }}
                      className={cn(
                        'flex w-full items-center justify-between gap-2 rounded-md px-2.5 py-2 text-left text-body transition-[background-color,transform] duration-[140ms] hover:bg-surface-2 active:scale-[.985]',
                        active ? 'bg-primary-soft/65 font-medium text-primary' : 'text-ink',
                      )}
                    >
                      <span className="min-w-0 flex-1 break-all">{p.modelLabels?.[m] || m}</span>
                      {active && <Check className="size-4 shrink-0 text-primary" aria-hidden />}
                    </button>
                  )
                })}
              </div>
            ))}
            {groups.length === 0 && (
              <p className="px-3 py-4 text-center text-caption text-muted">
                {q ? '没有匹配的模型。' : '还没有已连接的可用模型。'}
              </p>
            )}
          </div>

          <button
            type="button"
            onClick={() => { setOpen(false); openSettings() }}
            className="flex shrink-0 items-center justify-between border-t border-line px-3 py-2.5 text-caption font-medium text-primary transition-colors duration-[140ms] hover:bg-surface-2"
          >
            <span>管理供应商与模型</span>
            <ArrowUpRight className="size-3.5" />
          </button>
        </PortalPopover>
      </div>

      {(needsSetup || lacksModels || vertexKeyOnly) && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-warning-soft px-3.5 py-2.5 text-caption text-warning">
          <span>
            {needsSetup ? '此供应商尚未配置。' : vertexKeyOnly ? 'Vertex API Key 连接使用已登记和预设的模型。' : '请先同步供应商的模型列表。'}
          </span>
          <button type="button" onClick={openSettings} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
            {vertexKeyOnly ? '管理模型 ID' : '前往模型管理'}<ArrowUpRight className="size-3.5" />
          </button>
        </div>
      )}
    </div>
  )
}
