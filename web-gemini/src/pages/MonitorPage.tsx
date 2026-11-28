import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  AlertCircle, BookOpen, Check, CirclePause, CirclePlay, Clock, Coins, FileText,
  Hash, Layers, Plus, RefreshCw, Settings2, Sparkles, Terminal, Trash2,
} from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { COLLAPSE_MOTION, EASE_OUT, TAB_MOTION } from '../lib/motion'
import { navigate } from '../lib/router'
import { estimateJobCostCny, formatCny } from '../lib/pricing'
import { AUDIT_VERDICT_META, formatDateTime, formatNumber, formatTokens, formatWords, friendlyError, messageOf } from '../lib/format'
import type {
  BudgetProfile,
  ChapterRecord,
  JobRecord,
  LLMConfig,
  LLMProvider,
  MemeLevel,
  SSELogItem,
  StageBudgets,
  StageThinkingMap,
  ThinkingMode,
} from '../lib/types'
import { useStudio } from '../state/studio'
import type { ExtendPayload } from '../state/studio'
import {
  Alert, AnimatedNumber, Badge, Button, Card, Checkbox, Collapsible, Drawer, EmptyState, Field, IconButton,
  Input, PageHeader, Progress, ProgressRing, Select, SectionLabel, Segmented, StatusBadge, StaggerItem,
  Textarea, Thinking, useToast,
} from '../components/ui'
import { ModelPicker, ModelSelect, isModelChoiceReady, resolveSelectedModel, useModelCatalog } from '../components/model/ModelPicker'
import { StagePolicyControls, cleanStageBudgets, cleanStageThinking, supportsThinkingPolicy } from '../components/model/StagePolicyControls'
import { WorkflowGraph, useWorkflowMonitor } from './monitor/Workflow'

const STAGES: Record<string, string> = {
  topic: '构建选题', topic_selection: '构建选题', topic_lock: '锁定选题', book_plan: '全书规划',
  chapter_outlines: '分章细纲', chapter_writing: '章节撰写', chapter_verification: '章节审校', chapter_verify: '章节审校',
  chapter_repair: '章节返修', fact_extraction: '事实提取',
  arc_archive: '单元归档', drift_patch: '单元纠偏回修', volume_archive: '卷摘要归档',
  final_audit: '全书终审', evaluation: '成书评估', full_audit: '全书终审',
}

const jobTitle = (job: JobRecord) => job.title || job.bookPlan?.title || '未命名作品'
const checkpointChapter = (job: JobRecord) => (typeof job.checkpoint?.nextChapter === 'number' ? job.checkpoint.nextChapter : undefined)
const canResume = (job: JobRecord) => ['paused', 'interrupted', 'failed', 'error', 'pending'].includes(job.status)
const chapterPct = (job: JobRecord) => (job.totalChapters > 0 ? Math.min(100, Math.round((100 * (job.completedChapters ?? 0)) / job.totalChapters)) : 0)

export default function MonitorPage() {
  const { jobs, activeJob, selectJob, refreshJobs, service } = useStudio()

  if (service !== 'checking' && jobs.length === 0) {
    return (
      <>
        <PageHeader title="实时进度" description="在这里追踪每部作品的创作流程、章节进度和模型用量。" />
        <EmptyState
          title="还没有创作任务"
          description="从一个梗概或热点灵感开始，墨枢会自动完成选题、规划、撰写和审校。"
          action={<Button variant="gradient" size="lg" icon={<Sparkles className="size-5" />} onClick={() => navigate('create')}>创建第一部作品</Button>}
          className="py-24"
        />
      </>
    )
  }

  return (
    <>
      <PageHeader title="实时进度" description="追踪作品的工作流、章节与用量。" />
      <div className="grid grid-cols-1 gap-6 @[900px]/main:grid-cols-[300px_minmax(0,1fr)]">
        <JobList jobs={jobs} activeId={activeJob?.id} onSelect={selectJob} onRefresh={refreshJobs} />
        <AnimatePresence mode="wait" initial={false}>
          {activeJob ? (
            <motion.div key={activeJob.id} initial={TAB_MOTION.initial} animate={TAB_MOTION.animate} exit={TAB_MOTION.exit} className="min-w-0">
              <JobFocus job={activeJob} />
            </motion.div>
          ) : (
            <EmptyState key="none" icon={<BookOpen />} title="选择一部作品" description="在左侧列表中选择作品，即可查看工作流与进度。" />
          )}
        </AnimatePresence>
      </div>
    </>
  )
}

/* ---------------- Job list ---------------- */

