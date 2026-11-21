import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { BookOpenCheck, FileSearch, GitCompareArrows, Send, Sparkles, WandSparkles, X } from 'lucide-react'
import { api, REVISION_DISPATCHED } from '../lib/api'
import type { EditorEvidence, EditorPlan, EditorSession } from '../lib/api'
import { messageOf } from '../lib/format'
import type { RevisionRun, RevisionSelection } from '../lib/types'
import { Alert, Button, IconButton, Thinking } from '../components/ui'
import { useModelCatalog } from '../components/model/ModelPicker'
import EditorModelMenu from './EditorModelMenu'

const EditorReplyMarkdown = lazy(() => import('./EditorReplyMarkdown'))

type Tracking = { id: string; from: number | null; run: RevisionRun }
const starters = [
  { icon: BookOpenCheck, label: '这份评估怎么看？', prompt: '用新手能懂的话告诉我这份评估在说什么，哪些问题最值得先处理？请核对几个关键例子。' },
  { icon: GitCompareArrows, label: '这轮改稿改得怎么样？', prompt: '对照原稿和候选稿，告诉我这轮实际改好了什么、还有什么问题。请举核对过的例子。' },
  { icon: WandSparkles, label: '我想改一处', prompt: '我想改第 ' },
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
          className={'rounded-md border px-3 py-2 ' + (focusedEvidence === entry.id.toUpperCase() ? 'border-primary/55 bg-primary/5' : 'border-line')}>
          <summary className="cursor-pointer break-words text-caption font-medium text-body">{entry.id} · {entry.label}</summary>
          <div className="mt-2 max-h-72 overflow-y-auto text-caption leading-relaxed text-body"><EvidenceValue value={entry.data} /></div>
        </details>
      </li>)}</ul>
    </details>}
  </>
}

function Proposal({ plan, enabled, pending, onConfirm }: {
  plan: EditorPlan; enabled: boolean; pending: boolean; onConfirm: (plan: EditorPlan) => void
}) {
  return <section className="mt-3 rounded-lg border border-line bg-primary/5 p-3.5">
    <h3 className="mb-2 flex items-center gap-2 text-body font-medium text-ink"><FileSearch className="size-4 text-primary" />返修单</h3>
    <dl className="grid gap-2 text-caption text-body">
      <div><dt className="inline font-medium">要改 </dt><dd className="inline">第 {plan.chapters.join('、')} 章</dd></div>
      {plan.reviewChapters.length > 0 && <div><dt className="inline font-medium">一起检查 </dt><dd className="inline">第 {plan.reviewChapters.join('、')} 章</dd></div>}
      <div><dt className="font-medium">怎么改</dt><dd className="mt-0.5 whitespace-pre-wrap break-words">{plan.note}</dd></div>
      {plan.amendment && <div>
        <dt className="font-medium">同时更新全书统一口径</dt>
        <dd className="mt-0.5 break-words">{plan.amendment.before} → {plan.amendment.after}</dd>
        <dd className="mt-0.5 break-words text-muted">依据 · {plan.amendment.evidence}</dd>
      </div>}
    </dl>
    {enabled && <div className="mt-3">
      <Button variant="primary" loading={pending} disabled={pending} onClick={() => onConfirm(plan)}>确认并开始返修</Button>
      <p className="mt-1.5 text-caption text-muted">返修会生成新版候选稿，正文写回仍由你确认。</p>
    </div>}
  </section>
}

