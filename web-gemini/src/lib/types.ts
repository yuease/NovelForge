export type POVType = '第一人称' | '第三人称全知' | '第三人称限知'

export type LLMProvider =
  | 'mock'
  | 'deepseek'
  | 'openai'
  | 'openai-siwc'
  | 'gemini'
  | 'vertex'
  | 'anthropic'
  | 'qwen'
  | 'moonshot'
  | 'zhipu'
  | 'siliconflow'
  | 'openrouter'
  | 'ollama'
  | 'openai-compatible'

export type ThinkingMode = 'disabled' | 'enabled'

export type MemeLevel = 'none' | 'light' | 'rich'

export type MemeSourceMode = 'active_only' | 'include_archive_elements'

export type JobStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'error'
  | 'interrupted'

export type TabKey =
  | 'hotspots'
  | 'generator'
  | 'monitor'
  | 'facts'
  | 'reader'
  | 'evaluation'
  | 'acceptance'

export interface HotspotFeedMeta {
  sourceName?: string
  sourceUrl?: string
  boardDate?: string
  lastSuccessAt?: string | null
  lastAttemptAt?: string | null
  cachedAt?: string | null
  usingCache?: boolean
  lastError?: string | null
  feedVersion?: number
  itemCount?: number
  autoRefreshIntervalMs?: number
}

export interface LiveHotspotItem {
  id: string
  title: string
  author?: string
  genre?: string
  rank?: number
  hotScore?: number | string
  sourceName?: string
  sourceUrl?: string
  boardDate?: string
  fetchedAt?: string
  tags?: string[]
  summary?: string
}

export interface HotspotTrope {
  id: string
  title: string
  platform: string
  genre: string
  heatScore?: number
  tags?: string[]
  hookFormula?: string
  pacingTemplate?: string
  description: string
  goldenThreeChapters?: string[]
  exampleLogline?: string
}

export interface GeneratedConcept {
  title: string
  premise?: string
  logline?: string
  hook?: string
  genre: string
  recommendedPov?: POVType
  goldenThreeChapters?:
    | Array<{
        chapter?: number
        hook?: string
        summary?: string
      }>
    | string[]
  coreConflict?: string
  tags?: string[]
}

export interface PublicProviderConfig {
  provider: string
  hasKey: boolean
  maskedKey: string
  baseUrl?: string
  model?: string
  thinkingMode?: ThinkingMode
}

export interface ProviderCatalogEntry {
  providerId: LLMProvider
  label: string
  description: string
  configured: boolean
  baseUrl: string
  defaultBaseUrl: string
  apiKeyMasked: string
  requiresKey: boolean
  selectedModel: string
  models: string[]
  discoverySupported: boolean
  discoveryBlock?: 'vertex-oauth'
  modelSource: 'discovered' | 'preset' | 'manual'
  authKind?: 'oauth'
  oauthState?: SiwcState
  accountLabel?: string
  profileId?: string
  catalogVersion?: number
  allStages?: boolean
  modelLabels?: Record<string, string>
}

export type SiwcState = 'signed_out' | 'connected' | 'permission_required' | 'reauthorize' | 'storage_error' | 'disabled'
export type SiwcStatus = {
  enabled: boolean
  configured: boolean
  state: SiwcState
  accountLabel: string
  profileId: string
  quotaLimited: boolean
}

export interface ModelCatalog {
  activeProvider: LLMProvider
  providers: ProviderCatalogEntry[]
}

export interface ProviderTestResult {
  ok: boolean
  provider: string
  model?: string
  baseUrl?: string
  thinkingMode?: ThinkingMode
  latencyMs?: number
  replyPreview?: string
  usage?: unknown
  error?: string
  code?: string
}

export interface LLMConfig {
  provider: LLMProvider
  useSavedConnection?: boolean
  baseUrl?: string
  apiKey?: string
  model?: string
  lightModel?: string
  temperature?: number
  thinkingMode?: ThinkingMode
  maxRetries?: number
  timeoutMs?: number
  autoResumeOnRateLimit?: boolean
  testScenario?: string
}

