import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Archive, BookOpenCheck, Check, ChevronDown, Download, FileCheck2, GitBranch, ListChecks, Pause, PenLine, Play, Search, Settings2 } from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { downloadFile } from '../lib/download'
import { messageOf } from '../lib/format'
import type { Tone } from '../lib/format'
import { COLLAPSE_MOTION, SPRING_TACTILE } from '../lib/motion'
import type { JobRecord, ModelCatalog, RevisionAction, RevisionActionPayload, RevisionAmendment, RevisionRun, RevisionSelection } from '../lib/types'
import { Alert, Badge, Button, Card, CardHeader, Dialog, Disclosure, Drawer, Field, Input, PopoverSurface, Progress, Select, Textarea, Thinking, useToast } from '../components/ui'
import { findProvider, ModelPicker } from '../components/model/ModelPicker'
import CandidateRoundsPanel from './CandidateRoundsPanel'

const steps = [
  { label: '原稿审计', icon: Search, stages: ['prepare', 'audit'] },
  { label: '锁定台账', icon: BookOpenCheck, stages: ['ledger'] },
  { label: '分卷改写', icon: PenLine, stages: ['edit'] },
  { label: '独立终审', icon: ListChecks, stages: ['hard', 'review', 'repair', 'recheck', 'hotfix', 'hotcheck'] },
  { label: '合稿交付', icon: FileCheck2, stages: ['finish'] },
]

const revisionStatus: Record<RevisionRun['status'], { label: string; tone: Tone }> = {
  running: { label: '运行中', tone: 'primary' },
  paused: { label: '已暂停', tone: 'warning' },
  error: { label: '运行中断', tone: 'danger' },
  ready: { label: '候选稿就绪', tone: 'success' },
  needs_review: { label: '有待核实项', tone: 'warning' },
  waiting_author: { label: '待作者确认', tone: 'warning' },
  waiting_source: { label: '待补原文', tone: 'warning' },
  applied: { label: '已写回正文', tone: 'success' },
  archived: { label: '本轮已结束', tone: 'neutral' },
}

const stageLabel: Record<RevisionRun['stage'], string> = {
  prepare: '对照引文和章号', audit: '逐批只读审计', ledger: '生成全书台账',
  edit: '分卷精确替换', hard: '全书脚本验收', review: '逐批独立验收', repair: '返修关键问题',
  recheck: '复核返修和卷间衔接', hotfix: '按反馈定向返修',
  hotcheck: '复核反馈涉及的章节', finish: '汇总候选稿和改稿记录',
}

