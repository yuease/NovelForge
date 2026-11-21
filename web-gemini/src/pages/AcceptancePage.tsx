import { useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { CheckCircle2, ChevronDown, ExternalLink, KeyRound, Play, ShieldAlert, ShieldCheck, Sparkles, TimerReset, XCircle, Zap } from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { navigate } from '../lib/router'
import { messageOf } from '../lib/format'
import type { AcceptanceTestResult } from '../lib/types'
import { useStudio } from '../state/studio'
import { Badge, Button, Card, PageHeader, Progress, Stagger, StaggerItem, Thinking } from '../components/ui'

type SuiteId = 'foreshadowing' | 'fault-injection' | 'timeout-resume' | 'empty-premise-auto-topic' | 'audit-gate' | 'memes-admission'

const TESTS: Array<{ id: SuiteId; title: string; subtitle: string; icon: ReactNode; assertions: string[] }> = [
  {
    id: 'foreshadowing', title: '长线伏笔与跨章记忆', subtitle: '20 章闭环 · 铜钥匙在 Ch2 → Ch6 → Ch11 → Ch18 的转移追踪', icon: <KeyRound />,
    assertions: [
      '第 2 章主角将铜钥匙藏于红木箱底并写入事实库',
      '第 6 章钥匙转移至顾沉书房暗格，事实库更新并保留来源章节',
      '第 11 章第三人获知新位置，知情范围同步更新',
      '第 18 章钥匙引爆转折，正文与事实库一致，总字数误差 ≤ ±10%',
    ],
  },
  {
    id: 'fault-injection', title: '故障注入与自动修复', subtitle: '错误物品位置 / 越权知情检测与重试上限保护', icon: <ShieldAlert />,
    assertions: [
      '向草稿注入「未知情角色说出隐藏物品位置」或「伤势冲突」',
      '审校器精准拦截并引用来源章节编号',
      '触发自动修复循环并二次重审通过',
      '连续失败超过上限时安全停止，已提交章节完好保留',
    ],
  },
  {
    id: 'timeout-resume', title: '超时中断与断点续跑', subtitle: '模拟 LLM 超时 / 进程中断后的检查点无损恢复', icon: <TimerReset />,
    assertions: [
      '运行中模拟第 3 章请求超时（timeout-once）',
      '任务进入可恢复状态，章节与检查点完整落盘',
      '调用 Resume 从断点续跑至完本',
      '最终章节序号 1..N 连续，无缺失、重复或覆盖',
    ],
  },
  {
    id: 'empty-premise-auto-topic', title: '空梗概自动选题', subtitle: '自动 3 选 1 锁定选题 · 榜单刷新不污染已有任务', icon: <Sparkles />,
    assertions: [
      '梗概留空发起任务，生成 3 个候选并评分优选',
      '锁定不可变 topicPackage（含 selectionReason 与 sourceRefs）',
      '刷新实时榜单不影响已锁定任务',
      '总纲与前 3 章章纲继承核心冲突与黄金三章设计',
    ],
  },
  {
    id: 'audit-gate', title: '终审四重门禁', subtitle: 'audit-fail-once 终审重试 + 完本门禁校验', icon: <ShieldCheck />,
    assertions: [
      '模拟首次全书终审失败，自动触发重试',
      '二次终审基于定稿正文证据通过，输出章节引用',
      '校验门禁：章节完整、字数 ±10%、终审通过、TXT/MD 非空',
      '四重门禁全部通过后才标记为已完成',
    ],
  },
  {
    id: 'memes-admission', title: '梗 Wiki 准入与淘汰', subtitle: '按 earliestSpreadAt 判新旧 · 第 8 天自动过期且缓存不复活', icon: <Zap />,
    assertions: [
      '今天更新但传播超 7 天的老梗，拦截为 rejected_old',
      '仅 1 位创作者且无热榜证据的新梗，标为 pending_verification',
      '近 7 天有证据的新梗入库 active，第 8 天自动转 expired',
      '同步失败走缓存时，已过期梗不会被重新启用',
    ],
  },
]

export default function AcceptancePage() {
  const { selectJob, refreshJobs } = useStudio()
  const [running, setRunning] = useState<'all' | SuiteId | null>(null)
  const [results, setResults] = useState<Record<string, AcceptanceTestResult>>({})

  const run = async (suite: 'all' | SuiteId) => {
    setRunning(suite)
    try {
      const res = await api.runAcceptanceTest(suite)
      const next: Record<string, AcceptanceTestResult> = {}
      for (const r of Array.isArray(res.results) ? res.results : []) next[r.id] = r
      setResults((p) => ({ ...p, ...next }))
    } catch (err) {
      const msg = messageOf(err, '测试执行失败')
      const ids = suite === 'all' ? TESTS.map((t) => t.id) : [suite]
      setResults((p) => ({ ...p, ...Object.fromEntries(ids.map((id) => [id, { id, passed: false, error: msg }])) }))
    } finally {
      setRunning(null)
    }
  }

  const inspect = (jobId: string) => {
    selectJob(jobId)
    void refreshJobs()
    navigate('monitor')
  }

  const done = TESTS.filter((t) => results[t.id])
  const passed = done.filter((t) => results[t.id].passed).length

  return (
    <>
      <PageHeader
        title="运行验收"
        description="内置模拟模型的自动化验收，不消耗外部 API 额度。"
        actions={
          <Button variant="gradient" loading={running === 'all'} disabled={running !== null && running !== 'all'} icon={<Play className="size-4" />} onClick={() => void run('all')}>
            {running === 'all' ? '全套验收执行中…' : '运行全部 6 项'}
          </Button>
        }
      />

      <Card variant={running === 'all' ? 'glow' : 'tinted'} padding="lg" className="mb-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="text-caption text-muted">验收结果</div>
            <div className="mt-1 font-display text-display font-normal text-ink tabular-nums">
              {passed}<span className="text-headline text-muted"> / {TESTS.length}</span>
            </div>
          </div>
          <div className="flex gap-1.5">
            {TESTS.map((t) => {
              const r = results[t.id]
              const busy = running === t.id || running === 'all'
              return (
                <motion.span
                  key={t.id}
                  title={t.title}
                  animate={busy ? { opacity: [0.4, 1, 0.4] } : { opacity: 1 }}
                  transition={busy ? { duration: 1.2, repeat: Infinity } : undefined}
                  className={cn('h-10 w-3 rounded-full', r ? (r.passed ? 'bg-g-green' : 'bg-g-red') : busy ? 'bg-g-blue' : 'bg-surface-3')}
                />
              )
            })}
          </div>
        </div>
        <Progress value={passed} max={TESTS.length} size="md" className="mt-5" animated={running !== null} />
        {running && <Thinking className="mt-4" label={running === 'all' ? '正在运行全部验收' : `正在运行 · ${TESTS.find((t) => t.id === running)?.title}`} />}
      </Card>

      <Stagger className="grid gap-4 lg:grid-cols-2">
        {TESTS.map((t, i) => (
          <StaggerItem key={t.id}>
            <SuiteCard
              index={i + 1}
              test={t}
              result={results[t.id]}
              busy={running === t.id || running === 'all'}
              disabled={running !== null}
              onRun={() => void run(t.id)}
              onInspect={inspect}
            />
          </StaggerItem>
        ))}
      </Stagger>
    </>
  )
}

function SuiteCard({
  index, test, result, busy, disabled, onRun, onInspect,
}: { index: number; test: (typeof TESTS)[number]; result?: AcceptanceTestResult; busy: boolean; disabled: boolean; onRun: () => void; onInspect: (id: string) => void }) {
  const [showDetails, setShowDetails] = useState(false)
  const jobId = result?.jobId ?? (result?.details && typeof result.details === 'object' && 'jobId' in result.details ? String((result.details as Record<string, unknown>).jobId) : null)

  return (
    <Card variant={busy ? 'glow' : 'default'} padding="lg" className="h-full">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className={cn(
            'grid size-10 shrink-0 place-items-center rounded-md [&_svg]:size-5 transition-colors',
            result ? (result.passed ? 'bg-success-soft text-success' : 'bg-danger-soft text-danger') : 'bg-primary-soft text-primary',
          )}>
            {test.icon}
          </span>
          <div className="min-w-0">
            <div className="text-caption text-muted">验收 {index}</div>
            <h3 className="text-title font-medium text-ink">{test.title}</h3>
            <p className="mt-0.5 text-caption text-muted">{test.subtitle}</p>
          </div>
        </div>
        <AnimatePresence mode="wait">
          {result && (
            <motion.span key={String(result.passed)} initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: 'spring', stiffness: 500, damping: 22 }}>
              {result.passed ? <CheckCircle2 className="size-6 text-success" /> : <XCircle className="size-6 text-danger" />}
            </motion.span>
          )}
        </AnimatePresence>
      </div>

      <ul className="mt-4 grid gap-1.5">
        {test.assertions.map((a, i) => (
          <li key={i} className="flex gap-2.5 text-body text-body">
            <span className={cn('mt-[9px] size-1.5 shrink-0 rounded-full', result?.passed ? 'bg-g-green' : 'bg-line-strong')} />
            <span>{a}</span>
          </li>
        ))}
      </ul>

      <AnimatePresence>
        {result && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden">
            <div className={cn('mt-4 rounded-md px-4 py-3', result.passed ? 'bg-success-soft' : 'bg-danger-soft')}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className={cn('text-body font-medium', result.passed ? 'text-success' : 'text-danger')}>
                  {result.passed ? '验收通过' : '验收未通过'}{result.name ? ` · ${result.name}` : ''}
                </span>
                {result.durationMs !== undefined && <Badge tone="neutral">{(result.durationMs / 1000).toFixed(1)} 秒</Badge>}
              </div>
              {result.summary && <p className="mt-1 text-body text-ink">{result.summary}</p>}
              {result.error && <p className="mt-1 break-words text-body text-danger">{result.error}</p>}
              {result.details !== undefined && (
                <>
                  <button type="button" onClick={() => setShowDetails(!showDetails)} className="mt-2 inline-flex items-center gap-1 text-caption font-medium text-primary hover:underline">
                    <ChevronDown className={cn('size-3.5 transition-transform', showDetails && 'rotate-180')} />详细数据
                  </button>
                  {showDetails && <pre className="mt-2 max-h-56 overflow-auto rounded-sm bg-surface p-3 font-mono text-caption text-body">{JSON.stringify(result.details, null, 2)}</pre>}
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="mt-5 flex items-center justify-end gap-2">
        {jobId && <Button size="sm" variant="ghost" iconRight={<ExternalLink className="size-3.5" />} onClick={() => onInspect(jobId)}>查看产物</Button>}
        <Button size="sm" variant={result ? 'secondary' : 'tonal'} loading={busy} disabled={disabled && !busy} icon={<Play className="size-3.5" />} onClick={onRun}>
          {busy ? '执行中' : result ? '重新运行' : '运行'}
        </Button>
      </div>
    </Card>
  )
}