export default function EditorDesk({ open, onClose, jobId, title }: {
  open: boolean; onClose: () => void; jobId: string; title: string
}) {
  const [session, setSession] = useState<EditorSession | null>(null)
  const [input, setInput] = useState('')
  const [switchingModel, setSwitchingModel] = useState(false)
  const [asking, setAsking] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tracking, setTracking] = useState<Tracking | null>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const composer = useRef<HTMLTextAreaElement>(null)
  const retry = useRef<{ question: string; id: string } | null>(null)
  const { catalog, loading } = useModelCatalog()
  const canSend = Boolean(session?.model?.model && catalog?.providers.some((provider) =>
    provider.configured && provider.providerId === session.model?.provider))
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
        setSession((old) => {
          if (!old?.turns.length || !value.turns.length) return value
          const earlier = old.turns.filter((turn) => turn.id < value.turns[0].id)
          return { ...value, turns: [...earlier, ...value.turns], hasOlder: earlier.length ? old.hasOlder : value.hasOlder }
        })
      } catch (err) {
        if (current) setError(messageOf(err, '无法载入这本书的会话。'))
      } finally {
        refreshing = false
      }
    }
    if (asking || switchingModel) return
    void refresh()
    const timer = window.setInterval(() => void refresh(), 3000)
    return () => { current = false; window.clearInterval(timer) }
  }, [open, jobId, asking, switchingModel])

  const last = session?.turns[session.turns.length - 1]
  const lastAuthorTurn = session?.turns.filter((turn) => turn.answer.source !== 'revision-failure').pop()
  const lastFailureTurn = session?.turns.filter((turn) => turn.answer.source === 'revision-failure').pop()
  useEffect(() => {
    if (open && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight
  }, [asking, open, last?.id, tracking?.run.status])

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

  const chooseModel = async (model: RevisionSelection) => {
    if (session?.model?.provider === model.provider && session.model.model === model.model) return true
    setSwitchingModel(true)
    setError(null)
    try {
      const value = await api.saveEditorModel(jobId, model)
      setSession((old) => old && { ...old, ...value })
      return true
    } catch (err) {
      setError(messageOf(err, '切换总编模型失败。'))
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
    setError(null)
    setInput('')
    try {
      const { turn } = await api.askEditor(jobId, question, requestId)
      setSession((old) => old && {
        ...old, replying: false, turns: [...old.turns.filter((item) => item.id !== turn.id), turn],
      })
      retry.current = null
    } catch (err) {
      setInput(question)
      setError(messageOf(err, '总编暂时没有回复，请再试一次。'))
    } finally {
      setAsking('')
    }
  }

  const confirm = async (id: number, plan: EditorPlan, from: number | null) => {
    if (pending || receiving || plan.dispatched || plan.expiresAt * 1000 <= Date.now()) return
    setPending(true)
    setError(null)
    try {
      const { revision } = await api.dispatchEditorPlan(jobId, plan.token)
      setTracking({ id: revision.id, from, run: revision })
      setSession((old) => old && { ...old, turns: old.turns.map((turn) => turn.id === id
        ? { ...turn, answer: { ...turn.answer, plan: { ...plan, dispatched: true } } } : turn) })
      window.dispatchEvent(new CustomEvent(REVISION_DISPATCHED, { detail: jobId }))
    } catch (err) {
      setError(messageOf(err, '返修单提交失败。'))
    } finally {
      setPending(false)
    }
  }

  const diagnose = async () => {
    setPending(true)
    setError(null)
    try {
      await api.diagnoseRevision(jobId)
      setSession((old) => old && { ...old, replying: true })
    } catch (err) {
      setError(messageOf(err, '无法重新诊断修订断点。'))
    } finally {
      setPending(false)
    }
  }

  if (!open) return null
  return <>
    <button type="button" className="fixed inset-x-0 bottom-0 top-16 z-40 bg-black/25 min-[1200px]:hidden" aria-label="关闭总编面板" onClick={onClose} />
    <aside id="chief-editor-panel" aria-label="总编对话" className="fixed bottom-0 right-0 top-16 z-[45] flex w-full max-w-[var(--editor-width)] flex-col border-l border-line bg-surface shadow-float"
      onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }}>
      <div className="shrink-0 border-b border-line px-4 pb-3 pt-4 sm:px-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-title font-medium text-ink"><Sparkles className="size-4 text-primary" />总编</h2>
            <p className="mt-1 truncate text-caption text-muted" title={title}>{title}</p>
          </div>
          <IconButton label="收起总编" size="sm" className="shrink-0" onClick={onClose}><X className="size-4" /></IconButton>
        </div>
      </div>

      <div ref={scroll} data-editor-scroll className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 [overflow-anchor:none] sm:px-5">
        <div className="grid gap-4">
          {!session && !error && <Thinking label="正在载入这本书的对话…" />}
          {session && <>
            {session.failureWaitingForModel && <Alert tone="warning">修订中断已记录。选择总编模型后会自动开始诊断。</Alert>}
            {session.turns.length === 0 && !asking && <div className="grid gap-3">
              <p className="text-body text-muted">看不懂报告，或者有一段想改，都可以直接问。</p>
              <div className="flex flex-wrap gap-2">{starters.map(({ icon: Icon, label, prompt }, index) =>
                <Button key={label} variant="tonal" size="sm" disabled={!canSend || pending || switchingModel || session.replying}
                  onClick={() => { if (index === 2) { setInput(prompt); composer.current?.focus() } else void ask(prompt) }}
                  icon={<Icon className="size-4" />}>{label}</Button>)}</div>
            </div>}
            {session.hasOlder && <Button variant="ghost" size="sm" disabled={pending} className="justify-self-center" onClick={() => void loadOlder()}>载入更早的对话</Button>}
            {session.turns.map(({ id, question, answer }) => <div key={id} className="grid gap-2.5">
              <article className={answer.source === 'revision-failure' ? 'rounded-lg border border-line bg-surface-2 px-3.5 py-3' : 'ml-6 rounded-lg bg-primary/10 px-3.5 py-3'}>
                <p className="mb-1 text-caption font-medium text-muted">{answer.source === 'revision-failure' ? '修订工作流 · 自动转交' : '你'}</p>
                <p className="whitespace-pre-wrap break-words text-body text-ink">{question}</p>
              </article>
              <article className="mr-2 rounded-lg bg-surface-2 px-3.5 py-3">
                <p className="mb-1 text-caption font-medium text-muted">总编</p>
                <ReplyWithEvidence turnId={id} reply={answer.reply} entries={answer.evidence} />
                {answer.recoveryStatus && <p className="mt-2 text-caption text-body">{answer.recoveryStatus}</p>}
                {id === lastFailureTurn?.id && !answer.recoveryStatus && <Button variant="tonal" size="sm" className="mt-2"
                  disabled={!canSend || pending || switchingModel || Boolean(asking) || session.replying}
                  onClick={() => void diagnose()}>重新诊断断点</Button>}
                {answer.plan && <Proposal plan={answer.plan} pending={pending}
                  enabled={id === lastAuthorTurn?.id && !asking && !receiving && !answer.plan.dispatched && answer.plan.expiresAt * 1000 > Date.now()}
                  onConfirm={(plan) => void confirm(id, plan, answer.candidateTurn)} />}
                {answer.plan?.dispatched && <p className="mt-2 text-caption text-muted">返修单已提交</p>}
              </article>
            </div>)}
            {asking && <p className="ml-6 whitespace-pre-wrap break-words rounded-lg bg-primary/10 px-3.5 py-3 text-body text-ink">{asking}</p>}
            {(asking || session.replying) && <Thinking label="总编正在查看这本书的资料…" />}
            {tracking && (receiving ? <Thinking label="返修已开始，正在修改候选稿。" /> : hasNewCandidate
              ? <Alert tone={tracking.run.status === 'ready' ? 'success' : 'warning'}>
                <div className="grid justify-items-start gap-2">
                  <span>{tracking.run.status === 'ready' ? '新版候选稿已生成。' : '新版候选稿已生成，仍有内容待核实。'}</span>
                  <Button variant="tonal" size="sm" disabled={!canSend || Boolean(asking)} onClick={() => void ask('对照上一版和当前候选稿，告诉我这次定点返修改了什么，还有哪些地方需要检查。')}>查看这次返修改动</Button>
                </div>
              </Alert>
              : <Alert tone="warning">{tracking.run.error || '返修暂未交付，请查看评估后修订的状态。'}</Alert>)}
          </>}
          {error && <Alert tone="danger" onClose={() => setError(null)}>{error}</Alert>}
        </div>
      </div>

      <div className="shrink-0 px-4 pb-4 pt-2 sm:px-5">
        <form className="grid gap-1 rounded-2xl border border-line bg-canvas px-2.5 pb-2 pt-2.5 focus-within:border-primary/50"
          onSubmit={(event) => { event.preventDefault(); void ask(input) }}>
          <textarea ref={composer} value={input} onChange={(event) => setInput(event.target.value)}
            className="max-h-32 min-h-14 resize-y bg-transparent px-1.5 text-body leading-relaxed text-ink outline-none"
            maxLength={2400} rows={2} aria-label="问总编"
            placeholder={canSend ? '例如：上一轮改好了哪些地方？' : '在下方选择模型后开始对话'} />
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
            <EditorModelMenu catalog={catalog} selected={session?.model ?? null} choose={chooseModel}
              disabled={loading || !session || switchingModel || Boolean(asking) || Boolean(session?.replying)} />
            <Button type="submit" variant="primary" size="sm" icon={<Send className="size-4" />}
              disabled={!canSend || pending || switchingModel || Boolean(asking) || Boolean(session?.replying) || !input.trim()} loading={Boolean(asking)}>发送</Button>
          </div>
        </form>
      </div>
    </aside>
  </>
}