export interface CreateJobInput {
  title?: string
  premise: string
  genre: string
  pov: POVType
  targetWords: number
  totalChapters: number
  styleGuide?: string
  memeLevel?: MemeLevel
  memeSourceMode?: MemeSourceMode
  maxRepairRetries?: number
  enableDeAi?: boolean
  enableEvaluation?: boolean
  evalConfig?: {
    mode?: 'full' | 'flash_only' | 'pro_only'
    provider?: LLMProvider
    flashModel?: string
    proModel?: string
  }
  preferFreeform?: boolean
  hotspotContext?: unknown
  llmConfig: LLMConfig
}

export interface StageStat {
  calls?: number
  retries?: number
  durationMs?: number
  promptTokens?: number
  completionTokens?: number
  totalTokens?: number
  cacheHitTokens?: number
}

export interface AuditGate {
  chaptersComplete?: boolean
  wordCountInRange?: boolean
  auditPassed?: boolean
  exportValid?: boolean
  allPassed?: boolean
  reasons?: string[]
}

export interface TopicPackage {
  title?: string
  premise?: string
  coreConflict?: string
  protagonistMotivation?: string
  mainPlot?: string
  goldenThreeChapters?: unknown[]
  candidates?: Array<{
    title?: string
    coreConflict?: string
    genreFit?: number
    novelty?: number
    longFormPotential?: number
  }>
  selectionReason?: string
  inspirationMode?: string
  sourceRefs?: Array<{ title?: string; sourceName?: string; sourceUrl?: string; boardDate?: string }>
  lockedAt?: string
}

export interface JobErrorObject {
  code?: string
  message?: string
  chapterNumber?: number | null
  issues?: Array<{
    type?: string
    severity?: string
    message?: string
    sourceChapter?: number
  }>
  retryable?: boolean
}

export interface JobLogEntry {
  ts?: string
  level?: string
  stage?: string
  message?: string
  meta?: unknown
}

export interface AutoRetryState {
  waiting?: boolean
  code?: string
  message?: string
  attempt?: number
  maxAttempts?: number
  cooldownMs?: number
  resumeAt?: string
}

export interface TokenUsageSummary {
  promptTokens: number
  completionTokens: number
  totalTokens: number
  cacheHitTokens?: number
}

export type RevisionSelection = { provider: LLMProvider; model: string; useSavedConnection?: boolean }
export type EditorModelSelection = RevisionSelection & { siwcProfileId?: string }

export type RevisionAction = 'pause' | 'resume' | 'archive' | 'apply' | 'repair' | 'decide' | 'restore' | 'continue-round'
export type RevisionAmendment = { before: string; after: string; evidence: string }
export type RevisionActionPayload =
  | { chapters: number[]; note: string; amendment?: RevisionAmendment }
  | { editor?: RevisionSelection; verifier?: RevisionSelection }
  | { decisions: Record<string, string> }
  | { chapter: number; field: 'title' | 'content'; text: string; evidence: string }
  | { turn: number }
  | { force?: boolean }

export interface RevisionRun {
  id: string
  jobId: string
  status: 'running' | 'paused' | 'error' | 'ready' | 'needs_review' | 'waiting_author' | 'waiting_source' | 'applied' | 'archived'
  stage: 'prepare' | 'audit' | 'ledger' | 'edit' | 'hard' | 'review' | 'repair' | 'recheck' | 'hotfix' | 'hotcheck' | 'finish'
  workflowVersion?: 1 | 2
  ledgerVersion?: number
  reprocessBatches?: number | null
  reviewScope?: 'full' | 'incremental'
  affectedChapters?: number[]
  candidateTurn?: number
  cycleBaseTurn?: number
  candidateHistory?: number[]
  sourceDefects?: Array<{ chapter: number; field: 'title' | 'content' }>
  pendingDecisions?: Array<{ id: string; problem: string; chapters: number[]; options: string[]; evidence: Array<{ chapter: number; quote: string }> }>
  done: number
  total: number
  chapterCount: number
  changedCount: number
  blockingCount: number
  remainingIssueCounts?: { P0: number; P1: number; P2: number }
  reviewedIssues?: number
  reportIssueCount?: number
  auditBlindChapters?: number[]
  volumes: Array<{ number: number; start: number; end: number }>
  editor: RevisionSelection
  verifier: RevisionSelection
  hasLedger?: boolean
  hasCandidate?: boolean
  isMock: boolean
  pauseRequested?: boolean
  error?: string | null
  blockers?: Array<{ chapter?: number | null; problem: string }>
}

