import { useState } from 'react'
import type { FormEvent } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Check, ChevronRight, CircleOff, Cpu, FlaskConical, KeyRound, Link2, RefreshCw, Search, Settings2, Star, Zap } from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { messageOf } from '../lib/format'
import { COLLAPSE_MOTION, SPRING_INDICATOR, SPRING_TACTILE, TAB_MOTION } from '../lib/motion'
import { navigate } from '../lib/router'
import type { LLMProvider, ProviderCatalogEntry } from '../lib/types'
import {
  Alert, Badge, Button, Card, Disclosure, Divider, EmptyState, Field, Input, PageHeader, Select, Skeleton, StaggerItem, Textarea, useToast,
} from '../components/ui'
import { notifyCatalogChanged, useModelCatalog } from '../components/model/ModelPicker'
import ChatGPTConnection from './ChatGPTConnection'

const MARKS: Partial<Record<string, string>> = {
  openai: 'O', anthropic: 'A', gemini: 'G', deepseek: 'D', qwen: 'Q', moonshot: 'K', zhipu: 'Z',
  siliconflow: 'S', openrouter: 'OR', ollama: 'OL', 'openai-compatible': 'API', vertex: 'V', 'vertex-new': 'V+',
}
const MARK_COLORS = ['bg-g-blue', 'bg-g-red', 'bg-g-yellow', 'bg-g-green']
const SOURCE_LABEL = { discovered: '供应商同步', preset: '内置目录', manual: '连接预设' } as const

function markColor(id: string) {
  let h = 0
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return MARK_COLORS[h % MARK_COLORS.length]
}

function ProviderMark({ entry, size = 'md' }: { entry: ProviderCatalogEntry; size?: 'md' | 'lg' }) {
  const isMock = entry.providerId === 'mock'
  return (
    <span
      aria-hidden
      className={cn(
        'grid shrink-0 place-items-center rounded-md font-display font-medium text-white',
        size === 'lg' ? 'size-12 text-title' : 'size-9 text-caption',
        isMock ? 'bg-surface-3 text-muted' : markColor(entry.providerId),
        !entry.configured && !isMock && 'opacity-50 grayscale',
      )}
    >
      {isMock ? <Cpu className={size === 'lg' ? 'size-5' : 'size-4'} /> : MARKS[entry.providerId] || 'API'}
    </span>
  )
}