function JobList({ jobs, activeId, onSelect, onRefresh }: { jobs: JobRecord[]; activeId?: string; onSelect: (id: string) => void; onRefresh: () => Promise<void> }) {
  const [refreshing, setRefreshing] = useState(false)
  const [filter, setFilter] = useState<'all' | 'active' | 'done'>('all')
  const toast = useToast()
  const shown = jobs.filter((j) => filter === 'all' || (filter === 'done' ? j.status === 'completed' : j.status !== 'completed'))

  return (
    <aside aria-label="任务列表" className="min-w-0 @[900px]/main:sticky @[900px]/main:top-20 @[900px]/main:self-start">
      <div className="mb-3 flex items-center justify-between gap-2">
        <Segmented
          size="sm"
          value={filter}
          onChange={setFilter}
          options={[
            { value: 'all', label: '全部', count: jobs.length },
            { value: 'active', label: '进行中' },
            { value: 'done', label: '已完成' },
          ]}
        />
        <IconButton
          label="刷新任务列表"
          size="sm"
          disabled={refreshing}
          onClick={async () => {
            setRefreshing(true)
            try { await onRefresh() } catch { toast('暂时无法刷新列表', 'danger') } finally { setRefreshing(false) }
          }}
        >
          <RefreshCw className={cn('size-4', refreshing && 'animate-spin')} />
        </IconButton>
      </div>
      {shown.length === 0 && <p className="px-2 py-6 text-center text-caption text-muted">没有符合条件的作品</p>}
      <div className="flex gap-3 overflow-x-auto p-1 pb-2 @[900px]/main:max-h-[calc(100vh-200px)] @[900px]/main:flex-col @[900px]/main:overflow-y-auto @[900px]/main:overflow-x-hidden">
        {shown.map((job, idx) => {
          const selected = job.id === activeId
          const running = job.status === 'running'
          return (
            <StaggerItem key={job.id} index={idx} className="w-64 shrink-0 @[900px]/main:w-auto">
              <button
                type="button"
                onClick={() => onSelect(job.id)}
                aria-pressed={selected}
                className={cn(
                  'w-full rounded-lg border p-4 text-left transition-[border-color,background-color,box-shadow,transform] duration-[160ms] ease-[var(--ease-standard)] active:scale-[.985]',
                  selected ? 'border-primary bg-primary-soft/60 shadow-[0_0_0_1px_var(--nf-primary)]' : 'border-line bg-surface hover:border-line-strong hover:shadow-raised',
                  running && !selected && 'nf-glow-border',
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-caption text-muted">{job.genre || '小说'}</span>
                  <StatusBadge status={job.status} />
                </div>
                <div className="mt-2 truncate text-title font-medium text-ink">{jobTitle(job)}</div>
                <div className="mt-3 flex items-center justify-between text-caption tabular-nums text-muted">
                  <span>{job.completedChapters ?? 0} / {job.totalChapters} 章</span>
                  <span>{formatWords(job.totalWords)} 字</span>
                </div>
                <Progress value={chapterPct(job)} size="sm" animated={running} className="mt-2" />
              </button>
            </StaggerItem>
          )
        })}
      </div>
    </aside>
  )
}

/* ---------------- Focus ---------------- */

function JobFocus({ job }: { job: JobRecord }) {
  const { chapters, facts, sseLogs, sseConnected, pauseJob, resumeJob, updateJobConfig, extendJob, clearLogs, loadJobDetails } = useStudio()
  const { frame, live } = useWorkflowMonitor(job.id)
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const [panel, setPanel] = useState<'model' | 'extend' | null>(null)

  const handle = async (action: () => Promise<unknown>, ok?: string) => {
    setBusy(true)
    setActionError('')
    try {
      await action()
      setPanel(null)
      if (ok) toast(ok, 'success')
    } catch (err) {
      setActionError(messageOf(err, '操作失败，请重试。'))
    } finally {
      setBusy(false)
    }
  }

  const running = job.status === 'running'
  const checkpoint = checkpointChapter(job)
  const retry = job.autoRetryState
  const rawError = job.error ?? job.lastError
  const errorMessage = typeof rawError === 'string' ? rawError : rawError?.message
  const issues = typeof rawError === 'object' && rawError ? rawError.issues ?? [] : []
  const progress = job.status === 'completed' ? 100 : Math.max(0, Math.min(100, Math.round(job.progress ?? chapterPct(job))))
  const cost = estimateJobCostCny(job)
  const stageLabel = job.stage ? STAGES[job.stage] ?? job.stage : undefined

  return (
    <div className="grid min-w-0 grid-cols-1 gap-6">
      <Card variant={running ? 'glow' : 'default'} padding="lg" as="section" className="@container/hero">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="flex min-w-0 items-center gap-5">
            <ProgressRing value={progress} size={84} stroke={7}>
              <span className="font-display text-title font-medium tabular-nums text-ink"><AnimatedNumber value={progress} /><span className="text-caption text-muted">%</span></span>
            </ProgressRing>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={job.status} />
                {stageLabel && running && <Badge tone="neutral">{stageLabel}</Badge>}
                {job.writingProfile?.reasoningHeavy && (
                  <span title={job.writingProfile.signals?.length ? `识别信号：${job.writingProfile.signals.join('、')}` : undefined}>
                    <Badge
                      tone={job.llmConfig?.thinkingMode === 'disabled' && job.llmConfig?.thinkingModeSource === 'user' ? 'warning' : 'primary'}
                    >
                      重算术 / 考据题材
                    </Badge>
                  </span>
                )}
              </div>
              <h2 className="mt-2 truncate font-display text-headline font-normal text-ink">{jobTitle(job)}</h2>
              <p className="mt-0.5 truncate text-body text-muted">
                {job.genre || '小说'}{job.pov ? ` · ${job.pov}` : ''}{job.llmConfig?.model ? ` · ${job.llmConfig.model}` : ''}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2" aria-label="任务操作">
            <Button variant="secondary" icon={<Settings2 className="size-4" />} disabled={busy} onClick={() => setPanel('model')}>切换模型</Button>
            {running && (
              <Button variant="tonal" icon={<CirclePause className="size-4" />} loading={busy} onClick={() => void handle(() => pauseJob(job.id), '已暂停，检查点已保存')}>暂停</Button>
            )}
            {canResume(job) && (
              <Button
                variant="primary"
                icon={<CirclePlay className="size-4" />}
                loading={busy}
                onClick={() => void handle(async () => {
                  const res = await resumeJob(job.id)
                  if (res?.quotaPending?.retryAt) {
                    toast(`已尝试继续，但原模型预计 ${formatDateTime(res.quotaPending.retryAt)} 恢复额度，如再受限可点「切换模型」`, 'info')
                    return
                  }
                  toast('已继续创作', 'success')
                })}
              >
                {job.status === 'pending' ? '开始创作' : '继续创作'}
              </Button>
            )}
            {job.status === 'completed' && (
              <Button variant="primary" icon={<Plus className="size-4" />} disabled={busy} onClick={() => setPanel('extend')}>续写新篇</Button>
            )}
          </div>
        </div>

        <div className="mt-6 grid grid-cols-2 gap-3 @[560px]/hero:grid-cols-4">
          <Metric icon={<Layers />} label="已存章节" value={<AnimatedNumber value={job.completedChapters ?? 0} />} suffix={`/ ${job.totalChapters} 章`} />
          <Metric icon={<FileText />} label="累计字数" value={<AnimatedNumber value={job.totalWords ?? 0} />} suffix={`/ ${formatWords(job.targetWords)}`} />
          <Metric icon={<Hash />} label="Tokens" value={formatTokens(job.tokenUsage?.totalTokens)} suffix={job.tokenUsage?.cacheHitTokens ? `缓存 ${formatTokens(job.tokenUsage.cacheHitTokens)}` : undefined} />
          <Metric
            icon={<Coins />}
            label={cost.primaryRate.isApproximate ? '参考费用' : '费用估算'}
            value={formatCny(cost.spentCny)}
            suffix={job.status !== 'completed' && cost.predictedTotalCny > cost.spentCny ? `预计 ${formatCny(cost.predictedTotalCny)}` : undefined}
            title={`${cost.primaryRate.rateNote}${cost.basisLabel ? ` · ${cost.basisLabel}` : ''}`}
          />
        </div>

        <AnimatePresence initial={false}>
          {retry?.waiting && (
            <motion.div initial={COLLAPSE_MOTION.initial} animate={COLLAPSE_MOTION.animate} exit={COLLAPSE_MOTION.exit} className="overflow-hidden">
              <Alert tone="warning" className="mt-5" title="正在冷却，等待自动重试">
                {retry.message || retry.code}
                {retry.attempt ? ` · 第 ${retry.attempt}${retry.maxAttempts ? `/${retry.maxAttempts}` : ''} 次` : ''}
                {retry.retryAfterSource === 'server' ? ' · 按服务端建议冷却' : ''}
                {retry.resumeAt ? ` · 预计 ${new Date(retry.resumeAt).toLocaleTimeString('zh-CN', { hour12: false })} 恢复` : ''}
              </Alert>
            </motion.div>
          )}
        </AnimatePresence>
        {actionError && <Alert tone="danger" className="mt-5" onClose={() => setActionError('')}>{actionError}</Alert>}
        {running && !retry?.waiting && (
          <div className="mt-5"><Thinking label={stageLabel ? `${stageLabel}中${job.currentChapter ? ` · 第 ${job.currentChapter} 章` : ''}` : '创作中'} /></div>
        )}
      </Card>

      <WorkflowGraph
        frame={frame}
        live={live}
        jobStatus={job.status}
        nextChapter={checkpoint}
        totalChapters={job.totalChapters}
        completedChapters={job.completedChapters ?? 0}
        arcPlans={job.arcPlans}
        arcSummaries={facts?.arcSummaries}
        volumeSummaries={facts?.volumeSummaries}
      />

      {(job.status === 'failed' || job.status === 'error' || job.status === 'interrupted') && errorMessage && (() => {
        const info = friendlyError(rawError)
        const errorCode = typeof rawError === 'object' && rawError ? rawError.code : undefined
        const isQuotaStop = errorCode === 'QUOTA_EXCEEDED' || errorCode === 'SIWC_USAGE_LIMIT' || errorCode === 'QUOTA_SUSPECTED'
        return (
          <Alert
            tone={isQuotaStop ? 'warning' : 'danger'}
            title={info.title}
            action={isQuotaStop ? <Button size="sm" variant="secondary" onClick={() => setPanel('model')}>切换模型继续</Button> : undefined}
          >
            <p>{info.summary}</p>
            {issues.length > 0 && (
              <ul className="mt-2 grid gap-1">
                {issues.map((issue, i) => (
                  <li key={i} className="flex gap-2"><span>·</span><span>{issue.message ?? issue.type}{issue.sourceChapter ? `（来源第 ${issue.sourceChapter} 章）` : ''}</span></li>
                ))}
              </ul>
            )}
            {info.detail && (
              <details className="mt-2.5 text-caption">
                <summary className="cursor-pointer select-none opacity-80 hover:opacity-100">查看技术详情</summary>
                <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-sm bg-black/5 p-2.5 font-mono text-[11px] leading-relaxed dark:bg-white/5">{info.detail}</pre>
              </details>
            )}
          </Alert>
        )
      })()}
      {job.status === 'completed' && !errorMessage && (
        <Alert
          tone={job.auditGate?.override || job.auditGate?.verdict === 'revise_recommended' ? 'warning' : 'success'}
          title={
            job.auditGate?.override
              ? '已保存全部章节（作者确认带问题完本）'
              : job.auditGate?.verdict === 'revise_recommended'
                ? '已保存全部章节，终审建议局部精修'
                : '已保存全部章节'
          }
          action={(
            <div className="flex flex-wrap gap-2">
              {(job.auditGate?.override || job.auditGate?.verdict === 'revise_recommended') && (
                <Button size="sm" variant="secondary" onClick={() => navigate('evaluation')}>前往修订</Button>
              )}
              <Button size="sm" variant="secondary" onClick={() => navigate('reader')}>去阅读</Button>
            </div>
          )}
        >
          {job.auditGate?.override
            ? '成稿已放行完本，随时可在「成稿与导出」中导出，或前往评估与修订逐章精修。'
            : job.auditGate?.verdict === 'revise_recommended'
              ? '全书主线完整且无硬性冲突，可直接在「成稿与导出」阅读，或前往评估与修订打磨节奏与细节。'
              : '可以在「成稿与导出」中阅读、修订并导出全书。'}
        </Alert>
      )}

      <div className="grid min-w-0 grid-cols-1 gap-6 @[960px]/main:grid-cols-2">
        <RunDetails
          job={job}
          chapters={chapters}
          busy={busy}
          onAcceptAudit={() => void handle(async () => {
            await api.acceptAuditOverride(job.id)
            await loadJobDetails(job.id)
          }, '已确认带问题完本，可前往成稿与导出')}
        />
        <Diagnostics job={job} logs={sseLogs} connected={sseConnected} onClear={clearLogs} />
      </div>

      <Drawer open={panel === 'model'} onClose={() => setPanel(null)} title="任务模型" description="调整这部作品使用的模型与重试策略。" width={520}>
        <JobModelPanel
          job={job}
          busy={busy}
          onSave={(c) => void handle(() => updateJobConfig(job.id, c), '模型设置已保存')}
          onResume={(c) => void handle(async () => {
            const res = await resumeJob(job.id, c)
            if (res?.quotaPending?.retryAt) {
              toast(`已尝试继续，但原模型预计 ${formatDateTime(res.quotaPending.retryAt)} 恢复额度，如再受限可切换其他模型`, 'info')
              return
            }
            toast('已保存并继续创作', 'success')
          })}
        />
      </Drawer>
      <Drawer open={panel === 'extend' && job.status === 'completed'} onClose={() => setPanel(null)} title="续写新篇" description={`从第 ${job.totalChapters + 1} 章开始，保留已完成的章节。`} width={520}>
        <ExtendPanel job={job} busy={busy} onSubmit={(p) => void handle(() => extendJob(job.id, p), '续写已启动')} />
      </Drawer>
    </div>
  )
}

function Metric({ icon, label, value, suffix, title }: { icon: ReactNode; label: string; value: ReactNode; suffix?: string; title?: string }) {
  return (
    <div className="min-w-0 overflow-hidden rounded-md bg-surface-2 p-4" title={title}>
      <div className="flex items-center gap-1.5 truncate text-caption text-muted [&_svg]:size-3.5 [&_svg]:shrink-0">{icon}<span className="truncate">{label}</span></div>
      <div className="mt-1.5 truncate font-display text-headline font-normal tabular-nums text-ink">{value}</div>
      {suffix && <div className="truncate text-caption tabular-nums text-muted">{suffix}</div>}
    </div>
  )
}

function RunDetails({
  job,
  chapters: saved,
  busy,
  onAcceptAudit,
}: {
  job: JobRecord
  chapters: ChapterRecord[]
  busy: boolean
  onAcceptAudit: () => void
}) {
  const [confirmAccept, setConfirmAccept] = useState(false)
  const audit = job.auditGate
  const stageStats = Object.entries(job.stageStats ?? {})
  const usage = job.tokenUsage
  const byModel = Object.entries(job.tokenUsageByModel ?? {})
  const chapters = useMemo(() => [...saved].sort((a, b) => b.chapterNumber - a.chapterNumber).slice(0, 8), [saved])
  if (!audit && !stageStats.length && !usage && !chapters.length) return null
  const maxDuration = Math.max(1, ...stageStats.map(([, s]) => s.durationMs ?? 0))

  const verdictMeta = audit?.verdict ? AUDIT_VERDICT_META[audit.verdict] : undefined
  const hardBlockers = audit?.localDiagnostics?.hardBlockers?.length
    ? audit.localDiagnostics.hardBlockers
    : (job.auditReport?.blockingIssues ?? [])
  const capItems = audit?.localDiagnostics?.capItems ?? []
  const canOverrideAudit = Boolean(
    audit
    && audit.chaptersComplete
    && audit.wordCountInRange
    && audit.exportValid
    && (audit.verdict === 'blocked' || audit.auditPassed === false)
    && !audit.override
    && job.status === 'failed',
  )

  return (
    <Collapsible icon={<Layers />} title="任务详情" subtitle="最近章节、完本检查与模型调用">
      <div className="grid gap-6">
        {chapters.length > 0 && (
          <div>
            <SectionLabel>最近保存</SectionLabel>
            <ul className="grid gap-1">
              {chapters.map((c) => (
                <li key={c.chapterNumber} className="flex items-center justify-between gap-3 rounded-sm px-2 py-1.5 text-body hover:bg-surface-2">
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="w-14 shrink-0 text-caption tabular-nums text-muted">第 {c.chapterNumber} 章</span>
                    <span className="truncate text-ink">{c.title}</span>
                    {c.verified && <Check className="size-3.5 shrink-0 text-success" aria-label="已审校" />}
                  </span>
                  <span className="shrink-0 text-caption tabular-nums text-muted">{formatNumber(c.wordCount)} 字</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {audit && (
          <div>
            <SectionLabel
              actions={(
                <div className="flex flex-wrap items-center gap-1.5">
                  {verdictMeta && <Badge tone={verdictMeta.tone}>{verdictMeta.label}</Badge>}
                  {typeof audit.score === 'number' && (
                    <span className="text-caption tabular-nums text-muted">
                      {typeof audit.rawScore === 'number' && audit.rawScore !== audit.score
                        ? `${audit.score} 分（原评 ${audit.rawScore} → 封顶 ${audit.scoreCap ?? audit.score}）`
                        : `${audit.score} 分`}
                    </span>
                  )}
                </div>
              )}
            >
              完本检查
            </SectionLabel>
            <div className="grid grid-cols-2 gap-2">
              {([['章节完整', audit.chaptersComplete], ['篇幅达标', audit.wordCountInRange], ['全书终审', audit.auditPassed], ['导出验证', audit.exportValid]] as const).map(([label, ok]) => (
                <div key={label} className={cn('flex items-center justify-between rounded-md px-3 py-2 text-body', ok ? 'bg-success-soft text-success' : 'bg-surface-2 text-body')}>
                  <span>{label}</span>
                  <span className="text-caption font-medium">{ok ? '通过' : '待处理'}</span>
                </div>
              ))}
            </div>
            {audit.override && (
              <p className="mt-2.5 rounded-sm bg-warning-soft/75 px-3 py-2 text-caption text-warning">
                已于 {formatDateTime(audit.override.at)} 确认带问题完本（放行时待处理阻断 {audit.override.hardBlockerCount ?? 0} 项）。
              </p>
            )}
            {hardBlockers.length > 0 && (
              <div className="mt-3 rounded-md border border-danger/25 bg-danger-soft/50 p-3 text-caption">
                <div className="mb-1.5 font-medium text-danger">
                  硬性阻断问题（共 {audit.hardBlockerCount ?? hardBlockers.length} 项）
                </div>
                <ul className="grid gap-1.5">
                  {hardBlockers.slice(0, 6).map((item, i) => (
                    <li key={`${item.chapterNumber ?? 'x'}-${i}`} className="rounded-sm bg-surface/90 px-2.5 py-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {item.chapterNumber ? <Badge tone="danger" className="h-5 px-1.5 text-[11px] tabular-nums">第 {item.chapterNumber} 章</Badge> : null}
                        <span className="text-ink">{item.message || item.code || '待核对硬伤'}</span>
                      </div>
                      {item.quote && <p className="mt-0.5 truncate text-[11px] text-muted" title={item.quote}>原文：「{item.quote}」</p>}
                    </li>
                  ))}
                </ul>
                {(audit.hardBlockerCount ?? hardBlockers.length) > 6 && (
                  <p className="mt-1.5 text-[11px] text-muted">
                    其余 {(audit.hardBlockerCount ?? hardBlockers.length) - 6} 项可在「评估与修订」中逐章查看。
                  </p>
                )}
              </div>
            )}
            {capItems.length > 0 && (
              <div className="mt-2.5 flex flex-wrap gap-1.5 text-[11px] text-muted">
                {capItems.slice(0, 4).map((item, i) => (
                  <span key={i} className="rounded-sm bg-surface-2 px-2 py-0.5">
                    {item.chapterNumber ? `第${item.chapterNumber}章 · ` : ''}{item.message || item.code}
                  </span>
                ))}
              </div>
            )}
            {audit.reasons?.length ? <p className="mt-2 text-caption text-muted">{audit.reasons.join('；')}</p> : null}
            {(canOverrideAudit || audit.verdict === 'blocked' || audit.verdict === 'revise_recommended') && (
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button size="sm" variant="secondary" onClick={() => navigate('evaluation')}>
                  前往评估与修订
                </Button>
                {canOverrideAudit && !confirmAccept && (
                  <Button size="sm" variant="tonal" disabled={busy} onClick={() => setConfirmAccept(true)}>
                    确认带问题完本
                  </Button>
                )}
                {canOverrideAudit && confirmAccept && (
                  <div className="flex flex-wrap items-center gap-2 rounded-sm bg-warning-soft/80 px-2.5 py-1.5 text-caption">
                    <span className="text-warning">将跳过终审阻断直接标记完本，是否确认？</span>
                    <Button size="sm" variant="primary" loading={busy} onClick={() => { setConfirmAccept(false); onAcceptAudit() }}>确认完本</Button>
                    <Button size="sm" variant="secondary" disabled={busy} onClick={() => setConfirmAccept(false)}>取消</Button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
        {(usage || stageStats.length > 0) && (
          <div>
            <SectionLabel>模型调用</SectionLabel>
            {usage && (
              <p className="mb-3 text-caption text-muted">
                输入 {formatTokens(usage.promptTokens)} · 输出 {formatTokens(usage.completionTokens)} · 合计 {formatNumber(usage.totalTokens)} Tokens
              </p>
            )}
            {byModel.length > 1 && (
              <div className="mb-3 flex flex-wrap gap-2">
                {byModel.map(([m, u]) => <Badge key={m} tone="neutral">{m} · {formatTokens(u.totalTokens)}</Badge>)}
              </div>
            )}
            <ul className="grid gap-2.5">
              {stageStats.map(([stage, s]) => (
                <li key={stage}>
                  <div className="flex items-center justify-between text-caption">
                    <span className="text-body">{STAGES[stage] ?? stage}</span>
                    <span className="tabular-nums text-muted">{s.calls ?? 0} 次{s.retries ? ` · 重试 ${s.retries}` : ''} · {((s.durationMs ?? 0) / 1000).toFixed(1)} 秒</span>
                  </div>
                  <Progress value={s.durationMs ?? 0} max={maxDuration} size="sm" tone="primary" className="mt-1" />
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Collapsible>
  )
}

const LOG_FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'chapter', label: '章节' },
  { value: 'verify', label: '校验' },
  { value: 'repair', label: '修复' },
  { value: 'error', label: '错误' },
] as const
type LogFilter = (typeof LOG_FILTERS)[number]['value']

function Diagnostics({ job, logs, connected, onClear }: { job: JobRecord; logs: SSELogItem[]; connected: boolean; onClear: () => void }) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState<LogFilter>('all')
  const stored: SSELogItem[] = open && !logs.length
    ? (job.logs ?? []).slice().reverse().map((e, i) => ({
      id: `saved-${i}`,
      type: e.level === 'error' ? 'job:error' : e.stage || 'job:log',
      message: e.message ?? '',
      timestamp: e.ts ? new Date(e.ts).toLocaleTimeString('zh-CN', { hour12: false }) : '',
    }))
    : []
  const shown = (logs.length ? logs : stored).filter((e) => filter === 'all' || e.type.includes(filter))

  return (
    <Collapsible
      icon={<Terminal />}
      title="执行日志"
      subtitle={connected && logs.length ? <span className="inline-flex items-center gap-1.5"><span className="size-1.5 animate-pulse rounded-full bg-g-green" />{logs.length} 条实时事件</span> : '排查问题时展开'}
      onToggle={setOpen}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <Segmented size="sm" value={filter} onChange={setFilter} options={LOG_FILTERS} ariaLabel="日志筛选" />
        {logs.length > 0 && <Button size="sm" variant="ghost" icon={<Trash2 className="size-3.5" />} onClick={onClear}>清空</Button>}
      </div>
      <div className="max-h-96 overflow-y-auto rounded-md bg-surface-2 p-2 font-mono text-caption">
        {shown.length === 0 && <p className="p-4 text-center font-sans text-muted">没有符合条件的日志</p>}
        <AnimatePresence initial={false}>
          {shown.map((e) => {
            const isErr = e.type.includes('error')
            return (
              <motion.div
                key={e.id}
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.16, ease: EASE_OUT }}
                className={cn('grid grid-cols-[64px_minmax(0,1fr)] gap-x-3 rounded-sm px-2 py-1.5 transition-colors duration-[140ms] hover:bg-surface sm:grid-cols-[64px_120px_minmax(0,1fr)]', isErr && 'bg-danger-soft')}
              >
                <time className="tabular-nums text-muted">{e.timestamp || '—'}</time>
                <code className={cn('hidden truncate sm:block', isErr ? 'text-danger' : 'text-primary')}>{e.type}</code>
                <span className={cn('break-words font-sans', isErr ? 'text-danger' : 'text-body')}>{e.message}</span>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>
    </Collapsible>
  )
}

/* ---------------- Panels ---------------- */

function JobModelPanel({ job, busy, onSave, onResume }: { job: JobRecord; busy: boolean; onSave: (c: Partial<LLMConfig>) => void; onResume: (c: Partial<LLMConfig>) => void }) {
  const { catalog } = useModelCatalog()
  const config = job.llmConfig
  const [provider, setProvider] = useState<LLMProvider>(config?.provider ?? 'mock')
  const [model, setModel] = useState(config?.model ?? '')
  const [lightModel, setLightModel] = useState(config?.lightModel ?? '')
  const [timeout, setTimeoutSec] = useState(Math.round((config?.timeoutMs ?? 300000) / 1000))
  const [retries, setRetries] = useState(config?.maxRetries ?? 5)
  const [thinkingMode, setThinkingMode] = useState<ThinkingMode>(
    config?.thinkingModeSource === 'default' ? 'auto' : (config?.thinkingMode ?? 'auto'),
  )
  const [stageThinking, setStageThinking] = useState<StageThinkingMap>(config?.stageThinking ?? {})
  const [budgetProfile, setBudgetProfile] = useState<BudgetProfile>(
    config?.budgetProfile ?? (config?.stageBudgets && Object.keys(config.stageBudgets).length ? 'custom' : 'balanced'),
  )
  const [stageBudgets, setStageBudgets] = useState<StageBudgets>(config?.stageBudgets ?? {})
  const [autoResume, setAutoResume] = useState(config?.autoResumeOnRateLimit !== false && job.autoResumeOnRateLimit !== false)
  const resolved = resolveSelectedModel(catalog, provider, model)
  const saved = isModelChoiceReady(catalog, provider, resolved)
  const canSave = saved || (provider === config?.provider && resolved === config.model)
  const canThink = supportsThinkingPolicy(provider, resolved, lightModel)
  const patch: Partial<LLMConfig> = {
    provider, model: resolved, lightModel, timeoutMs: Math.max(timeout, 30) * 1000,
    maxRetries: Math.min(20, Math.max(retries, 0)), autoResumeOnRateLimit: autoResume,
    budgetProfile,
    stageBudgets: budgetProfile === 'custom' ? (cleanStageBudgets(stageBudgets) ?? {}) : {},
    ...(canThink ? { thinkingMode, stageThinking: cleanStageThinking(stageThinking) ?? {} } : {}),
    ...(saved ? { useSavedConnection: true } : {}),
  }

  return (
    <div className="grid gap-5">
      <ModelPicker
        provider={provider}
        model={model}
        catalog={catalog}
        disabled={busy}
        label="主模型"
        onChange={(p, m) => { if (p !== provider) { setLightModel(''); setStageThinking({}) } setProvider(p); setModel(m) }}
      />
      <ModelSelect provider={provider} model={lightModel} onChange={setLightModel} catalog={catalog} disabled={busy} label="辅助模型" emptyLabel="与主模型相同" hint="用于审校、事实提取和归档纠偏，推荐 gemini-3.8-flash 或 gemini-4-hydrogen。" />
      <StagePolicyControls
        provider={provider}
        model={resolved}
        lightModel={lightModel}
        disabled={busy}
        thinkingMode={thinkingMode}
        onThinkingModeChange={setThinkingMode}
        stageThinking={stageThinking}
        onStageThinkingChange={setStageThinking}
        budgetProfile={budgetProfile}
        onBudgetProfileChange={setBudgetProfile}
        stageBudgets={stageBudgets}
        onStageBudgetsChange={setStageBudgets}
      />
      <div className="grid grid-cols-2 gap-3">
        <Field label="超时（秒）">{(id) => <Input id={id} type="number" min={30} value={timeout} onChange={(e) => setTimeoutSec(Number(e.target.value))} />}</Field>
        <Field label="重试上限">{(id) => <Input id={id} type="number" min={0} max={20} value={retries} onChange={(e) => setRetries(Number(e.target.value))} />}</Field>
      </div>
      <Checkbox checked={autoResume} onChange={setAutoResume} label="限流后自动续跑" description="遇到临时限流或超时，等待后从断点继续。" />
      {job.status === 'running' && <Alert tone="primary">模型配置将在下次运行时生效。</Alert>}
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="secondary" disabled={busy || !canSave} onClick={() => onSave(patch)}>保存设置</Button>
        {canResume(job) && (
          <Button variant="primary" icon={<CirclePlay className="size-4" />} loading={busy} disabled={!canSave} onClick={() => onResume(patch)}>保存并继续</Button>
        )}
      </div>
    </div>
  )
}

function ExtendPanel({ job, busy, onSubmit }: { job: JobRecord; busy: boolean; onSubmit: (p: ExtendPayload) => void }) {
  const [addChapters, setAddChapters] = useState(10)
  const [target, setTarget] = useState(0)
  const [direction, setDirection] = useState('')
  const [memeLevel, setMemeLevel] = useState<MemeLevel | ''>('')
  const [finalizeBook, setFinalize] = useState(true)
  const targetInvalid = !!target && target <= (job.totalWords ?? 0)
  const valid = addChapters >= 1 && addChapters <= 300 && !targetInvalid

  return (
    <form
      className="grid gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        if (!valid) return
        onSubmit({ addChapters, targetTotalWords: target || undefined, direction: direction.trim() || undefined, memeLevel: memeLevel || undefined, finalizeBook })
      }}
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="追加章节" hint="1–300 章">{(id) => <Input id={id} type="number" min={1} max={300} required value={addChapters || ''} onChange={(e) => setAddChapters(Number(e.target.value))} />}</Field>
        <Field label="目标总字数" error={targetInvalid ? `需大于当前 ${formatNumber(job.totalWords)} 字` : undefined} hint="留空按章节计算">
          {(id) => <Input id={id} type="number" min={0} value={target || ''} placeholder="自动" onChange={(e) => setTarget(Number(e.target.value))} />}
        </Field>
      </div>
      <Field label="网感浓度">
        {(id) => (
          <Select id={id} value={memeLevel} onChange={(e) => setMemeLevel(e.target.value as MemeLevel | '')}>
            <option value="">沿用原设置</option>
            <option value="none">写实正剧</option>
            <option value="light">适度玩梗</option>
            <option value="rich">爆款网感</option>
          </Select>
        )}
      </Field>
      <Field label="续写方向">{(id) => <Textarea id={id} rows={3} value={direction} onChange={(e) => setDirection(e.target.value)} placeholder="可留空，沿用已有故事线" />}</Field>
      <Checkbox checked={finalizeBook} onChange={setFinalize} label="续写结束后定稿" />
      <div className="flex justify-end pt-2">
        <Button type="submit" variant="primary" loading={busy} disabled={!valid} icon={<Clock className="size-4" />}>启动续写</Button>
      </div>
      <p className="flex items-center gap-1.5 text-caption text-muted"><AlertCircle className="size-3.5" />续写会沿用当前作品的事实库与人物设定。</p>
    </form>
  )
}