export interface JobRecord {
  id: string
  title?: string
  premise: string
  genre: string
  pov: POVType
  targetWords: number
  totalChapters: number
  status: JobStatus
  stage?: string
  progress?: number
  currentChapter?: number
  completedChapters?: number
  totalWords?: number
  tokenUsage?: TokenUsageSummary
  tokenUsageByModel?: Record<string, TokenUsageSummary>
  stageStats?: Record<string, StageStat>
  autoResumeOnRateLimit?: boolean
  autoRetryState?: AutoRetryState | null
  auditGate?: AuditGate | null
  topicPackage?: TopicPackage | null
  inspirationMode?: string | null
  memeLevel?: MemeLevel
  memeSourceMode?: MemeSourceMode
  error?: string | JobErrorObject | null
  lastError?: string | JobErrorObject | null
  logs?: JobLogEntry[]
  checkpoint?: Record<string, unknown> | null
  canonicalFacts?: CanonicalFacts | null
  repairLogs?: RepairLogEntry[]
  auditReport?: unknown
  evaluationStatus?: 'running' | 'completed' | 'error' | null
  evaluationProgress?: string
  evaluationCheckpoint?: {
    mode: 'full' | 'flash_only' | 'pro_only'
    stage: 'flash' | 'pro' | 'finalizing'
    totalBatches: number
    completedBatches: number
    updatedAt: string
    selection?: {
      flashProvider: LLMProvider
      flashModel: string
      proProvider: LLMProvider
      proModel: string
      useSavedFlashConnection?: boolean
      useSavedProConnection?: boolean
    } | null
  } | null
  evaluationReport?: BookEvaluationReport | null
  arcPlans?: ArcPlan[]
  bookPlan?: {
    title?: string
    synopsis?: string
    canonLocks?: CanonLocks | null
    volumes?: Array<{
      volumeNumber?: number
      title?: string
      startChapter?: number
      endChapter?: number
      summary?: string
    }>
    [key: string]: unknown
  } | null
  createdAt?: string
  updatedAt?: string
  llmConfig?: LLMConfig
}

export interface DimensionScoreItem {
  score: number
  chapterNumber?: number
  evidence?: string
  comment?: string
  fixAdvice?: string
}

export interface AiFlavorIssueItem {
  id: string
  chapterNumber: number
  category:
    | 'over_explaining'
    | 'formulaic_reaction'
    | 'shared_voice'
    | 'static_situation'
    | 'repetitive_hooks'
    | 'inconsistent_details'
    | string
  quote: string
  impact: string
  priority: 'P0' | 'P1' | 'P2' | string
  suggestion: string
  resolved?: boolean
}

