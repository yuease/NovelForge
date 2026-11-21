import { useEffect, useRef, useState } from 'react'
import { motion } from 'motion/react'
import {
  ArrowDownRight, ArrowLeft, ArrowRight, BookOpenCheck, CircleAlert, ClipboardList,
  Database, Feather, Flag, Lightbulb, ListTree, Play, ScanLine, ShieldCheck, TimerReset, Wrench,
} from 'lucide-react'
import { cn } from '../../lib/cn'
import { Badge, Card } from '../../components/ui'

/* LangGraph workflow visualization. Contract: SSE `/api/jobs/:id/workflow/stream`, event `workflow`, payload WorkflowFrame. */

const W = 826
const H = 292

const FLOW_NODES = [
  { id: 'prepare', title: '准备任务', icon: Play, x: 104, y: 46 },
  { id: 'topic', title: '锁定选题', icon: Lightbulb, x: 310, y: 46 },
  { id: 'plan', title: '全书规划', icon: ClipboardList, x: 516, y: 46 },
  { id: 'outlines', title: '分章细纲', icon: ListTree, x: 722, y: 46 },
  { id: 'chapter', title: '章节撰写', icon: Feather, x: 722, y: 146 },
  { id: 'verify', title: '一致性审校', icon: ShieldCheck, x: 516, y: 146 },
  { id: 'commit', title: '章节入库', icon: Database, x: 310, y: 146 },
  { id: 'calibrate', title: '篇幅校准', icon: ScanLine, x: 104, y: 146 },
  { id: 'audit', title: '全书终审', icon: BookOpenCheck, x: 104, y: 246 },
  { id: 'finish', title: '收尾评估', icon: Flag, x: 310, y: 246 },
  { id: 'repair', title: '自动修复', icon: Wrench, x: 516, y: 246, branch: true },
  { id: 'reject', title: '停止待处理', icon: CircleAlert, x: 722, y: 246, branch: true },
] as const

type GraphNode = (typeof FLOW_NODES)[number]['id'] | 'retry'

