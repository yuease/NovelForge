import { useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import {
  AlertTriangle, Archive, ArrowDownRight, ArrowRight, BookOpenCheck, Check, CircleAlert,
  ClipboardList, Database, Feather, Flag, Layers, Lightbulb, ListTree, Play, ScanLine, ShieldCheck,
  Sparkles, TimerReset, Wrench,
} from 'lucide-react'
import { cn } from '../../lib/cn'
import { EASE_EXIT, EASE_OUT, SPRING_TACTILE, boundedDelay } from '../../lib/motion'
import type { ArcPlan, ArcSummary, VolumeSummary } from '../../lib/types'
import { Badge, Card, Progress } from '../../components/ui'

/* LangGraph workflow visualization. Contract: SSE `/api/jobs/:id/workflow/stream`, event `workflow`, payload WorkflowFrame. */

const W = 838
const H = 368

const FLOW_NODES = [
  { id: 'prepare', title: '准备任务', icon: Play, x: 104, y: 46 },
  { id: 'topic', title: '锁定选题', icon: Lightbulb, x: 314, y: 46 },
  { id: 'plan', title: '全书规划', icon: ClipboardList, x: 524, y: 46 },
  { id: 'outlines', title: '分章细纲', icon: ListTree, x: 734, y: 46 },
  { id: 'archive', title: '单元归档', icon: Archive, x: 524, y: 136, branch: true },
  { id: 'chapter', title: '章节撰写', icon: Feather, x: 734, y: 226 },
  { id: 'verify', title: '一致性审校', icon: ShieldCheck, x: 524, y: 226 },
  { id: 'commit', title: '章节入库', icon: Database, x: 314, y: 226 },
  { id: 'calibrate', title: '篇幅校准', icon: ScanLine, x: 104, y: 226 },
  { id: 'audit', title: '全书终审', icon: BookOpenCheck, x: 104, y: 320 },
  { id: 'finish', title: '收尾评估', icon: Flag, x: 314, y: 320 },
  { id: 'repair', title: '自动修复', icon: Wrench, x: 524, y: 320, branch: true },
  { id: 'reject', title: '停止待处理', icon: CircleAlert, x: 734, y: 320, branch: true },
] as const

type GraphNode = (typeof FLOW_NODES)[number]['id'] | 'retry'

interface FlowEdge {
  from: GraphNode
  to: GraphNode
  d: string
  branch?: boolean
  /** Only render when traversed or actively followed (keeps legacy/resume shortcuts visually clean). */
  dynamic?: boolean
  label?: { text: string; x: number; y: number }
}

const EDGES: FlowEdge[] = [
  /* Row 1: Setup & rolling outlines */
  { from: 'prepare', to: 'topic', d: 'M190 46 H227' },
  { from: 'topic', to: 'plan', d: 'M400 46 H437' },
  { from: 'plan', to: 'outlines', d: 'M610 46 H647' },
  { from: 'outlines', to: 'chapter', d: 'M750 71 V200' },

  /* Row 3: Chapter execution & verification */
  { from: 'chapter', to: 'verify', d: 'M648 226 H611' },
  { from: 'verify', to: 'commit', d: 'M438 226 H401' },
  { from: 'commit', to: 'calibrate', d: 'M228 226 H191' },

  /* Row 4: Finalization */
  { from: 'calibrate', to: 'audit', d: 'M104 251 V294' },
  { from: 'audit', to: 'finish', d: 'M190 320 H227' },

  /* Inner chapter loop */
  {
    from: 'commit',
    to: 'chapter',
    d: 'M336 201 V189 Q336 181 344 181 H710 Q718 181 718 189 V200',
    branch: true,
    label: { text: '下一章', x: 404, y: 175 },
  },

  /* Rolling arc archive -> next arc outlines loop */
  {
    from: 'commit',
    to: 'archive',
    d: 'M304 201 V144 Q304 136 312 136 H437',
    branch: true,
    label: { text: '单元归档', x: 372, y: 128 },
  },
  {
    from: 'archive',
    to: 'outlines',
    d: 'M610 136 H688 Q696 136 696 128 V72',
    branch: true,
    label: { text: '滚动下单元', x: 652, y: 128 },
  },

  /* Repair & reject branches */
  { from: 'verify', to: 'repair', d: 'M514 251 V294', branch: true },
  { from: 'repair', to: 'verify', d: 'M534 295 V252', branch: true },
  { from: 'verify', to: 'reject', d: 'M582 251 V265 Q582 273 590 273 H726 Q734 273 734 281 V294', branch: true },

  /* Dynamic resume / legacy volume-archive edges */
  { from: 'plan', to: 'archive', d: 'M524 71 V110', branch: true, dynamic: true },
  { from: 'outlines', to: 'archive', d: 'M676 71 V118 Q676 126 668 126 H611', branch: true, dynamic: true },
  { from: 'archive', to: 'chapter', d: 'M610 144 H696 Q704 144 704 152 V200', branch: true, dynamic: true },
  { from: 'archive', to: 'calibrate', d: 'M438 144 H112 Q104 144 104 152 V200', branch: true, dynamic: true },
  { from: 'commit', to: 'outlines', d: 'M320 201 V96 Q320 88 328 88 H668 Q676 88 676 80 V72', branch: true, dynamic: true },
]

export interface WorkflowFrame {
  jobId: string
  epoch: string
  revision: number
  runId: string | null
  runActive: boolean
  activeNode: GraphNode | null
  activeChapter: number | null
  activeSince: string | null
  lastNode: GraphNode | null
  lastChapter: number | null
  errorNode: GraphNode | null
  jobStatus: string | null
  nodeVisits: Partial<Record<GraphNode, number>>
  edgeVisits: Record<string, number>
}

export function useWorkflowMonitor(jobId: string | undefined) {
  const [connection, setConnection] = useState<{ jobId: string; state: 'live' | 'waiting' } | null>(null)
  const [frame, setFrame] = useState<WorkflowFrame | null>(null)

  useEffect(() => {
    if (!jobId) return
    const stream = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/workflow/stream`)
    stream.onopen = () => setConnection({ jobId, state: 'waiting' })
    stream.onerror = () => setConnection({ jobId, state: 'waiting' })
    const update = (event: MessageEvent<string>) => {
      try {
        const incoming = JSON.parse(event.data) as WorkflowFrame
        if (incoming.jobId !== jobId || typeof incoming.epoch !== 'string' || !Number.isFinite(incoming.revision) || !incoming.nodeVisits || !incoming.edgeVisits) return
        setFrame((prev) => (prev?.jobId === jobId && prev.epoch === incoming.epoch && prev.revision > incoming.revision ? prev : incoming))
        setConnection({ jobId, state: 'live' })
      } catch { /* keep last valid frame */ }
    }
    stream.addEventListener('workflow', update)
    return () => { stream.removeEventListener('workflow', update); stream.close() }
  }, [jobId])

  return {
    frame: frame?.jobId === jobId ? frame : null,
    live: connection?.jobId === jobId && connection?.state === 'live',
  }
}

const nodeLabel = (id: GraphNode | null | undefined) => (id === 'retry' ? '等待重试' : FLOW_NODES.find((n) => n.id === id)?.title)

export interface WorkflowGraphProps {
  frame: WorkflowFrame | null
  live: boolean
  jobStatus: string
  nextChapter?: number
  totalChapters?: number
  completedChapters?: number
  arcPlans?: ArcPlan[]
  arcSummaries?: ArcSummary[]
  volumeSummaries?: VolumeSummary[]
}

export function WorkflowGraph({
  frame,
  live,
  jobStatus,
  nextChapter,
  totalChapters = 0,
  completedChapters = 0,
  arcPlans,
  arcSummaries,
  volumeSummaries,
}: WorkflowGraphProps) {
  const pane = useRef<HTMLDivElement>(null)
  const reducedMotion = useReducedMotion()
  const current = live && frame?.runActive ? frame.activeNode : null

  useEffect(() => {
    const el = pane.current
    if (!el || !current || current === 'retry') return
    const node = el.querySelector<HTMLElement>(`[data-node="${current}"]`)
    if (!node || el.scrollWidth <= el.clientWidth) return
    el.scrollTo({ left: node.offsetLeft - el.clientWidth / 2, behavior: 'smooth' })
  }, [current])

  const failed = jobStatus === 'failed' || jobStatus === 'interrupted' || jobStatus === 'error'
  const currentName = nodeLabel(current)
  const lastName = nodeLabel(frame?.lastNode)
  const chapter = current ? frame?.activeChapter : undefined
  const activeHint = current === 'outlines' && typeof chapter === 'number' && chapter > 1
    ? `正在规划下阶段细纲 · 第 ${chapter} 章起`
    : currentName
      ? `正在执行 · ${currentName}${typeof chapter === 'number' ? ` · 第 ${chapter} 章` : ''}`
      : null
  const status = !live
    ? '正在连接工作流'
    : activeHint
      ?? (jobStatus === 'paused' ? '已暂停，检查点已保存'
        : jobStatus === 'completed' ? '本次任务已完成'
          : failed ? '工作流已停止'
            : frame?.runActive || jobStatus === 'running' ? '正在调度下一节点' : '等待启动')
  const retryVisits = frame?.nodeVisits.retry ?? 0
  const skipped = frame?.edgeVisits['outlines:calibrate'] ?? 0

  const sortedArcs = useMemo(
    () => (Array.isArray(arcPlans) ? [...arcPlans].sort((a, b) => a.startChapter - b.startChapter) : []),
    [arcPlans],
  )
  const summaryByArc = useMemo(() => {
    const map = new Map<number, ArcSummary>()
    for (const row of arcSummaries ?? []) {
      if (typeof row.arc === 'number') map.set(row.arc, row)
    }
    return map
  }, [arcSummaries])

  const rollingLoops = frame?.edgeVisits['archive:outlines'] ?? 0

  return (
    <Card padding="none" as="section" aria-label="创作流水线" className="min-w-0 overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 pt-5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-title font-medium text-ink">创作流水线</h3>
            {sortedArcs.length > 0 && (
              <Badge tone="primary" icon={<Sparkles />}>
                滚动单元 · {sortedArcs.length} 段
              </Badge>
            )}
          </div>
          <p className="mt-0.5 flex items-center gap-2 text-body text-muted" role="status">
            <span className={cn('size-2 shrink-0 rounded-full', current ? 'animate-breathe bg-g-blue' : jobStatus === 'completed' ? 'bg-g-green' : failed ? 'bg-g-red' : 'bg-line-strong')} />
            <AnimatePresence mode="wait" initial={false}>
              <motion.span
                key={status}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0, transition: { duration: 0.18, ease: EASE_OUT } }}
                exit={{ opacity: 0, y: -4, transition: { duration: 0.1, ease: EASE_EXIT } }}
                className={cn('truncate', current && 'font-medium text-ink')}
              >
                {status}
              </motion.span>
            </AnimatePresence>
          </p>
        </div>
        <div className="flex items-center gap-2">
          {totalChapters > 0 && (
            <Badge tone={jobStatus === 'completed' ? 'success' : 'neutral'} className="tabular-nums">
              {completedChapters} / {totalChapters} 章
            </Badge>
          )}
          <Badge tone={live ? 'success' : 'neutral'} dot live={live}>{live ? '实时' : '连接中'}</Badge>
        </div>
      </div>

      <div ref={pane} tabIndex={0} aria-label="工作流图，可横向滚动" className="overflow-x-auto px-5 pb-2 pt-4">
        <div className="relative mx-auto min-w-[700px]" style={{ aspectRatio: `${W} / ${H}` }}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="absolute inset-0 size-full overflow-visible" aria-hidden>
            <defs>
              <marker id="wf-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M0 0 L8 4 L0 8 z" fill="context-stroke" />
              </marker>
              <linearGradient id="wf-flow" x1="0" x2="1">
                <stop offset="0" stopColor="#4285f4" /><stop offset=".5" stopColor="#9b72cb" /><stop offset="1" stopColor="#ea4335" />
              </linearGradient>
              <filter id="wf-glow" x="-40%" y="-40%" width="180%" height="180%">
                <feGaussianBlur stdDeviation="2.2" result="blur" />
                <feMerge>
                  <feMergeNode in="blur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            </defs>
            {EDGES.map((e) => {
              const key = `${e.from}:${e.to}`
              const visits = frame?.edgeVisits[key] ?? 0
              const travelled = visits > 0
              const following = current === e.to && frame?.lastNode === e.from
              if (e.dynamic && !travelled && !following) return null
              return (
                <g key={key}>
                  <path
                    d={e.d}
                    fill="none"
                    strokeWidth={following ? 2.4 : travelled ? 1.8 : 1.4}
                    strokeDasharray={e.branch ? '4 4' : undefined}
                    markerEnd="url(#wf-arrow)"
                    className={cn(
                      'transition-[stroke,stroke-width,opacity] duration-500',
                      following ? 'stroke-g-blue' : travelled ? 'stroke-primary/65' : 'stroke-line-strong/85',
                    )}
                  />
                  {e.label && (
                    <text
                      x={e.label.x}
                      y={e.label.y}
                      textAnchor="middle"
                      className={cn(
                        'select-none text-[10px] font-medium transition-colors duration-300',
                        following ? 'fill-primary' : travelled ? 'fill-body' : 'fill-muted',
                      )}
                    >
                      {e.label.text}{visits > 1 ? ` ×${visits}` : ''}
                    </text>
                  )}
                  {following && (
                    <>
                      <motion.path
                        d={e.d}
                        fill="none"
                        stroke="url(#wf-flow)"
                        strokeWidth={3}
                        strokeLinecap="round"
                        strokeDasharray="6 14"
                        animate={{ strokeDashoffset: [20, 0] }}
                        transition={{ duration: 0.55, repeat: Infinity, ease: 'linear' }}
                      />
                      <circle r="3.2" fill="url(#wf-flow)" filter="url(#wf-glow)">
                        <animateMotion dur="1.15s" repeatCount="indefinite" path={e.d} />
                      </circle>
                    </>
                  )}
                </g>
              )
            })}
          </svg>
          <ol>
            {FLOW_NODES.map((n) => {
              const visits = frame?.nodeVisits[n.id] ?? 0
              const active = n.id === current
              const isBranch = 'branch' in n
              const error = !current && failed && (frame?.errorNode === n.id || (n.id === 'reject' && frame?.lastNode === n.id))
              const done = !active && !error && visits > 0
              const label = active
                ? (typeof chapter === 'number' ? `第 ${chapter} 章 · 执行中` : '执行中')
                : error ? '中断'
                  : visits ? `经过 ${visits} 次`
                    : isBranch ? '条件分支' : '待执行'
              const Icon = n.icon
              return (
                <li
                  key={n.id}
                  data-node={n.id}
                  aria-current={active ? 'step' : undefined}
                  className="absolute -translate-x-1/2 -translate-y-1/2"
                  style={{ left: `${(n.x / W) * 100}%`, top: `${(n.y / H) * 100}%` }}
                >
                  {active && (
                    <motion.span
                      aria-hidden
                      className="pointer-events-none absolute -inset-1.5 rounded-full bg-[image:var(--nf-gradient-soft)]"
                      animate={reducedMotion ? { opacity: 0.65 } : { opacity: [0.35, 0.85, 0.35], scale: [0.98, 1.03, 0.98] }}
                      transition={reducedMotion ? { duration: 0 } : { duration: 2.2, repeat: Infinity, ease: 'easeInOut' }}
                    />
                  )}
                  <motion.div
                    animate={active ? { scale: 1.035 } : { scale: 1 }}
                    transition={SPRING_TACTILE}
                    className={cn(
                      'relative flex w-[168px] items-center gap-2.5 rounded-full border bg-surface py-1.5 pl-1.5 pr-3 transition-[border-color,box-shadow,background-color] duration-200',
                      active && 'nf-glow-border',
                      !active && error && 'border-danger/50 bg-danger-soft',
                      done && 'border-primary/35 shadow-card',
                      !active && !error && !visits && (isBranch ? 'border-dashed border-line-strong' : 'border-line'),
                    )}
                  >
                    <span
                      className={cn(
                        'grid size-8 shrink-0 place-items-center rounded-full transition-colors duration-200',
                        active ? 'nf-gradient-bg is-animated text-white' : error ? 'bg-danger text-white' : visits ? 'bg-primary-soft text-primary' : 'bg-surface-2 text-muted',
                      )}
                    >
                      <Icon className="size-4" aria-hidden />
                    </span>
                    <span className="min-w-0 leading-tight">
                      <span className={cn('block truncate text-caption font-medium', active || visits ? 'text-ink' : 'text-body')}>{n.title}</span>
                      <motion.span
                        key={`${n.id}-${label}`}
                        initial={{ opacity: 0.45, y: 2 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ duration: 0.16, ease: EASE_OUT }}
                        className={cn('block truncate text-[11px] tabular-nums', active ? 'font-medium text-primary' : error ? 'text-danger' : 'text-muted')}
                      >
                        {label}
                      </motion.span>
                    </span>
                  </motion.div>
                </li>
              )
            })}
          </ol>
        </div>
      </div>

      <ArcProgressStrip
        totalChapters={totalChapters}
        completedChapters={completedChapters}
        activeNode={current}
        activeChapter={chapter ?? undefined}
        jobStatus={jobStatus}
        arcs={sortedArcs}
        summaryByArc={summaryByArc}
        volumeSummaries={volumeSummaries}
      />

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-5 py-3 text-caption text-muted">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          {nextChapter && jobStatus !== 'completed' && <span>续跑检查点 · 第 {nextChapter} 章</span>}
          {lastName && <span>最近节点 · {lastName}{frame?.lastChapter ? `（第 ${frame.lastChapter} 章）` : ''}</span>}
          {rollingLoops > 0 && <span className="inline-flex items-center gap-1 text-primary"><Sparkles className="size-3" />已滚动规划 {rollingLoops} 轮新单元</span>}
          {skipped > 0 && <span className="inline-flex items-center gap-1"><ArrowRight className="size-3" />章节齐全，细纲直达校准</span>}
          <span className="inline-flex items-center gap-1"><ArrowDownRight className="size-3" />虚线表示自动循环、归档或修复路径</span>
        </div>
        <div
          data-node="retry"
          className={cn(
            'inline-flex items-center gap-2 rounded-full px-3 py-1 transition-colors',
            current === 'retry' ? 'nf-glow-border bg-warning-soft text-warning' : 'bg-surface-2',
          )}
        >
          <TimerReset className="size-3.5" aria-hidden />
          <span className="font-medium text-body">自动重试</span>
          <span>{current === 'retry' ? '冷却等待中' : retryVisits ? `已重试 ${retryVisits} 次` : '异常冷却后自动续跑'}</span>
        </div>
      </div>
    </Card>
  )
}

function ArcProgressStrip({
  totalChapters,
  completedChapters,
  activeNode,
  activeChapter,
  jobStatus,
  arcs,
  summaryByArc,
  volumeSummaries,
}: {
  totalChapters: number
  completedChapters: number
  activeNode: GraphNode | null
  activeChapter?: number
  jobStatus: string
  arcs: ArcPlan[]
  summaryByArc: Map<number, ArcSummary>
  volumeSummaries?: VolumeSummary[]
}) {
  const reducedMotion = useReducedMotion()
  if (totalChapters <= 0) return null

  /* Rolling-arc segmented timeline */
  if (arcs.length > 0) {
    const plannedEnd = Math.max(...arcs.map((a) => a.endChapter), 0)
    const remainingUnplanned = Math.max(0, totalChapters - plannedEnd)
    const archivedCount = arcs.filter((a) => summaryByArc.has(a.arcIndex)).length
    const rollingNext = activeNode === 'outlines' && typeof activeChapter === 'number' && activeChapter > plannedEnd

    return (
      <div className="border-t border-line/80 bg-surface-2/40 px-5 py-3.5">
        <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2 text-caption">
          <span className="inline-flex items-center gap-1.5 font-medium text-body">
            <Layers className="size-3.5 text-primary" />
            单元滚动进度
          </span>
          <span className="tabular-nums text-muted">
            已规划 {Math.min(plannedEnd, totalChapters)} / {totalChapters} 章 · 已归档 {archivedCount} 个单元
          </span>
        </div>
        <div className="flex max-h-[260px] flex-wrap gap-2 overflow-y-auto">
          {arcs.map((arc, idx) => {
            const span = Math.max(1, arc.endChapter - arc.startChapter + 1)
            const doneInArc = Math.max(0, Math.min(span, completedChapters - arc.startChapter + 1))
            const fillRatio = doneInArc / span
            const summary = summaryByArc.get(arc.arcIndex)
            const driftCount = summary?.driftChecks?.length ?? 0
            const isWritingHere =
              typeof activeChapter === 'number'
              && activeChapter >= arc.startChapter
              && activeChapter <= arc.endChapter
              && activeNode !== null
              && ['chapter', 'verify', 'repair', 'commit'].includes(activeNode)
            const isArchivingHere = activeNode === 'archive' && activeChapter === arc.endChapter
            const isLiveHere = isWritingHere || isArchivingHere
            const complete = doneInArc >= span

            const tooltip = [
              `第 ${arc.arcIndex} 单元 · 第 ${arc.startChapter}–${arc.endChapter} 章《${arc.arcTitle}》`,
              arc.arcGoal ? `目标：${arc.arcGoal}` : '',
              summary ? `已归档 · 已演桥段 ${summary.usedBeats?.length ?? 0} 条${driftCount ? ` · 快检提醒 ${driftCount} 条` : ''}` : '',
            ].filter(Boolean).join('\n')

            return (
              <motion.div
                key={arc.arcIndex}
                initial={{ opacity: 0, y: 5 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.2, delay: boundedDelay(idx, 0.018, 0.14), ease: EASE_OUT }}
                title={tooltip}
                style={{ flex: `${span} 1 150px` }}
                className={cn(
                  'min-w-0 max-w-full overflow-hidden rounded-md border bg-surface px-3 py-2 transition-colors duration-200',
                  isLiveHere ? 'nf-glow-border' : summary ? 'border-success/35' : complete ? 'border-primary/35' : 'border-line',
                )}
              >
                <div className="flex items-center justify-between gap-1.5 text-[11px]">
                  <span className="font-medium tabular-nums text-ink">
                    U{arc.arcIndex} · {arc.startChapter}–{arc.endChapter}章
                  </span>
                  <span className="inline-flex shrink-0 items-center gap-1">
                    {driftCount > 0 && (
                      <span className="inline-flex items-center gap-0.5 text-warning" title={`单元快检发现 ${driftCount} 条待衔接项`}>
                        <AlertTriangle className="size-3" />{driftCount}
                      </span>
                    )}
                    {isArchivingHere ? (
                      <span className="font-medium text-primary">归档中</span>
                    ) : isWritingHere ? (
                      <span className="font-medium text-primary">第{activeChapter}章</span>
                    ) : summary ? (
                      <span className="inline-flex items-center gap-0.5 text-success"><Check className="size-3" />已归档</span>
                    ) : complete ? (
                      <span className="text-primary">已完稿</span>
                    ) : (
                      <span className="tabular-nums text-muted">{doneInArc}/{span}</span>
                    )}
                  </span>
                </div>
                <div className="mt-0.5 truncate text-caption text-muted">{arc.arcTitle}</div>
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface-3">
                  <motion.div
                    className={cn(
                      'h-full w-full origin-left rounded-full',
                      isLiveHere ? 'nf-gradient-bg is-animated' : summary ? 'bg-g-green' : 'bg-g-blue',
                    )}
                    initial={reducedMotion ? false : { scaleX: 0 }}
                    animate={{ scaleX: fillRatio }}
                    transition={reducedMotion ? { duration: 0 } : { duration: 0.45, ease: EASE_OUT }}
                  />
                </div>
              </motion.div>
            )
          })}

          {remainingUnplanned > 0 && (
            <div
              style={{ flex: `${Math.min(remainingUnplanned, 8)} 1 130px` }}
              className={cn(
                'flex min-w-0 max-w-full flex-col justify-center rounded-md border border-dashed px-3 py-2 text-[11px] transition-colors duration-200',
                rollingNext ? 'nf-glow-border bg-surface text-primary' : 'border-line-strong bg-surface/50 text-muted',
              )}
            >
              <span className="font-medium">{rollingNext ? '正在规划下单元…' : '待滚动规划'}</span>
              <span className="mt-0.5 tabular-nums opacity-85">剩余 {remainingUnplanned} 章</span>
            </div>
          )}
        </div>
      </div>
    )
  }

  /* Legacy non-rolling book progress strip */
  const volCount = volumeSummaries?.length ?? 0
  return (
    <div className="border-t border-line/80 bg-surface-2/40 px-5 py-3">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 text-caption">
        <span className="inline-flex items-center gap-1.5 font-medium text-body">
          <Layers className="size-3.5 text-primary" />
          章节推进
        </span>
        <span className="tabular-nums text-muted">
          已完成 {completedChapters} / {totalChapters} 章{volCount > 0 ? ` · 已归档 ${volCount} 卷` : ''}
        </span>
      </div>
      <Progress value={completedChapters} max={totalChapters} size="md" animated={jobStatus === 'running'} />
    </div>
  )
}
