import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  AlertTriangle, Archive, ArrowUpRight, Ban, BookOpen, Brain, CheckCircle2, Clock, Coins, Compass,
  EyeOff, HeartPulse, Layers, Lock, MapPin, Package, ShieldAlert, ShieldCheck, Sparkles,
  Tag as TagIcon, TrendingUp, Users, Wrench,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { FORESHADOW_STATUS_LABEL, SKELETON_CODE_LABEL, issueTypeLabel } from '../lib/format'
import type { Tone } from '../lib/format'
import { EASE_OUT, TAB_MOTION, boundedDelay } from '../lib/motion'
import { navigate } from '../lib/router'
import type {
  ArcPlan,
  ArcSummary,
  BookPlan,
  BookPlanVolume,
  CanonLocks,
  ChapterRecord,
  CharacterRelationshipChange,
  CharacterStateEntry,
  FactEntry,
  LedgerBalance,
  RepairLogEntry,
  StanceTier,
  StoryClock,
  VolumeSummary,
} from '../lib/types'
import { useStudio } from '../state/studio'
import {
  AnimatedNumber, Badge, Button, Card, EmptyState, PageHeader, SectionLabel, Segmented,
  Stagger, StaggerItem, Tag,
} from '../components/ui'

type FactTab = 'ledger' | 'items' | 'secrets' | 'characters' | 'timeline' | 'foreshadowing' | 'repairs'

const STANCE_META: Record<StanceTier, { label: string; tone: Tone }> = {
  hostile: { label: '敌对', tone: 'danger' },
  wary: { label: '戒备', tone: 'warning' },
  transactional: { label: '交易', tone: 'neutral' },
  ally: { label: '盟友', tone: 'primary' },
  intimate: { label: '亲密', tone: 'success' },
}

const DRIFT_KIND_LABEL: Record<string, string> = {
  value: '数值',
  clock: '时钟',
  canon: '口径',
  character_ooc: '人设',
}

const EXIT_PLAN_LABEL: Record<string, string> = {
  retain: '留场',
  exit: '退场',
  background: '转幕后',
}

const SEVERITY_LABEL: Record<string, string> = {
  low: '轻伤',
  mild: '轻伤',
  medium: '中度',
  moderate: '中度',
  high: '重伤',
  severe: '重伤',
  critical: '危重',
}

function FastItem({ index, children }: { index: number; children: ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 7 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, delay: boundedDelay(index), ease: EASE_OUT }}
    >
      {children}
    </motion.div>
  )
}

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

