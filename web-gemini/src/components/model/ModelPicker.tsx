import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ArrowUpRight, RotateCw } from 'lucide-react'
import { api } from '../../lib/api'
import { navigate } from '../../lib/router'
import type { LLMProvider, ModelCatalog, ProviderCatalogEntry } from '../../lib/types'
import { Field, Select } from '../ui/Form'
import { cn } from '../../lib/cn'

/* Shared model catalog logic (ported 1:1 from the old ModelPicker) with the new UI kit. */

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
  return entry?.configured && entry.discoverySupported ? JSON.stringify([entry.providerId, entry.baseUrl, entry.apiKeyMasked]) : ''
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
  return catalog?.providers.find((e) => e.providerId === provider) ?? (provider === 'mock' ? DEMO_PROVIDER : undefined)
}

export function resolveSelectedModel(catalog: ModelCatalog | null | undefined, provider: LLMProvider, model: string): string {
  const entry = findProvider(catalog, provider)
  return model || entry?.selectedModel || entry?.models[0] || ''
}

export function isModelChoiceReady(catalog: ModelCatalog | null | undefined, provider: LLMProvider, model: string): boolean {
  return Boolean(findProvider(catalog, provider)?.configured && resolveSelectedModel(catalog, provider, model))
}

const openSettings = () => navigate('models')

/* ---------------- ModelSelect ---------------- */

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
  const saved = findProvider(catalog, provider)
  const key = requestKeyOf(saved)
  const live = useSyncExternalStore(subscribe, () => providerRequests.get(key))

  useEffect(() => { if (!disabled) requestProviderModels(key, provider) }, [key, provider, disabled, saved])

  const waiting = Boolean(!disabled && key && (!live || live.status === 'loading'))
  const entry = live?.status === 'ready' ? live.entry : saved
  const models = entry?.models ?? []
  const value = emptyLabel ? model : model || entry?.selectedModel || models[0] || ''
  const isLegacy = includeLegacy && value && !models.includes(value)
  const hasChoices = models.length > 0 || !!isLegacy
  const fallback = !disabled && live?.status === 'fallback'

  return (
    <Field
      label={label}
      hint={fallback ? undefined : hint}
      error={fallback ? (
        <span className="inline-flex items-center gap-2 text-warning">
          未能取得模型列表，显示已保存或预设模型。
          <button type="button" className="inline-flex items-center gap-1 font-medium text-primary hover:underline" onClick={() => requestProviderModels(key, provider, true)}>
            <RotateCw className="size-3" />重试
          </button>
        </span>
      ) : undefined}
    >
      {(id) => (
        <Select
          id={id}
          value={waiting ? '' : value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={() => requestProviderModels(key, provider)}
          aria-busy={waiting}
          disabled={disabled || waiting || !hasChoices}
          className={cn(waiting && 'nf-skeleton text-muted')}
        >
          {waiting ? <option value="">正在获取模型列表…</option> : (
            <>
              {(emptyLabel || !hasChoices) && <option value="">{emptyLabel || '暂无可选模型'}</option>}
              {isLegacy && <option value={value}>{value}（已有）</option>}
              {models.map((m) => <option key={m} value={m}>{m}</option>)}
            </>
          )}
        </Select>
      )}
    </Field>
  )
}

/* ---------------- ModelPicker (provider + model) ---------------- */

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
  const entry = findProvider(catalog, provider)
  const all = catalog?.providers ?? [DEMO_PROVIDER]
  const options = all.filter((p) => p.configured || (includeLegacy && p.providerId === provider))
  const needsSetup = !entry?.configured
  const lacksModels = !!entry?.configured && !resolveSelectedModel(catalog, provider, model)
  const vertexKeyOnly = !!entry?.configured && entry.discoveryBlock === 'vertex-oauth'

  return (
    <div className={cn('grid gap-3', className)}>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={label === '模型' ? '供应商' : `${label} · 供应商`}>
          {(id) => (
            <Select
              id={id}
              value={provider}
              disabled={disabled}
              onChange={(e) => {
                const next = e.target.value as LLMProvider
                onChange(next, resolveSelectedModel(catalog, next, ''))
              }}
            >
              {!options.some((o) => o.providerId === provider) && <option value={provider}>{entry?.label || provider}</option>}
              {options.map((o) => <option key={o.providerId} value={o.providerId}>{o.label}</option>)}
            </Select>
          )}
        </Field>
        <ModelSelect
          provider={provider}
          label={label}
          model={model}
          onChange={(v) => onChange(provider, v)}
          catalog={catalog}
          disabled={disabled}
          includeLegacy={includeLegacy}
        />
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