export default function ModelsPage() {
  const { catalog, loading, error, refresh } = useModelCatalog()
  const params = new URLSearchParams(window.location.hash.split('?')[1])
  const [siwcResult] = useState(() => params.get('siwc'))
  const [current, setCurrent] = useState<LLMProvider | null>(
    siwcResult || params.get('provider') === 'openai-siwc' ? 'openai-siwc' : null,
  )
  const [query, setQuery] = useState('')
  const [updates, setUpdates] = useState<Partial<Record<LLMProvider, ProviderCatalogEntry>>>({})

  const providers = (catalog?.providers ?? []).map((e) => updates[e.providerId] ?? e)
  const active = providers.find((e) => e.providerId === catalog?.activeProvider && e.providerId !== 'mock' && e.configured)
  const selected = providers.find((e) => e.providerId === current) ?? active ?? providers.find((e) => e.providerId !== 'mock') ?? providers[0]
  const q = query.trim().toLowerCase()
  const visible = providers.filter((e) => !q || `${e.label} ${e.providerId} ${e.description}`.toLowerCase().includes(q))
  const configured = visible.filter((e) => e.configured)
  const pending = visible.filter((e) => !e.configured)

  const replace = (entry: ProviderCatalogEntry) => {
    setUpdates((u) => ({ ...u, [entry.providerId]: entry }))
    notifyCatalogChanged()
  }

  const header = (
    <PageHeader
      title="模型管理"
      description="连接模型供应商，同步模型目录并设置默认模型。"
      actions={
        <Button size="sm" variant="ghost" icon={<FlaskConical className="size-4" />} onClick={() => navigate('acceptance')}>
          系统自检
        </Button>
      }
    />
  )

  if (loading && !catalog) {
    return (
      <>
        {header}
        <div className="grid gap-6 lg:grid-cols-[320px_minmax(0,1fr)]">
          <Skeleton className="h-[480px] rounded-lg" />
          <Skeleton className="h-[480px] rounded-lg" />
        </div>
      </>
    )
  }
  if (!catalog && error) {
    return (
      <>
        {header}
        <EmptyState icon={<CircleOff />} title="无法读取模型目录" description={error} action={<Button variant="primary" onClick={() => void refresh()}>重试</Button>} />
      </>
    )
  }

  const renderItem = (e: ProviderCatalogEntry, idx: number) => {
    const sel = selected?.providerId === e.providerId
    return (
      <StaggerItem key={e.providerId} index={idx}>
        <button
          type="button"
          aria-current={sel ? 'true' : undefined}
          onClick={() => {
            setCurrent(e.providerId)
            if (window.innerWidth < 1024) document.getElementById('provider-detail')?.scrollIntoView({ behavior: 'smooth' })
          }}
          className={cn('relative flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left transition-[color,background-color,transform] duration-150 active:scale-[.985]', sel ? '' : 'hover:bg-surface-2')}
        >
          {sel && <motion.span layoutId="provider-active" className="absolute inset-0 rounded-md bg-primary-soft" transition={SPRING_INDICATOR} />}
          <span className="relative"><ProviderMark entry={e} /></span>
          <span className="relative min-w-0 flex-1">
            <span className={cn('block truncate text-body', sel ? 'font-medium text-primary' : 'text-ink')}>{e.label}</span>
            <span className="block text-caption text-muted">{e.providerId === 'mock' ? '本机可用' : e.configured ? `${e.models.length} 个模型` : '待配置'}</span>
          </span>
          {e.providerId === catalog?.activeProvider && e.configured && <Badge tone="primary" className="relative">默认</Badge>}
          <ChevronRight className={cn('relative size-4 text-muted transition-transform duration-200 ease-[var(--ease-emphasized)]', sel && 'translate-x-0.5 text-primary')} />
        </button>
      </StaggerItem>
    )
  }

  return (
    <>
      {header}
      <div className="grid items-start gap-6 @[820px]/main:grid-cols-[300px_minmax(0,1fr)]">
        <Card padding="none" as="aside" className="@[820px]/main:sticky @[820px]/main:top-20">
          <div className="border-b border-line p-4">
            <Input leading={<Search />} aria-label="搜索供应商" value={query} placeholder="搜索供应商" onChange={(e) => setQuery(e.target.value)} className="rounded-full" />
          </div>
          <nav aria-label="模型供应商" className="max-h-[360px] overflow-y-auto p-2 @[820px]/main:max-h-[calc(100vh-240px)]">
            {configured.length > 0 && <div className="px-3 pb-1 pt-2 text-caption font-medium text-muted">已连接</div>}
            <div>{configured.map(renderItem)}</div>
            {pending.length > 0 && <div className="px-3 pb-1 pt-4 text-caption font-medium text-muted">可添加</div>}
            <div>{pending.map(renderItem)}</div>
            {!visible.length && <p className="py-8 text-center text-caption text-muted">没有匹配的供应商</p>}
          </nav>
        </Card>

        <div id="provider-detail" className="min-w-0 scroll-mt-20">
          <AnimatePresence mode="wait" initial={false}>
            {selected ? (
              <motion.div key={selected.providerId} {...TAB_MOTION}>
                {selected.authKind === 'oauth'
                  ? <ChatGPTConnection entry={selected} result={siwcResult} onUpdate={replace} />
                  : <ProviderDetail entry={selected} isActiveProvider={selected.providerId === catalog?.activeProvider} onUpdate={replace} />}
              </motion.div>
            ) : (
              <EmptyState title="模型目录暂不可用" />
            )}
          </AnimatePresence>
        </div>
      </div>
    </>
  )
}

