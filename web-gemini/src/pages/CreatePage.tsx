import { useEffect, useId, useState } from 'react'
import type { FormEvent } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { BookMarked, Cpu, Feather, Lightbulb, Rocket, Ruler, Sparkles, Wand2, X } from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { COLLAPSE_MOTION, SPRING_INDICATOR } from '../lib/motion'
import { navigate } from '../lib/router'
import { formatNumber, formatWords, messageOf } from '../lib/format'
import type { CreateJobInput, LLMProvider, MemeLevel, MemeSourceMode, POVType, ThinkingMode } from '../lib/types'
import { useStudio } from '../state/studio'
import { useSessionState } from '../state/prefs'
import {
  Alert, Badge, Button, Card, CardHeader, Checkbox, ChoiceCards, Disclosure, Divider, Field, Input,
  KeyValue, PageHeader, Select, Slider, Switch,
} from '../components/ui'
import { findProvider, isModelChoiceReady, ModelPicker, ModelSelect, resolveSelectedModel, useModelCatalog } from '../components/model/ModelPicker'

const GENRES = [
  '都市脑洞', '都市高武', '都市重生爽文', '都市种田', '悬疑推理', '悬疑脑洞', '无限流悬疑', '玄幻修仙', '玄幻脑洞',
  '现代言情', '现言脑洞', '古言脑洞', '古言权谋宅斗', '历史架空', '历史脑洞', '年代', '末世求生', '末世囤货脑洞',
  '游戏体育', '星光璀璨', '科幻星际', '职场商战',
]

const STYLES = [
  { value: '番茄爽文', label: '番茄爽文', hint: '节奏明快、强钩子' },
  { value: '红果短剧感', label: '红果短剧感', hint: '强冲突、高密度反转' },
  { value: '传统悬疑厚重', label: '传统悬疑厚重', hint: '伏笔细节、推理' },
]

const POV_OPTIONS: Array<{ value: POVType; label: string; description: string; badge?: boolean }> = [
  { value: '第三人称限知', label: '第三人称限知', description: '紧贴主角视角，严守知情范围，适合悬疑、反转与强代入感长篇。', badge: true },
  { value: '第一人称', label: '第一人称', description: '强情绪张力与内心独白，不写主角未目击的他人心理。' },
  { value: '第三人称全知', label: '第三人称全知', description: '上帝视角多线叙事，适合群像、权谋与宏大世界观。' },
]

const MEME_OPTIONS: Array<{ value: MemeLevel; label: string; description: string; badge?: boolean }> = [
  { value: 'none', label: '写实正剧', description: '严肃写实、强沉浸，不加入弹幕吐槽或网络热梗。' },
  { value: 'light', label: '适度玩梗', description: '主线紧张在线，适度穿插嘴替吐槽、反差金句与打脸节奏。', badge: true },
  { value: 'rich', label: '爆款网感', description: '强反差、高频名场面金句、弹幕心理戏与热梗二创。' },
]

const WORD_PRESETS = [
  { label: '2 万字', sub: '快速验证 · 10 章', words: 20000, chapters: 10 },
  { label: '4 万字', sub: '标准测试 · 20 章', words: 40000, chapters: 20 },
  { label: '10 万字', sub: '标准长篇 · 40 章', words: 100000, chapters: 40 },
  { label: '30 万字', sub: '完整长篇 · 100 章', words: 300000, chapters: 100 },
]

const SAMPLE = {
  title: '七号密钥',
  genre: '悬疑推理',
  premise: '落魄古董修复师林深在老宅阁楼的红木箱底藏了一把刻有七号铭文的铜钥匙，却不知这把钥匙正是十年前悬案的唯一物证。随着旧友顾沉的介入、钥匙悄然转移以及幕后黑手逼近，所有秘密将在第18章彻底引爆。',
}

const MODEL_CHOICE_KEY = 'novel_forge_generation_choice'
type ModelChoice = { provider: LLMProvider; model: string }

function loadModelChoice(): ModelChoice | null {
  try {
    const saved = JSON.parse(localStorage.getItem(MODEL_CHOICE_KEY) || 'null')
    if (typeof saved?.provider === 'string' && typeof saved?.model === 'string') return saved
  } catch { /* storage unavailable */ }
  return null
}

