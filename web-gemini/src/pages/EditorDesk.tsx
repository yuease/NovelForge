import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { ArrowDown, BookOpenCheck, FileSearch, GitCompareArrows, RotateCcw, Send, Sparkles, WandSparkles, Wrench, X } from 'lucide-react'
import { api, ApiError, REVISION_DISPATCHED } from '../lib/api'
import type { EditorEvidence, EditorPlan, EditorRecoveryPlan, EditorSession } from '../lib/api'
import { cn } from '../lib/cn'
import { messageOf } from '../lib/format'
import { EASE_EXIT, EASE_OUT, SPRING_SHEET } from '../lib/motion'
import type { EditorModelSelection, RevisionRun } from '../lib/types'
import { Alert, Button, IconButton, Thinking } from '../components/ui'
import { useModelCatalog } from '../components/model/ModelPicker'
import EditorModelMenu from './EditorModelMenu'

const EditorReplyMarkdown = lazy(() => import('./EditorReplyMarkdown'))

type Tracking = { id: string; from: number | null; run: RevisionRun }
const starters = [
  { icon: BookOpenCheck, label: '点评这份评估', prompt: '帮我核对一下这份评估报告：结合原文看看哪些是真问题、哪些是评估模型误判或吹毛求疵，最值得先改什么？' },
  { icon: GitCompareArrows, label: '验收这轮修订', prompt: '帮我深查一下这轮修订的效果：核对改后的大改动章节和未解决的问题，告诉我改得怎么样、有没有必要再修或重评？' },
  { icon: WandSparkles, label: '全书定向修改', prompt: '请帮我全书排查，把' },
]
const busyStatus = ['running', 'pausing']
const labels: Record<string, string> = {
  chapter: '章', chapterNumber: '章', claimedChapter: '报告列的章', priority: '重要程度', quote: '引文',
  impact: '影响', problem: '问题', suggestion: '建议', reason: '理由', decision: '处理方式', chapters: '涉及章节',
  candidateEvidence: '候选稿证据', evidence: '依据', status: '状态', before: '旧口径', after: '新口径',
  label: '名称', entries: '记录', origin: '来源', affectedChapters: '影响章节', openingCents: '期初金额（分）',
  deltaCents: '变动金额（分）', balanceCents: '余额（分）',
}

function EvidenceValue({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'boolean') return <>{value ? '是' : '否'}</>
  if (typeof value !== 'object') return <span className="whitespace-pre-wrap break-words">{String(value)}</span>
  if (Array.isArray(value)) {
    return value.every((v) => typeof v === 'number' || typeof v === 'string')
      ? <>{value.join('、')}</>
      : <ul className="ml-3 grid gap-2.5">{value.map((entry, i) => <li key={i} className="border-l border-line pl-3"><EvidenceValue value={entry} /></li>)}</ul>
  }
  return <dl className="grid gap-1">{Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== null && item !== '' && item !== undefined && (!Array.isArray(item) || item.length))
    .map(([key, item]) => <div key={key}>
      <dt className="inline font-medium text-ink">{labels[key] ?? key} · </dt><dd className="inline"><EvidenceValue value={item} /></dd>
    </div>)}</dl>
}

