import { useEffect, useState } from 'react'
import { BarChart3, Pause, Play, RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import { formatDateTime, messageOf } from '../lib/format'
import type { BookEvaluationReport, CandidateEvaluationRound, EvaluationRoundSummary, RevisionRun, RevisionSelection } from '../lib/types'
import { Alert, Badge, Button, Disclosure, Progress, Thinking } from '../components/ui'

const dimensions: Array<[keyof EvaluationRoundSummary['scores'], string]> = [
  ['sellingPointAndOpening', '卖点与开篇'], ['worldSettingAndCausality', '设定与因果'],
  ['characterAndChoices', '人物与选择'], ['plotAndPacing', '主线与节奏'],
  ['foreshadowingAndEnding', '伏笔与结局'], ['styleAndReadability', '文风与阅读'],
]
const levels = [['P0', '严重问题'], ['P1', '建议优先改'], ['P2', '可再打磨']] as const
const turnLabel = (turn: number) => '第 ' + (turn + 1) + ' 版候选稿'
const readiness: Record<string, string> = {
  ready_to_submit: '可准备投稿', revise_recommended: '建议再精修', major_rework: '建议先调整结构',
}

function RoundComparison({ from, fromLabel, to }: { from?: EvaluationRoundSummary | null; fromLabel: string; to: EvaluationRoundSummary }) {
  const different = from && (from.models?.flash !== to.models?.flash || from.models?.pro !== to.models?.pro || from.mode !== to.mode)
  return <div className="grid gap-2.5 text-caption text-body">
    {from && <p className="text-muted">对照 · {fromLabel}{different ? '（评估模型或模式有变化，分数仅供参考）' : ''}</p>}
    <ul className="grid grid-cols-1 gap-2 sm:grid-cols-3">{levels.map(([key, label]) =>
      <li key={key} className="flex items-center justify-between gap-2 rounded-md bg-surface-2 px-3 py-2">
        {label}<span className="font-medium tabular-nums">{from ? from.issues[key] + ' → ' : ''}{to.issues[key]}</span>
      </li>)}</ul>
    {dimensions.some(([key]) => to.scores[key] !== undefined) && <ul className="grid gap-x-5 gap-y-2 sm:grid-cols-2">
      {dimensions.map(([key, label]) => to.scores[key] !== undefined && <li key={key} className="flex justify-between gap-2">
        {label}<span className="tabular-nums">{from?.scores[key] !== undefined ? from.scores[key] + ' → ' : ''}{to.scores[key]} / 5</span>
      </li>)}
    </ul>}
  </div>
}

function RoundReport({ report }: { report: BookEvaluationReport }) {
  return <div className="grid gap-3">
    {report.readiness && readiness[report.readiness] && <div><Badge tone="primary">{readiness[report.readiness]}</Badge></div>}
    {report.overallSummary && <p className="whitespace-pre-wrap text-body text-ink">{report.overallSummary}</p>}
    {Boolean(report.rewritePlan?.length) && <Disclosure title="下一轮可以怎么改">
      <ol className="list-decimal space-y-2 pl-5 text-body text-ink">
        {report.rewritePlan?.map((suggestion, i) => <li key={i}>{suggestion}</li>)}
      </ol>
    </Disclosure>}
    {Boolean(report.aiFlavorIssues?.length) && <Disclosure title={'全部问题（' + report.aiFlavorIssues?.length + '）'}>
      <ul className="grid gap-2.5 text-caption text-body">{report.aiFlavorIssues?.map((issue, i) =>
        <li key={i} className="border-l border-line pl-3">
          <p className="font-medium">第 {issue.chapterNumber} 章 · {levels.find(([key]) => key === issue.priority)?.[1] ?? '待核对'}</p>
          {issue.impact && <p className="mt-0.5">{issue.impact}</p>}
          {issue.quote && <blockquote className="mt-0.5 text-muted">「{issue.quote}」</blockquote>}
          {issue.suggestion && <p className="mt-0.5">{issue.suggestion}</p>}
        </li>)}</ul>
    </Disclosure>}
  </div>
}

export default function CandidateRoundsPanel({ run, flash, pro, modelReady, revisionBusy, onContinue }: {
  run: RevisionRun; flash: RevisionSelection; pro: RevisionSelection; modelReady: boolean
  revisionBusy: boolean; onContinue: (turn: number) => Promise<void>
}) {
  const [history, setHistory] = useState<CandidateEvaluationRound[]>([])
  const [baseline, setBaseline] = useState<EvaluationRoundSummary | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [report, setReport] = useState<CandidateEvaluationRound | null>(null)
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const current = history.find((item) => item.turn === run.candidateTurn)
  const selectedRound = history.find((item) => item.turn === selected)
  const previous = history.filter((item) => item.summary && item.turn < (selected ?? 0)).slice(-1)[0]
  const active = history.find((item) => item.status === 'running')
  const running = Boolean(active)
  const evaluable = ['ready', 'needs_review', 'applied'].includes(run.status) && run.candidateTurn !== undefined
  const canContinue = current?.status === 'completed' && !current.isMock && !run.isMock
    && (run.status === 'ready' || run.status === 'needs_review')

  useEffect(() => {
    let live = true
    const refresh = async () => {
      try {
        const value = await api.candidateRounds(run.jobId, run.id)
        if (live) {
          setHistory(value.rounds)
          setBaseline(value.baseline)
          setSelected((turn) => turn ?? value.rounds.filter((item) => item.status === 'completed').slice(-1)[0]?.turn ?? null)
        }
      } catch (err) {
        if (live) setError(messageOf(err, '无法读取多轮评估记录。'))
      }
    }
    void refresh()
    const timer = running ? window.setInterval(() => void refresh(), 2200) : undefined
    return () => { live = false; window.clearInterval(timer) }
  }, [run.id, run.jobId, run.candidateTurn, running])

  useEffect(() => {
    if (selected === null || selectedRound?.status !== 'completed') return
    let live = true
    void api.candidateReport(run.jobId, run.id, selected).then((value) => {
      if (live) setReport(value)
    }).catch((err: unknown) => { if (live) setError(messageOf(err, '无法打开这份评估报告。')) })
    return () => { live = false }
  }, [run.id, run.jobId, selected, selectedRound?.status])

  const evaluate = async (action: 'start' | 'pause' | 'restart', turn = run.candidateTurn) => {
    if (turn === undefined || pending) return
    setPending(action)
    setError(null)
    try {
      const result = await api.changeCandidateEvaluation(run.jobId, run.id, turn,
        action === 'pause' ? { action } : { action, flash, pro })
      setHistory((items) => [...items.filter((item) => item.turn !== result.turn), result].sort((a, b) => a.turn - b.turn))
      setSelected(result.turn)
    } catch (err) {
      setError(messageOf(err, '操作失败，请重试。'))
    } finally {
      setPending(null)
    }
  }

  return <section className="mt-4 grid gap-3 border-t border-line pt-4" aria-label="多轮评估">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h3 className="flex items-center gap-2 text-body font-medium text-ink"><BarChart3 className="size-4 text-primary" />多轮评估</h3>
        <p className="mt-1 text-caption text-muted">直接评估修订后的稿件，看这一版比上一轮好在哪里。</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {active
          ? <Button variant="tonal" loading={pending === 'pause'} disabled={pending !== null}
              icon={<Pause className="size-4" />} onClick={() => void evaluate('pause', active.turn)}>暂停评估</Button>
          : current?.status !== 'completed' && <Button variant="tonal" loading={pending === 'start'}
              disabled={!modelReady || !evaluable || pending !== null || running || revisionBusy}
              icon={<Play className="size-4" />} onClick={() => void evaluate('start')}>
              {current?.status === 'error' || current?.status === 'paused' ? '继续评估这版' : '评估这版候选稿'}
            </Button>}
        {canContinue && <Button variant="primary" disabled={revisionBusy || pending !== null}
          icon={<RefreshCw className="size-4" />} onClick={() => void onContinue(current.turn)}>按最新版评估继续修</Button>}
      </div>
    </div>
    {!modelReady && !current && evaluable && <p className="text-caption text-muted">在模型设置中选择初审和精读模型后，就可以评估这版。</p>}
    {active && <div className="grid gap-2">
      <Thinking label={turnLabel(active.turn) + ' · ' + (active.progress || '正在评估…')} />
      {Boolean(active.total) && <Progress value={active.done} max={active.total} label="候选稿评估进度" />}
    </div>}
    {current?.status === 'paused' && <p className="text-caption text-muted">已暂停，保存了 {current.done} 批初审结果。</p>}
    {current?.status === 'error' && <Alert tone="warning">
      <div className="grid justify-items-start gap-2">
        <span>{current.error || '本轮评估中断，已通过的批次已经保存。'}</span>
        {current.error?.includes('从头评估') && <Button variant="tonal" disabled={!modelReady || pending !== null}
          loading={pending === 'restart'} onClick={() => void evaluate('restart')}>从头评估这版</Button>}
      </div>
    </Alert>}
    {error && <Alert tone="danger" onClose={() => setError(null)}>{error}</Alert>}
    {history.some((item) => item.status === 'completed') && <div className="flex flex-wrap gap-2" aria-label="已完成的候选稿评估">
      {history.filter((item) => item.status === 'completed').map((item) =>
        <Button key={item.turn} variant={item.turn === selected ? 'tonal' : 'ghost'}
          aria-pressed={item.turn === selected} onClick={() => setSelected(item.turn)}>{turnLabel(item.turn)}</Button>)}
    </div>}
    {selectedRound?.summary && <div className="grid gap-3 rounded-lg border border-line px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-body font-medium text-ink">{turnLabel(selectedRound.turn)} · 评估结果</p>
        {selectedRound.summary.evaluatedAt && <span className="text-caption text-muted">{formatDateTime(selectedRound.summary.evaluatedAt)}</span>}
      </div>
      <RoundComparison from={previous?.summary ?? baseline} fromLabel={previous ? turnLabel(previous.turn) : '修订前原稿'}
        to={selectedRound.summary} />
      {report?.turn === selectedRound.turn && report.report && <RoundReport report={report.report} />}
      <a className="justify-self-start text-caption text-primary hover:underline"
        href={api.revisionExportUrl(run.jobId, run.id, `candidate-${selectedRound.turn}`)} download>下载这版候选稿</a>
    </div>}
  </section>
}