function RevisionFlow({ run, finished }: { run: RevisionRun | null; finished: boolean }) {
  const current = run ? steps.findIndex(step => step.stages.includes(run.stage)) : -1
  return (
    <ol className="grid gap-4 lg:grid-cols-5" aria-label="修订流程图">
      {steps.map(({ label, icon: Icon }, index) => {
        const complete = index < current || (finished && current === index)
        const active = index === current && !complete
        const live = active && run?.status === 'running'
        const paused = active && !!run && ['paused', 'waiting_author', 'waiting_source'].includes(run.status)
        const failed = active && run?.status === 'error'
        return (
          <li key={label} aria-current={active ? 'step' : undefined} className="relative min-w-0">
            {index > 0 && (
              <span
                aria-hidden
                className={cn(
                  'absolute -top-4 left-[22px] h-4 w-0.5 transition-colors duration-300 lg:top-1/2 lg:-left-4 lg:h-0.5 lg:w-4',
                  live ? 'nf-gradient-bg is-animated' : index <= current ? 'bg-primary/35' : 'bg-line-strong',
                )}
              />
            )}
            <motion.div
              initial={false}
              animate={{ scale: live ? 1.03 : 1 }}
              transition={SPRING_TACTILE}
              className={cn(
                'relative flex min-h-11 items-center gap-2 rounded-full border py-1.5 pr-3 pl-1.5 transition-[border-color,background-color] duration-200',
                live && 'nf-glow-border border-line bg-surface',
                paused && 'border-warning/45 bg-warning-soft',
                failed && 'border-danger/45 bg-danger-soft',
                !live && !paused && !failed && (complete ? 'border-primary/30 bg-surface' : 'border-line bg-surface'),
              )}
            >
              <span className={cn(
                'grid size-8 shrink-0 place-items-center rounded-full transition-colors duration-200',
                live ? 'nf-gradient-bg is-animated text-white' : paused ? 'bg-warning-soft text-warning' :
                  failed ? 'bg-danger-soft text-danger' : complete ? 'bg-primary-soft text-primary' : 'bg-surface-2 text-muted',
              )}>
                <Icon className="size-4" aria-hidden />
              </span>
              <span className="min-w-0 leading-tight">
                <span className={cn('block text-caption font-medium', active || complete ? 'text-ink' : 'text-body')}>{label}</span>
                {live && <span className="block text-caption text-primary">执行中</span>}
                {paused && <span className="block text-caption text-warning">{revisionStatus[run.status].label}</span>}
                {failed && <span className="block text-caption text-danger">中断</span>}
                {complete && <span className="sr-only">已完成</span>}
                {!active && !complete && <span className="sr-only">待执行</span>}
              </span>
              {complete && (
                <motion.span
                  initial={{ scale: 0.5, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  transition={SPRING_TACTILE}
                  className="ml-auto shrink-0 text-primary lg:hidden xl:block"
                >
                  <Check className="size-3.5" aria-hidden />
                </motion.span>
              )}
            </motion.div>
          </li>
        )
      })}
    </ol>
  )
}

function RevisionAuthorInput({ run, busy, error, onClose, onSubmit }: {
  run: RevisionRun; busy: boolean; error: string | null; onClose: () => void
  onSubmit: (action: 'decide' | 'restore', payload: RevisionActionPayload) => void
}) {
  const [decisions, setDecisions] = useState<Record<string, string>>({})
  const [sourceIndex, setSourceIndex] = useState(0)
  const [sourceText, setSourceText] = useState('')
  const [sourceEvidence, setSourceEvidence] = useState('')
  const choosing = run.status === 'waiting_author'
  const defect = run.sourceDefects?.[sourceIndex]
  const ready = choosing
    ? Boolean(run.pendingDecisions?.length && run.pendingDecisions.every(item => decisions[item.id]?.trim()))
    : Boolean(defect && sourceText.trim() && sourceEvidence.trim())
  const submit = () => {
    if (choosing) onSubmit('decide', { decisions })
    else if (defect) onSubmit('restore', { chapter: defect.chapter, field: defect.field, text: sourceText, evidence: sourceEvidence })
  }

  return (
    <Drawer open onClose={onClose} title={choosing ? '确认关键口径' : '补充可靠原文'}
      footer={<Button variant="primary" loading={busy} disabled={busy || !ready} onClick={submit}>确认并继续</Button>}>
      {choosing ? (
        <div className="grid gap-5">
          {run.pendingDecisions?.map(item => (
            <div key={item.id} className="grid gap-2 border-b border-line pb-4">
              <p className="text-body text-body">{item.problem}</p>
              {item.chapters.length > 0 && <p className="text-caption text-muted">涉及第 {item.chapters.join('、')} 章</p>}
              {item.evidence.map(({ chapter, quote }) => <p key={chapter + quote} className="text-caption text-muted">第 {chapter} 章 · {quote}</p>)}
              {item.options.map(option => <Button key={option} size="sm" variant="tonal" className="justify-self-start"
                onClick={() => setDecisions(values => ({ ...values, [item.id]: option }))}>{option}</Button>)}
              <Field label="选定口径">
                {(id) => <Textarea id={id} rows={2} value={decisions[item.id] || ''} maxLength={1200}
                  onChange={event => setDecisions({ ...decisions, [item.id]: event.target.value })} />}
              </Field>
            </div>
          ))}
        </div>
      ) : (
        <div className="grid gap-4">
          <Field label="缺损字段">
            {(id) => <Select id={id} value={sourceIndex} onChange={event => {
              setSourceIndex(Number(event.target.value)); setSourceText(''); setSourceEvidence('')
            }}>
              {run.sourceDefects?.map((item, index) => (
                <option key={item.chapter + item.field} value={index}>第 {item.chapter} 章 · {item.field === 'title' ? '标题' : '正文'}</option>
              ))}
            </Select>}
          </Field>
          <Field label="完整原文">
            {(id) => <Textarea id={id} rows={10} maxLength={100000} value={sourceText} onChange={event => setSourceText(event.target.value)} />}
          </Field>
          <Field label="原文来源">
            {(id) => <Input id={id} maxLength={600} value={sourceEvidence} onChange={event => setSourceEvidence(event.target.value)} />}
          </Field>
        </div>
      )}
      {error && <Alert tone="danger" className="mt-3">{error}</Alert>}
    </Drawer>
  )
}

type Props = {
  job: JobRecord
  refreshToken?: number
  roundModels: { flash: RevisionSelection; pro: RevisionSelection; ready: boolean }
  editor: RevisionSelection
  verifier: RevisionSelection
  selectedEditor?: RevisionSelection
  selectedVerifier?: RevisionSelection
  onEditorChange: (provider: RevisionSelection['provider'], model: string) => void
  onVerifierChange: (provider: RevisionSelection['provider'], model: string) => void
  catalog: ModelCatalog | null
  catalogLoading: boolean
  modelAvailable: (choice: RevisionSelection) => boolean
  modelsReady: boolean
  onApplied: () => Promise<void> | void
}

export default function RevisionPanel({
  job, refreshToken, roundModels, editor, verifier, selectedEditor, selectedVerifier, onEditorChange, onVerifierChange,
  catalog, catalogLoading, modelAvailable, modelsReady, onApplied,
}: Props) {
  const toast = useToast()
  const [run, setRun] = useState<RevisionRun | null>(null)
  const [legacy, setLegacy] = useState<RevisionRun | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [pending, setPending] = useState<RevisionAction | 'start' | null>(null)
  const [downloadingKey, setDownloadingKey] = useState<string | null>(null)
  const [archiveMenuOpen, setArchiveMenuOpen] = useState(false)
  const archiveMenuRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirmApply, setConfirmApply] = useState(false)
  const [showModels, setShowModels] = useState(false)
  const [volumeSize, setVolumeSize] = useState(20)
  const [concurrency, setConcurrency] = useState(3)
  const [targetChapters, setTargetChapters] = useState('')
  const [feedback, setFeedback] = useState('')
  const [amendment, setAmendment] = useState<RevisionAmendment | null>(null)
  const [showAuthorInput, setShowAuthorInput] = useState(false)
  const running = run?.status === 'running'
  const interrupted = run?.status === 'paused' || run?.status === 'error'
  const waiting = run?.status === 'waiting_author' || run?.status === 'waiting_source'
  const reviewable = run?.status === 'ready' || run?.status === 'needs_review'
  const hasCandidate = Boolean(run?.hasCandidate || reviewable || run?.status === 'applied')
  const needsAuthorForce = run?.status === 'needs_review' || run?.status === 'archived'
  const canApply = Boolean(
    run && !run.isMock && run.changedCount > 0 && hasCandidate
    && (run.status === 'ready' || run.status === 'needs_review' || run.status === 'archived')
  )
  const startBlocked = !loaded ? '正在读取修订状态。'
    : job.evaluationStatus === 'running' ? '等当前评估完成后再修订。'
    : !job.evaluationReport ? '完成当前正文的评估后可开始。'
    : job.status !== 'completed' && job.status !== 'failed' ? '写作任务结束后可开始修订。'
    : (job.completedChapters ?? job.totalChapters) !== job.totalChapters ? '还未保存全部章节，补齐后可开始。'
    : !modelsReady ? '请在模型设置中选好精修和精读模型。' : null
  const canStart = startBlocked === null
  const showStart = !running && !interrupted && !reviewable && !waiting
  const status = run ? revisionStatus[run.status] : null
  const editorOverride = !run?.isMock && selectedEditor?.provider !== 'mock' ? selectedEditor : undefined
  const verifierOverride = !run?.isMock && selectedVerifier?.provider !== 'mock' ? selectedVerifier : undefined
  const resumeEditor = editorOverride ?? run?.editor
  const resumeVerifier = verifierOverride ?? run?.verifier
  const modelLabel = ({ provider, model }: RevisionSelection) =>
    (findProvider(catalog, provider)?.label || provider) + ' · ' +
    (provider !== 'mock' && model === 'mock-novel-v1' ? '连接默认模型' : model)
  const changeReady = (!editorOverride || modelAvailable(editorOverride)) && (!verifierOverride || modelAvailable(verifierOverride))
  const resumeCatalog = catalog && { ...catalog, providers: catalog.providers.filter(p => p.providerId !== 'mock') }

  useEffect(() => {
    let active = true
    const refresh = async () => {
      try {
        const response = await api.getRevision(job.id)
        if (active) {
          setRun(response.revision)
          setLegacy(response.legacyRevision ?? null)
          setLoaded(true)
        }
      } catch (err) {
        if (active) setError(messageOf(err, '无法读取修订进度'))
      }
    }
    void refresh()
    const timer = running || run?.status === 'error'
      ? window.setInterval(() => { void refresh() }, running ? 2500 : 5000)
      : undefined
    return () => { active = false; window.clearInterval(timer) }
  }, [job.id, running, run?.status, refreshToken])

  useEffect(() => {
    if (!archiveMenuOpen) return
    const onPointer = (e: PointerEvent) => {
      if (e.target instanceof Node && !archiveMenuRef.current?.contains(e.target)) setArchiveMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setArchiveMenuOpen(false) }
    document.addEventListener('pointerdown', onPointer)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      window.removeEventListener('keydown', onKey)
    }
  }, [archiveMenuOpen])

  const exportRevisionFile = async (runId: string, artifact: Parameters<typeof api.revisionExportUrl>[2], label: string, fallbackName: string) => {
    const key = `${runId}:${artifact}`
    if (downloadingKey) return
    setDownloadingKey(key)
    setArchiveMenuOpen(false)
    try {
      await downloadFile(api.revisionExportUrl(job.id, runId, artifact), fallbackName)
      toast(`已下载${label}`, 'success')
    } catch (err) {
      toast(messageOf(err, `下载${label}失败`), 'danger')
    } finally {
      setDownloadingKey(null)
    }
  }

  // Do not show the large revision card before the book has an evaluation report or any revision history.
  if (!job.evaluationReport && !run && !legacy) return null

  const start = async () => {
    setPending('start')
    setError(null)
    try {
      const response = await api.startRevision(job.id, { editor, verifier, volumeSize, concurrency })
      setRun(response.revision)
    } catch (err) {
      setError(messageOf(err, '启动修订失败'))
    } finally {
      setPending(null)
    }
  }

  const act = async (action: RevisionAction, input?: RevisionActionPayload) => {
    if (!run || (action === 'resume' && !changeReady)) return
    setError(null)
    const chapters = targetChapters.trim().split(/[\s,，、]+/).filter(Boolean).map(Number)
    if (action === 'repair' && (!feedback.trim() || !chapters.length || chapters.length > 20 || chapters.some(n => !Number.isInteger(n) || n < 1 || n > run.chapterCount))) {
      setError('请输入 1–20 个有效章号和具体返修反馈。')
      return
    }
    setPending(action)
    try {
      const payload = input ?? (action === 'repair' ? { chapters, note: feedback.trim(), amendment: amendment ?? undefined } :
        action === 'resume' ? { editor: editorOverride, verifier: verifierOverride } : undefined)
      const { revision } = await api.changeRevision(job.id, run.id, action, payload)
      setRun(revision)
      if (action === 'apply') { setConfirmApply(false); await onApplied() }
      if (action === 'repair') { setFeedback(''); setTargetChapters(''); setAmendment(null) }
      if (action === 'decide' || action === 'restore') setShowAuthorInput(false)
    } catch (err) {
      setError(messageOf(err, '修订操作失败'))
    } finally {
      setPending(null)
    }
  }

  const archiveFiles: Array<{ artifact: Parameters<typeof api.revisionExportUrl>[2]; label: string; name: string }> = run ? [
    ...(run.hasLedger ? [{ artifact: 'ledger' as const, label: '全书设定清单', name: 'revision_ledger.json' }] : []),
    ...(run.hasLedger && run.workflowVersion === 2 ? [{ artifact: 'decisions' as const, label: '评估问题裁决', name: 'revision_decisions.json' }] : []),
    ...((hasCandidate || run.status === 'archived') ? [
      { artifact: 'original' as const, label: '修订前备份原稿', name: 'original_backup.txt' },
      { artifact: 'evaluation' as const, label: '修订前备份评估', name: 'evaluation_backup.json' },
    ] : []),
  ] : []

  return (
    <Card padding="lg" as="section" className="mb-6" aria-label="全书修订（先出候选稿）">
      <CardHeader
        title="全书修订（先出候选稿）"
        icon={<GitBranch />}
        description={run ? (
          <span>{run.chapterCount} 章{run.volumes.length > 0 && ' · ' + run.volumes.length + ' 卷'}{run.ledgerVersion && ' · 设定清单第 ' + run.ledgerVersion + ' 版'}{run.isMock && ' · 模拟运行'}</span>
        ) : '按评估报告分卷系统修订，先生成独立候选稿，审校通过后由你确认再写回正文。'}
        actions={status && <Badge tone={status.tone} dot live={running}>{status.label}</Badge>}
      />
      <RevisionFlow run={run} finished={Boolean(hasCandidate && run?.stage === 'finish')} />

      {run && (
        <div className="mt-4 grid gap-2.5">
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
            {running ? (
              <Thinking label={run.pauseRequested || pending === 'pause' ? '正在停止未完成的批次…' : stageLabel[run.stage]} />
            ) : <p className="text-body text-body">{stageLabel[run.stage]}</p>}
            {run.total > 0 && <p className="text-caption tabular-nums text-muted">本阶段 {run.done}/{run.total} 批</p>}
          </div>
          {run.total > 0 && <Progress size="sm" value={run.done} max={run.total} tone={interrupted ? 'primary' : 'gradient'} animated={running} label="当前修订阶段进度" />}
          {run.status === 'paused' && <p className="text-caption text-muted">已保存的批次会保留，继续时会重跑未完成的批次。</p>}
          {hasCandidate && <p className="text-caption text-muted">{run.changedCount} 章有改动 · P0 待核实 {run.blockingCount} 项</p>}
          {hasCandidate && run.remainingIssueCounts && (
            <p className="text-caption text-muted">
              余留问题 · P1 {run.remainingIssueCounts.P1} 项 · P2 {run.remainingIssueCounts.P2} 项，详见改稿记录
            </p>
          )}
          {running && Boolean(run.reprocessBatches) && <p className="text-caption text-muted">{run.reprocessBatches} 批使用当前台账重跑</p>}
          {running && run.reviewScope === 'incremental' && ['review', 'recheck', 'hotcheck'].includes(run.stage) && (
            <p className="text-caption text-muted">复核受影响章节及接缝</p>
          )}
          {(run.reportIssueCount !== undefined || Boolean(run.auditBlindChapters?.length)) && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-caption text-muted">
              {run.reportIssueCount !== undefined && <span>报告 {run.reportIssueCount} 条</span>}
              {Boolean(run.auditBlindChapters?.length) && <span>{run.auditBlindChapters?.length} 章初审盲区纳入复查</span>}
            </div>
          )}
        </div>
      )}
      {run?.error && <Alert tone={run.status === 'error' ? 'danger' : 'warning'} className="mt-3">{run.error}</Alert>}
      {run?.status === 'error' && !run.isMock && <p className="mt-2 text-caption text-muted">修订已停在断点，可打开右侧「问总编」手动发起排查与恢复。</p>}
      {error && <Alert tone="danger" className="mt-3" onClose={() => setError(null)}>{error}</Alert>}

      {showStart && (
        <div className="mt-5 flex flex-wrap items-end gap-3 border-t border-line pt-4">
          <Field label="每卷章数">
            {(id) => <Select id={id} value={volumeSize} onChange={event => setVolumeSize(Number(event.target.value))}>
              {[10, 15, 20, 25, 30, 40].map(n => <option key={n} value={n}>{n} 章</option>)}
            </Select>}
          </Field>
          <Field label="并行批次">
            {(id) => <Select id={id} value={concurrency} onChange={event => setConcurrency(Number(event.target.value))}>
              {[1, 2, 3, 4].map(n => <option key={n} value={n}>{n} 批</option>)}
            </Select>}
          </Field>
          <Button variant="primary" icon={<Play size={15} />} loading={pending === 'start'} disabled={!canStart || pending !== null} onClick={() => void start()}>开始修订</Button>
          <span className="self-center text-caption text-muted">
            {startBlocked ?? (job.status === 'failed'
              ? '章节已齐，可修订。写作终审结果会保留。'
              : '使用上方精修与精读模型，会调用模型。')}
          </span>
        </div>
      )}

      {run && (
        <div className={cn('mt-4 flex flex-wrap items-center gap-2', !showStart && 'border-t border-line pt-4')}>
          {running && !run.pauseRequested && <Button variant="tonal" icon={<Pause size={15} />} loading={pending === 'pause'} disabled={pending !== null} onClick={() => void act('pause')}>暂停</Button>}
          {interrupted && (
            <Button variant="primary" icon={<Play size={15} />} loading={pending === 'resume'} disabled={!changeReady || pending !== null} onClick={() => void act('resume')}>从断点继续</Button>
          )}
          {interrupted && !run.isMock && <Button variant="secondary" icon={<Settings2 size={15} />} disabled={pending !== null} onClick={() => setShowModels(true)}>切换模型</Button>}
          {waiting && <Button variant="primary" onClick={() => setShowAuthorInput(true)}>{run.status === 'waiting_author' ? '确认关键口径' : '补充可靠原文'}</Button>}
          {(interrupted || reviewable || waiting) && (
            <Button variant="ghost" icon={<Archive size={15} />} disabled={pending !== null}
              title="仅结束当前修订轮次并保留备份，不会修改当前正文"
              onClick={() => void act('archive')}>放弃本轮（不写回）</Button>
          )}
          {hasCandidate && <>
            <Button
              size="sm"
              variant="secondary"
              loading={downloadingKey === `${run.id}:candidate`}
              disabled={downloadingKey !== null && downloadingKey !== `${run.id}:candidate`}
              icon={<Download className="size-3.5" />}
              onClick={() => void exportRevisionFile(run.id, 'candidate', '候选稿', `${job.title || 'candidate'}_候选稿.txt`)}
            >
              下载候选稿
            </Button>
            <Button
              size="sm"
              variant="ghost"
              loading={downloadingKey === `${run.id}:report`}
              disabled={downloadingKey !== null && downloadingKey !== `${run.id}:report`}
              icon={<Download className="size-3.5" />}
              onClick={() => void exportRevisionFile(run.id, 'report', '改稿记录', `${job.title || 'revision'}_改稿记录.md`)}
            >
              改稿记录
            </Button>
          </>}
          {archiveFiles.length > 0 && (
            <div ref={archiveMenuRef} className="relative">
              <Button
                size="sm"
                variant="ghost"
                aria-expanded={archiveMenuOpen}
                aria-haspopup="menu"
                iconRight={<ChevronDown className={cn('size-3.5 transition-transform duration-200 ease-[var(--ease-emphasized)]', archiveMenuOpen && 'rotate-180')} />}
                onClick={() => setArchiveMenuOpen((v) => !v)}
              >
                修订档案
              </Button>
              <PopoverSurface
                open={archiveMenuOpen}
                origin="top-left"
                role="menu"
                className="absolute left-0 top-full z-30 mt-1.5 w-48 overflow-hidden rounded-xl border border-line bg-surface p-1.5 shadow-float"
              >
                {archiveFiles.map((item) => (
                  <button
                    key={item.artifact}
                    type="button"
                    role="menuitem"
                    disabled={downloadingKey !== null}
                    onClick={() => void exportRevisionFile(run.id, item.artifact, item.label, item.name)}
                    className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-caption text-body transition-[color,background-color,transform] duration-150 hover:bg-surface-2 hover:text-ink active:scale-[.98] disabled:opacity-50"
                  >
                    <Download className="size-3.5 shrink-0 text-muted" />
                    <span>{item.label}</span>
                  </button>
                ))}
              </PopoverSurface>
            </div>
          )}
          {canApply && (
            <Button
              variant={run.status === 'archived' ? 'secondary' : 'primary'}
              onClick={() => setConfirmApply(true)}
              disabled={pending !== null}
            >
              {run.status === 'archived' ? '恢复并写回正文' : run.status === 'needs_review' ? '仍写回正文' : '写回正文'}
            </Button>
          )}
        </div>
      )}
      {(interrupted || running) && !run?.isMock && resumeEditor && resumeVerifier && (
        <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-caption text-muted">
          <div className="flex min-w-0 gap-1.5"><dt className="shrink-0">编辑</dt><dd className="break-all text-body">{modelLabel(running ? run.editor : resumeEditor)}</dd></div>
          <div className="flex min-w-0 gap-1.5"><dt className="shrink-0">审校</dt><dd className="break-all text-body">{modelLabel(running ? run.verifier : resumeVerifier)}</dd></div>
        </dl>
      )}
      {(reviewable || waiting) && Boolean(run?.blockers?.length) && (
        <ul className="mt-3 space-y-1 rounded-xl bg-surface-2 px-4 py-3 text-caption text-muted">
          {run?.blockers?.map((item, index) => <li key={index}>{item.chapter ? '第 ' + item.chapter + ' 章' : '全书'} · {item.problem}</li>)}
        </ul>
      )}
      {run && (run.candidateHistory?.length ?? 0) > 1 && (
        <div className="mt-4 border-t border-line pt-3">
          <Disclosure title="历次候选稿">
            <ul className="grid gap-2 text-caption text-muted">
              {run.candidateHistory?.map(turn => (
                <li key={turn} className="flex flex-wrap items-center gap-3">
                  <span>第 {turn + 1} 版候选稿</span>
                  <button type="button" className="text-primary hover:underline disabled:opacity-50" disabled={downloadingKey !== null}
                    onClick={() => void exportRevisionFile(run.id, `candidate-${turn}`, `第 ${turn + 1} 版候选稿`, `candidate_v${turn + 1}.txt`)}>正文</button>
                  <button type="button" className="text-primary hover:underline disabled:opacity-50" disabled={downloadingKey !== null}
                    onClick={() => void exportRevisionFile(run.id, `report-${turn}`, `第 ${turn + 1} 版改稿记录`, `report_v${turn + 1}.md`)}>改稿记录</button>
                </li>
              ))}
            </ul>
          </Disclosure>
        </div>
      )}
      {run?.workflowVersion === 2 && run.candidateTurn !== undefined && (
        <CandidateRoundsPanel key={run.id} run={run} flash={roundModels.flash} pro={roundModels.pro}
          modelReady={roundModels.ready} revisionBusy={pending !== null || running}
          onContinue={(turn) => act('continue-round', { turn })} />
      )}
      {reviewable && !run?.isMock && (
        <div className="mt-4 border-t border-line pt-3">
          <Disclosure title="定向返修">
            <div className="grid gap-2.5 text-caption text-muted">
              <Field label="章号">
                {(id) => <Input id={id} value={targetChapters} placeholder="例如 51、65、99" onChange={event => setTargetChapters(event.target.value)} />}
              </Field>
              <Field label="返修反馈">
                {(id) => <Textarea id={id} rows={3} value={feedback} maxLength={2400} onChange={event => setFeedback(event.target.value)} />}
              </Field>
              {run.workflowVersion === 2 && (
                <label className="flex items-center gap-2">
                  <input type="checkbox" className="accent-primary" checked={Boolean(amendment)} onChange={event => setAmendment(event.target.checked ? { before: '', after: '', evidence: '' } : null)} />
                  同步更新台账
                </label>
              )}
              <AnimatePresence initial={false}>
                {amendment && (
                  <motion.div {...COLLAPSE_MOTION} className="grid gap-2.5 overflow-hidden">
                    {([['before', '当前口径'], ['after', '新口径'], ['evidence', '依据及影响范围']] as const).map(([field, label]) => (
                      <Field key={field} label={label}>
                        {(id) => <Textarea id={id} rows={2} maxLength={1200} value={amendment[field]}
                          onChange={event => setAmendment({ ...amendment, [field]: event.target.value })} />}
                      </Field>
                    ))}
                  </motion.div>
                )}
              </AnimatePresence>
              <Button className="justify-self-start" variant="tonal" loading={pending === 'repair'}
                disabled={pending !== null || !targetChapters.trim() || !feedback.trim() || Boolean(amendment && Object.values(amendment).some(value => !value.trim()))}
                onClick={() => void act('repair')}>返修指定章节</Button>
            </div>
          </Disclosure>
        </div>
      )}
      {run && waiting && showAuthorInput && <RevisionAuthorInput run={run} busy={pending !== null} error={error}
        onClose={() => setShowAuthorInput(false)} onSubmit={(action, payload) => void act(action, payload)} />}
      {legacy && (
        <div className="mt-4 border-t border-line pt-4">
          <Disclosure title="早期修订归档（只读）">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-caption">
              <Badge tone="neutral">只读</Badge>
              {(legacy.hasCandidate || ['ready', 'needs_review', 'applied'].includes(legacy.status)) && <>
                <button type="button" className="text-primary hover:underline disabled:opacity-50" disabled={downloadingKey !== null}
                  onClick={() => void exportRevisionFile(legacy.id, 'candidate', '早期候选稿', 'legacy_candidate.txt')}>旧候选稿</button>
                <button type="button" className="text-primary hover:underline disabled:opacity-50" disabled={downloadingKey !== null}
                  onClick={() => void exportRevisionFile(legacy.id, 'report', '早期改稿记录', 'legacy_report.md')}>旧改稿记录</button>
              </>}
              {legacy.hasLedger && <button type="button" className="text-primary hover:underline disabled:opacity-50" disabled={downloadingKey !== null}
                onClick={() => void exportRevisionFile(legacy.id, 'ledger', '早期设定清单', 'legacy_ledger.json')}>旧设定清单</button>}
              <button type="button" className="text-primary hover:underline disabled:opacity-50" disabled={downloadingKey !== null}
                onClick={() => void exportRevisionFile(legacy.id, 'original', '修订前原稿', 'legacy_original.txt')}>修订前原稿</button>
              <button type="button" className="text-primary hover:underline disabled:opacity-50" disabled={downloadingKey !== null}
                onClick={() => void exportRevisionFile(legacy.id, 'evaluation', '修订前评估', 'legacy_evaluation.json')}>修订前评估</button>
            </div>
          </Disclosure>
        </div>
      )}
      <Drawer open={showModels && interrupted} onClose={() => setShowModels(false)} title="切换修订模型"
        description="续跑时使用所选模型处理剩余批次。"
        footer={<Button variant="primary" onClick={() => setShowModels(false)}>完成</Button>}>
        {resumeEditor && resumeVerifier && (
          <div className="grid gap-6">
            <ModelPicker label="续跑编辑模型" {...resumeEditor} catalog={resumeCatalog} disabled={catalogLoading || pending !== null} onChange={onEditorChange} />
            <ModelPicker label="续跑审校模型" {...resumeVerifier} catalog={resumeCatalog} disabled={catalogLoading || pending !== null} onChange={onVerifierChange} />
          </div>
        )}
      </Drawer>
      <Dialog open={confirmApply} onClose={() => setConfirmApply(false)}
        title={run?.status === 'archived' ? '恢复已归档候选稿并写回正文' : '将候选稿写回正文'}
        description="写回后需要重新评估。原稿与本轮使用的评估可从修订记录区域下载。"
        footer={<><Button variant="ghost" onClick={() => setConfirmApply(false)}>取消</Button><Button variant="primary" loading={pending === 'apply'} onClick={() => void act('apply', needsAuthorForce ? { force: true } : undefined)}>确认写回</Button></>}>
        {error && <Alert tone="danger">{error}</Alert>}
        {run?.status === 'needs_review' && (
          <Alert tone="warning" className="mb-3" title="本轮尚有待核实提醒">
            审校仍保留了 {run.blockingCount || run.blockers?.length || 0} 条待核实项（常见于跨章复核提醒）。如果你或总编已确认改后正文符合预期，可直接由你拍板写回正文。
          </Alert>
        )}
        {run?.status === 'archived' && (
          <Alert tone="warning" className="mb-3" title="恢复此前已结束的修订版">
            本轮修订此前点击了归档结束，尚未写回正文。确认后将把保存的这版候选稿正式写回当前正文，旧原稿仍会保留备份。
          </Alert>
        )}
        <p className="text-body text-muted">本次将更新 {run?.changedCount ?? 0} 章。</p>
        {run?.remainingIssueCounts && run.remainingIssueCounts.P1 + run.remainingIssueCounts.P2 > 0 && (
          <p className="mt-2 text-body text-muted">仍有 {run.remainingIssueCounts.P1} 项 P1 和 {run.remainingIssueCounts.P2} 项 P2，可在改稿记录中核对。</p>
        )}
      </Dialog>
    </Card>
  )
}