export interface BookEvaluationReport {
  evaluatedAt?: string
  sourceHash?: string
  mode?: string
  models?: {
    flash?: string | null
    pro?: string | null
  }
  bookTitle?: string
  totalChapters?: number
  totalWords?: number
  deterministicSignals?: {
    totalChapters?: number
    reactionStats?: Record<string, { totalCount: number; chapterCount: number; sampleChapters: number[] }>
    endingHookStats?: Record<string, { count: number; chapters: number[] }>
  }
  overallSummary?: string
  readiness?: 'ready_to_submit' | 'revise_recommended' | 'major_rework' | string
  topVerdicts?: {
    openingHook?: { verdict?: string; summary?: string }
    middlePacing?: { verdict?: string; summary?: string }
    endingPayoff?: { verdict?: string; summary?: string }
  } | null
  blindReaderFirst3Chapters?: {
    whyKeepReading?: Array<{ chapterNumber?: number; quote?: string; reason?: string }>
    whereToSkip?: Array<{ chapterNumber?: number; quote?: string; reason?: string }>
  } | null
  sixDimensions?: {
    sellingPointAndOpening?: DimensionScoreItem
    worldSettingAndCausality?: DimensionScoreItem
    characterAndChoices?: DimensionScoreItem
    plotAndPacing?: DimensionScoreItem
    foreshadowingAndEnding?: DimensionScoreItem
    styleAndReadability?: DimensionScoreItem
  } | null
  batchSummaries?: Array<{
    batchRange?: string
    timelineSummary?: string
    situationDelta?: string
    endingHookSummary?: Array<{ chapterNumber?: number; hookType?: string }>
    extractedSettings?: string[]
  }>
  aiFlavorIssues?: AiFlavorIssueItem[]
  rewritePlan?: string[]
  lastDeAiAt?: string
}

export interface EvaluationRoundSummary {
  evaluatedAt?: string
  issues: Record<'P0' | 'P1' | 'P2', number>
  scores: Partial<Record<keyof NonNullable<BookEvaluationReport['sixDimensions']>, number>>
  models?: BookEvaluationReport['models']
  mode?: string
}

export interface CandidateEvaluationRound {
  turn: number
  status: 'running' | 'paused' | 'error' | 'completed'
  progress?: string
  error?: string
  done: number
  total?: number
  stage?: 'flash' | 'pro' | 'finalizing'
  selection: { flash: RevisionSelection; pro: RevisionSelection }
  summary?: EvaluationRoundSummary | null
  report?: BookEvaluationReport | null
  isMock?: boolean
}

export interface ChapterRecord {
  chapterNumber: number
  title?: string
  content: string
  wordCount?: number
  summary?: string
  status?: string
  verified?: boolean
  repairCount?: number
  issues?: string[]
  updatedAt?: string
}

export interface FactEntry {
  key?: string
  name?: string
  description?: string
  status?: string
  location?: string
  holder?: string
  knownBy?: string[]
  severity?: string
  sourceChapter?: number
  updatedChapter?: number
  [key: string]: unknown
}

export interface CanonLocks {
  powerScale?: string
  currencyAndEra?: string
  worldTaboos?: string[]
  keyAssets?: string[]
}

export interface ArcVillainChip {
  villain: string
  newChips?: string[]
  exitPlan?: 'retain' | 'exit' | 'background' | string
}

export interface ArcPlan {
  arcIndex: number
  startChapter: number
  endChapter: number
  arcTitle: string
  arcGoal: string
  villainChips?: ArcVillainChip[]
}

export interface StoryClock {
  day?: string
  season?: string
  timeOfDay?: string
  countdown?: string
  fieldChapters?: Partial<Record<'day' | 'season' | 'timeOfDay' | 'countdown', number>>
}

export interface LedgerBalanceChange {
  chapter: number
  value: string
  change?: string
}

export interface LedgerBalance {
  label: string
  value: string
  updatedAtChapter?: number
  recentChanges?: LedgerBalanceChange[]
}

export type StanceTier = 'hostile' | 'wary' | 'transactional' | 'ally' | 'intimate'

export interface CharacterRelationshipChange {
  change?: string
  sourceChapter?: number
  updatedAtChapter?: number
}

export interface CharacterStateEntry {
  sourceChapter?: number
  lastSeenChapter?: number
  updatedAtChapter?: number
  currentGoal?: string | null
  emotion?: string | null
  stance?: string | null
  relationships?: Record<string, CharacterRelationshipChange>
  stanceTier?: StanceTier
  stanceTierChapter?: number
  knownFacts?: string[]
  knownFactChapters?: Record<string, number>
  storyRelationships?: Record<string, CharacterRelationshipChange>
}