function ProviderDetail({ entry, isActiveProvider, onUpdate }: { entry: ProviderCatalogEntry; isActiveProvider: boolean; onUpdate: (e: ProviderCatalogEntry) => void }) {
  const toast = useToast()
  const [editing, setEditing] = useState(false)
  const [endpoint, setEndpoint] = useState('')
  const [key, setKey] = useState('')
  const [manualIds, setManualIds] = useState('')
  const [busy, setBusy] = useState<'save' | 'discover' | 'model' | 'default' | 'test' | null>(null)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const isMock = entry.providerId === 'mock'
  const vertexKeyOnly = entry.discoveryBlock === 'vertex-oauth'
  const showForm = !isMock && (!entry.configured || editing)
  const allowsManual = !isMock && (!entry.discoverySupported || entry.providerId === 'openai-compatible' || entry.providerId === 'ollama')
  const currentModel = entry.selectedModel || entry.models[0] || ''

  const beginEdit = () => {
    setEndpoint(entry.baseUrl || '')
    setKey('')
    setManualIds(entry.modelSource === 'manual' ? entry.models.join('\n') : '')
    setMessage(null)
    setEditing(true)
  }

  const discover = async (okText?: string) => {
    const { provider } = await api.discoverProviderModels(entry.providerId)
    onUpdate(provider)
    const msg = okText || `已同步 ${provider.models.length} 个模型`
    setMessage({ type: 'success', text: msg })
    toast(msg, 'success')
  }

  const saveConnection = async (e: FormEvent) => {
    e.preventDefault()
    setBusy('save')
    setMessage(null)
    try {
      const models = manualIds.split(/[\n,，]+/).map((s) => s.trim()).filter(Boolean)
      const { provider } = await api.saveModelProvider(entry.providerId, {
        ...(endpoint.trim() || entry.defaultBaseUrl ? { baseUrl: endpoint.trim() } : {}),
        ...(key.trim() ? { apiKey: key.trim() } : {}),
        ...(allowsManual && models.length ? { models: [...new Set(models)] } : {}),
      })
      onUpdate(provider)
      setKey('')
      setEditing(false)
      setMessage({ type: 'success', text: '连接设置已保存。' })
      toast('连接设置已保存', 'success')
      if (provider.discoverySupported && provider.configured && models.length === 0) {
        try { await discover('连接设置已保存，模型列表已同步') } catch (err) {
          setMessage({ type: 'error', text: `连接已保存，暂时无法同步模型：${messageOf(err, '请检查连接设置。')}` })
        }
      }
    } catch (err) {
      setMessage({ type: 'error', text: messageOf(err, '连接设置保存失败。') })
    } finally {
      setBusy(null)
    }
  }

  const wrap = (kind: NonNullable<typeof busy>, fn: () => Promise<void>, fallback: string) => async () => {
    setBusy(kind)
    setMessage(null)
    try { await fn() } catch (err) {
      const msg = messageOf(err, fallback)
      setMessage({ type: 'error', text: msg })
      toast(msg, 'danger')
    } finally { setBusy(null) }
  }

  const refreshModels = wrap('discover', () => discover(), '无法同步模型列表。')
  const selectDefaultModel = async (nextModel: string) => {
    if (!nextModel || nextModel === entry.selectedModel || busy !== null || !entry.configured || isMock) return
    setBusy('model')
    setMessage(null)
    try {
      const { provider } = await api.saveModelProvider(entry.providerId, { selectedModel: nextModel })
      onUpdate(provider)
      toast(`已将 ${nextModel} 设为默认模型`, 'success')
    } catch (err) {
      const msg = messageOf(err, '无法保存默认模型。')
      setMessage({ type: 'error', text: msg })
      toast(msg, 'danger')
    } finally {
      setBusy(null)
    }
  }
  const setAsActiveProvider = wrap('default', async () => {
    await api.saveProviderConfig({ provider: entry.providerId, model: currentModel || undefined })
    notifyCatalogChanged()
    toast(`已将 ${entry.label} 设为全站默认供应商`, 'success')
  }, '无法切换默认供应商。')
  const testConnection = wrap('test', async () => {
    const r = await api.testProviderConnection({ provider: entry.providerId, model: currentModel })
    if (r.ok) {
      const msg = `模型连接正常${r.latencyMs ? ` · ${r.latencyMs} 毫秒` : ''}${r.replyPreview ? ` · “${r.replyPreview.slice(0, 40)}”` : ''}`
      setMessage({ type: 'success', text: msg })
      toast(`连接测试通过${r.latencyMs ? `（${r.latencyMs}ms）` : ''}`, 'success')
    } else {
      const msg = r.error || '模型连接测试失败。'
      setMessage({ type: 'error', text: msg })
      toast(msg, 'danger')
    }
  }, '模型连接测试失败。')

  return (
    <Card padding="lg" as="section" aria-label={`${entry.label} 详情`}>
      <div className="flex flex-wrap items-start gap-4">
        <ProviderMark entry={entry} size="lg" />
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-headline font-normal text-ink">{entry.label}</h2>
          <p className="mt-0.5 text-body text-muted">{entry.description}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {entry.configured && !isMock && !isActiveProvider && (
            <Button size="sm" variant="tonal" loading={busy === 'default'} disabled={busy !== null && busy !== 'default'} icon={<Star className="size-3.5" />} onClick={() => void setAsActiveProvider()}>
              设为默认供应商
            </Button>
          )}
          <Badge tone={isMock || entry.configured ? 'success' : 'neutral'} dot>{isMock ? '本机可用' : isActiveProvider ? '当前默认' : entry.configured ? '已连接' : '待配置'}</Badge>
        </div>
      </div>

      <Divider />

      {/* Connection */}
      <section>
        <div className="mb-4 flex items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-title font-medium text-ink"><Link2 className="size-[18px] text-primary" />连接</h3>
          {entry.configured && !isMock && !showForm && (
            <Button size="sm" variant="ghost" icon={<Settings2 className="size-4" />} disabled={busy !== null} onClick={beginEdit}>编辑</Button>
          )}
        </div>
        {isMock && <p className="text-body text-muted">模拟模型随程序提供，无需 API 密钥，也不会调用外部服务。</p>}
        {entry.configured && !isMock && !showForm && (
          <dl className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-md bg-surface-2 px-4 py-3">
              <dt className="text-caption text-muted">API 地址</dt>
              <dd className="mt-0.5 truncate font-mono text-caption text-ink">{entry.baseUrl || entry.defaultBaseUrl || '供应商默认地址'}</dd>
            </div>
            <div className="rounded-md bg-surface-2 px-4 py-3">
              <dt className="text-caption text-muted">API 密钥</dt>
              <dd className="mt-0.5 truncate font-mono text-caption text-ink">{entry.apiKeyMasked || (entry.requiresKey ? '由运行环境提供' : '此连接无需密钥')}</dd>
            </div>
          </dl>
        )}
        <AnimatePresence initial={false}>
          {showForm && (
            <motion.form
              {...COLLAPSE_MOTION}
              className="grid gap-4 overflow-hidden"
              onSubmit={(e) => void saveConnection(e)}
            >
              <Field label="API 地址" hint={entry.defaultBaseUrl ? `留空使用默认值 ${entry.defaultBaseUrl}` : undefined}>
                {(id) => (
                  <Input id={id} type="url" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder={entry.defaultBaseUrl || 'https://你的服务地址/v1'}
                    required={!entry.defaultBaseUrl && !entry.baseUrl} disabled={busy !== null} className="font-mono" />
                )}
              </Field>
              <Field label={<span className="inline-flex items-center gap-1.5"><KeyRound className="size-3.5" />API 密钥{!entry.requiresKey && <span className="text-muted">（可选）</span>}</span>}>
                {(id) => (
                  <Input id={id} type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
                    placeholder={entry.apiKeyMasked ? `${entry.apiKeyMasked}（留空保留）` : '粘贴供应商 API 密钥'}
                    required={entry.requiresKey && !entry.configured} disabled={busy !== null} className="font-mono" />
                )}
              </Field>
              {allowsManual && (
                <Disclosure title="自定义模型目录" defaultOpen={vertexKeyOnly || entry.modelSource === 'manual'}>
                  <Field
                    label="模型 ID"
                    hint={vertexKeyOnly ? '保存后可在任务里直接选择。API 密钥留空即可保留原密钥。' : '仅在服务不支持同步或目录缺少模型时使用。'}
                  >
                    {(id) => <Textarea id={id} rows={3} value={manualIds} onChange={(e) => setManualIds(e.target.value)} placeholder="每行一个模型 ID" disabled={busy !== null} className="font-mono" />}
                  </Field>
                </Disclosure>
              )}
              <div className="flex gap-2">
                <Button type="submit" variant="primary" loading={busy === 'save'} disabled={busy !== null && busy !== 'save'}>{busy === 'save' ? '正在保存…' : '保存连接'}</Button>
                {entry.configured && <Button variant="ghost" disabled={busy !== null} onClick={() => setEditing(false)}>取消</Button>}
              </div>
            </motion.form>
          )}
        </AnimatePresence>
      </section>

      <Divider />

      {/* Models */}
      <section>
        <div className="mb-4 flex items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-title font-medium text-ink"><Cpu className="size-[18px] text-primary" />模型</h3>
          {entry.configured && entry.discoverySupported && (
            <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('size-4', busy === 'discover' && 'animate-spin')} />} disabled={busy !== null} onClick={() => void refreshModels()}>
              {busy === 'discover' ? '同步中…' : '同步目录'}
            </Button>
          )}
        </div>
        {vertexKeyOnly && <Alert tone="primary" className="mb-4">官方 Vertex 的模型目录需要 OAuth 2.0。API Key 连接可使用已保存和预设的模型，也可以在连接设置中登记模型 ID。</Alert>}
        {entry.models.length ? (
          <div className="grid gap-4">
            <p className="text-caption text-muted">
              {entry.models.length} 个可选模型 · {SOURCE_LABEL[entry.modelSource]}
              {!entry.configured ? ' · 保存连接后可用于任务' : !isMock ? ' · 点击任意模型即时设为默认' : ''}
            </p>
            <div className="flex flex-wrap items-end gap-3">
              <Field label="默认模型" className="min-w-[240px] flex-1">
                {(id) => (
                  <Select id={id} value={currentModel} onChange={(e) => void selectDefaultModel(e.target.value)} disabled={busy !== null || !entry.configured || isMock}>
                    {currentModel && !entry.models.includes(currentModel) && <option value={currentModel}>{currentModel}</option>}
                    {entry.models.map((m) => <option key={m} value={m}>{m}</option>)}
                  </Select>
                )}
              </Field>
              {entry.configured && !isMock && (
                <Button variant="secondary" loading={busy === 'test'} disabled={(busy !== null && busy !== 'test') || !currentModel} icon={<Zap className="size-4" />} onClick={() => void testConnection()} title="向当前默认模型发送一条简短请求">
                  测试连接
                </Button>
              )}
            </div>
            <div className="flex max-h-48 flex-wrap gap-1.5 overflow-y-auto">
              {entry.models.map((m) => {
                const active = m === currentModel
                return (
                  <button
                    key={m}
                    type="button"
                    disabled={!entry.configured || isMock || busy !== null}
                    aria-pressed={active}
                    onClick={() => void selectDefaultModel(m)}
                    className={cn(
                      'inline-flex items-center gap-1 rounded-full px-3 py-1 font-mono text-caption transition-[color,background-color,transform] duration-150 active:scale-[.95] disabled:cursor-default disabled:active:scale-100',
                      active ? 'bg-primary text-on-primary shadow-card' : 'bg-surface-2 text-body hover:bg-surface-3 disabled:hover:bg-surface-2',
                    )}
                  >
                    {active && (
                      <motion.span initial={{ scale: 0.5, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={SPRING_TACTILE}>
                        <Check className="size-3" aria-hidden />
                      </motion.span>
                    )}
                    {m}
                  </button>
                )
              })}
            </div>
          </div>
        ) : (
          <p className="text-body text-muted">{entry.configured ? '尚无可选模型。同步目录后即可在任务中选择。' : '配置连接后可同步模型列表。'}</p>
        )}
      </section>

      <AnimatePresence initial={false}>
        {message && (
          <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
            <Alert tone={message.type === 'error' ? 'danger' : 'success'} className="mt-6" onClose={() => setMessage(null)}>{message.text}</Alert>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  )
}