function ReplyWithEvidence({ turnId, reply, entries }: { turnId: number; reply: string; entries: EditorEvidence[] }) {
  const prefix = 'editor-evidence-' + turnId + '-'
  const section = useRef<HTMLDetailsElement>(null)
  const [focusedEvidence, setFocusedEvidence] = useState<string | null>(null)
  const jump = (id: string) => {
    const outer = section.current
    const target = document.getElementById(prefix + id)
    if (!outer || !(target instanceof HTMLDetailsElement) || !outer.contains(target)) return
    outer.open = true
    target.open = true
    setFocusedEvidence(id)
    target.querySelector('summary')?.focus({ preventScroll: true })
    const viewport = target.closest<HTMLDivElement>('[data-editor-scroll]')
    if (viewport) {
      const top = viewport.scrollTop + target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 12
      viewport.scrollTo({ top, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
    }
  }

  return <>
    <Suspense fallback={<p className="whitespace-pre-wrap break-words text-body text-ink">{reply}</p>}>
      <EditorReplyMarkdown evidencePrefix={prefix} reply={reply} evidenceIds={entries.map((entry) => entry.id)} onEvidence={jump} />
    </Suspense>
    {entries.length > 0 && <details ref={section} className="mt-3 border-t border-line pt-2.5">
      <summary className="cursor-pointer text-caption font-medium text-primary">查看核对依据（{entries.length}）</summary>
      <ul className="mt-3 grid gap-2.5">{entries.map((entry) => <li key={entry.id}>
        <details id={prefix + entry.id.toUpperCase()}
          className={cn('rounded-md border px-3 py-2 transition-colors duration-[160ms]', focusedEvidence === entry.id.toUpperCase() ? 'border-primary/55 bg-primary/5' : 'border-line')}>
          <summary className="cursor-pointer break-words text-caption font-medium text-body">{entry.id} · {entry.label}</summary>
          <div className="mt-2 max-h-72 overflow-y-auto text-caption leading-relaxed text-body"><EvidenceValue value={entry.data} /></div>
        </details>
      </li>)}</ul>
    </details>}
  </>
}

function ChapterSummary({ chapters }: { chapters: number[] }) {
  if (chapters.length <= 12) return <>第 {chapters.join('、')} 章（共 {chapters.length} 章）</>
  return <details className="inline">
    <summary className="inline cursor-pointer text-primary">共 {chapters.length} 章（第 {chapters.slice(0, 6).join('、')}… 章，点开查看全部）</summary>
    <p className="mt-1 break-words text-caption text-body">第 {chapters.join('、')} 章</p>
  </details>
}

function Proposal({ plan, enabled, expired, pending, onConfirm }: {
  plan: EditorPlan; enabled: boolean; expired?: boolean; pending: boolean; onConfirm: (plan: EditorPlan) => void
}) {
  const targeted = plan.mode === 'targeted'
  return (
    <motion.section
      initial={{ opacity: 0, y: 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: EASE_OUT }}
      className="mt-3 rounded-lg border border-line bg-primary/5 p-3.5"
    >
      <h3 className="mb-2 flex items-center gap-2 text-body font-medium text-ink">
        <FileSearch className="size-4 text-primary" />{targeted ? '全书定向修改提案（先出候选稿）' : '候选稿返修提案'}
      </h3>
      <dl className="grid gap-2 text-caption text-body">
        <div><dt className="inline font-medium">要改 </dt><dd className="inline"><ChapterSummary chapters={plan.chapters} /></dd></div>
        {plan.reviewChapters.length > 0 && <div><dt className="inline font-medium">一起复核 </dt><dd className="inline"><ChapterSummary chapters={plan.reviewChapters} /></dd></div>}
        <div><dt className="font-medium">怎么改</dt><dd className="mt-0.5 whitespace-pre-wrap break-words">{plan.note}</dd></div>
        {plan.amendment && <div>
          <dt className="font-medium">同时更新全书统一口径</dt>
          <dd className="mt-0.5 break-words">{plan.amendment.before} → {plan.amendment.after}</dd>
          <dd className="mt-0.5 break-words text-muted">依据 · {plan.amendment.evidence}</dd>
        </div>}
      </dl>
      {enabled ? <div className="mt-3">
        <Button variant="primary" loading={pending} disabled={pending} onClick={() => onConfirm(plan)}>
          {targeted ? '确认并生成候选稿' : '确认并开始返修'}
        </Button>
        <p className="mt-1.5 text-caption text-muted">修改结果会先保存为独立候选稿，不会直接覆盖正文，采纳仍由你确认。</p>
      </div> : expired ? (
        <p className="mt-3 text-caption text-muted">此提案已过期，可让总编结合最新正文重新拟定。</p>
      ) : null}
    </motion.section>
  )
}

function RecoveryProposal({ plan, enabled, expired, pending, onConfirm }: {
  plan: EditorRecoveryPlan; enabled: boolean; expired?: boolean; pending: boolean; onConfirm: (plan: EditorRecoveryPlan) => void
}) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: EASE_OUT }}
      className="mt-3 rounded-lg border border-warning/30 bg-warning-soft p-3.5"
    >
      <h3 className="mb-2 flex items-center gap-2 text-body font-medium text-ink">
        <RotateCcw className="size-4 text-primary" />断点恢复方案（待你确认）
      </h3>
      <dl className="grid gap-2 text-caption text-body">
        <div><dt className="inline font-medium">恢复阶段 </dt><dd className="inline">{plan.stageLabel}</dd></div>
        <div><dt className="font-medium">纠错指令</dt><dd className="mt-0.5 whitespace-pre-wrap break-words">{plan.note}</dd></div>
      </dl>
      {enabled ? <div className="mt-3">
        <Button variant="primary" loading={pending} disabled={pending} onClick={() => onConfirm(plan)}>确认并恢复断点</Button>
        <p className="mt-1.5 text-caption text-muted">点击后将带着这条纠错指令从当前断点继续，已保存批次会自动复用。</p>
      </div> : expired ? (
        <p className="mt-3 text-caption text-muted">此恢复方案已过期，可点击重新排查断点获取最新方案。</p>
      ) : null}
    </motion.section>
  )
}

