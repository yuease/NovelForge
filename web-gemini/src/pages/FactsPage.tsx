import { useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { BookOpen, Clock, EyeOff, HeartPulse, MapPin, Package, ShieldCheck, Sparkles, Users, Wrench } from 'lucide-react'
import { cn } from '../lib/cn'
import { navigate } from '../lib/router'
import type { FactEntry, RepairLogEntry } from '../lib/types'
import { useStudio } from '../state/studio'
import { AnimatedNumber, Badge, Button, Card, EmptyState, PageHeader, SectionLabel, Segmented, Stagger, StaggerItem, Tag } from '../components/ui'

type FactTab = 'items' | 'secrets' | 'characters' | 'timeline' | 'foreshadowing' | 'repairs'

function text(val: unknown): string {
  if (val === null || val === undefined) return ''
  if (typeof val === 'string') return val
  if (typeof val === 'number' || typeof val === 'boolean') return String(val)
  try { return JSON.stringify(val) } catch { return String(val) }
}

function normalize(input?: Record<string, FactEntry> | FactEntry[]): Array<{ key: string; entry: FactEntry }> {
  if (!input) return []
  if (Array.isArray(input)) {
    return input.map((entry, i) => ({
      key: String(entry?.name ?? entry?.title ?? entry?.dayOrTime ?? entry?.id ?? entry?.key ?? `#${i + 1}`),
      entry: typeof entry === 'object' && entry !== null ? entry : { description: String(entry) },
    }))
  }
  return Object.entries(input).map(([key, entry]) => ({ key, entry: typeof entry === 'object' && entry !== null ? entry : { description: String(entry) } }))
}

function Citation({ entry }: { entry: FactEntry }) {
  const ch = entry.updatedChapter ?? entry.sourceChapter
  if (!ch) return null
  const first = entry.sourceChapter && entry.updatedChapter && entry.sourceChapter !== entry.updatedChapter ? entry.sourceChapter : null
  return (
    <Badge tone="primary" icon={<BookOpen />} className="shrink-0">
      第 {ch} 章{first ? ` · 初见 ${first}` : ''}
    </Badge>
  )
}

function FactCard({ title, entry, children, tone }: { title: ReactNode; entry: FactEntry; children?: ReactNode; tone?: 'danger' }) {
  return (
    <div className={cn('h-full rounded-md p-4 transition-colors', tone === 'danger' ? 'bg-danger-soft' : 'bg-surface-2 hover:bg-surface-3')}>
      <div className="flex items-start justify-between gap-2">
        <div className={cn('text-body font-medium', tone === 'danger' ? 'text-danger' : 'text-ink')}>{title}</div>
        <Citation entry={entry} />
      </div>
      {children && <div className="mt-2 grid gap-1 text-body text-body">{children}</div>}
    </div>
  )
}

const Row = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="flex gap-2 text-caption"><span className="shrink-0 text-muted">{label}</span><span className="text-ink">{children}</span></div>
)