export interface ArcEvidenceItem {
  chapter?: number
  text?: string
  quote?: string
}

export interface ArcDriftCheck {
  chapter: number
  kind: 'value' | 'clock' | 'canon' | 'character_ooc' | string
  issue: string
  fixHint: string
  quote: string
}

export interface ArcSummary {
  arc?: number
  startChapter: number
  endChapter: number
  sourceHash?: string
  mainline: string
  turningPoints?: Array<{ chapter?: number; text?: string }>
  usedBeats?: ArcEvidenceItem[]
  usedHooks?: ArcEvidenceItem[]
  villainStatus?: string
  driftChecks?: ArcDriftCheck[]
  needsReviewChapters?: number[]
}

export interface VolumeSummary {
  volume?: number
  startChapter: number
  endChapter: number
  sourceHash?: string
  mainline: string
  turningPoints?: Array<{ chapter?: number; text?: string }>
  needsReviewChapters?: number[]
}

export interface CanonicalFacts {
  characters?: Record<string, FactEntry> | FactEntry[]
  injuries?: Record<string, FactEntry> | FactEntry[]
  timeline?: FactEntry[] | Record<string, FactEntry>
  items?: Record<string, FactEntry> | FactEntry[]
  secrets?: Record<string, FactEntry> | FactEntry[]
  locations?: Record<string, FactEntry> | FactEntry[]
  foreshadowing?: Record<string, FactEntry> | FactEntry[]
  relations?: Record<string, FactEntry> | FactEntry[]
  storyClock?: StoryClock | null
  ledgerBalances?: LedgerBalance[]
  characterStates?: Record<string, CharacterStateEntry>
  arcSummaries?: ArcSummary[]
  volumeSummaries?: VolumeSummary[]
  factHistory?: Array<{
    chapter?: number
    type?: string
    action?: string
    summary?: string
    timestamp?: string
    [key: string]: unknown
  }>
}

export interface RepairIssueItem {
  type?: string
  severity?: string
  message?: string
  sourceChapter?: number
}

export interface RepairLogEntry {
  id?: string
  chapterNumber?: number
  attempt?: number
  issues?: Array<string | RepairIssueItem>
  citedChapters?: number[]
  action?: string
  resolved?: boolean
  message?: string
  timestamp?: string
  ts?: string
  [key: string]: unknown
}

export interface SSELogItem {
  id: string
  type: string
  message: string
  chapterNumber?: number
  stage?: string
  timestamp: string
  raw?: unknown
}

export interface AcceptanceTestResult {
  id: string
  name?: string
  passed: boolean
  jobId?: string
  durationMs?: number
  summary?: string
  details?: unknown
  error?: string
}

export type MemeStatus = 'active' | 'pending_verification' | 'rejected_old' | 'expired'

export interface MemeSyncMeta {
  lastSuccessAt?: string | null
  lastAttemptAt?: string | null
  lastSyncedUpdatedAt?: string | null
  overlapMs?: number
  autoRefreshIntervalMs?: number
  usingCache?: boolean
  lastError?: string | null
  retryCount?: number
  lastSyncAdded?: number
  lastSyncUpdated?: number
  totalEntries?: number
}

export interface MemeCounts {
  all: number
  active: number
  pending_verification: number
  rejected_old: number
  expired: number
}

export interface MemeEntry {
  slug: string
  title: string
  summary?: string
  tags?: string[]
  aliases?: string[]
  mergedSlugs?: string[]
  sourceUrl?: string
  updatedAt?: string
  earliestSpreadAt?: string | null
  heatEvidenceUrls?: string[]
  independentCreatorsCount?: number
  hasHotListRecord?: boolean
  verifiedAt?: string | null
  status: MemeStatus
  statusReason?: string
}