export default function EditorDesk({
  open, onClose, jobId, title, width, onWidthChange, onResizingChange,
}: {
  open: boolean
  onClose: () => void
  jobId: string
  title: string
  width?: number | null
  onWidthChange?: (next: number | null) => void
  onResizingChange?: (resizing: boolean) => void
}) {
  const [resizing, setResizing] = useState(false)

  /* 1:1 direct-manipulation pointer drag-resize on desktop wide screens */
  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!onWidthChange) return
    event.preventDefault()
    setResizing(true)
    onResizingChange?.(true)
    const clamp = (raw: number) => Math.max(340, Math.min(Math.min(640, window.innerWidth - 360), Math.round(raw)))
    const move = (e: PointerEvent) => onWidthChange(clamp(window.innerWidth - e.clientX))
    const end = () => {
      setResizing(false)
      onResizingChange?.(false)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', end)
      window.removeEventListener('pointercancel', end)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', end)
    window.addEventListener('pointercancel', end)
  }

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.button
            key="editor-scrim"
            type="button"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1, transition: { duration: 0.18, ease: EASE_OUT } }}
            exit={{ opacity: 0, transition: { duration: 0.14, ease: EASE_EXIT } }}
            className="fixed inset-x-0 bottom-0 top-16 z-40 bg-black/26 backdrop-blur-[1.5px] min-[1200px]:hidden"
            aria-label="关闭总编面板"
            onClick={onClose}
          />
          <motion.aside
            key="editor-panel"
            id="chief-editor-panel"
            aria-label="总编对话"
            initial={{ x: '100%' }}
            animate={{ x: 0, transition: SPRING_SHEET }}
            exit={{ x: '100%', transition: { duration: 0.2, ease: EASE_EXIT } }}
            className="fixed bottom-0 right-0 top-16 z-[45] flex w-full max-w-[var(--editor-width)] flex-col border-l border-line bg-surface shadow-float"
            onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }}
          >
            {/* 1:1 follow drag-resize edge handle on desktop wide screens */}
            {onWidthChange && (
              <div
                role="separator"
                tabIndex={0}
                aria-orientation="vertical"
                aria-label="拖拽调整总编面板宽度（双击恢复默认）"
                title="拖拽调整宽度 · 双击恢复默认"
                onPointerDown={startResize}
                onDoubleClick={() => onWidthChange(null)}
                onKeyDown={(e) => {
                  const base = width ?? 420
                  if (e.key === 'ArrowLeft') { e.preventDefault(); onWidthChange(Math.min(640, base + 24)) }
                  else if (e.key === 'ArrowRight') { e.preventDefault(); onWidthChange(Math.max(340, base - 24)) }
                }}
                className="group absolute inset-y-0 -left-1.5 z-10 hidden w-3 cursor-col-resize items-center justify-center min-[1200px]:flex"
              >
                <span className={cn(
                  'h-12 w-1 rounded-full transition-[background-color,height,opacity] duration-[160ms] ease-[var(--ease-standard)]',
                  resizing ? 'h-20 bg-primary opacity-100' : 'bg-line-strong/0 opacity-0 group-hover:bg-primary/50 group-hover:opacity-100 group-focus-visible:bg-primary group-focus-visible:opacity-100',
                )} />
              </div>
            )}
            <EditorDeskContent key={jobId} open={open} onClose={onClose} jobId={jobId} title={title} />
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  )
}

