import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  ArrowRight, BookOpen, Database, ExternalLink, Flame, Globe, LayoutTemplate, RefreshCw, Search, Sparkles, Wand2, Zap,
} from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { formatDateTime, messageOf } from '../lib/format'
import type { Tone } from '../lib/format'
import { COLLAPSE_MOTION, TAB_MOTION } from '../lib/motion'
import type { GeneratedConcept, HotspotFeedMeta, HotspotTrope, LiveHotspotItem, LLMProvider, MemeCounts, MemeEntry, MemeSyncMeta } from '../lib/types'
import { useStudio } from '../state/studio'
import { useSessionState } from '../state/prefs'
import {
  Alert, Badge, Button, Card, EmptyState, Input, PageHeader, Segmented, Skeleton, Stagger, StaggerItem, Tag, Thinking, useToast,
} from '../components/ui'
import { findProvider, ModelPicker, resolveDefaultRealModel, resolveSelectedModel, useModelCatalog } from '../components/model/ModelPicker'

const PLATFORMS = [
  { value: 'all', label: '全部平台' },
  { value: 'tomato', label: '番茄小说' },
  { value: 'hongguo', label: '红果短剧' },
  { value: 'douyin', label: '抖音' },
] as const
type Platform = (typeof PLATFORMS)[number]['value']

const PLATFORM_META: Record<string, { label: string; tone: Tone }> = {
  tomato: { label: '番茄小说', tone: 'warning' },
  hongguo: { label: '红果短剧', tone: 'danger' },
  douyin: { label: '抖音热梗', tone: 'primary' },
}

const MEME_STATUS: Record<string, { label: string; tone: Tone }> = {
  active: { label: '可用新梗', tone: 'success' },
  pending_verification: { label: '待核验', tone: 'warning' },
  rejected_old: { label: '已过时', tone: 'danger' },
  expired: { label: '已过期', tone: 'neutral' },
}

const MEME_PAGE_SIZE = 50
type Source = 'tropes' | 'live' | 'memes'
type MemeFilter = 'active' | 'pending_verification' | 'rejected_old' | 'expired' | 'all'