interface Draft {
  title: string
  premise: string
  genre: string
  pov: POVType
  targetWords: number
  totalChapters: number
  stylePreset: string
  memeLevel: MemeLevel
  memeSourceMode: MemeSourceMode
  preferFreeform: boolean
  temperature: number
  maxRetries: number
  timeoutSec: number
  llmMaxRetries: number
  autoResumeOnRateLimit: boolean
  enableEvaluation: boolean
  hotspotContext?: unknown
}

const DEFAULT_DRAFT: Draft = {
  title: '', premise: '', genre: '悬疑推理', pov: '第三人称限知', targetWords: 40000, totalChapters: 20,
  stylePreset: '番茄爽文', memeLevel: 'light', memeSourceMode: 'active_only', preferFreeform: false,
  temperature: 0.75, maxRetries: 3, timeoutSec: 300, llmMaxRetries: 5, autoResumeOnRateLimit: true, enableEvaluation: false,
}

export default function CreatePage() {
  const { prefill, consumePrefill, onJobCreated } = useStudio()
  const presetRingId = useId()
  const [draft, setDraft] = useSessionState<Draft>('nf_create_draft', DEFAULT_DRAFT)
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }))

  // Hotspot → Create hand-off
  useEffect(() => {
    if (!prefill) return
    setDraft((d) => ({
      ...d,
      title: prefill.title ?? d.title,
      premise: prefill.premise ?? d.premise,
      genre: prefill.genre ?? d.genre,
      hotspotContext: prefill.hotspotContext,
    }))
    consumePrefill()
  }, [prefill, consumePrefill, setDraft])

  const { catalog, loading: catalogLoading, error: catalogError } = useModelCatalog()
  const [modelChoice, setModelChoice] = useState<ModelChoice | null>(loadModelChoice)
  const [lightChoice, setLightChoice] = useState<ModelChoice | null>(null)
  const [thinkingChoice, setThinkingChoice] = useState<ThinkingMode | ''>('')
  const preferred = modelChoice && findProvider(catalog, modelChoice.provider)?.configured ? modelChoice.provider : catalog?.activeProvider
  const provider: LLMProvider = preferred && findProvider(catalog, preferred)?.configured ? preferred : 'mock'
  const available = findProvider(catalog, provider)?.models ?? []
  const model = resolveSelectedModel(catalog, provider, modelChoice?.provider === provider && available.includes(modelChoice.model) ? modelChoice.model : '')
  const lightModel = lightChoice?.provider === provider && available.includes(lightChoice.model) ? lightChoice.model : ''
  const modelReady = isModelChoiceReady(catalog, provider, model)

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [premiseFocused, setPremiseFocused] = useState(false)

  function selectModel(nextProvider: LLMProvider, nextModel: string) {
    const choice = { provider: nextProvider, model: nextModel }
    setModelChoice(choice)
    if (nextProvider !== provider) { setLightChoice(null); setThinkingChoice('') }
    try { localStorage.setItem(MODEL_CHOICE_KEY, JSON.stringify(choice)) } catch { /* ignore */ }
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!modelReady || catalogLoading || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const d = draft
      const payload: CreateJobInput = {
        title: d.title.trim() || undefined,
        premise: d.premise.trim(),
        genre: d.genre,
        pov: d.pov,
        targetWords: d.targetWords,
        totalChapters: d.totalChapters,
        styleGuide: d.stylePreset,
        memeLevel: d.memeLevel,
        memeSourceMode: d.memeSourceMode,
        maxRepairRetries: Math.max(0, Math.min(10, d.maxRetries)),
        enableEvaluation: d.enableEvaluation,
        ...(d.enableEvaluation ? { evalConfig: { mode: 'full' as const, provider, flashModel: lightModel || model, proModel: model } } : {}),
        preferFreeform: d.preferFreeform,
        hotspotContext: d.hotspotContext,
        llmConfig: {
          provider,
          useSavedConnection: true,
          model,
          lightModel: lightModel || undefined,
          thinkingMode: provider === 'deepseek' ? thinkingChoice || undefined : undefined,
          temperature: d.temperature,
          timeoutMs: Math.max(30, d.timeoutSec) * 1000,
          maxRetries: Math.max(0, Math.min(20, d.llmMaxRetries)),
          autoResumeOnRateLimit: d.autoResumeOnRateLimit,
        },
      }
      const res = await api.createJob(payload)
      setDraft(DEFAULT_DRAFT)
      onJobCreated(res.job)
    } catch (err) {
      setError(messageOf(err, '启动创作失败'))
    } finally {
      setSubmitting(false)
    }
  }

  const perChapter = Math.round(draft.targetWords / Math.max(1, draft.totalChapters))
  const autoTopic = !draft.premise.trim()

  return (
    <form onSubmit={handleSubmit}>
      <PageHeader
        title={<>开始一部<span className="nf-gradient-text"> 新作品</span></>}
        description="写下故事方向，墨枢会完成选题、规划、撰写与审校。没有梗概也可以，交给热点选题。"
      />

      <div className="grid gap-6 @[860px]/main:grid-cols-[minmax(0,1fr)_350px]">
        <div className="grid min-w-0 gap-6">
          {/* Premise – the hero input */}
          <Card padding="lg" className={cn('transition-shadow duration-300', premiseFocused && 'nf-glow-border')}>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <label htmlFor="premise" className="flex items-center gap-2 text-title font-medium text-ink">
                <Feather className="size-[18px] text-primary" />故事梗概
              </label>
              <div className="flex items-center gap-1">
                <Button size="sm" variant="ghost" icon={<Lightbulb className="size-4" />} onClick={() => navigate('hotspots')}>找灵感</Button>
                <Button size="sm" variant="ghost" icon={<Wand2 className="size-4" />} onClick={() => setDraft((d) => ({ ...d, ...SAMPLE }))}>填入示例</Button>
              </div>
            </div>
            <textarea
              id="premise"
              rows={6}
              value={draft.premise}
              onChange={(e) => set('premise', e.target.value)}
              onFocus={() => setPremiseFocused(true)}
              onBlur={() => setPremiseFocused(false)}
              placeholder="主角是谁、想要什么、谁会阻止他，故事最后会走向哪里？"
              className="w-full resize-y border-0 bg-transparent p-0 font-display text-title leading-relaxed text-ink placeholder:text-muted focus:outline-none focus:ring-0"
            />
            <AnimatePresence initial={false}>
              {draft.hotspotContext != null && (
                <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
                  <div className="mt-3 flex items-center gap-2">
                    <Badge tone="primary" icon={<Sparkles />}>已关联热点灵感</Badge>
                    <button type="button" className="rounded-full p-1 text-muted transition-colors hover:bg-surface-2 hover:text-ink" aria-label="移除热点关联" onClick={() => set('hotspotContext', undefined)}>
                      <X className="size-3.5" />
                    </button>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
            <Divider className="my-4" />
            <div className="flex flex-wrap items-center justify-between gap-3 text-caption text-muted">
              <span>{autoTopic ? '留空将自动采集近期热点榜单，三选一生成选题' : `${draft.premise.trim().length} 字`}</span>
              {autoTopic && (
                <Checkbox checked={draft.preferFreeform} onChange={(v) => set('preferFreeform', v)} label={<span className="text-caption">无热点时按题材自由构思</span>} />
              )}
            </div>
          </Card>

          {/* Basics */}
          <Card padding="lg">
            <CardHeader icon={<BookMarked />} title="基本设定" description="书名可留空，系统会自动命名。" />
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="书名" className="sm:col-span-3">
                {(id) => <Input id={id} value={draft.title} onChange={(e) => set('title', e.target.value)} placeholder="例如：消失的七号当铺" />}
              </Field>
              <Field label="题材">
                {(id) => (
                  <Select id={id} value={draft.genre} onChange={(e) => set('genre', e.target.value)}>
                    {!GENRES.includes(draft.genre) && <option value={draft.genre}>{draft.genre}</option>}
                    {GENRES.map((g) => <option key={g} value={g}>{g}</option>)}
                  </Select>
                )}
              </Field>
              <Field label="文风" className="sm:col-span-2" hint={STYLES.find((s) => s.value === draft.stylePreset)?.hint}>
                {(id) => (
                  <Select id={id} value={draft.stylePreset} onChange={(e) => set('stylePreset', e.target.value)}>
                    {STYLES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                  </Select>
                )}
              </Field>
            </div>

            <Divider />
            <div className="mb-3 flex items-baseline justify-between">
              <h4 className="text-body font-medium text-ink">叙事视角</h4>
              <span className="text-caption text-muted">审校时严格校验</span>
            </div>
            <ChoiceCards
              value={draft.pov}
              onChange={(v) => set('pov', v)}
              options={POV_OPTIONS.map((o) => ({ ...o, badge: o.badge ? <Badge tone="primary">推荐</Badge> : undefined }))}
            />

            <Divider />
            <div className="mb-3 flex items-baseline justify-between">
              <h4 className="text-body font-medium text-ink">网感浓度</h4>
              <span className="text-caption text-muted">玩梗与吐槽的密度</span>
            </div>
            <ChoiceCards
              value={draft.memeLevel}
              onChange={(v) => set('memeLevel', v)}
              options={MEME_OPTIONS.map((o) => ({ ...o, badge: o.badge ? <Badge tone="primary">推荐</Badge> : undefined }))}
            />
            <AnimatePresence initial={false}>
              {draft.memeLevel !== 'none' && (
                <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
                  <Checkbox
                    className="mt-4"
                    checked={draft.memeSourceMode === 'include_archive_elements'}
                    onChange={(v) => set('memeSourceMode', v ? 'include_archive_elements' : 'active_only')}
                    label="允许借鉴历史梗档案"
                    description="近 7 天新梗库为空时，借鉴本地梗 Wiki 中的反差结构与吐槽句式做元素化二创，不照抄老梗词。"
                  />
                </motion.div>
              )}
            </AnimatePresence>
          </Card>

          {/* Length */}
          <Card padding="lg">
            <CardHeader icon={<Ruler />} title="篇幅规模" description="动态分配单章目标，完本误差 ≤ ±10%。" />
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {WORD_PRESETS.map((p) => {
                const active = draft.targetWords === p.words && draft.totalChapters === p.chapters
                return (
                  <button
                    key={p.label}
                    type="button"
                    onClick={() => setDraft((d) => ({ ...d, targetWords: p.words, totalChapters: p.chapters }))}
                    aria-pressed={active}
                    className={cn(
                      'relative rounded-md border px-4 py-3 text-left transition-[border-color,background-color,transform] duration-150 active:scale-[.975]',
                      active ? 'border-transparent' : 'border-line hover:border-line-strong hover:bg-surface-2/60',
                    )}
                  >
                    {active && (
                      <motion.span
                        layoutId={presetRingId}
                        transition={SPRING_INDICATOR}
                        aria-hidden
                        className="pointer-events-none absolute inset-0 rounded-md border border-primary bg-primary-soft/70 shadow-[0_0_0_1px_var(--nf-primary)]"
                      />
                    )}
                    <div className={cn('relative font-display text-title font-medium transition-colors duration-150', active ? 'text-primary' : 'text-ink')}>{p.label}</div>
                    <div className="relative mt-0.5 text-caption text-muted">{p.sub}</div>
                  </button>
                )
              })}
            </div>
            <div className="mt-5 grid gap-4 sm:grid-cols-3">
              <Field label="目标总字数">
                {(id) => <Input id={id} type="number" min={5000} max={2000000} step={5000} value={draft.targetWords} onChange={(e) => set('targetWords', Number(e.target.value) || 20000)} />}
              </Field>
              <Field label="总章节数">
                {(id) => <Input id={id} type="number" min={3} max={500} value={draft.totalChapters} onChange={(e) => set('totalChapters', Number(e.target.value) || 10)} />}
              </Field>
              <div className="grid gap-1.5">
                <span className="text-caption font-medium text-body">单章约</span>
                <div className="flex h-10 items-center rounded-md bg-surface-2 px-3.5 text-body tabular-nums text-primary">
                  {formatNumber(perChapter)} 字 / 章
                </div>
              </div>
            </div>
          </Card>
        </div>

        {/* Right rail */}
        <aside className="min-w-0 @[860px]/main:sticky @[860px]/main:top-20 @[860px]/main:self-start">
          <div className="grid gap-6">
            <Card padding="lg">
              <CardHeader
                icon={<Cpu />}
                title="模型"
                actions={<Button size="sm" variant="ghost" onClick={() => navigate('models')}>管理</Button>}
              />
              <div className="grid gap-4">
                <ModelPicker provider={provider} model={model} catalog={catalog} label="主模型" onChange={selectModel} disabled={submitting || catalogLoading} includeLegacy={false} />
                {provider !== 'mock' && (
                  <ModelSelect
                    provider={provider}
                    model={lightModel}
                    onChange={(v) => setLightChoice({ provider, model: v })}
                    catalog={catalog}
                    label="辅助模型"
                    emptyLabel="与主模型相同"
                    disabled={submitting || catalogLoading}
                    includeLegacy={false}
                    hint="审校、事实提取和字数调整可用更轻量的模型。"
                  />
                )}
                {catalogLoading && <p className="text-caption text-muted" role="status">正在读取已配置的模型…</p>}
                {catalogError && <p className="text-caption text-warning" role="status">暂时无法读取模型目录，模拟模型仍可试用。</p>}
                {provider === 'mock' && !catalogLoading && <p className="text-caption text-muted">模拟模型用于预览流程，不会调用外部服务。</p>}
              </div>

              <Divider />
              <Disclosure title="高级设置">
                <div className="grid gap-5">
                  <Slider label="创意温度" min={0.1} max={1.2} step={0.05} value={draft.temperature} onChange={(v) => set('temperature', v)} format={(v) => v.toFixed(2)} />
                  <div className="grid grid-cols-2 gap-3">
                    <Field label="单章修复次数">{(id) => <Input id={id} type="number" min={0} max={10} value={draft.maxRetries} onChange={(e) => set('maxRetries', Number(e.target.value))} />}</Field>
                    <Field label="请求重试">{(id) => <Input id={id} type="number" min={0} max={20} value={draft.llmMaxRetries} onChange={(e) => set('llmMaxRetries', Number(e.target.value))} />}</Field>
                    <Field label="超时（秒）" className="col-span-2">{(id) => <Input id={id} type="number" min={30} max={1800} value={draft.timeoutSec} onChange={(e) => set('timeoutSec', Number(e.target.value))} />}</Field>
                  </div>
                  {provider === 'deepseek' && (
                    <Field label="思考模式">
                      {(id) => (
                        <Select id={id} value={thinkingChoice} onChange={(e) => setThinkingChoice(e.target.value as ThinkingMode | '')}>
                          <option value="">使用连接设置</option>
                          <option value="enabled">启用</option>
                          <option value="disabled">关闭</option>
                        </Select>
                      )}
                    </Field>
                  )}
                  <Switch checked={draft.autoResumeOnRateLimit} onChange={(v) => set('autoResumeOnRateLimit', v)} label="限流自动续跑" description="临时限流或超时后等待，从断点继续" />
                  <Switch checked={draft.enableEvaluation} onChange={(v) => set('enableEvaluation', v)} label="完本后自动评估" />
                </div>
              </Disclosure>
            </Card>

            <Card variant="tinted" padding="lg">
              <h3 className="mb-4 text-title font-medium text-ink">即将创作</h3>
              <KeyValue
                items={[
                  { label: '书名', value: draft.title.trim() || <span className="text-muted">自动命名</span> },
                  { label: '题材', value: draft.genre },
                  { label: '视角', value: draft.pov },
                  { label: '篇幅', value: `${formatWords(draft.targetWords)}字 · ${draft.totalChapters} 章` },
                  { label: '选题', value: autoTopic ? '热点自动选题' : '按梗概' },
                  { label: '模型', value: model || '—' },
                ]}
              />
              <AnimatePresence initial={false}>
                {error && (
                  <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
                    <Alert tone="danger" className="mt-4" onClose={() => setError(null)}>{error}</Alert>
                  </motion.div>
                )}
              </AnimatePresence>
              <Button
                type="submit"
                variant="gradient"
                size="lg"
                block
                className="mt-6"
                loading={submitting}
                disabled={!modelReady || catalogLoading}
                icon={<Rocket className="size-5" />}
              >
                {submitting ? '正在创建…' : '开始创作'}
              </Button>
              {!modelReady && !catalogLoading && <p className="mt-2 text-center text-caption text-muted">请先选择可用的模型</p>}
            </Card>
          </div>
        </aside>
      </div>
    </form>
  )
}

