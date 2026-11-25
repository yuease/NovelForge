import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { CheckCircle2, Eye, FileSearch, FileUp, ListChecks, Play, Quote, Sparkles, Star, ThumbsDown, ThumbsUp, Wand2 } from 'lucide-react'
import { api, REVISION_DISPATCHED } from '../lib/api'
import type { ManuscriptImportResult } from '../lib/api'
import { cn } from '../lib/cn'
import { COLLAPSE_MOTION, EASE_OUT, TAB_MOTION } from '../lib/motion'
import { navigate } from '../lib/router'
import { formatDateTime, formatNumber, messageOf, PRIORITY_META, READINESS_META } from '../lib/format'
import type { Tone } from '../lib/format'
import type { AiFlavorIssueItem, BookEvaluationReport, LLMProvider } from '../lib/types'
import { useStudio } from '../state/studio'
import {
  Alert, Badge, Button, Card, CardHeader, Collapsible, Dialog, Disclosure, EmptyState, PageHeader, Progress, ProgressRing,
  Segmented, Select, Stagger, StaggerItem, Tag, Thinking, useToast,
} from '../components/ui'
import { isModelChoiceReady, ModelPicker, resolveSelectedModel, useModelCatalog } from '../components/model/ModelPicker'
import ImportManuscriptDrawer from '../components/ImportManuscriptDrawer'
import RevisionPanel from './RevisionPanel'

type SixKey = keyof NonNullable<BookEvaluationReport['sixDimensions']>
const SIX: Array<{ key: SixKey; label: string }> = [
  { key: 'sellingPointAndOpening', label: '卖点与开篇' },
  { key: 'worldSettingAndCausality', label: '设定与因果' },
  { key: 'characterAndChoices', label: '人物与选择' },
  { key: 'plotAndPacing', label: '主线与节奏' },
  { key: 'foreshadowingAndEnding', label: '伏笔与结局' },
  { key: 'styleAndReadability', label: '文风与阅读' },
]

const AI_CATEGORY: Record<string, string> = {
  over_explaining: '写完又解释一遍',
  formulaic_reaction: '人物反应套公式',
  shared_voice: '人物共用一张嘴',
  static_situation: '情节动了处境没变',
  repetitive_hooks: '章末同一种钩子',
  inconsistent_details: '细节前后打架',
}

type Role = 'flash' | 'review' | 'rewrite'
type Choice = { provider: LLMProvider; model: string }
type Mode = 'full' | 'flash_only' | 'pro_only'
const failedBatch = (batch: { timelineSummary?: string }) => /^JSON\s*解析失败/.test(batch.timelineSummary || '')