export default function FactsPage() {
  const { facts, repairLogs, activeJob } = useStudio()
  const [tab, setTab] = useState<FactTab>('items')

  const characters = normalize(facts?.characters)
  const injuries = normalize(facts?.injuries)
  const items = normalize(facts?.items)
  const secrets = normalize(facts?.secrets)
  const timeline = normalize(facts?.timeline)
  const locations = normalize(facts?.locations)
  const foreshadowing = normalize(facts?.foreshadowing)
  const title = activeJob?.title || activeJob?.bookPlan?.title

  if (!activeJob) {
    return (
      <>
        <PageHeader title="跨章事实" description="人物、物品、秘密与伏笔的一致性追踪。" />
        <EmptyState icon={<ShieldCheck />} title="还没有选中作品" action={<Button variant="primary" onClick={() => navigate('monitor')}>选择作品</Button>} />
      </>
    )
  }

  const paidOff = foreshadowing.filter(({ entry }) => entry.status === 'paid_off').length
  const resolved = repairLogs.filter((l) => l.resolved).length

  const tabs = [
    { value: 'items' as const, label: '物品', icon: <Package />, count: items.length },
    { value: 'secrets' as const, label: '秘密', icon: <EyeOff />, count: secrets.length },
    { value: 'characters' as const, label: '人物', icon: <Users />, count: characters.length + injuries.length },
    { value: 'timeline' as const, label: '时间线', icon: <Clock />, count: timeline.length + locations.length },
    { value: 'foreshadowing' as const, label: '伏笔', icon: <Sparkles />, count: foreshadowing.length },
    { value: 'repairs' as const, label: '修复日志', icon: <Wrench />, count: repairLogs.length },
  ]

  return (
    <>
      <PageHeader
        eyebrow={title ? `《${title}》` : undefined}
        title="跨章事实"
        description="每条事实都引用来源章节；越权知情、物品瞬移或伤势冲突会触发自动修复。"
      />

      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Summary icon={<Users />} label="人物" value={characters.length} />
        <Summary icon={<Package />} label="物品" value={items.length} />
        <Summary icon={<Sparkles />} label="伏笔回收" value={paidOff} suffix={`/ ${foreshadowing.length}`} />
        <Summary icon={<Wrench />} label="修复通过" value={resolved} suffix={`/ ${repairLogs.length}`} />
      </div>

      <Segmented value={tab} onChange={setTab} options={tabs} className="mb-5" ariaLabel="事实分类" />

      <AnimatePresence mode="wait">
        <motion.div key={tab} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: 0.2 }}>
          <Card padding="lg">
            {tab === 'items' && (
              items.length ? (
                <Stagger className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {items.map(({ key, entry }) => (
                    <StaggerItem key={key}>
                      <FactCard title={entry.name || key} entry={entry}>
                        {entry.location && <Row label="位置"><span className="inline-flex items-center gap-1"><MapPin className="size-3 text-success" />{String(entry.location)}</span></Row>}
                        {Boolean(entry.holder || entry.owner) && <Row label="持有者">{text(entry.holder || entry.owner)}</Row>}
                        {entry.status && <Row label="状态">{String(entry.status)}</Row>}
                        {entry.description && <p className="mt-1 text-caption text-muted">{String(entry.description)}</p>}
                      </FactCard>
                    </StaggerItem>
                  ))}
                </Stagger>
              ) : <Empty>暂无物品位置记录</Empty>
            )}

            {tab === 'secrets' && (
              secrets.length ? (
                <Stagger className="grid gap-3 md:grid-cols-2">
                  {secrets.map(({ key, entry }) => {
                    const known = (Array.isArray(entry.holders) ? entry.holders : Array.isArray(entry.knownBy) ? entry.knownBy : []) as unknown[]
                    const excluded = (Array.isArray(entry.excludedCharacters) ? entry.excludedCharacters : []) as unknown[]
                    const body = text(entry.secret || entry.description)
                    return (
                      <StaggerItem key={key}>
                        <FactCard title={text(entry.name || key)} entry={entry}>
                          {body && <p>{body}</p>}
                          {known.length > 0 && (
                            <div className="mt-2 flex flex-wrap items-center gap-1.5">
                              <span className="text-caption text-muted">知情</span>
                              {known.map((w, i) => <Badge key={i} tone="primary">{text(w)}</Badge>)}
                            </div>
                          )}
                          {excluded.length > 0 && (
                            <div className="mt-1 flex flex-wrap items-center gap-1.5">
                              <span className="text-caption text-muted">不知情</span>
                              {excluded.map((w, i) => <Tag key={i} className="bg-surface line-through decoration-muted/50">{text(w)}</Tag>)}
                            </div>
                          )}
                        </FactCard>
                      </StaggerItem>
                    )
                  })}
                </Stagger>
              ) : <Empty>暂无秘密知情范围记录</Empty>
            )}

            {tab === 'characters' && (
              characters.length || injuries.length ? (
                <div className="grid gap-8">
                  {characters.length > 0 && (
                    <div>
                      <SectionLabel>人物状态</SectionLabel>
                      <Stagger className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                        {characters.map(({ key, entry }) => (
                          <StaggerItem key={key}>
                            <FactCard title={entry.name || key} entry={entry}>
                              <p>{entry.description || entry.status || JSON.stringify(entry)}</p>
                            </FactCard>
                          </StaggerItem>
                        ))}
                      </Stagger>
                    </div>
                  )}
                  {injuries.length > 0 && (
                    <div>
                      <SectionLabel><span className="inline-flex items-center gap-1.5 text-danger"><HeartPulse className="size-3.5" />伤势追踪</span></SectionLabel>
                      <Stagger className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                        {injuries.map(({ key, entry }) => (
                          <StaggerItem key={key}>
                            <FactCard tone="danger" title={entry.name || key} entry={entry}>
                              <p>{entry.description || entry.status || entry.severity || JSON.stringify(entry)}</p>
                            </FactCard>
                          </StaggerItem>
                        ))}
                      </Stagger>
                    </div>
                  )}
                </div>
              ) : <Empty>暂无人物与伤势记录</Empty>
            )}

            {tab === 'timeline' && (
              timeline.length || locations.length ? (
                <ol className="relative ml-2 border-l-2 border-line">
                  {[...timeline, ...locations].map(({ key, entry }, i) => (
                    <motion.li
                      key={`${key}-${i}`}
                      initial={{ opacity: 0, x: -10 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: Math.min(i * 0.04, 0.6), duration: 0.3 }}
                      className="relative pb-6 pl-6 last:pb-0"
                    >
                      <span className={cn('absolute -left-[7px] top-1.5 size-3 rounded-full ring-4 ring-surface', i < timeline.length ? 'bg-g-blue' : 'bg-g-green')} />
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div>
                          <div className="flex items-center gap-2 text-body font-medium text-ink">
                            {i >= timeline.length && <MapPin className="size-3.5 text-success" />}
                            {text(entry.dayOrTime || entry.name || key)}
                          </div>
                          <p className="mt-0.5 text-body text-body">{text(entry.event || entry.description || entry.status || entry)}</p>
                        </div>
                        <Citation entry={entry} />
                      </div>
                    </motion.li>
                  ))}
                </ol>
              ) : <Empty>暂无时间线与地点记录</Empty>
            )}

            {tab === 'foreshadowing' && (
              foreshadowing.length ? (
                <Stagger className="grid gap-3">
                  {foreshadowing.map(({ key, entry }) => {
                    const paid = entry.status === 'paid_off'
                    return (
                      <StaggerItem key={key}>
                        <div className="flex gap-4 rounded-md bg-surface-2 p-4">
                          <span className={cn('mt-0.5 grid size-8 shrink-0 place-items-center rounded-full', paid ? 'bg-success-soft text-success' : 'bg-primary-soft text-primary')}>
                            <Sparkles className="size-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-start justify-between gap-2">
                              <div className="text-body font-medium text-ink">{text(entry.clue || entry.name || key)}</div>
                              <div className="flex items-center gap-2">
                                {entry.status && <Badge tone={paid ? 'success' : 'warning'}>{paid ? `已回收 · 第 ${text(entry.payoffChapter) || '?'} 章` : text(entry.status)}</Badge>}
                                <Citation entry={entry} />
                              </div>
                            </div>
                            {Boolean(entry.details || entry.description) && <p className="mt-1 text-body text-body">{text(entry.details || entry.description)}</p>}
                          </div>
                        </div>
                      </StaggerItem>
                    )
                  })}
                </Stagger>
              ) : <Empty>暂无长线伏笔记录</Empty>
            )}

            {tab === 'repairs' && (repairLogs.length ? <RepairList logs={repairLogs} /> : <Empty>暂无审校与修复记录</Empty>)}
          </Card>
        </motion.div>
      </AnimatePresence>
    </>
  )
}