export default function HotspotsPage() {
  const { sendConceptToCreate } = useStudio()
  const toast = useToast()
  const { catalog } = useModelCatalog()
  const [manualModel, setManualModel] = useSessionState<{ provider: LLMProvider; model: string } | null>('nf_hot_model_choice', null)
  const fallbackChoice = resolveDefaultRealModel(catalog)
  const manualValid = Boolean(manualModel && findProvider(catalog, manualModel.provider)?.configured)
  const conceptProvider: LLMProvider = manualValid && manualModel ? manualModel.provider : fallbackChoice.provider
  const conceptModel = manualValid && manualModel
    ? resolveSelectedModel(catalog, manualModel.provider, manualModel.model)
    : fallbackChoice.model

  const [platform, setPlatform] = useSessionState<Platform>('nf_hot_platform', 'all')
  const [source, setSource] = useSessionState<Source>('nf_hot_source', 'tropes')
  const [query, setQuery] = useState('')
  const [tropes, setTropes] = useState<HotspotTrope[]>([])
  const [liveItems, setLiveItems] = useState<LiveHotspotItem[]>([])
  const [feedMeta, setFeedMeta] = useState<HotspotFeedMeta | null>(null)
  const [memes, setMemes] = useState<MemeEntry[]>([])
  const [memeSyncMeta, setMemeSyncMeta] = useState<MemeSyncMeta | null>(null)
  const [memeCounts, setMemeCounts] = useState<MemeCounts>({ all: 0, active: 0, pending_verification: 0, rejected_old: 0, expired: 0 })
  const [memeFilter, setMemeFilter] = useState<MemeFilter>('active')
  const [memeOffset, setMemeOffset] = useState(0)
  const [memeTotal, setMemeTotal] = useState(0)
  const [syncingMemes, setSyncingMemes] = useState(false)
  const [refreshingFeed, setRefreshingFeed] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [selectedTrope, setSelectedTrope] = useSessionState<HotspotTrope | null>('nf_hot_trope', null)
  const [keywords, setKeywords] = useSessionState('nf_hot_keywords', '')
  const [generating, setGenerating] = useState(false)
  const [concept, setConcept] = useSessionState<GeneratedConcept | null>('nf_hot_concept', null)
  const resultRef = useRef<HTMLDivElement>(null)

  const loadHotspots = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.getHotspots({ platform, query: query.trim() || undefined })
      const list = Array.isArray(res.tropes) ? res.tropes : []
      setTropes(list)
      setLiveItems(Array.isArray(res.liveItems) ? res.liveItems : [])
      setFeedMeta(res.feedMeta ?? null)
      if (!selectedTrope && list.length > 0) setSelectedTrope(list[0])
    } catch (err) {
      setError(messageOf(err, '加载热点灵感失败'))
    } finally {
      setLoading(false)
    }
  }

  const loadMemes = async (status: string = memeFilter, offset = memeOffset) => {
    try {
      const res = await api.getMemes({ status, q: query.trim() || undefined, limit: MEME_PAGE_SIZE, offset })
      setMemes(Array.isArray(res.items) ? res.items : [])
      setMemeTotal(typeof res.total === 'number' ? res.total : 0)
      setMemeSyncMeta(res.syncMeta ?? null)
      if (res.counts) setMemeCounts(res.counts)
    } catch { /* backend unavailable */ }
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void loadHotspots() }, [platform])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void loadMemes(memeFilter, memeOffset) }, [memeFilter, memeOffset])

  const runSearch = () => {
    if (source === 'memes') { setMemeOffset(0); void loadMemes(memeFilter, 0) } else void loadHotspots()
  }

  const handleRefreshFeed = async () => {
    setRefreshingFeed(true)
    setError(null)
    try {
      const res = await api.refreshHotspots()
      if (res.feedMeta) setFeedMeta(res.feedMeta)
      if (Array.isArray(res.liveItems)) setLiveItems(res.liveItems)
      toast('榜单已刷新', 'success')
    } catch (err) {
      setError(messageOf(err, '刷新榜单失败'))
    } finally {
      setRefreshingFeed(false)
    }
  }

  const handleSyncMemes = async () => {
    setSyncingMemes(true)
    setError(null)
    try {
      const res = await api.syncMemes()
      if (res.syncMeta) setMemeSyncMeta(res.syncMeta)
      if (res.counts) setMemeCounts(res.counts)
      await loadMemes(memeFilter)
      toast(res.usingCache ? '同步失败，已使用缓存' : '新梗库已同步', res.usingCache ? 'info' : 'success')
    } catch (err) {
      setError(messageOf(err, '同步梗 Wiki 失败'))
    } finally {
      setSyncingMemes(false)
    }
  }

  const handleGenerate = async (opts?: { trope?: HotspotTrope; keywords?: string }) => {
    const target = opts?.trope ?? selectedTrope
    const kw = opts?.keywords ?? keywords
    if (opts?.trope) setSelectedTrope(opts.trope)
    if (opts?.keywords !== undefined) setKeywords(opts.keywords)
    setGenerating(true)
    setError(null)
    resultRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    try {
      const parsed = await api.generateConcept({
        tropeId: target?.id,
        platform: target?.platform ?? (platform !== 'all' ? platform : undefined),
        genre: target?.genre,
        keywords: kw.trim() || undefined,
        llmConfig: { provider: conceptProvider, model: conceptModel },
      })
      setConcept(parsed)
    } catch (err) {
      setError(messageOf(err, '生成高概念失败'))
    } finally {
      setGenerating(false)
    }
  }

  return (
    <>
      <PageHeader
        title="热点灵感"
        description="聚合番茄、红果、抖音的爆款套路与黄金三章钩子，一键孵化选题。"
      />

      {/* Prompt bar */}
      <Card padding="sm" className="mb-6">
        <div className="flex flex-col gap-2 md:flex-row md:items-center">
          <div className="flex min-w-0 flex-1 items-center gap-3 rounded-full bg-surface-2 py-1 pl-4 pr-1 focus-within:ring-2 focus-within:ring-primary/30">
            <Wand2 className="size-5 shrink-0 text-primary" />
            <input
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void handleGenerate() }}
              placeholder="补充你的脑洞关键词，如：古董钥匙、时间回溯、真假千金…"
              className="h-10 min-w-0 flex-1 bg-transparent text-body text-ink placeholder:text-muted focus:outline-none"
            />
            <Button variant="gradient" loading={generating} icon={<Sparkles className="size-4" />} onClick={() => void handleGenerate()}>
              生成选题
            </Button>
          </div>
        </div>
      </Card>

      <AnimatePresence initial={false}>
        {error && (
          <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
            <Alert tone="danger" className="mb-6" onClose={() => setError(null)}>{error}</Alert>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="grid gap-6 @[920px]/main:grid-cols-[minmax(0,1fr)_380px]">
        {/* Left: sources */}
        <section className="min-w-0">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <Segmented
              value={source}
              onChange={setSource}
              options={[
                { value: 'tropes', label: '爆款模板', icon: <LayoutTemplate />, count: tropes.length },
                { value: 'live', label: '实时热榜', icon: <Flame />, count: liveItems.length },
                { value: 'memes', label: '新梗 Wiki', icon: <Zap />, count: memeCounts.active },
              ]}
            />
            <div className="flex items-center gap-2">
              <Input
                leading={<Search />}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') runSearch() }}
                placeholder="搜索题材、钩子、标签"
                className="w-56 rounded-full"
              />
              {source === 'memes' ? (
                <Button size="sm" variant="secondary" loading={syncingMemes} icon={<RefreshCw className="size-3.5" />} onClick={() => void handleSyncMemes()}>同步</Button>
              ) : (
                <Button size="sm" variant="secondary" loading={refreshingFeed} icon={<RefreshCw className="size-3.5" />} onClick={() => void handleRefreshFeed()}>刷新榜单</Button>
              )}
            </div>
          </div>

          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={source} {...TAB_MOTION}>
              {source === 'tropes' && (
                <>
                  <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                    <Segmented size="sm" value={platform} onChange={setPlatform} options={PLATFORMS} ariaLabel="按平台筛选模板" />
                  </div>
                  <TropeGrid
                    tropes={tropes}
                    loading={loading}
                    selectedId={selectedTrope?.id}
                    onSelect={setSelectedTrope}
                    onApply={(t) => void handleGenerate({ trope: t })}
                  />
                </>
              )}
              {source === 'live' && (
                <>
                  <FeedMetaBar meta={feedMeta} count={liveItems.length} />
                  <LiveList items={liveItems} onUse={(it) => void handleGenerate({ keywords: `${it.title} ${it.genre || ''}`.trim() })} />
                </>
              )}
              {source === 'memes' && (
                <MemeList
                  memes={memes}
                  meta={memeSyncMeta}
                  counts={memeCounts}
                  filter={memeFilter}
                  onFilter={(f) => { setMemeOffset(0); setMemeFilter(f) }}
                  offset={memeOffset}
                  total={memeTotal}
                  onPage={setMemeOffset}
                  onUse={(m) => void handleGenerate({ keywords: `${m.title} ${m.summary || ''}`.slice(0, 80) })}
                />
              )}
            </motion.div>
          </AnimatePresence>
        </section>

        {/* Right: concept */}
        <aside ref={resultRef} className="min-w-0 @[920px]/main:sticky @[920px]/main:top-20 @[920px]/main:self-start">
          <Card variant={generating ? 'glow' : 'default'} padding="lg">
            <div className="mb-4 flex items-center justify-between gap-2">
              <h3 className="flex items-center gap-2 text-title font-medium text-ink"><BookOpen className="size-[18px] text-primary" />选题预览</h3>
              {selectedTrope && <span className="max-w-[55%] truncate text-caption text-muted">模板 · {selectedTrope.title}</span>}
            </div>
            <ModelPicker
              label="灵感模型"
              provider={conceptProvider}
              model={conceptModel}
              catalog={catalog}
              disabled={generating}
              onChange={(p, m) => setManualModel({ provider: p, model: m })}
            />
            <div className="mt-5 border-t border-line pt-5">
              <AnimatePresence mode="wait" initial={false}>
                {generating ? (
                  <motion.div key="gen" {...TAB_MOTION} className="grid gap-3">
                    <Thinking label="正在孵化选题" />
                    <Skeleton className="mt-2 h-7 w-2/3" />
                    <Skeleton className="h-4" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-4/5" />
                  </motion.div>
                ) : concept ? (
                  <ConceptView
                    key={concept.title}
                    concept={concept}
                    onSend={() => sendConceptToCreate({
                      title: concept.title,
                      premise: concept.premise || concept.logline || concept.hook || '',
                      genre: concept.genre || selectedTrope?.genre || '都市脑洞',
                      hotspotContext: { trope: selectedTrope, concept },
                    })}
                  />
                ) : (
                  <motion.div key="empty" {...TAB_MOTION} className="py-10 text-center">
                    <Sparkles className="mx-auto size-8 text-primary" />
                    <p className="mt-3 text-body text-muted">选择模板或热点，生成书名、梗概和黄金三章。</p>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </Card>
        </aside>
      </div>
    </>
  )
}

/* ---------------- Concept ---------------- */

function ConceptView({ concept, onSend }: { concept: GeneratedConcept; onSend: () => void }) {
  const chapters = Array.isArray(concept.goldenThreeChapters) ? concept.goldenThreeChapters : []
  return (
    <Stagger className="grid gap-5">
      <StaggerItem>
        <div className="text-caption text-muted">推荐书名</div>
        <div className="mt-1 font-display text-headline font-normal nf-gradient-text">《{concept.title}》</div>
        {(concept.tags?.length || concept.recommendedPov) && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {concept.genre && <Tag>{concept.genre}</Tag>}
            {concept.recommendedPov && <Tag>{concept.recommendedPov}</Tag>}
            {concept.tags?.map((t) => <Tag key={t}>{t}</Tag>)}
          </div>
        )}
      </StaggerItem>
      <StaggerItem>
        <div className="text-caption text-muted">核心梗概</div>
        <p className="mt-1 text-body leading-relaxed text-ink">{concept.premise || concept.logline || concept.hook}</p>
      </StaggerItem>
      {concept.hook && concept.logline && (
        <StaggerItem>
          <div className="text-caption text-muted">开篇钩子</div>
          <p className="mt-1 text-body leading-relaxed text-body">{concept.hook}</p>
        </StaggerItem>
      )}
      {concept.coreConflict && (
        <StaggerItem>
          <div className="text-caption text-muted">核心冲突</div>
          <p className="mt-1 text-body leading-relaxed text-body">{concept.coreConflict}</p>
        </StaggerItem>
      )}
      {chapters.length > 0 && (
        <StaggerItem>
          <div className="mb-2 text-caption text-muted">黄金三章</div>
          <ol className="relative grid gap-3 border-l-2 border-line pl-4">
            {chapters.map((item, i) => (
              <li key={i} className="relative">
                <span className="absolute -left-[23px] top-0.5 grid size-4 place-items-center rounded-full bg-primary text-[10px] text-on-primary">{typeof item === 'string' ? i + 1 : item.chapter ?? i + 1}</span>
                {typeof item === 'string' ? (
                  <p className="text-body text-body">{item}</p>
                ) : (
                  <>
                    <p className="text-body font-medium text-ink">{item.hook}</p>
                    {item.summary && <p className="text-caption text-muted">{item.summary}</p>}
                  </>
                )}
              </li>
            ))}
          </ol>
        </StaggerItem>
      )}
      <StaggerItem>
        <Button variant="primary" block size="lg" iconRight={<ArrowRight className="size-4" />} onClick={onSend}>用这个选题新建作品</Button>
      </StaggerItem>
    </Stagger>
  )
}

/* ---------------- Sources ---------------- */

function TropeGrid({ tropes, loading, selectedId, onSelect, onApply }: { tropes: HotspotTrope[]; loading: boolean; selectedId?: string; onSelect: (t: HotspotTrope) => void; onApply: (t: HotspotTrope) => void }) {
  if (loading && tropes.length === 0) {
    return <div className="grid gap-4 sm:grid-cols-2">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-44 rounded-lg" />)}</div>
  }
  if (tropes.length === 0) return <EmptyState icon={<LayoutTemplate />} title="暂无匹配的模板" description="切换平台或清空搜索词后重试。" />
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {tropes.map((t, idx) => {
        const selected = t.id === selectedId
        const pm = PLATFORM_META[t.platform?.toLowerCase()] ?? { label: t.platform, tone: 'neutral' as Tone }
        return (
          <StaggerItem key={t.id} index={idx}>
            <div
              className={cn(
                'group flex h-full flex-col rounded-lg border bg-surface p-5 transition-[border-color,box-shadow,transform] duration-200 active:scale-[.994]',
                selected ? 'border-primary shadow-[0_0_0_1px_var(--nf-primary)]' : 'nf-card-lift border-line hover:border-line-strong',
              )}
            >
              <button type="button" onClick={() => onSelect(t)} aria-pressed={selected} className="flex-1 text-left">
                <div className="flex items-center justify-between gap-2">
                  <Badge tone={pm.tone}>{pm.label}</Badge>
                  {t.heatScore !== undefined && (
                    <span className="inline-flex items-center gap-1 text-caption font-medium text-warning"><Flame className="size-3.5" />{t.heatScore}</span>
                  )}
                </div>
                <h3 className={cn('mt-3 text-title font-medium transition-colors', selected ? 'text-primary' : 'text-ink group-hover:text-primary')}>{t.title}</h3>
                <p className="mt-1.5 line-clamp-2 text-body text-muted">{t.description}</p>
                {t.hookFormula && <p className="mt-3 rounded-md bg-surface-2 px-3 py-2 text-caption text-body"><span className="font-medium text-primary">钩子 · </span>{t.hookFormula}</p>}
              </button>
              <div className="mt-4 flex items-center justify-between">
                <Tag>{t.genre}</Tag>
                <Button size="sm" variant="ghost" iconRight={<ArrowRight className="size-3.5" />} onClick={() => onApply(t)}>套用生成</Button>
              </div>
            </div>
          </StaggerItem>
        )
      })}
    </div>
  )
}