export default function EvaluationPage() {
  const { jobs, activeJob, loadJobDetails, onJobCreated } = useStudio()
  const toast = useToast()
  const [importOpen, setImportOpen] = useState(false)
  const [importResult, setImportResult] = useState<ManuscriptImportResult | null>(null)
  const [confirmBatchPolish, setConfirmBatchPolish] = useState(false)
  const [mode, setMode] = useState<Mode>('full')
  const [revisionRefresh, setRevisionRefresh] = useState(0)
  useEffect(() => {
    const refresh = (event: Event) => {
      if ((event as CustomEvent<string>).detail === activeJob?.id) setRevisionRefresh((value) => value + 1)
    }
    window.addEventListener(REVISION_DISPATCHED, refresh)
    return () => window.removeEventListener(REVISION_DISPATCHED, refresh)
  }, [activeJob?.id])
  const checkpoint = activeJob?.evaluationCheckpoint
  const canResume = Boolean(checkpoint && checkpoint.mode === mode)
  useEffect(() => {
    if (checkpoint?.mode) setMode(checkpoint.mode)
  }, [activeJob?.id, checkpoint?.mode])

  const { catalog, loading: catalogLoading } = useModelCatalog()
  const [choices, setChoices] = useState<Partial<Record<Role, Choice & { jobId?: string }>>>({})
  const currentProvider = activeJob?.llmConfig?.provider ?? catalog?.activeProvider ?? 'mock'
  const currentModel = activeJob?.llmConfig?.model || ''

  const modelFor = (role: Role): Choice => {
    const saved = choices[role]
    if (saved && saved.jobId === activeJob?.id) return { provider: saved.provider, model: resolveSelectedModel(catalog, saved.provider, saved.model) }
    const previous = checkpoint?.selection
    if (previous && role !== 'rewrite') {
      return role === 'flash'
        ? { provider: previous.flashProvider, model: previous.flashModel }
        : { provider: previous.proProvider, model: previous.proModel }
    }
    const m = (role === 'flash' ? activeJob?.llmConfig?.lightModel : '') || currentModel
    return { provider: currentProvider, model: resolveSelectedModel(catalog, currentProvider, m) }
  }
  const setModel = (role: Role, provider: LLMProvider, model: string) => setChoices((o) => ({ ...o, [role]: { jobId: activeJob?.id, provider, model } }))
  const usesSaved = (role: Role) => {
    const c = choices[role]
    const saved = checkpoint?.selection
    if ((!c || c.jobId !== activeJob?.id) && saved && role !== 'rewrite') {
      return role === 'flash' ? saved.useSavedFlashConnection : saved.useSavedProConnection
    }
    return Boolean(c && c.jobId === activeJob?.id && isModelChoiceReady(catalog, c.provider, c.model))
  }

  const flash = modelFor('flash')
  const review = modelFor('review')
  const rewrite = modelFor('rewrite')
  const available = (c: Choice) => isModelChoiceReady(catalog, c.provider, c.model) || Boolean(c.model && activeJob?.llmConfig?.provider === c.provider && activeJob.llmConfig?.apiKey)
  const evaluationReady = (mode === 'pro_only' || available(flash)) && (mode === 'flash_only' || available(review))
  const resumeReady = canResume && checkpoint?.stage === 'finalizing' ? true
    : canResume && checkpoint?.stage === 'pro' ? available(review) : evaluationReady
  const rewriteReady = available(rewrite)

  const [category, setCategory] = useState('all')
  const [priority, setPriority] = useState<'all' | 'P0' | 'P1' | 'P2'>('all')
  const [busy, setBusy] = useState(false)
  const [deAiBusy, setDeAiBusy] = useState<number | 'batch' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const report = activeJob?.evaluationReport ?? null
  const running = activeJob?.evaluationStatus === 'running'
  const failedBatchCount = report?.batchSummaries?.filter(failedBatch).length || 0
  const repairMode = !checkpoint && report?.sourceHash && failedBatchCount &&
    (report.mode === 'full' || report.mode === 'flash_only') ? report.mode : null
  const repairReady = Boolean(repairMode && available(flash) && (repairMode === 'flash_only' || available(review)))

  useEffect(() => {
    if (!activeJob?.id || !running) return
    const t = window.setInterval(() => { void loadJobDetails(activeJob.id) }, 2500)
    return () => window.clearInterval(t)
  }, [activeJob?.id, running, loadJobDetails])

  const allIssues: AiFlavorIssueItem[] = useMemo(() => (Array.isArray(report?.aiFlavorIssues) ? report.aiFlavorIssues : []), [report?.aiFlavorIssues])
  const issues = useMemo(
    () => allIssues.filter((it) => (category === 'all' || it.category === category) && (priority === 'all' || it.priority === priority)),
    [allIssues, category, priority],
  )

  const runEvaluation = async (action: 'start' | 'restart' | 'repair' = 'start') => {
    const runMode = action === 'repair' ? repairMode : mode
    const ready = action === 'repair' ? repairReady : action === 'restart' ? evaluationReady : resumeReady
    if (!activeJob || !runMode || !ready) return
    const resume = canResume && action === 'start'
    setBusy(true)
    setError(null)
    try {
      await api.evaluateJob(activeJob.id, {
        mode: runMode,
        provider: runMode === 'pro_only' ? review.provider : flash.provider,
        flashProvider: flash.provider,
        flashModel: flash.model || undefined,
        proProvider: review.provider,
        proModel: review.model || undefined,
        useSavedFlashConnection: usesSaved('flash'),
        useSavedProConnection: usesSaved('review'),
        ...(action === 'repair' ? { repairFailedBatches: true } :
          resume ? { resumeEvaluation: true } : { batchSize: 10, restartEvaluation: Boolean(checkpoint) }),
        background: true,
      })
      await loadJobDetails(activeJob.id)
      toast(action === 'repair' ? '正在补跑失败批次' : resume ? '正在从断点继续评估' : '评估已在后台启动', 'success')
    } catch (err) {
      setError(messageOf(err, '启动评估失败'))
    } finally {
      setBusy(false)
    }
  }

  const deAi = async (chapterNumbers?: number[]) => {
    if (!activeJob || !rewriteReady) return
    setDeAiBusy(chapterNumbers?.length === 1 ? chapterNumbers[0] : 'batch')
    setError(null)
    try {
      await api.deAiJob(activeJob.id, {
        ...(chapterNumbers?.length ? { chapterNumbers } : { priorities: ['P0', 'P1'] }),
        provider: rewrite.provider,
        model: rewrite.model,
        useSavedConnection: usesSaved('rewrite'),
        background: true,
      })
      await loadJobDetails(activeJob.id)
      toast(chapterNumbers?.length ? `已启动第 ${chapterNumbers.join('、')} 章原地润色` : '已启动严重/建议问题章节的快速润色', 'success')
    } catch (err) {
      setError(messageOf(err, '快速润色启动失败'))
    } finally {
      setDeAiBusy(null)
    }
  }

  const heading = (
    <>
      <PageHeader
        eyebrow={activeJob ? `《${activeJob.title || activeJob.bookPlan?.title || '当前作品'}》` : undefined}
        title="作品评估"
        description="初审分批查找全书问题，精读给出六维评分与开篇反馈；可在顶栏或左下角随时切换作品。"
        actions={<Button variant="tonal" icon={<FileUp className="size-4" />} onClick={() => setImportOpen(true)}>导入 TXT</Button>}
      />
      <ImportManuscriptDrawer open={importOpen} onClose={() => setImportOpen(false)} onImported={(result) => {
        onJobCreated(result.job, 'evaluation')
        setError(null)
        setImportOpen(false)
        setImportResult(result)
      }} />
    </>
  )

  if (!activeJob) {
    return (
      <>
        {heading}
        <EmptyState icon={<Sparkles />} title="还没有可评估的作品" action={<Button variant="primary" onClick={() => navigate(jobs.length ? 'monitor' : 'create')}>{jobs.length ? '选择作品' : '新建作品'}</Button>} />
      </>
    )
  }

  const pickerDisabled = catalogLoading || running

  return (
    <>
      {heading}
      <AnimatePresence initial={false}>
        {importResult?.job.id === activeJob.id && (
          <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
            <div className="mb-6 grid gap-2">
              <Alert tone="success" title={`已导入《${activeJob.title || '导入作品'}》`} onClose={() => setImportResult(null)}>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span>{importResult.totalChapters} 章 · {formatNumber(importResult.totalWords)} 字</span>
                  <Button variant="ghost" size="sm" onClick={() => navigate('reader')}>打开成稿与导出</Button>
                </div>
              </Alert>
              {importResult.warnings.length > 0 && (
                <Alert tone="warning" title="导入提示">
                  <ul className="ml-4 list-disc space-y-1">
                    {importResult.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
                  </ul>
                </Alert>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Run panel */}
      <Card variant={running ? 'glow' : 'default'} padding="lg" className="mb-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="grid gap-2">
            <span className="text-caption font-medium text-body">评估模式</span>
            <Segmented
              value={mode}
              onChange={setMode}
              options={[{ value: 'full', label: '初审 + 精读' }, { value: 'flash_only', label: '仅初审' }, { value: 'pro_only', label: '仅精读' }]}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {allIssues.length > 0 && (
              <Button variant="tonal" loading={deAiBusy === 'batch'} disabled={!rewriteReady || catalogLoading || running || deAiBusy !== null} icon={<Wand2 className="size-4" />} onClick={() => setConfirmBatchPolish(true)}>
                快速润色（直接改正文）
              </Button>
            )}
            {canResume && !running && (
              <Button variant="tonal" disabled={!evaluationReady || catalogLoading || busy} onClick={() => void runEvaluation('restart')}>从头评估</Button>
            )}
            {repairMode && !running && (
              <Button variant="tonal" disabled={!repairReady || catalogLoading || busy} onClick={() => void runEvaluation('repair')}>补跑失败批次（{failedBatchCount}）</Button>
            )}
            <Button variant="gradient" loading={busy} disabled={!resumeReady || catalogLoading || running} icon={<Play className="size-4" />} onClick={() => void runEvaluation()}>
              {running ? '评估运行中' : canResume ? '继续评估' : checkpoint ? '从头评估' : report ? '重新评估' : '开始评估'}
            </Button>
          </div>
        </div>

        <div className="mt-4">
          {running ? (
            <Thinking label={activeJob.evaluationProgress || '正在执行评估…'} />
          ) : (
            <p className="text-caption text-muted">
              {activeJob.evaluationProgress || (report?.evaluatedAt ? `上次评估于 ${formatDateTime(report.evaluatedAt)}` : '尚未对本书运行过评估')}
            </p>
          )}
        </div>
        <AnimatePresence initial={false}>
          {checkpoint && (
            <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
              <div className="mt-3 grid gap-2">
                <div className="flex flex-wrap justify-between gap-x-3 gap-y-1 text-caption text-muted" role="status">
                  <span>{checkpoint.stage === 'pro' ? '初审已完成，续跑从精读开始。' :
                    checkpoint.stage === 'finalizing' ? '模型结果已保存，待汇总报告。' : '续跑会跳过已保存的批次。'}</span>
                  {checkpoint.totalBatches > 0 && (
                    <span className="tabular-nums">已保存初审 {checkpoint.completedBatches}/{checkpoint.totalBatches} 批</span>
                  )}
                </div>
                {checkpoint.totalBatches > 0 && (
                  <Progress size="sm" value={checkpoint.completedBatches} max={checkpoint.totalBatches}
                    tone={running ? 'gradient' : 'primary'} animated={running} label="初审批次进度" />
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
        {repairMode && !running && (
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-caption text-muted">
            <Badge tone="warning" dot>初审待补跑</Badge>
            <span>保留已通过的批次{repairMode === 'full' ? '，补齐后重新精读' : ''}。</span>
          </div>
        )}
        <AnimatePresence initial={false}>
          {!resumeReady && !catalogLoading && (
            <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
              <Alert tone="warning" className="mt-4">所选评估模型不可用，请在下方模型设置中选择已配置的模型。</Alert>
            </motion.div>
          )}
        </AnimatePresence>
        <AnimatePresence initial={false}>
          {error && (
            <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
              <Alert tone="danger" className="mt-4" onClose={() => setError(null)}>{error}</Alert>
            </motion.div>
          )}
        </AnimatePresence>

        <div className="mt-5 border-t border-line pt-4">
          <Disclosure title="模型设置">
            {running && (
              <p className="mb-3 text-caption text-muted">评估运行中，等待本轮评估结束后可切换模型。</p>
            )}
            <div className="grid gap-6 xl:grid-cols-3">
              <ModelPicker label="初审模型" provider={flash.provider} model={flash.model} catalog={catalog} disabled={pickerDisabled} onChange={(p, m) => setModel('flash', p, m)} />
              <ModelPicker label="精读模型" provider={review.provider} model={review.model} catalog={catalog} disabled={pickerDisabled} onChange={(p, m) => setModel('review', p, m)} />
              <ModelPicker label="精修模型" provider={rewrite.provider} model={rewrite.model} catalog={catalog} disabled={pickerDisabled || deAiBusy !== null} onChange={(p, m) => setModel('rewrite', p, m)} />
            </div>
          </Disclosure>
        </div>
      </Card>

      <RevisionPanel
        key={activeJob.id} job={activeJob}
        refreshToken={revisionRefresh}
        roundModels={{ flash: { ...flash, useSavedConnection: usesSaved('flash') },
          pro: { ...review, useSavedConnection: usesSaved('review') },
          ready: available(flash) && available(review) && !catalogLoading }}
        editor={{ ...rewrite, useSavedConnection: usesSaved('rewrite') }}
        verifier={{ ...review, useSavedConnection: usesSaved('review') }}
        selectedEditor={choices.rewrite?.jobId === activeJob.id ? { ...rewrite, useSavedConnection: usesSaved('rewrite') } : undefined}
        selectedVerifier={choices.review?.jobId === activeJob.id ? { ...review, useSavedConnection: usesSaved('review') } : undefined}
        onEditorChange={(provider, model) => setModel('rewrite', provider, model)}
        onVerifierChange={(provider, model) => setModel('review', provider, model)}
        catalog={catalog}
        catalogLoading={catalogLoading}
        modelAvailable={available}
        modelsReady={rewriteReady && available(review) && !catalogLoading}
        onApplied={() => loadJobDetails(activeJob.id)}
      />

      {!report ? (
        <EmptyState icon={<FileSearch />} title="暂无评估报告" description="运行评估后，这里会展示主编判断、六维评分、盲测反馈和 AI 味清单。" />
      ) : (
        <Report
          report={report}
          issues={issues}
          totalIssues={allIssues.length}
          category={category}
          priority={priority}
          onCategory={setCategory}
          onPriority={setPriority}
          canRewrite={rewriteReady && !catalogLoading && !running && deAiBusy === null}
          deAiBusy={deAiBusy}
          onDeAi={(n) => void deAi([n])}
        />
      )}

      <Dialog
        open={confirmBatchPolish}
        onClose={() => setConfirmBatchPolish(false)}
        title="快速润色（直接改写正文）"
        description="此操作会针对所有「必须修」与「建议改」问题所在的章节，直接在后台原地润色并更新正文。"
        footer={(
          <>
            <Button variant="ghost" onClick={() => setConfirmBatchPolish(false)}>取消</Button>
            <Button variant="primary" loading={deAiBusy === 'batch'} onClick={() => { setConfirmBatchPolish(false); void deAi() }}>确认开始润色</Button>
          </>
        )}
      >
        <p className="text-body text-muted">
          如果你希望先生成一份独立候选稿、核对后再决定是否写回正文，建议取消本弹窗，改用下方的「全书修订（先出候选稿）」。
        </p>
      </Dialog>
    </>
  )
}

/* ---------------- Report ---------------- */

function Report({
  report, issues, totalIssues, category, priority, onCategory, onPriority, canRewrite, deAiBusy, onDeAi,
}: {
  report: BookEvaluationReport
  issues: AiFlavorIssueItem[]
  totalIssues: number
  category: string
  priority: 'all' | 'P0' | 'P1' | 'P2'
  onCategory: (v: string) => void
  onPriority: (v: 'all' | 'P0' | 'P1' | 'P2') => void
  canRewrite: boolean
  deAiBusy: number | 'batch' | null
  onDeAi: (chapter: number) => void
}) {
  const reducedMotion = useReducedMotion()
  const scores = SIX.map((d) => report.sixDimensions?.[d.key]?.score).filter((s): s is number => typeof s === 'number')
  const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null
  const readiness = READINESS_META[report.readiness ?? ''] ?? { label: report.readiness || '待定', tone: 'neutral' as Tone }
  const verdicts = report.topVerdicts
    ? [
      { title: '开头想不想追', data: report.topVerdicts.openingHook },
      { title: '中段读不读得下去', data: report.topVerdicts.middlePacing },
      { title: '结尾值不值得', data: report.topVerdicts.endingPayoff },
    ]
    : []
  const blind = report.blindReaderFirst3Chapters
  const signals = report.deterministicSignals
  const p0 = issues.filter((i) => i.priority === 'P0').length

  return (
    <Stagger className="grid gap-6">
      {/* Verdict hero */}
      <StaggerItem>
        <Card variant="tinted" padding="lg">
          <div className="flex flex-wrap items-center gap-6">
            {avg !== null && (
              <ProgressRing value={(avg / 5) * 100} size={104} stroke={8}>
                <div className="text-center leading-none">
                  <div className="font-display text-headline text-ink tabular-nums">{avg.toFixed(1)}</div>
                  <div className="mt-1 text-caption text-muted">/ 5</div>
                </div>
              </ProgressRing>
            )}
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={readiness.tone} dot>{readiness.label}</Badge>
                {report.models?.pro && <Tag>精读 · {report.models.pro}</Tag>}
                {report.models?.flash && <Tag>初审 · {report.models.flash}</Tag>}
                {report.lastDeAiAt && <Tag>上次精修 {formatDateTime(report.lastDeAiAt)}</Tag>}
              </div>
              <h2 className="mt-3 font-display text-title font-medium text-ink">主编判断</h2>
              {report.overallSummary && <p className="mt-1 text-body leading-relaxed text-body">{report.overallSummary}</p>}
            </div>
          </div>
          {verdicts.length > 0 && (
            <div className="mt-6 grid gap-3 md:grid-cols-3">
              {verdicts.map((v) => (
                <div key={v.title} className="rounded-md bg-surface p-4 shadow-card">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-caption text-muted">{v.title}</span>
                    <Badge tone="primary">{v.data?.verdict || '—'}</Badge>
                  </div>
                  <p className="mt-2 text-body text-body">{v.data?.summary || '—'}</p>
                </div>
              ))}
            </div>
          )}
        </Card>
      </StaggerItem>

      {/* Six dimensions */}
      {report.sixDimensions && (
        <StaggerItem>
          <Card padding="lg">
            <CardHeader icon={<Star />} title="六维评分" description="1–5 分，含章号与原文证据" />
            <div className="grid gap-x-8 gap-y-6 lg:grid-cols-2">
              {SIX.map((d, i) => {
                const v = report.sixDimensions?.[d.key]
                if (!v) return null
                const tone = v.score >= 4 ? 'bg-g-green' : v.score >= 3 ? 'bg-g-blue' : v.score >= 2 ? 'bg-g-yellow' : 'bg-g-red'
                return (
                  <div key={d.key}>
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-body font-medium text-ink">{d.label}</span>
                      <span className="font-display text-title tabular-nums text-ink">{v.score}<span className="text-caption text-muted"> / 5</span></span>
                    </div>
                    <div className="mt-2 flex gap-1">
                      {[1, 2, 3, 4, 5].map((n) => {
                        const fill = v.score >= n ? 1 : v.score > n - 1 ? v.score - n + 1 : 0
                        return (
                          <div key={n} className="h-2 flex-1 overflow-hidden rounded-full bg-surface-3">
                            <motion.div
                              className={cn('h-full w-full origin-left rounded-full', tone)}
                              initial={reducedMotion ? false : { scaleX: 0 }}
                              animate={{ scaleX: fill }}
                              transition={reducedMotion ? { duration: 0 } : { delay: 0.06 + i * 0.03 + n * 0.025, duration: 0.34, ease: EASE_OUT }}
                            />
                          </div>
                        )
                      })}
                    </div>
                    {v.evidence && (
                      <p className="mt-3 flex gap-2 text-caption text-body">
                        <Quote className="mt-0.5 size-3.5 shrink-0 text-muted" />
                        <span>{v.chapterNumber ? <span className="mr-1 text-primary">第 {v.chapterNumber} 章</span> : null}“{v.evidence}”</span>
                      </p>
                    )}
                    {v.comment && <p className="mt-1.5 text-body text-muted">{v.comment}</p>}
                    {v.fixAdvice && <p className="mt-1 text-body text-success">建议：{v.fixAdvice}</p>}
                  </div>
                )
              })}
            </div>
          </Card>
        </StaggerItem>
      )}

      {/* Blind reader */}
      {blind && (
        <StaggerItem>
          <Card padding="lg">
            <CardHeader icon={<Eye />} title="前三章盲测" description="无大纲视角的读者反馈" />
            <div className="grid gap-4 md:grid-cols-2">
              <BlindColumn tone="success" icon={<ThumbsUp />} title="为什么想继续读" items={blind.whyKeepReading ?? []} />
              <BlindColumn tone="danger" icon={<ThumbsDown />} title="在哪里想跳读" items={blind.whereToSkip ?? []} />
            </div>
          </Card>
        </StaggerItem>
      )}

      {/* AI flavor */}
      <StaggerItem>
        <Card padding="lg">
          <CardHeader
            icon={<Sparkles />}
            title="AI 味清单"
            description={`共 ${totalIssues} 条命中${p0 ? ` · 当前筛选含 ${p0} 条必须修` : ''}`}
            actions={
              <Select aria-label="AI 味类别" value={category} onChange={(e) => onCategory(e.target.value)} className="h-8 rounded-full text-caption">
                <option value="all">全部类别</option>
                {Object.entries(AI_CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </Select>
            }
          />
          <Segmented
            size="sm"
            value={priority}
            onChange={onPriority}
            className="mb-4"
            options={[
              { value: 'all', label: '全部' },
              { value: 'P0', label: PRIORITY_META.P0.label },
              { value: 'P1', label: PRIORITY_META.P1.label },
              { value: 'P2', label: PRIORITY_META.P2.label },
            ]}
          />
          {signals && (Object.keys(signals.reactionStats ?? {}).length > 0 || Object.keys(signals.endingHookStats ?? {}).length > 0) && (
            <div className="mb-4 flex flex-wrap gap-2">
              {Object.entries(signals.reactionStats ?? {}).map(([label, s]) => (
                <Tag key={label}>套话「{label}」{s.totalCount} 次 · {s.chapterCount} 章</Tag>
              ))}
              {Object.entries(signals.endingHookStats ?? {}).map(([label, s]) => (
                <Badge key={label} tone="warning">章末「{label}」{s.count} 章</Badge>
              ))}
            </div>
          )}
          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={`${category}-${priority}`} {...TAB_MOTION} className="grid gap-3">
              {issues.map((it) => (
                <div
                  key={it.id}
                  className={cn('rounded-md p-4 transition-colors duration-200', it.resolved ? 'bg-success-soft/60' : 'bg-surface-2')}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={PRIORITY_META[it.priority as 'P0' | 'P1' | 'P2']?.tone ?? 'neutral'}>
                        {PRIORITY_META[it.priority as 'P0' | 'P1' | 'P2']?.label ?? it.priority}
                      </Badge>
                      <span className="text-body font-medium text-primary">第 {it.chapterNumber} 章</span>
                      <Tag className="bg-surface">{AI_CATEGORY[it.category] || it.category}</Tag>
                      {it.resolved && <Badge tone="success" icon={<CheckCircle2 />}>已润色</Badge>}
                    </div>
                    {!it.resolved && (
                      <Button size="sm" variant="ghost" loading={deAiBusy === it.chapterNumber} disabled={!canRewrite} icon={<Wand2 className="size-3.5" />} title="直接原地润色本章正文；如需先生成候选稿对比，请使用上方「全书修订」" onClick={() => onDeAi(it.chapterNumber)}>
                        原地润色本章
                      </Button>
                    )}
                  </div>
                  {it.quote && <p className="mt-3 border-l-2 border-line-strong pl-3 font-serif text-body text-ink">“{it.quote}”</p>}
                  <p className="mt-2 text-body text-muted">影响：{it.impact}</p>
                  {it.suggestion && <p className="mt-1 text-body text-success">建议：{it.suggestion}</p>}
                </div>
              ))}
              {issues.length === 0 && <p className="rounded-md bg-surface-2 py-8 text-center text-body text-muted">当前筛选条件下没有条目</p>}
            </motion.div>
          </AnimatePresence>
        </Card>
      </StaggerItem>

      {(report.batchSummaries?.length || report.rewritePlan?.length) ? (
        <StaggerItem className="grid gap-6 xl:grid-cols-2">
          {report.batchSummaries?.length ? (
            <Collapsible icon={<FileSearch />} title="分批初审摘要" subtitle={`${report.batchSummaries.length} 个批次${report.batchSummaries.some(failedBatch) ? ' · 含未通过校验的批次' : ''}`}>
              <ol className="grid gap-4">
                {report.batchSummaries.map((b, i) => (
                  <li key={i} className="rounded-md bg-surface-2 p-4">
                    <div className="text-body font-medium text-ink">{b.batchRange || `批次 ${i + 1}`}</div>
                    {failedBatch(b) ? <p className="mt-1 text-body text-muted">该批校验未通过，待补跑。</p> :
                      b.timelineSummary && <p className="mt-1 text-body text-body">{b.timelineSummary}</p>}
                    {!failedBatch(b) && b.situationDelta && <p className="mt-1 text-caption text-muted">处境变化：{b.situationDelta}</p>}
                    {b.endingHookSummary?.length ? (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {b.endingHookSummary.map((h, j) => <Tag key={j} className="bg-surface">第{h.chapterNumber}章 · {h.hookType}</Tag>)}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ol>
            </Collapsible>
          ) : <div />}
          {report.rewritePlan?.length ? (
            <Collapsible icon={<ListChecks />} title="改稿计划" subtitle={`${report.rewritePlan.length} 项`} defaultOpen>
              <ol className="grid gap-2">
                {report.rewritePlan.map((step, i) => (
                  <li key={i} className="flex gap-3 text-body text-body">
                    <span className="grid size-6 shrink-0 place-items-center rounded-full bg-primary-soft text-caption font-medium text-primary">{i + 1}</span>
                    <span className="pt-0.5">{step}</span>
                  </li>
                ))}
              </ol>
            </Collapsible>
          ) : null}
        </StaggerItem>
      ) : null}
    </Stagger>
  )
}

function BlindColumn({ tone, icon, title, items }: { tone: 'success' | 'danger'; icon: ReactNode; title: string; items: Array<{ chapterNumber?: number; quote?: string; reason?: string }> }) {
  return (
    <div className={cn('rounded-md p-4', tone === 'success' ? 'bg-success-soft' : 'bg-danger-soft')}>
      <div className={cn('mb-3 flex items-center gap-2 text-body font-medium [&_svg]:size-4', tone === 'success' ? 'text-success' : 'text-danger')}>{icon}{title}</div>
      <div className="grid gap-3">
        {items.length === 0 && <p className="text-caption text-muted">无</p>}
        {items.map((it, i) => (
          <div key={i} className="rounded-sm bg-surface p-3">
            {it.chapterNumber && <span className="text-caption text-primary">第 {it.chapterNumber} 章</span>}
            {it.quote && <p className="mt-0.5 font-serif text-body text-ink">“{it.quote}”</p>}
            {it.reason && <p className="mt-1 text-caption text-muted">{it.reason}</p>}
          </div>
        ))}
      </div>
    </div>
  )
}