function RepairList({ logs }: { logs: RepairLogEntry[] }) {
  return (
    <Stagger className="grid gap-3">
      {logs.map((log, idx) => {
        const ts = log.ts || log.timestamp
        return (
          <StaggerItem key={log.id ?? idx}>
            <div className="rounded-md bg-surface-2 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-body font-medium text-ink">第 {log.chapterNumber ?? '?'} 章</span>
                  {log.attempt !== undefined && <span className="text-caption text-muted">第 {log.attempt} 次修复</span>}
                  <Badge tone={log.resolved ? 'success' : 'danger'} dot>{log.resolved ? '已修复并通过重审' : '待处理 / 超限'}</Badge>
                  {log.citedChapters?.length ? <span className="text-caption text-muted">引用第 {log.citedChapters.join('、')} 章</span> : null}
                </div>
                {ts && <span className="text-caption tabular-nums text-muted">{new Date(ts).toLocaleString('zh-CN', { hour12: false })}</span>}
              </div>
              {log.message && <p className="mt-2 text-body text-body">{text(log.message)}</p>}
              {Array.isArray(log.issues) && log.issues.length > 0 && (
                <ul className="mt-3 grid gap-1.5">
                  {log.issues.map((iss, i) => (
                    typeof iss === 'string' ? (
                      <li key={i} className="rounded-sm bg-surface px-3 py-2 text-caption text-body">{iss}</li>
                    ) : iss && typeof iss === 'object' ? (
                      <li key={i} className="flex items-start gap-2 rounded-sm bg-surface px-3 py-2 text-caption">
                        <Badge tone={iss.severity === 'warning' ? 'warning' : 'danger'} className="shrink-0">
                          {text(iss.type || 'issue')}{iss.sourceChapter ? ` · 第${iss.sourceChapter}章` : ''}
                        </Badge>
                        <span className="pt-0.5 text-body">{text(iss.message || iss)}</span>
                      </li>
                    ) : null
                  ))}
                </ul>
              )}
            </div>
          </StaggerItem>
        )
      })}
    </Stagger>
  )
}

function Summary({ icon, label, value, suffix }: { icon: ReactNode; label: string; value: number; suffix?: string }) {
  return (
    <Card padding="md">
      <div className="flex items-center gap-2 text-caption text-muted [&_svg]:size-3.5">{icon}{label}</div>
      <div className="mt-1.5 font-display text-headline text-ink"><AnimatedNumber value={value} />{suffix && <span className="ml-1 text-body text-muted">{suffix}</span>}</div>
    </Card>
  )
}

const Empty = ({ children }: { children: ReactNode }) => <p className="py-12 text-center text-body text-muted">{children}</p>