function FeedMetaBar({ meta, count }: { meta: HotspotFeedMeta | null; count: number }) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md bg-surface-2 px-4 py-2.5 text-caption text-muted">
      <span className="inline-flex items-center gap-1.5 font-medium text-body"><Globe className="size-3.5 text-primary" />{meta?.sourceName || '百度小说风云榜'}</span>
      {meta?.boardDate && <span>榜单 {meta.boardDate}</span>}
      <span>{count} 条</span>
      {meta?.lastSuccessAt && <span>同步于 {formatDateTime(meta.lastSuccessAt)}</span>}
      {meta?.usingCache && <Badge tone="warning" icon={<Database />}>缓存兜底{meta.cachedAt ? ` · ${formatDateTime(meta.cachedAt)}` : ''}</Badge>}
      {meta?.lastError && <span className="text-danger">采集异常：{meta.lastError}</span>}
    </div>
  )
}

function LiveList({ items, onUse }: { items: LiveHotspotItem[]; onUse: (it: LiveHotspotItem) => void }) {
  if (items.length === 0) return <EmptyState icon={<Flame />} title="暂无实时榜单" description="点击「刷新榜单」采集最新热点。" />
  return (
    <Card padding="none">
      <div>
        {items.map((it, idx) => {
          const rank = it.rank ?? idx + 1
          return (
            <StaggerItem key={it.id || idx} index={idx} className="group flex gap-4 border-b border-line px-5 py-4 transition-colors duration-150 last:border-0 hover:bg-surface-2/50">
              <span className={cn('grid size-8 shrink-0 place-items-center rounded-full font-display text-body font-medium tabular-nums', rank <= 3 ? 'nf-gradient-bg text-white' : 'bg-surface-2 text-muted')}>{rank}</span>
              <div className="min-w-0 flex-1">
                <div className="flex items-start justify-between gap-3">
                  <h3 className="text-body font-medium text-ink">{it.title}</h3>
                  {it.hotScore && <span className="shrink-0 text-caption tabular-nums text-warning">热度 {it.hotScore}</span>}
                </div>
                {it.summary && <p className="mt-1 line-clamp-2 text-caption text-muted">{it.summary}</p>}
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-muted">
                  {it.genre && <Tag>{it.genre}</Tag>}
                  {it.author && <span>{it.author}</span>}
                  {it.boardDate && <span>{it.boardDate}</span>}
                  {it.sourceUrl ? (
                    <a href={it.sourceUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">{it.sourceName || '来源'}<ExternalLink className="size-3" /></a>
                  ) : it.sourceName && <span>{it.sourceName}</span>}
                  <button type="button" onClick={() => onUse(it)} className="ml-auto inline-flex items-center gap-1 font-medium text-primary opacity-80 transition-opacity hover:opacity-100 group-hover:opacity-100">
                    以此生成<ArrowRight className="size-3.5" />
                  </button>
                </div>
              </div>
            </StaggerItem>
          )
        })}
      </div>
    </Card>
  )
}

function MemeList({
  memes, meta, counts, filter, onFilter, offset, total, onPage, onUse,
}: {
  memes: MemeEntry[]; meta: MemeSyncMeta | null; counts: MemeCounts; filter: MemeFilter; onFilter: (f: MemeFilter) => void
  offset: number; total: number; onPage: (o: number) => void; onUse: (m: MemeEntry) => void
}) {
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented
          size="sm"
          value={filter}
          onChange={onFilter}
          options={[
            { value: 'active', label: '可用', count: counts.active },
            { value: 'pending_verification', label: '待核验', count: counts.pending_verification },
            { value: 'rejected_old', label: '老梗', count: counts.rejected_old },
            { value: 'expired', label: '过期', count: counts.expired },
            { value: 'all', label: '全部', count: counts.all },
          ]}
        />
        <div className="flex flex-wrap items-center gap-2 text-caption text-muted">
          <span>近 7 天新热梗</span>
          {meta?.lastSuccessAt && <span>· 同步于 {formatDateTime(meta.lastSuccessAt)}</span>}
          {meta?.usingCache && <Badge tone="warning">缓存兜底</Badge>}
        </div>
      </div>
      {meta?.lastError && <Alert tone="warning">同步异常：{meta.lastError}</Alert>}

      {memes.length === 0 ? (
        <EmptyState icon={<Zap />} title="暂无词条" description="切换状态，或点击「同步」拉取最新的梗。" />
      ) : (
        <div className="grid gap-3">
          {memes.map((m, idx) => {
            const st = MEME_STATUS[m.status] ?? { label: m.status, tone: 'neutral' as Tone }
            return (
              <StaggerItem key={m.slug} index={idx}>
                <Card padding="md" className="hover:border-line-strong">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <h3 className="text-body font-medium text-ink">{m.title}</h3>
                      <Badge tone={st.tone}>{st.label}</Badge>
                    </div>
                    {m.sourceUrl && <a href={m.sourceUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-caption text-primary hover:underline">出处<ExternalLink className="size-3" /></a>}
                  </div>
                  {m.summary && <p className="mt-1.5 text-body text-body">{m.summary}</p>}
                  {m.statusReason && <p className="mt-1 text-caption text-muted">入选说明：{m.statusReason}</p>}
                  <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted">
                    <Meta label="最早传播">{m.earliestSpreadAt ? new Date(m.earliestSpreadAt).toLocaleDateString('zh-CN') : '未核实'}</Meta>
                    <Meta label="独立创作者">{m.independentCreatorsCount ?? 0} 位</Meta>
                    <Meta label="热榜证据">{m.hasHotListRecord ? '有' : '无'}</Meta>
                    {m.status === 'active' && (
                      <button type="button" onClick={() => onUse(m)} className="ml-auto inline-flex items-center gap-1 font-medium text-primary hover:underline">以此新梗生成<ArrowRight className="size-3.5" /></button>
                    )}
                  </div>
                </Card>
              </StaggerItem>
            )
          })}
        </div>
      )}

      {total > 0 && (
        <div className="flex items-center justify-between gap-2 text-caption text-muted">
          <span className="tabular-nums">第 {offset + 1}–{Math.min(offset + memes.length, total)} 条 / 共 {total.toLocaleString()} 条</span>
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - MEME_PAGE_SIZE))}>上一页</Button>
            <Button size="sm" variant="secondary" disabled={offset + MEME_PAGE_SIZE >= total} onClick={() => onPage(offset + MEME_PAGE_SIZE)}>下一页</Button>
          </div>
        </div>
      )}
    </div>
  )
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return <span>{label} <span className="text-body">{children}</span></span>
}