function FactCard({ title, entry, badge, children, tone }: { title: ReactNode; entry: FactEntry; badge?: ReactNode; children?: ReactNode; tone?: 'danger' | 'success' }) {
  return (
    <div
      className={cn(
        'h-full rounded-md p-4 transition-colors',
        tone === 'danger'
          ? 'bg-danger-soft'
          : tone === 'success'
            ? 'bg-success-soft/65'
            : 'bg-surface-2 hover:bg-surface-3',
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <div className={cn('text-body font-medium', tone === 'danger' ? 'text-danger' : 'text-ink')}>{title}</div>
          {badge}
        </div>
        <Citation entry={entry} />
      </div>
      {children && <div className="mt-2 grid gap-1.5 text-body text-body">{children}</div>}
    </div>
  )
}

const Row = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="flex gap-2 text-caption"><span className="shrink-0 text-muted">{label}</span><span className="min-w-0 text-ink">{children}</span></div>
)

function hasCanonLocks(locks?: CanonLocks | null): locks is CanonLocks {
  if (!locks) return false
  return Boolean(
    locks.powerScale
    || locks.currencyAndEra
    || locks.worldTaboos?.length
    || locks.keyAssets?.length
    || locks.identityAliases?.length
    || locks.boundaries?.length
    || locks.assetSets?.length,
  )
}

function volumeSpan(vol: BookPlanVolume): [number, number] | null {
  if (Array.isArray(vol.chapters) && vol.chapters.length === 2) {
    return [vol.chapters[0], vol.chapters[1]]
  }
  if (typeof vol.startChapter === 'number' && typeof vol.endChapter === 'number') {
    return [vol.startChapter, vol.endChapter]
  }
  return null
}

function hasEscalationContent(vol: BookPlanVolume): boolean {
  const e = vol.escalation
  if (!e) return false
  return Boolean(e.pressure || e.costAtRisk || e.stageTransition || e.retiredTactics?.length)
}

interface VerifiedBoundaryState {
  chapter: number
  result: string
  quote: string
}

const COMMITTED_STATUSES = new Set(['completed', 'verified', 'repaired'])

function resolveBoundaryStates(chapters: ChapterRecord[], plan: BookPlan | null): Map<string, VerifiedBoundaryState> {
  const map = new Map<string, VerifiedBoundaryState>()
  if (!plan || plan.planVersion !== 2 || !plan.canonLocks?.boundaries?.length) return map
  const ordered = [...chapters]
    .filter((ch) => typeof ch.chapterNumber === 'number' && COMMITTED_STATUSES.has(ch.status ?? 'completed'))
    .sort((a, b) => a.chapterNumber - b.chapterNumber)
  for (const ch of ordered) {
    const v = ch.verification
    if (v?.passed !== true || !Array.isArray(v.boundaryChanges)) continue
    for (const change of v.boundaryChanges) {
      if (change?.planRef && change.result) {
        map.set(change.planRef, { chapter: ch.chapterNumber, result: change.result, quote: change.quote || '' })
      }
    }
  }
  return map
}

function hasStoryClock(clock?: StoryClock | null): clock is StoryClock {
  if (!clock) return false
  return Boolean(clock.day || clock.season || clock.timeOfDay || clock.countdown)
}

function isHealedInjury(entry: FactEntry): boolean {
  const s = text(entry.status).toLowerCase()
  return s === 'healed' || s === 'recovered' || s.includes('痊愈') || s.includes('已愈') || s.includes('愈合')
}

export default function FactsPage() {
  const { facts, repairLogs, activeJob, chapters } = useStudio()
  const [selectedTab, setSelectedTab] = useState<FactTab | null>(null)

  const characters = normalize(facts?.characters)
  const injuries = normalize(facts?.injuries)
  const items = normalize(facts?.items)
  const secrets = normalize(facts?.secrets)
  const timeline = normalize(facts?.timeline)
  const locations = normalize(facts?.locations)
  const foreshadowing = normalize(facts?.foreshadowing)
  const title = activeJob?.title || activeJob?.bookPlan?.title

  const bookPlan = activeJob?.bookPlan ?? null
  const canonLocks = bookPlan?.canonLocks ?? null
  const planVolumes = useMemo(() => (Array.isArray(bookPlan?.volumes) ? bookPlan.volumes : []), [bookPlan?.volumes])
  const hasVolumeLadder = Boolean(
    bookPlan?.planVersion === 2
    && planVolumes.length > 0
    && (planVolumes.some(hasEscalationContent) || bookPlan.volumeLadderStatus === 'partial'),
  )
  const boundaryStates = useMemo(() => resolveBoundaryStates(chapters, bookPlan), [chapters, bookPlan])
  const storyClock = facts?.storyClock ?? null
  const ledgerBalances = useMemo(() => (Array.isArray(facts?.ledgerBalances) ? facts.ledgerBalances : []), [facts?.ledgerBalances])
  const arcPlans = useMemo(() => (Array.isArray(activeJob?.arcPlans) ? [...activeJob.arcPlans].sort((a, b) => a.startChapter - b.startChapter) : []), [activeJob?.arcPlans])
  const arcSummaries = useMemo(() => (Array.isArray(facts?.arcSummaries) ? [...facts.arcSummaries].sort((a, b) => a.endChapter - b.endChapter) : []), [facts?.arcSummaries])
  const volumeSummaries = useMemo(() => (Array.isArray(facts?.volumeSummaries) ? [...facts.volumeSummaries].sort((a, b) => a.endChapter - b.endChapter) : []), [facts?.volumeSummaries])
  const characterStates = useMemo(() => (facts?.characterStates && typeof facts.characterStates === 'object' ? facts.characterStates : {}), [facts?.characterStates])

  /* Merge base characters with characterStates so Phase 4 locked roles always appear */
  const mergedCharacters = useMemo(() => {
    const byName = new Map<string, { key: string; entry: FactEntry; state?: CharacterStateEntry }>()
    for (const item of characters) {
      const name = text(item.entry.name || item.key)
      byName.set(name, { ...item, state: characterStates[name] ?? characterStates[item.key] })
    }
    for (const [name, state] of Object.entries(characterStates)) {
      if (!byName.has(name)) {
        const ch = state.updatedAtChapter ?? state.stanceTierChapter ?? state.lastSeenChapter ?? state.sourceChapter
        byName.set(name, {
          key: name,
          entry: { name, updatedChapter: ch, sourceChapter: state.sourceChapter },
          state,
        })
      }
    }
    return Array.from(byName.values())
  }, [characters, characterStates])

  const hasLedgerSection =
    hasCanonLocks(canonLocks)
    || hasVolumeLadder
    || hasStoryClock(storyClock)
    || ledgerBalances.length > 0
    || arcPlans.length > 0
    || arcSummaries.length > 0
    || volumeSummaries.length > 0

  const ledgerCount =
    (hasCanonLocks(canonLocks) ? 1 : 0)
    + (hasVolumeLadder ? planVolumes.length : 0)
    + (hasStoryClock(storyClock) ? 1 : 0)
    + ledgerBalances.length
    + Math.max(arcPlans.length, arcSummaries.length)
    + volumeSummaries.length

  const tab: FactTab = selectedTab ?? (hasLedgerSection ? 'ledger' : 'items')

  if (!activeJob) {
    return (
      <>
        <PageHeader title="跨章事实" description="口径锁、随章台账、人物状态、物品、秘密与伏笔的一致性追踪。" />
        <EmptyState icon={<ShieldCheck />} title="还没有选中作品" action={<Button variant="primary" onClick={() => navigate('monitor')}>选择作品</Button>} />
      </>
    )
  }

  const paidOff = foreshadowing.filter(({ entry }) => entry.status === 'paid_off').length
  const resolved = repairLogs.filter((l) => l.resolved).length

  const tabs = [
    { value: 'ledger' as const, label: '台账', icon: <Layers />, count: ledgerCount },
    { value: 'characters' as const, label: '人物', icon: <Users />, count: mergedCharacters.length + injuries.length },
    { value: 'items' as const, label: '物品', icon: <Package />, count: items.length },
    { value: 'secrets' as const, label: '秘密', icon: <EyeOff />, count: secrets.length },
    { value: 'timeline' as const, label: '时间线', icon: <Clock />, count: timeline.length + locations.length },
    { value: 'foreshadowing' as const, label: '伏笔', icon: <Sparkles />, count: foreshadowing.length },
    { value: 'repairs' as const, label: '修复', icon: <Wrench />, count: repairLogs.length },
  ]

  return (
    <>
      <PageHeader
        eyebrow={title ? `《${title}》` : undefined}
        title="跨章事实"
        description="锁定全书硬设定、剧情时钟与人物认知；越权知情、数值冲突或伤势矛盾会自动触发修复。"
      />

      <div className={cn('mb-6 grid grid-cols-2 gap-3', hasLedgerSection ? 'md:grid-cols-3 lg:grid-cols-5' : 'md:grid-cols-4')}>
        {hasLedgerSection && (
          <Summary
            icon={<Archive />}
            label={arcPlans.length > 0 || arcSummaries.length > 0 ? '单元归档' : '核心台账'}
            value={arcPlans.length > 0 || arcSummaries.length > 0 ? arcSummaries.length : ledgerBalances.length}
            suffix={arcPlans.length > 0 ? `/ ${arcPlans.length} 段` : ledgerBalances.length > 0 ? '账户' : undefined}
          />
        )}
        <Summary icon={<Users />} label="人物" value={mergedCharacters.length} />
        <Summary icon={<Package />} label="物品" value={items.length} />
        <Summary icon={<Sparkles />} label="伏笔回收" value={paidOff} suffix={`/ ${foreshadowing.length}`} />
        <Summary icon={<Wrench />} label="修复通过" value={resolved} suffix={`/ ${repairLogs.length}`} />
      </div>

      <Segmented size="sm" value={tab} onChange={setSelectedTab} options={tabs} className="mb-5" ariaLabel="事实分类" />

      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={tab} {...TAB_MOTION}>
          <Card padding="lg">
            {tab === 'ledger' && (
              hasLedgerSection ? (
                <LedgerAndArcsView
                  bookPlan={bookPlan}
                  canonLocks={canonLocks}
                  planVolumes={planVolumes}
                  hasVolumeLadder={hasVolumeLadder}
                  boundaryStates={boundaryStates}
                  storyClock={storyClock}
                  ledgerBalances={ledgerBalances}
                  arcPlans={arcPlans}
                  arcSummaries={arcSummaries}
                  volumeSummaries={volumeSummaries}
                />
              ) : <Empty>暂无核心口径锁、随章台账或单元归档记录</Empty>
            )}

            {tab === 'items' && (
              items.length ? (
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {items.map(({ key, entry }, idx) => (
                    <FastItem key={key} index={idx}>
                      <FactCard title={entry.name || key} entry={entry}>
                        {entry.location && <Row label="位置"><span className="inline-flex items-center gap-1"><MapPin className="size-3 text-success" />{String(entry.location)}</span></Row>}
                        {Boolean(entry.holder || entry.owner) && <Row label="持有者">{text(entry.holder || entry.owner)}</Row>}
                        {entry.status && <Row label="状态">{String(entry.status)}</Row>}
                        {entry.description && <p className="mt-1 text-caption text-muted">{String(entry.description)}</p>}
                      </FactCard>
                    </FastItem>
                  ))}
                </div>
              ) : <Empty>暂无物品位置记录</Empty>
            )}

            {tab === 'secrets' && (
              secrets.length ? (
                <div className="grid gap-3 md:grid-cols-2">
                  {secrets.map(({ key, entry }, idx) => {
                    const known = (Array.isArray(entry.holders) ? entry.holders : Array.isArray(entry.knownBy) ? entry.knownBy : []) as unknown[]
                    const excluded = (Array.isArray(entry.excludedCharacters) ? entry.excludedCharacters : []) as unknown[]
                    const body = text(entry.secret || entry.description)
                    return (
                      <FastItem key={key} index={idx}>
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
                      </FastItem>
                    )
                  })}
                </div>
              ) : <Empty>暂无秘密知情范围记录</Empty>
            )}

            {tab === 'characters' && (
              mergedCharacters.length || injuries.length ? (
                <div className="grid gap-8">
                  {mergedCharacters.length > 0 && (
                    <div>
                      <SectionLabel>人物状态与认知锁</SectionLabel>
                      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                        {mergedCharacters.map(({ key, entry, state }, idx) => {
                          const tier = state?.stanceTier ? STANCE_META[state.stanceTier] : null
                          const knownFacts = state?.knownFacts ?? []
                          const knownChapters = state?.knownFactChapters ?? {}
                          const combinedLinks: Array<[string, CharacterRelationshipChange]> = Object.entries({
                            ...(state?.relationships ?? {}),
                            ...(state?.storyRelationships ?? {}),
                          })
                            .sort((a, b) => (b[1]?.updatedAtChapter ?? 0) - (a[1]?.updatedAtChapter ?? 0))
                            .slice(0, 3)
                          const desc = text(entry.description || entry.status)

                          return (
                            <FastItem key={key} index={idx}>
                              <FactCard
                                title={entry.name || key}
                                entry={entry}
                                badge={
                                  tier ? (
                                    <Badge
                                      tone={tier.tone}
                                      className="h-5 px-2 text-[11px]"
                                    >
                                      {tier.label}{state?.stanceTierChapter ? ` · 第${state.stanceTierChapter}章` : ''}
                                    </Badge>
                                  ) : undefined
                                }
                              >
                                {desc && <p className="text-body text-body">{desc}</p>}
                                {state?.currentGoal && <Row label="目标">{state.currentGoal}</Row>}
                                {state?.emotion && <Row label="情绪">{state.emotion}</Row>}
                                {!tier && state?.stance && <Row label="立场">{state.stance}</Row>}
                                {knownFacts.length > 0 && (
                                  <div className="mt-1 rounded-sm bg-surface/80 p-2.5">
                                    <div className="mb-1 flex items-center gap-1 text-[11px] font-medium text-muted">
                                      <Brain className="size-3 text-primary" />已确认认知
                                    </div>
                                    <ul className="grid gap-1 text-caption text-ink">
                                      {knownFacts.map((fact, factIdx) => (
                                        <li key={factIdx} className="flex items-start justify-between gap-2">
                                          <span className="min-w-0">· {fact}</span>
                                          {knownChapters[fact] && (
                                            <span className="shrink-0 text-[11px] tabular-nums text-muted">第{knownChapters[fact]}章</span>
                                          )}
                                        </li>
                                      ))}
                                    </ul>
                                  </div>
                                )}
                                {combinedLinks.length > 0 && (
                                  <div className="mt-1 flex flex-wrap gap-1.5">
                                    {combinedLinks.map(([other, rel]) => (
                                      <Tag key={other} className="h-auto bg-surface py-1 text-[11px]">
                                        <span className="font-medium text-ink">{other}</span>
                                        <span className="mx-1 text-muted">·</span>
                                        <span>{rel.change || '关系变化'}</span>
                                        {rel.updatedAtChapter ? <span className="ml-1 tabular-nums text-muted">（第{rel.updatedAtChapter}章）</span> : null}
                                      </Tag>
                                    ))}
                                  </div>
                                )}
                                {!desc && !state?.currentGoal && !state?.emotion && !knownFacts.length && !combinedLinks.length && (
                                  <p className="text-caption text-muted">已纳入人物状态追踪</p>
                                )}
                              </FactCard>
                            </FastItem>
                          )
                        })}
                      </div>
                    </div>
                  )}
                  {injuries.length > 0 && (
                    <div>
                      <SectionLabel><span className="inline-flex items-center gap-1.5 text-danger"><HeartPulse className="size-3.5" />伤势追踪</span></SectionLabel>
                      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                        {injuries.map(({ key, entry }, idx) => {
                          const healed = isHealedInjury(entry)
                          const bodyPart = text(entry.bodyPart)
                          const rawSeverity = text(entry.severity)
                          const severity = SEVERITY_LABEL[rawSeverity.toLowerCase()] ?? rawSeverity
                          const owner = text(entry.character || entry.name || key)
                          return (
                            <FastItem key={`${key}-${idx}`} index={idx}>
                              <FactCard
                                tone={healed ? 'success' : 'danger'}
                                title={owner}
                                entry={entry}
                                badge={
                                  <>
                                    {bodyPart && <Tag className="h-5 bg-surface/85 px-2 text-[11px]">{bodyPart}</Tag>}
                                    {healed
                                      ? <Tag className="h-5 bg-surface/85 px-2 text-[11px] font-medium text-success">已愈</Tag>
                                      : severity
                                        ? <Tag className="h-5 bg-surface/85 px-2 text-[11px] font-medium text-danger">{severity}</Tag>
                                        : null}
                                  </>
                                }
                              >
                                <p>{text(entry.description || entry.injury || entry.status || rawSeverity || entry)}</p>
                              </FactCard>
                            </FastItem>
                          )
                        })}
                      </div>
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
                      initial={{ opacity: 0, x: -8 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: boundedDelay(i, 0.02, 0.18), duration: 0.22, ease: EASE_OUT }}
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
                <div className="grid gap-3">
                  {foreshadowing.map(({ key, entry }, idx) => {
                    const paid = entry.status === 'paid_off'
                    return (
                      <StaggerItem key={key} index={idx}>
                        <div className="flex gap-4 rounded-md bg-surface-2 p-4">
                          <span className={cn('mt-0.5 grid size-8 shrink-0 place-items-center rounded-full', paid ? 'bg-success-soft text-success' : 'bg-primary-soft text-primary')}>
                            <Sparkles className="size-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-start justify-between gap-2">
                              <div className="text-body font-medium text-ink">{text(entry.clue || entry.name || key)}</div>
                              <div className="flex items-center gap-2">
                                {entry.status && (
                                  <Badge tone={paid ? 'success' : 'warning'}>
                                    {paid
                                      ? `已回收 · 第 ${text(entry.payoffChapter) || '?'} 章`
                                      : FORESHADOW_STATUS_LABEL[text(entry.status).toLowerCase()] ?? text(entry.status)}
                                  </Badge>
                                )}
                                <Citation entry={entry} />
                              </div>
                            </div>
                            {Boolean(entry.details || entry.description) && <p className="mt-1 text-body text-body">{text(entry.details || entry.description)}</p>}
                          </div>
                        </div>
                      </StaggerItem>
                    )
                  })}
                </div>
              ) : <Empty>暂无长线伏笔记录</Empty>
            )}

            {tab === 'repairs' && (repairLogs.length ? <RepairList logs={repairLogs} /> : <Empty>暂无审校与修复记录</Empty>)}
          </Card>
        </motion.div>
      </AnimatePresence>
    </>
  )
}

/* ---------------- Phase 3–4 Ledger & Arcs View ---------------- */

function LedgerAndArcsView({
  bookPlan,
  canonLocks,
  planVolumes,
  hasVolumeLadder,
  boundaryStates,
  storyClock,
  ledgerBalances,
  arcPlans,
  arcSummaries,
  volumeSummaries,
}: {
  bookPlan: BookPlan | null
  canonLocks: CanonLocks | null
  planVolumes: BookPlanVolume[]
  hasVolumeLadder: boolean
  boundaryStates: Map<string, VerifiedBoundaryState>
  storyClock: StoryClock | null
  ledgerBalances: LedgerBalance[]
  arcPlans: ArcPlan[]
  arcSummaries: ArcSummary[]
  volumeSummaries: VolumeSummary[]
}) {
  const showLocks = hasCanonLocks(canonLocks)
  const showClock = hasStoryClock(storyClock)

  /* Merge arcPlans and arcSummaries chronologically */
  const arcRecords = useMemo(() => {
    const map = new Map<number, { index: number; plan?: ArcPlan; summary?: ArcSummary }>()
    for (const plan of arcPlans) {
      map.set(plan.arcIndex, { index: plan.arcIndex, plan })
    }
    for (const summary of arcSummaries) {
      const idx = typeof summary.arc === 'number' ? summary.arc : map.size + 1
      const existing = map.get(idx)
      map.set(idx, { index: idx, plan: existing?.plan, summary })
    }
    return Array.from(map.values()).sort((a, b) => {
      const startA = a.plan?.startChapter ?? a.summary?.startChapter ?? a.index
      const startB = b.plan?.startChapter ?? b.summary?.startChapter ?? b.index
      return startA - startB
    })
  }, [arcPlans, arcSummaries])

  const identityAliases = canonLocks?.identityAliases ?? []
  const boundaries = canonLocks?.boundaries ?? []
  const assetSets = canonLocks?.assetSets ?? []

  return (
    <div className="grid gap-8">
      {/* 1. Core Canon Locks */}
      {showLocks && canonLocks && (
        <motion.section
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, ease: EASE_OUT }}
        >
          <SectionLabel
            actions={(
              <div className="flex flex-wrap items-center gap-1.5">
                {bookPlan?.degraded && <Badge tone="warning">临时降级总纲</Badge>}
                <Badge tone="primary" icon={<Lock />}>不可变硬基准</Badge>
              </div>
            )}
          >
            全书硬设定
          </SectionLabel>
          <div className="rounded-md border border-primary/15 bg-[image:var(--nf-gradient-soft)] p-5">
            {(canonLocks.powerScale || canonLocks.currencyAndEra) && (
              <div className="grid gap-4 md:grid-cols-2">
                {canonLocks.powerScale && (
                  <div className="rounded-md bg-surface/85 p-3.5 shadow-card">
                    <div className="text-caption font-medium text-muted">战力与体系标尺</div>
                    <p className="mt-1 text-body text-ink">{canonLocks.powerScale}</p>
                  </div>
                )}
                {canonLocks.currencyAndEra && (
                  <div className="rounded-md bg-surface/85 p-3.5 shadow-card">
                    <div className="text-caption font-medium text-muted">货币与时代口径</div>
                    <p className="mt-1 text-body text-ink">{canonLocks.currencyAndEra}</p>
                  </div>
                )}
              </div>
            )}
            {(canonLocks.worldTaboos?.length || canonLocks.keyAssets?.length) ? (
              <div className={cn('grid gap-3 md:grid-cols-2', (canonLocks.powerScale || canonLocks.currencyAndEra) && 'mt-4')}>
                {canonLocks.worldTaboos && canonLocks.worldTaboos.length > 0 && (
                  <div>
                    <div className="mb-1.5 flex items-center gap-1.5 text-caption font-medium text-danger">
                      <ShieldAlert className="size-3.5" />世界硬禁忌
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {canonLocks.worldTaboos.map((item, i) => (
                        <span key={i} className="rounded-sm bg-danger-soft px-2.5 py-1 text-caption text-danger">
                          {item}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {canonLocks.keyAssets && canonLocks.keyAssets.length > 0 && (
                  <div>
                    <div className="mb-1.5 flex items-center gap-1.5 text-caption font-medium text-primary">
                      <Sparkles className="size-3.5" />核心资产与金手指
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {canonLocks.keyAssets.map((item, i) => (
                        <span key={i} className="rounded-sm bg-surface/90 px-2.5 py-1 text-caption text-ink shadow-card">
                          {item}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : null}

            {/* V2 Structured Locks: Identity Aliases, Closed Asset Sets, Space/Rule Boundaries */}
            {(identityAliases.length > 0 || assetSets.length > 0) && (
              <div className="mt-4 grid gap-4 border-t border-primary/10 pt-4 md:grid-cols-2">
                {identityAliases.length > 0 && (
                  <div>
                    <div className="mb-2 flex items-center gap-1.5 text-caption font-medium text-primary">
                      <Users className="size-3.5" />固定称呼与身份别名
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {identityAliases.map((row, i) => (
                        <span
                          key={`${row.character}-${row.scope}-${i}`}
                          className="inline-flex flex-wrap items-center gap-1 rounded-sm bg-surface/90 px-2.5 py-1 text-caption text-ink shadow-card"
                        >
                          <span className="font-medium">{row.character}</span>
                          <span className="text-muted">· {row.scope}</span>
                          <span className="text-primary">「{row.alias}」</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {assetSets.length > 0 && (
                  <div>
                    <div className="mb-2 flex items-center gap-1.5 text-caption font-medium text-primary">
                      <Package className="size-3.5" />核心系列清单（全员口径）
                    </div>
                    <div className="grid gap-2">
                      {assetSets.map((set, i) => (
                        <div key={`${set.name}-${i}`} className="rounded-md bg-surface/90 p-3 shadow-card">
                          <div className="flex items-center justify-between gap-2 text-caption">
                            <span className="font-medium text-ink">{set.name}</span>
                            <span className="tabular-nums text-[11px] text-muted">共 {set.members.length} 项</span>
                          </div>
                          <div className="mt-1.5 flex flex-wrap gap-1.5">
                            {set.members.map((member, j) => (
                              <Tag key={`${member}-${j}`} className="h-auto bg-surface-2 py-0.5 text-[11px] text-ink">
                                {member}
                              </Tag>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {boundaries.length > 0 && (
              <div className="mt-4 border-t border-primary/10 pt-4">
                <div className="mb-2 flex items-center gap-1.5 text-caption font-medium text-ink">
                  <Compass className="size-3.5 text-primary" />舞台空间与规则边界
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  {boundaries.map((b, i) => {
                    const isAbsolute = b.changeWhen === null || b.changeWhen === undefined || b.changeWhen === ''
                    const activeChange = boundaryStates.get(`canonLocks.boundaries[${i}]`)
                    return (
                      <div key={`${b.subject}-${i}`} className="rounded-md bg-surface/90 p-3.5 shadow-card">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="text-body font-medium text-ink">{b.subject}</span>
                          {isAbsolute ? (
                            <Badge tone="danger" className="h-5 px-2 text-[11px]">绝对禁制</Badge>
                          ) : activeChange ? (
                            <Badge tone="success" icon={<CheckCircle2 />} className="h-5 px-2 text-[11px]">
                              第 {activeChange.chapter} 章起已变更
                            </Badge>
                          ) : (
                            <Badge tone="neutral" className="h-5 px-2 text-[11px]">可按条件变更</Badge>
                          )}
                        </div>
                        <p className="mt-1 text-caption text-body">{b.rule}</p>
                        {!isAbsolute && (
                          <p className="mt-1 text-[11px] text-muted">变更条件：{b.changeWhen}</p>
                        )}
                        {activeChange && (
                          <div
                            title={activeChange.quote ? `原文引证：「${activeChange.quote}」` : undefined}
                            className="mt-2 rounded-sm bg-success-soft/80 px-2.5 py-1.5 text-caption text-success"
                          >
                            当前生效：{activeChange.result}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        </motion.section>
      )}

      {/* 2. Story Clock & Ledger Balances */}
      {(showClock || ledgerBalances.length > 0) && (
        <div className={cn('grid gap-6', showClock && ledgerBalances.length > 0 && 'lg:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)]')}>
          {showClock && storyClock && (
            <section>
              <SectionLabel>剧情时钟</SectionLabel>
              <Stagger className="grid grid-cols-2 gap-3">
                {([
                  ['day', '当前天数', storyClock.day],
                  ['season', '季节气候', storyClock.season],
                  ['timeOfDay', '叙事时段', storyClock.timeOfDay],
                  ['countdown', '关键倒计时', storyClock.countdown],
                ] as const).filter(([, , val]) => Boolean(val)).map(([field, label, val]) => {
                  const ch = storyClock.fieldChapters?.[field]
                  return (
                    <StaggerItem key={field}>
                      <div className="h-full rounded-md bg-surface-2 p-3.5 transition-colors hover:bg-surface-3">
                        <div className="flex items-center justify-between gap-2 text-caption text-muted">
                          <span className="inline-flex items-center gap-1"><Clock className="size-3 text-primary" />{label}</span>
                          {ch ? <span className="tabular-nums text-[11px] text-primary">第{ch}章</span> : null}
                        </div>
                        <div className="mt-1.5 font-display text-title font-medium text-ink">{val}</div>
                      </div>
                    </StaggerItem>
                  )
                })}
              </Stagger>
            </section>
          )}

          {ledgerBalances.length > 0 && (
            <section>
              <SectionLabel actions={<span className="text-caption tabular-nums text-muted">核心账户 · {ledgerBalances.length} / 6</span>}>
                核心余额账本
              </SectionLabel>
              <Stagger className="grid gap-3 sm:grid-cols-2">
                {ledgerBalances.map((account) => (
                  <StaggerItem key={account.label}>
                    <div className="h-full rounded-md bg-surface-2 p-4 transition-colors hover:bg-surface-3">
                      <div className="flex items-start justify-between gap-2">
                        <span className="inline-flex items-center gap-1.5 text-caption font-medium text-muted">
                          <Coins className="size-3.5 text-warning" />
                          {account.label}
                        </span>
                        {account.updatedAtChapter ? (
                          <Badge tone="primary" className="h-5 px-2 text-[11px]">第 {account.updatedAtChapter} 章</Badge>
                        ) : null}
                      </div>
                      <div className="mt-1.5 font-display text-headline font-normal tabular-nums text-ink">{account.value}</div>
                      {account.recentChanges && account.recentChanges.length > 0 && (
                        <div className="mt-2.5 flex flex-wrap gap-1.5">
                          {account.recentChanges.map((mv, idx) => (
                            <span
                              key={`${mv.chapter}-${idx}`}
                              className="inline-flex items-center gap-1 rounded-sm bg-surface px-2 py-0.5 text-[11px] tabular-nums text-body"
                            >
                              <span className="text-muted">第{mv.chapter}章</span>
                              {mv.change && <span className="font-medium text-primary">{mv.change}</span>}
                              <span>→ {mv.value}</span>
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </StaggerItem>
                ))}
              </Stagger>
            </section>
          )}
        </div>
      )}

      {/* 2.5 Volume Escalation Ladder (V2 BookPlan) */}
      {hasVolumeLadder && (
        <section>
          <SectionLabel
            actions={(
              <div className="flex flex-wrap items-center gap-2">
                {bookPlan?.volumeLadderStatus === 'partial' && (
                  <Badge tone="warning">部分卷阶梯待补全</Badge>
                )}
                <span className="text-caption tabular-nums text-muted">共 {planVolumes.length} 卷</span>
              </div>
            )}
          >
            分卷升级阶梯
          </SectionLabel>
          <div className="grid gap-3 md:grid-cols-2">
            {planVolumes.map((vol, idx) => {
              const span = volumeSpan(vol)
              const volNo = vol.volume ?? vol.volumeNumber ?? idx + 1
              const ladder = vol.escalation
              const retired = ladder?.retiredTactics ?? []
              return (
                <StaggerItem key={volNo} index={idx}>
                  <div className="flex h-full flex-col rounded-md bg-surface-2 p-4 transition-colors hover:bg-surface-3/70">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <span className="font-display text-body font-medium text-ink">
                          第 {volNo} 卷{vol.title ? ` · ${vol.title}` : ''}
                        </span>
                      </div>
                      {span && (
                        <Badge tone="neutral" className="shrink-0 tabular-nums">
                          第 {span[0]}–{span[1]} 章
                        </Badge>
                      )}
                    </div>
                    {ladder && hasEscalationContent(vol) ? (
                      <div className="mt-3 grid gap-2 text-caption">
                        {ladder.pressure && (
                          <div className="rounded-sm bg-surface/85 px-3 py-2">
                            <div className="mb-0.5 flex items-center gap-1 text-[11px] font-medium text-primary">
                              <TrendingUp className="size-3" />新增压迫
                            </div>
                            <p className="text-ink">{ladder.pressure}</p>
                          </div>
                        )}
                        {ladder.costAtRisk && (
                          <div className="rounded-sm bg-surface/85 px-3 py-2">
                            <div className="mb-0.5 flex items-center gap-1 text-[11px] font-medium text-warning">
                              <AlertTriangle className="size-3" />真实代价
                            </div>
                            <p className="text-ink">{ladder.costAtRisk}</p>
                          </div>
                        )}
                        {ladder.stageTransition && (
                          <div className="flex items-start gap-1.5 rounded-sm bg-primary-soft/65 px-2.5 py-1.5 text-primary">
                            <ArrowUpRight className="mt-0.5 size-3.5 shrink-0" />
                            <span>跃迁条件：{ladder.stageTransition}</span>
                          </div>
                        )}
                        {retired.length > 0 && (
                          <div className="mt-0.5">
                            <div className="mb-1 flex items-center gap-1 text-[11px] font-medium text-danger">
                              <Ban className="size-3" />已退役手段（本卷禁复刻）
                            </div>
                            <div className="grid gap-1">
                              {retired.map((item, j) => (
                                <div key={`${item.tactic}-${j}`} className="rounded-sm bg-danger-soft/75 px-2.5 py-1.5">
                                  <span className="font-medium text-danger">{item.tactic}</span>
                                  {item.whyFails && (
                                    <span className="ml-1.5 text-[11px] text-body">· {item.whyFails}</span>
                                  )}
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    ) : (
                      <p className="mt-2 text-caption text-muted">{vol.summary || '本卷尚未补充升级阶梯'}</p>
                    )}
                  </div>
                </StaggerItem>
              )
            })}
          </div>
        </section>
      )}

      {/* 3. Rolling Arc Plans + Archives & Drift Checks */}
      {arcRecords.length > 0 && (
        <section>
          <SectionLabel actions={<span className="text-caption tabular-nums text-muted">共 {arcRecords.length} 个剧情单元</span>}>
            剧情单元记录
          </SectionLabel>
          <div className="grid gap-4">
            {arcRecords.map(({ index, plan, summary }, idx) => {
              const start = plan?.startChapter ?? summary?.startChapter ?? 1
              const end = plan?.endChapter ?? summary?.endChapter ?? start
              const driftChecks = summary?.driftChecks ?? []
              const usedBeats = summary?.usedBeats ?? []
              const usedHooks = summary?.usedHooks ?? []
              const turningPoints = summary?.turningPoints ?? []
              const needsReview = summary?.needsReviewChapters ?? []

              return (
                <StaggerItem key={index} index={idx}>
                  <div className="rounded-md bg-surface-2 p-5 transition-colors hover:bg-surface-3/70">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-display text-title font-medium text-ink">
                            第 {index} 单元{plan?.arcTitle ? ` · ${plan.arcTitle}` : ''}
                          </span>
                          <Badge tone="neutral" className="tabular-nums">第 {start}–{end} 章</Badge>
                          {summary
                            ? <Badge tone="success" dot>已归档</Badge>
                            : <Badge tone="primary" dot>规划中 / 推进中</Badge>}
                          {driftChecks.length > 0 && (
                            <Badge tone="warning" icon={<AlertTriangle />}>快检提醒 {driftChecks.length}</Badge>
                          )}
                          {needsReview.length > 0 && (
                            <Badge tone="warning">待复核：第 {needsReview.join('、')} 章</Badge>
                          )}
                        </div>
                        {plan?.arcGoal && (
                          <p className="mt-1.5 text-caption text-muted">单元目标：{plan.arcGoal}</p>
                        )}
                      </div>
                    </div>

                    {summary?.mainline && (
                      <p className="mt-3 rounded-sm bg-surface/85 px-3.5 py-2.5 text-body text-ink">{summary.mainline}</p>
                    )}

                    {/* Stage 2 Themes, Narrative Skeleton & Shape */}
                    {((summary?.themes?.length ?? 0) > 0 || (summary?.skeleton?.length ?? plan?.skeleton?.length ?? 0) > 0 || summary?.shape) && (
                      <div className="mt-2.5 flex flex-wrap items-center gap-2 text-caption">
                        {summary?.themes && summary.themes.length > 0 && (
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-[11px] text-muted">已结主题</span>
                            {summary.themes.map((topic, i) => (
                              <Tag key={`${topic}-${i}`} className="h-auto bg-surface py-0.5 text-[11px] text-ink">{topic}</Tag>
                            ))}
                          </div>
                        )}
                        {(() => {
                          const codes = summary?.skeleton?.length ? summary.skeleton : plan?.skeleton
                          if (!codes?.length) return null
                          return (
                            <span className="inline-flex flex-wrap items-center gap-1 rounded-sm bg-surface px-2 py-0.5 text-[11px] text-body">
                              <span className="text-muted">骨架</span>
                              <span>{codes.map((c) => SKELETON_CODE_LABEL[c] ?? c).join(' → ')}</span>
                            </span>
                          )
                        })()}
                        {summary?.shape && (
                          <span className="text-[11px] text-muted">{summary.shape}</span>
                        )}
                      </div>
                    )}

                    {/* Volume Escalation Progress (V2) */}
                    {summary?.volumeProgress && (summary.volumeProgress.escalation || summary.volumeProgress.cost || summary.volumeProgress.retiredReused?.length) && (
                      <div className="mt-2.5 flex flex-wrap items-center gap-2 text-caption">
                        {summary.volumeProgress.escalation && (
                          <span className="inline-flex items-center gap-1 rounded-sm bg-primary-soft/70 px-2.5 py-1 text-primary">
                            <TrendingUp className="size-3 shrink-0" />
                            <span>升级：{summary.volumeProgress.escalation}</span>
                          </span>
                        )}
                        {summary.volumeProgress.cost && (
                          <span className="inline-flex items-center gap-1 rounded-sm bg-surface px-2.5 py-1 text-body">
                            <span className="text-muted">代价：</span>
                            <span>{summary.volumeProgress.cost}</span>
                          </span>
                        )}
                        {summary.volumeProgress.retiredReused && summary.volumeProgress.retiredReused.length > 0 && (
                          <span className="inline-flex items-center gap-1 rounded-sm bg-warning-soft px-2.5 py-1 text-warning">
                            <Ban className="size-3 shrink-0" />
                            <span>退役复演：{summary.volumeProgress.retiredReused.join('、')}</span>
                          </span>
                        )}
                      </div>
                    )}

                    {/* Turning points */}
                    {turningPoints.length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        {turningPoints.map((pt, i) => (
                          <span key={i} className="inline-flex items-center gap-1.5 rounded-sm bg-surface px-2.5 py-1 text-caption text-body">
                            {pt.chapter ? <span className="font-medium tabular-nums text-primary">第{pt.chapter}章</span> : null}
                            <span>{pt.text}</span>
                          </span>
                        ))}
                      </div>
                    )}

                    {/* Villain status / planned chips */}
                    {(summary?.villainStatus || (plan?.villainChips && plan.villainChips.length > 0)) && (
                      <div className="mt-3 grid gap-1.5 text-caption">
                        {summary?.villainStatus && (
                          <Row label="反派动向">{summary.villainStatus}</Row>
                        )}
                        {plan?.villainChips && plan.villainChips.length > 0 && (
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="text-muted">本单元筹码</span>
                            {plan.villainChips.map((v, i) => (
                              <Tag key={i} className="h-auto bg-surface py-0.5 text-[11px]">
                                <span className="font-medium text-ink">{v.villain}</span>
                                {v.exitPlan && (
                                  <span className="ml-1 text-muted">({EXIT_PLAN_LABEL[v.exitPlan] ?? v.exitPlan})</span>
                                )}
                                {v.newChips?.length ? <span className="ml-1 text-body">：{v.newChips.join('、')}</span> : null}
                              </Tag>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                    {/* Used Beats & Used Hooks */}
                    {(usedBeats.length > 0 || usedHooks.length > 0) && (
                      <div className="mt-3.5 grid gap-2.5 md:grid-cols-2">
                        {usedBeats.length > 0 && (
                          <div>
                            <div className="mb-1.5 flex items-center gap-1 text-[11px] font-medium text-muted">
                              <TagIcon className="size-3 text-primary" />已演桥段（后续规避重复）
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                              {usedBeats.map((beat, i) => (
                                <span
                                  key={i}
                                  title={beat.quote ? `原文引证：「${beat.quote}」` : undefined}
                                  className="inline-flex items-center gap-1 rounded-sm bg-primary-soft/80 px-2 py-0.5 text-caption text-primary"
                                >
                                  {beat.chapter ? <span className="tabular-nums opacity-75">第{beat.chapter}章</span> : null}
                                  <span>{beat.text}</span>
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                        {usedHooks.length > 0 && (
                          <div>
                            <div className="mb-1.5 flex items-center gap-1 text-[11px] font-medium text-muted">
                              <Sparkles className="size-3 text-muted" />已用章末钩子
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                              {usedHooks.map((hook, i) => (
                                <span
                                  key={i}
                                  title={hook.quote ? `原文引证：「${hook.quote}」` : undefined}
                                  className="inline-flex items-center gap-1 rounded-sm bg-surface px-2 py-0.5 text-caption text-body"
                                >
                                  {hook.chapter ? <span className="tabular-nums text-muted">第{hook.chapter}章</span> : null}
                                  <span>{hook.text}</span>
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    )}

                    {/* Drift Checks */}
                    {driftChecks.length > 0 && (
                      <div className="mt-4 rounded-md border border-warning/30 bg-warning-soft/75 p-3.5">
                        <div className="mb-2 flex items-center gap-1.5 text-caption font-medium text-warning">
                          <AlertTriangle className="size-3.5" />
                          单元自检提醒（已自动带入下一单元规划）
                        </div>
                        <ul className="grid gap-2">
                          {driftChecks.map((dc, i) => (
                            <li key={i} className="rounded-sm bg-surface/90 px-3 py-2 text-caption">
                              <div className="flex flex-wrap items-center gap-2">
                                <Badge tone="warning" className="h-5 px-2 text-[11px]">
                                  {DRIFT_KIND_LABEL[dc.kind] ?? dc.kind} · 第 {dc.chapter} 章
                                </Badge>
                                <span className="font-medium text-ink">{dc.issue}</span>
                              </div>
                              {dc.fixHint && <p className="mt-1 text-body">衔接建议：{dc.fixHint}</p>}
                              {dc.quote && <p className="mt-1 truncate text-[11px] text-muted" title={dc.quote}>原文：「{dc.quote}」</p>}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                </StaggerItem>
              )
            })}
          </div>
        </section>
      )}

      {/* 4. Ten-Chapter Horizon Volume Summaries (also populated under rolling mode after WF-03) */}
      {volumeSummaries.length > 0 && (
        <section>
          <SectionLabel actions={<span className="text-caption tabular-nums text-muted">共 {volumeSummaries.length} 段</span>}>
            {arcRecords.length > 0 ? '十章远景卷摘要' : '卷摘要归档'}
          </SectionLabel>
          <div className="grid gap-3">
            {volumeSummaries.map((vol, idx) => (
              <StaggerItem key={vol.volume ?? idx} index={idx}>
                <div className="rounded-md bg-surface-2 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-body font-medium text-ink">第 {vol.volume ?? idx + 1} 卷</span>
                    <Badge tone="neutral" className="tabular-nums">第 {vol.startChapter}–{vol.endChapter} 章</Badge>
                    {vol.needsReviewChapters?.length ? (
                      <Badge tone="warning">待复核：第 {vol.needsReviewChapters.join('、')} 章</Badge>
                    ) : null}
                  </div>
                  <p className="mt-2 text-body text-ink">{vol.mainline}</p>
                  {vol.turningPoints && vol.turningPoints.length > 0 && (
                    <div className="mt-2.5 flex flex-wrap gap-1.5">
                      {vol.turningPoints.map((pt, i) => (
                        <Tag key={i} className="h-auto bg-surface py-0.5 text-[11px]">
                          {pt.chapter ? <span className="mr-1 font-medium tabular-nums text-primary">第{pt.chapter}章</span> : null}
                          {pt.text}
                        </Tag>
                      ))}
                    </div>
                  )}
                </div>
              </StaggerItem>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function RepairList({ logs }: { logs: RepairLogEntry[] }) {
  return (
    <div className="grid gap-3">
      {logs.map((log, idx) => {
        const ts = log.ts || log.timestamp
        return (
          <StaggerItem key={log.id ?? idx} index={idx}>
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
                          {issueTypeLabel(text(iss.type || 'issue'))}{iss.sourceChapter ? ` · 第${iss.sourceChapter}章` : ''}
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
    </div>
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