const EDGES: { from: GraphNode; to: GraphNode; d: string; branch?: boolean }[] = [
  { from: 'prepare', to: 'topic', d: 'M192 46 H220' },
  { from: 'topic', to: 'plan', d: 'M398 46 H426' },
  { from: 'plan', to: 'outlines', d: 'M604 46 H632' },
  { from: 'outlines', to: 'chapter', d: 'M722 75 V115' },
  { from: 'chapter', to: 'verify', d: 'M634 146 H606' },
  { from: 'verify', to: 'commit', d: 'M428 146 H400' },
  { from: 'commit', to: 'calibrate', d: 'M222 146 H194' },
  { from: 'calibrate', to: 'audit', d: 'M104 175 V215' },
  { from: 'audit', to: 'finish', d: 'M192 246 H220' },
  { from: 'commit', to: 'chapter', d: 'M310 117 V97 Q310 89 318 89 H696 Q704 89 704 97 V115', branch: true },
  { from: 'verify', to: 'repair', d: 'M506 175 V215', branch: true },
  { from: 'repair', to: 'verify', d: 'M526 217 V177', branch: true },
  { from: 'verify', to: 'reject', d: 'M574 175 V193 Q574 200 582 200 H714 Q722 200 722 209 V215', branch: true },
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

export function WorkflowGraph({ frame, live, jobStatus, nextChapter }: { frame: WorkflowFrame | null; live: boolean; jobStatus: string; nextChapter?: number }) {
  const pane = useRef<HTMLDivElement>(null)
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
  const status = !live
    ? '正在连接工作流'
    : currentName
      ? `正在执行 · ${currentName}`
      : jobStatus === 'paused' ? '已暂停，检查点已保存'
        : jobStatus === 'completed' ? '本次任务已完成'
          : failed ? '工作流已停止'
            : frame?.runActive || jobStatus === 'running' ? '正在调度下一节点' : '等待启动'
  const chapter = current ? frame?.activeChapter : undefined
  const retryVisits = frame?.nodeVisits.retry ?? 0
  const skipped = frame?.edgeVisits['outlines:calibrate'] ?? 0

  return (
    <Card padding="none" as="section" aria-label="LangGraph 工作流">
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 pt-5">
        <div>
          <h3 className="text-title font-medium text-ink">工作流</h3>
          <p className="mt-0.5 flex items-center gap-2 text-body text-muted" role="status">
            <span className={cn('size-2 rounded-full', current ? 'animate-breathe bg-g-blue' : 'bg-line-strong')} />
            <span className={cn(current && 'font-medium text-ink')}>{status}{typeof chapter === 'number' ? ` · 第 ${chapter} 章` : ''}</span>
          </p>
        </div>
        <Badge tone={live ? 'success' : 'neutral'} dot live={live}>{live ? '实时' : '连接中'}</Badge>
      </div>

      <div ref={pane} tabIndex={0} aria-label="工作流图，可横向滚动" className="overflow-x-auto px-5 pb-2 pt-4">
        <div className="relative mx-auto min-w-[760px]" style={{ aspectRatio: `${W} / ${H}` }}>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="absolute inset-0 size-full overflow-visible" aria-hidden>
            <defs>
              <marker id="wf-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M0 0 L8 4 L0 8 z" fill="context-stroke" />
              </marker>
              <linearGradient id="wf-flow" x1="0" x2="1">
                <stop offset="0" stopColor="#4285f4" /><stop offset=".5" stopColor="#9b72cb" /><stop offset="1" stopColor="#ea4335" />
              </linearGradient>
            </defs>
            {EDGES.map((e) => {
              const key = `${e.from}:${e.to}`
              const travelled = Boolean(frame?.edgeVisits[key])
              const following = current === e.to && frame?.lastNode === e.from
              return (
                <g key={key}>
                  <path
                    d={e.d}
                    fill="none"
                    strokeWidth={following ? 2.5 : 1.5}
                    strokeDasharray={e.branch ? '4 4' : undefined}
                    markerEnd="url(#wf-arrow)"
                    className={cn(
                      'transition-[stroke] duration-500',
                      following ? 'stroke-g-blue' : travelled ? 'stroke-primary/60' : 'stroke-line-strong',
                    )}
                  />
                  {following && (
                    <motion.path
                      d={e.d}
                      fill="none"
                      stroke="url(#wf-flow)"
                      strokeWidth={3}
                      strokeLinecap="round"
                      strokeDasharray="6 14"
                      animate={{ strokeDashoffset: [20, 0] }}
                      transition={{ duration: 0.6, repeat: Infinity, ease: 'linear' }}
                    />
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
              const label = active ? '执行中' : error ? '中断' : visits ? `经过 ${visits} 次` : isBranch ? '条件分支' : '待执行'
              const Icon = n.icon
              return (
                <li
                  key={n.id}
                  data-node={n.id}
                  aria-current={active ? 'step' : undefined}
                  className="absolute -translate-x-1/2 -translate-y-1/2"
                  style={{ left: `${(n.x / W) * 100}%`, top: `${(n.y / H) * 100}%` }}
                >
                  <motion.div
                    layout
                    animate={active ? { scale: 1.06 } : { scale: 1 }}
                    transition={{ type: 'spring', stiffness: 400, damping: 26 }}
                    className={cn(
                      'flex w-[168px] items-center gap-2.5 rounded-full border bg-surface py-1.5 pl-1.5 pr-3 transition-[border-color,box-shadow,background-color] duration-300',
                      active && 'nf-glow-border',
                      !active && error && 'border-danger/50 bg-danger-soft',
                      !active && !error && visits > 0 && 'border-primary/30',
                      !active && !error && !visits && (isBranch ? 'border-dashed border-line-strong' : 'border-line'),
                    )}
                  >
                    <span
                      className={cn(
                        'grid size-8 shrink-0 place-items-center rounded-full transition-colors duration-300',
                        active ? 'nf-gradient-bg is-animated text-white' : error ? 'bg-danger text-white' : visits ? 'bg-primary-soft text-primary' : 'bg-surface-2 text-muted',
                      )}
                    >
                      <Icon className="size-4" aria-hidden />
                    </span>
                    <span className="min-w-0 leading-tight">
                      <span className={cn('block truncate text-caption font-medium', active || visits ? 'text-ink' : 'text-body')}>{n.title}</span>
                      <span className={cn('block text-[11px]', active ? 'text-primary' : error ? 'text-danger' : 'text-muted')}>{label}</span>
                    </span>
                  </motion.div>
                </li>
              )
            })}
          </ol>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-5 py-3 text-caption text-muted">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          {nextChapter && jobStatus !== 'completed' && <span>续跑检查点 · 第 {nextChapter} 章</span>}
          {lastName && <span>最近节点 · {lastName}{frame?.lastChapter ? `（第 ${frame.lastChapter} 章）` : ''}</span>}
          {skipped > 0 && <span className="inline-flex items-center gap-1"><ArrowRight className="size-3" />章节齐全，细纲直达校准</span>}
          <span className="inline-flex items-center gap-1"><ArrowDownRight className="size-3" />虚线为修复、返环或终止分支</span>
        </div>
        <div
          data-node="retry"
          className={cn(
            'inline-flex items-center gap-2 rounded-full px-3 py-1',
            current === 'retry' ? 'nf-glow-border bg-warning-soft text-warning' : 'bg-surface-2',
          )}
        >
          <TimerReset className="size-3.5" aria-hidden />
          <span className="font-medium text-body">等待重试</span>
          <span>{current === 'retry' ? '执行中' : retryVisits ? `经过 ${retryVisits} 次` : '可重试异常时'}</span>
          <ArrowLeft className="size-3" aria-hidden /><span>返回选题</span>
        </div>
      </div>
    </Card>
  )
}