function EditorDeskContent({
  open, onClose, jobId, title,
}: {
  open: boolean
  onClose: () => void
  jobId: string
  title: string
}) {
  const [session, setSession] = useState<EditorSession | null>(null)
  const [input, setInput] = useState('')
  const [switchingModel, setSwitchingModel] = useState(false)
  const [asking, setAsking] = useState('')
  const [liveStatus, setLiveStatus] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [modelError, setModelError] = useState<string | undefined>()
  const [tracking, setTracking] = useState<Tracking | null>(null)
  const [hasUnread, setHasUnread] = useState(false)
  const scroll = useRef<HTMLDivElement>(null)
  const nearBottom = useRef(true)
  const prevLastId = useRef<number | undefined>(undefined)
  const composer = useRef<HTMLTextAreaElement>(null)
  const retry = useRef<{ question: string; id: string } | null>(null)
  const { catalog, loading } = useModelCatalog()
  const usingSiwc = session?.model?.provider === 'openai-siwc'
  const siwc = catalog?.providers.find((provider) => provider.providerId === 'openai-siwc')
  const canSend = Boolean(session?.model?.model && catalog?.providers.some((provider) =>
    provider.configured && provider.providerId === session.model?.provider &&
    (!usingSiwc || (provider.profileId === session.model.siwcProfileId && provider.models.includes(session.model.model)))))
  const signInRequired = ['SIWC_LOGIN_REQUIRED', 'SIWC_PERMISSION_REQUIRED', 'SIWC_DISABLED'].includes(modelError || '')
  const usageLimited = ['SIWC_USAGE_LIMIT', 'SIWC_RATE_LIMIT'].includes(modelError || '')
  const manageSiwc = () => { onClose(); window.location.hash = '/models?provider=openai-siwc' }
  const receiving = tracking && busyStatus.includes(tracking.run.status)
  const hasNewCandidate = tracking && !receiving && tracking.run.hasCandidate
    && tracking.run.candidateTurn !== tracking.from

  useEffect(() => {
    if (!open) return
    let current = true
    let refreshing = false
    const refresh = async () => {
      if (refreshing) return
      refreshing = true
      try {
        const value = await api.editorSession(jobId)
        if (!current) return
        if (asking) {
          setLiveStatus(value.statusMessage ?? null)
          return
        }
        setLiveStatus(value.statusMessage ?? null)
        setSession((old) => {
          if (!old?.turns.length || !value.turns.length) return value
          const earlier = old.turns.filter((turn) => turn.id < value.turns[0].id)
          return { ...value, turns: [...earlier, ...value.turns], hasOlder: earlier.length ? old.hasOlder : value.hasOlder }
        })
      } catch (err) {
        if (current && !asking) setError(messageOf(err, '无法载入这本书的会话。'))
      } finally {
        refreshing = false
      }
    }
    if (switchingModel) return
    if (!asking) void refresh()
    const timer = window.setInterval(() => void refresh(), asking ? 1600 : 3000)
    return () => { current = false; window.clearInterval(timer) }
  }, [open, jobId, asking, switchingModel])

  const last = session?.turns[session.turns.length - 1]
  const lastAuthorTurn = session?.turns.filter((turn) => turn.answer.source !== 'revision-failure').pop()
  const lastFailureTurn = session?.turns.filter((turn) => turn.answer.source === 'revision-failure').pop()

  const scrollToBottom = (smooth = false) => {
    const el = scroll.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
    nearBottom.current = true
    setHasUnread(false)
  }

  useEffect(() => {
    if (!open) return
    requestAnimationFrame(() => scrollToBottom(false))
  }, [open])

  useEffect(() => {
    if (!open) return
    if (asking) {
      scrollToBottom(true)
      return
    }
    const newReply = last?.id !== undefined && prevLastId.current !== undefined && last.id !== prevLastId.current
    prevLastId.current = last?.id
    if (nearBottom.current) {
      scrollToBottom(false)
    } else if (newReply) {
      setHasUnread(true)
    }
  }, [asking, open, last?.id, tracking?.run.status, liveStatus])

  useEffect(() => {
    if (!open || !tracking || !receiving) return
    let current = true
    const timer = window.setInterval(() => {
      void api.getRevision(jobId).then(({ revision }) => {
        if (current && revision?.id === tracking.id) {
          setTracking((old) => old ? { ...old, run: revision } : null)
        } else if (current) setTracking(null)
      }).catch(() => {})
    }, 2300)
    return () => { current = false; window.clearInterval(timer) }
  }, [open, jobId, tracking, receiving])

  const chooseModel = async (model: EditorModelSelection) => {
    if (session?.model?.provider === model.provider && session.model.model === model.model
      && session.model.siwcProfileId === model.siwcProfileId) return true
    setSwitchingModel(true)
    setError(null)
    setModelError(undefined)
    try {
      const value = await api.saveEditorModel(jobId, model)
      setSession((old) => old && { ...old, ...value })
      return true
    } catch (err) {
      setError(messageOf(err, '切换总编模型失败。'))
      setModelError(err instanceof ApiError ? err.code : undefined)
      return false
    } finally {
      setSwitchingModel(false)
    }
  }

  const loadOlder = async () => {
    const first = session?.turns[0]
    if (!first) return
    setPending(true)
    setError(null)
    try {
      const older = await api.editorSession(jobId, first.id)
      const height = scroll.current?.scrollHeight ?? 0
      const offset = scroll.current?.scrollTop ?? 0
      setSession((value) => value && { ...value, hasOlder: older.hasOlder, turns: [
        ...older.turns.filter((turn) => !value.turns.some((item) => item.id === turn.id)), ...value.turns,
      ] })
      requestAnimationFrame(() => { if (scroll.current) scroll.current.scrollTop = offset + scroll.current.scrollHeight - height })
    } catch (err) {
      setError(messageOf(err, '无法读取较早的对话。'))
    } finally {
      setPending(false)
    }
  }

  const ask = async (text: string) => {
    const question = text.trim()
    if (!question || asking || pending || switchingModel || !canSend || session?.replying) return
    const requestId = retry.current?.question === question ? retry.current.id : crypto.randomUUID()
    retry.current = { question, id: requestId }
    setAsking(question)
    setLiveStatus('总编正在查证这本书的资料…')
    setError(null)
    setModelError(undefined)
    setInput('')
    try {
      const { turn } = await api.askEditor(jobId, question, requestId)
      setSession((old) => old && {
        ...old, replying: false, statusMessage: null, turns: [...old.turns.filter((item) => item.id !== turn.id), turn],
      })
      retry.current = null
    } catch (err) {
      setInput(question)
      setError(messageOf(err, '总编暂时没有回复，请再试一次。'))
      setModelError(err instanceof ApiError ? err.code : undefined)
    } finally {
      setAsking('')
      setLiveStatus(null)
    }
  }

  const confirm = async (id: number, plan: EditorPlan, from: number | null) => {
    if (pending || receiving || plan.dispatched || plan.expiresAt * 1000 <= Date.now()) return
    setPending(true)
    setError(null)
    try {
      const { revision } = await api.dispatchEditorPlan(jobId, plan.token)
      setTracking({ id: revision.id, from: plan.mode === 'targeted' ? null : from, run: revision })
      setSession((old) => old && { ...old, turns: old.turns.map((turn) => turn.id === id
        ? { ...turn, answer: { ...turn.answer, plan: { ...plan, dispatched: true } } } : turn) })
      window.dispatchEvent(new CustomEvent(REVISION_DISPATCHED, { detail: jobId }))
    } catch (err) {
      setError(messageOf(err, '返修单提交失败。'))
    } finally {
      setPending(false)
    }
  }

  const confirmRecovery = async (id: number, plan: EditorRecoveryPlan, from: number | null) => {
    if (pending || receiving || plan.dispatched || plan.expiresAt * 1000 <= Date.now()) return
    setPending(true)
    setError(null)
    try {
      const { revision } = await api.dispatchEditorRecovery(jobId, plan.token)
      setTracking({ id: revision.id, from, run: revision })
      setSession((old) => old && {
        ...old, pendingFailure: null,
        turns: old.turns.map((turn) => turn.id === id
          ? { ...turn, answer: { ...turn.answer, recoveryPlan: { ...plan, dispatched: true } } } : turn),
      })
      window.dispatchEvent(new CustomEvent(REVISION_DISPATCHED, { detail: jobId }))
    } catch (err) {
      setError(messageOf(err, '恢复断点失败。'))
    } finally {
      setPending(false)
    }
  }

  const diagnose = async () => {
    setPending(true)
    setError(null)
    try {
      await api.diagnoseRevision(jobId)
      setSession((old) => old && { ...old, replying: true, statusMessage: '总编正在排查中断原因…' })
    } catch (err) {
      setError(messageOf(err, '无法排查修订断点。'))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
            <div className="shrink-0 border-b border-line px-4 pb-3 pt-4 sm:px-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="flex items-center gap-2 text-title font-medium text-ink"><Sparkles className="size-4 text-primary" />总编</h2>
                  <p className="mt-1 truncate text-caption text-muted" title={title}>{title}</p>
                </div>
                <IconButton label="收起总编" size="sm" className="shrink-0" onClick={onClose}><X className="size-4" /></IconButton>
              </div>
            </div>

            <div
              ref={scroll}
              data-editor-scroll
              onScroll={() => {
                const el = scroll.current
                if (!el) return
                const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 72
                nearBottom.current = atBottom
                if (atBottom) setHasUnread(false)
              }}
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 [overflow-anchor:none] sm:px-5"
            >
              <div className="grid gap-4">
                {!session && !error && <Thinking label="正在载入这本书的对话…" />}
                {session && <>
                  {session.pendingFailure && <Alert tone="warning">
                    <div className="grid justify-items-start gap-2">
                      <div>
                        <p className="font-medium">修订在「{session.pendingFailure.stageLabel}」中断</p>
                        <p className="mt-0.5 break-words text-caption">{session.pendingFailure.message}</p>
                      </div>
                      {canSend ? (
                        <Button variant="tonal" size="sm" icon={<Wrench className="size-4" />}
                          disabled={pending || switchingModel || Boolean(asking) || session.replying}
                          onClick={() => void diagnose()}>让总编排查中断</Button>
                      ) : (
                        <span className="text-caption text-muted">请在下方先选择总编模型，再点此排查中断原因。</span>
                      )}
                    </div>
                  </Alert>}
                  {session.turns.length === 0 && !asking && <div className="grid gap-3">
                    <p className="text-body text-muted">可以让我点评评估报告、验收这轮改稿、排查中断报错，或直接提全书修改要求。</p>
                    <div className="flex flex-wrap gap-2">{starters.map(({ icon: Icon, label, prompt }, index) =>
                      <Button key={label} variant="tonal" size="sm" disabled={!canSend || pending || switchingModel || session.replying}
                        onClick={() => { if (index === 2) { setInput(prompt); composer.current?.focus() } else void ask(prompt) }}
                        icon={<Icon className="size-4" />}>{label}</Button>)}</div>
                  </div>}
                  {session.hasOlder && <Button variant="ghost" size="sm" disabled={pending} className="justify-self-center" onClick={() => void loadOlder()}>载入更早的对话</Button>}
                  {session.turns.map(({ id, question, answer }) => (
                    <motion.div
                      key={id}
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.2, ease: EASE_OUT }}
                      className="grid gap-2.5"
                    >
                      <article className={answer.source === 'revision-failure' ? 'rounded-lg border border-line bg-surface-2 px-3.5 py-3' : 'ml-6 rounded-lg bg-primary/10 px-3.5 py-3'}>
                        <p className="mb-1 text-caption font-medium text-muted">{answer.source === 'revision-failure' ? '断点排查' : '你'}</p>
                        <p className="whitespace-pre-wrap break-words text-body text-ink">{question}</p>
                      </article>
                      <article className="mr-2 rounded-lg bg-surface-2 px-3.5 py-3">
                        <p className="mb-1 text-caption font-medium text-muted">总编</p>
                        <ReplyWithEvidence turnId={id} reply={answer.reply} entries={answer.evidence} />
                        {answer.recoveryStatus && <p className="mt-2 text-caption text-body">{answer.recoveryStatus}</p>}
                        {answer.recoveryPlan && <RecoveryProposal plan={answer.recoveryPlan} pending={pending}
                          enabled={id === lastFailureTurn?.id && Boolean(session.pendingFailure) && !asking && !receiving && !answer.recoveryPlan.dispatched && answer.recoveryPlan.expiresAt * 1000 > Date.now()}
                          expired={!answer.recoveryPlan.dispatched && answer.recoveryPlan.expiresAt * 1000 <= Date.now()}
                          onConfirm={(plan) => void confirmRecovery(id, plan, answer.candidateTurn)} />}
                        {answer.recoveryPlan?.dispatched && <p className="mt-2 text-caption text-muted">已确认提交断点恢复</p>}
                        {id === lastFailureTurn?.id && Boolean(session.pendingFailure) && !answer.recoveryPlan?.dispatched && !answer.recoveryStatus && <Button variant="tonal" size="sm" className="mt-2"
                          disabled={!canSend || pending || switchingModel || Boolean(asking) || session.replying}
                          onClick={() => void diagnose()}>重新排查断点</Button>}
                        {answer.plan && <Proposal plan={answer.plan} pending={pending}
                          enabled={id === lastAuthorTurn?.id && !asking && !receiving && !answer.plan.dispatched && answer.plan.expiresAt * 1000 > Date.now()}
                          expired={!answer.plan.dispatched && answer.plan.expiresAt * 1000 <= Date.now()}
                          onConfirm={(plan) => void confirm(id, plan, answer.candidateTurn)} />}
                        {answer.plan?.dispatched && <p className="mt-2 text-caption text-muted">修改单已提交</p>}
                      </article>
                    </motion.div>
                  ))}
                  <AnimatePresence>
                    {asking && (
                      <motion.p
                        key="asking-bubble"
                        initial={{ opacity: 0, y: 8, scale: 0.98 }}
                        animate={{ opacity: 1, y: 0, scale: 1 }}
                        exit={{ opacity: 0 }}
                        transition={{ duration: 0.18, ease: EASE_OUT }}
                        className="ml-6 whitespace-pre-wrap break-words rounded-lg bg-primary/10 px-3.5 py-3 text-body text-ink"
                      >
                        {asking}
                      </motion.p>
                    )}
                  </AnimatePresence>
                  {(asking || session.replying) && <Thinking label={liveStatus || session.statusMessage || '总编正在查证这本书的资料…'} />}
                  {tracking && (receiving ? <Thinking label="修改已开始，正在生成新版候选稿…" /> : hasNewCandidate
                    ? <Alert tone={tracking.run.status === 'ready' ? 'success' : 'warning'}>
                      <div className="grid justify-items-start gap-2">
                        <span>{tracking.run.status === 'ready' ? '新版候选稿已生成。' : '新版候选稿已生成，仍有内容待核实。'}</span>
                        <Button variant="tonal" size="sm" disabled={!canSend || Boolean(asking)} onClick={() => void ask('对照改前改后，告诉我这次定向修改改得怎么样，有没有遗漏或改坏的地方？')}>验收这次修改</Button>
                      </div>
                    </Alert>
                    : <Alert tone="warning">{tracking.run.error || '修订暂未交付，请查看评估后修订的状态。'}</Alert>)}
                </>}
                <AnimatePresence>
                  {error && <Alert key="editor-error" tone="danger" onClose={() => { setError(null); setModelError(undefined) }}>
                    <span>{error}</span>
                    {signInRequired && <button type="button" className="ml-2 text-primary underline"
                      onClick={manageSiwc}>前往模型管理</button>}
                    {usageLimited && <a className="ml-2 text-primary underline" href="https://chatgpt.com/settings/usage"
                      target="_blank" rel="noreferrer">查看 ChatGPT 用量</a>}
                  </Alert>}
                </AnimatePresence>
              </div>
            </div>

            <div className="relative shrink-0 px-4 pb-4 pt-2 sm:px-5">
              <AnimatePresence>
                {hasUnread && (
                  <motion.div
                    initial={{ opacity: 0, y: 8, scale: 0.94 }}
                    animate={{ opacity: 1, y: 0, scale: 1, transition: { duration: 0.18, ease: EASE_OUT } }}
                    exit={{ opacity: 0, y: 6, scale: 0.96, transition: { duration: 0.12, ease: EASE_EXIT } }}
                    className="pointer-events-none absolute -top-9 inset-x-0 flex justify-center"
                  >
                    <button
                      type="button"
                      onClick={() => scrollToBottom(true)}
                      className="pointer-events-auto inline-flex items-center gap-1 rounded-full border border-line bg-surface px-3 py-1 text-caption font-medium text-primary shadow-raised transition-[background-color,transform] duration-[140ms] hover:bg-primary-soft active:scale-95"
                    >
                      <ArrowDown className="size-3.5" />查看新回复
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
              <form className="grid gap-1 rounded-2xl border border-line bg-canvas px-2.5 pb-2 pt-2.5 transition-[border-color,box-shadow] duration-[160ms] ease-[var(--ease-standard)] focus-within:border-primary/50 focus-within:ring-4 focus-within:ring-primary/10"
                onSubmit={(event) => { event.preventDefault(); void ask(input) }}>
                <textarea
                  ref={composer}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                      event.preventDefault()
                      void ask(input)
                    }
                  }}
                  className="max-h-32 min-h-14 resize-y bg-transparent px-1.5 text-body leading-relaxed text-ink outline-none"
                  maxLength={2400} rows={2} aria-label="问总编"
                  placeholder={canSend ? '例如：帮我把全书真实国家名虚构化，或点评这份评估报告（Enter 发送）' : '在下方选择模型后开始对话'}
                />
                <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <EditorModelMenu catalog={catalog} selected={session?.model ?? null} choose={chooseModel}
                      disabled={loading || !session || switchingModel || Boolean(asking) || Boolean(session?.replying)} />
                    <span className="hidden truncate text-[11px] text-muted sm:inline">Shift+Enter 换行</span>
                  </div>
                  <Button type="submit" variant="primary" size="sm" icon={<Send className="size-4" />}
                    disabled={!canSend || pending || switchingModel || Boolean(asking) || Boolean(session?.replying) || !input.trim()} loading={Boolean(asking)}>发送</Button>
                </div>
              </form>
              {usingSiwc && <p className="mt-2 px-1 text-caption text-muted">
                {canSend ? '使用 ChatGPT 计划' : siwc?.configured && siwc.profileId !== session?.model?.siwcProfileId
                  ? 'ChatGPT 账号已变更，请重新选择模型。' : '请在模型管理检查 ChatGPT 授权和模型目录。'}
                {' · '}
                {canSend ? <a className="text-primary underline" href="https://chatgpt.com/settings/usage"
                  target="_blank" rel="noreferrer">管理用量</a>
                  : <button type="button" className="text-primary underline" onClick={manageSiwc}>模型管理</button>}
              </p>}
            </div>
    </>
  )
}
