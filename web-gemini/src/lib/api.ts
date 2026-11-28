import type {
  AcceptanceTestResult,
  BookEvaluationReport,
  CandidateEvaluationRound,
  ChapterRecord,
  CreateJobInput,
  DriftLedgerEntry,
  EvaluationRoundSummary,
  EditorModelSelection,
  GeneratedConcept,
  HotspotFeedMeta,
  HotspotTrope,
  JobRecord,
  LLMConfig,
  LiveHotspotItem,
  MemeCounts,
  MemeEntry,
  MemeSyncMeta,
  ModelCatalog,
  ProviderCatalogEntry,
  ProviderTestResult,
  PublicProviderConfig,
  RevisionAction,
  RevisionActionPayload,
  RevisionRun,
  RevisionSelection,
  SiwcStatus,
  ThinkingMode,
} from './types'

const API_BASE = '/api'
const roundsPath = (jobId: string, runId: string) =>
  '/jobs/' + encodeURIComponent(jobId) + '/revisions/' + encodeURIComponent(runId) + '/round-evaluations'

export type EditorEvidence = { id: string; label: string; data: Record<string, unknown> }
export type EditorPlan = {
  mode?: 'repair' | 'targeted'
  chapters: number[]
  reviewChapters: number[]
  note: string
  amendment?: { before: string; after: string; evidence: string }
  token: string
  expiresAt: number
  dispatched?: boolean
}
export type EditorRecoveryPlan = {
  stage: string
  stageLabel: string
  note: string
  token: string
  expiresAt: number
  dispatched?: boolean
}
export type EditorPendingFailure = {
  revisionId: string
  stage: string
  stageLabel: string
  message: string
  done: number
  total: number
  occurredAt?: string
}
export type EditorAnswer = {
  reply: string
  evidence: EditorEvidence[]
  plan: EditorPlan | null
  recoveryPlan?: EditorRecoveryPlan | null
  revisionId: string | null
  candidateTurn: number | null
  source?: 'revision-failure'
  recoveryStatus?: string
}
export type EditorTurn = { id: number; question: string; answer: EditorAnswer; createdAt: string }
export type EditorSettings = { model: EditorModelSelection | null }
export type EditorSession = EditorSettings & {
  turns: EditorTurn[]
  hasOlder: boolean
  replying: boolean
  statusMessage?: string | null
  pendingFailure?: EditorPendingFailure | null
  failureWaitingForModel: boolean
}
export type ManuscriptSplitMode = 'auto' | 'single' | 'chunk'
export type ManuscriptImportInput = {
  text: string
  title?: string
  splitMode: ManuscriptSplitMode
  chunkWords?: number
}
export type ManuscriptImportResult = {
  job: JobRecord
  warnings: string[]
  detectedBy: string
  totalChapters: number
  totalWords: number
  inferredTitle: string | null
}
export const REVISION_DISPATCHED = 'novelforge:revision-dispatched'
const editorPath = (jobId: string) => '/jobs/' + encodeURIComponent(jobId) + '/editor-desk'

export class ApiError extends Error {
  readonly code?: string

  constructor(message: string, code?: string) {
    super(message)
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    ...init,
  })
  if (!res.ok) {
    let errMsg = `请求失败 (${res.status})`
    let code: string | undefined
    try {
      const data = await res.json()
      if (data?.error) errMsg = data.error
      else if (data?.message) errMsg = data.message
      if (typeof data?.code === 'string') code = data.code
    } catch {
      const text = await res.text().catch(() => '')
      if (text) errMsg = text
    }
    throw new ApiError(errMsg, code)
  }
  return res.json() as Promise<T>
}

export const api = {
  siwcStatus: () => request<SiwcStatus>('/auth/openai-siwc/status'),
  startSiwc: (mode: 'login' | 'consent' | 'switch') => request<{ url: string }>('/auth/openai-siwc/start', {
    method: 'POST', body: JSON.stringify({ mode, returnOrigin: window.location.origin }),
  }),
  signOutSiwc: () => request<SiwcStatus & { revocationConfirmed: boolean }>('/auth/openai-siwc/sign-out', {
    method: 'POST',
  }),
  async getModelCatalog(): Promise<ModelCatalog> {
    return request('/model-catalog')
  },

  async saveModelProvider(
    provider: string,
    payload: { apiKey?: string; baseUrl?: string; selectedModel?: string; models?: string[] },
  ): Promise<{ provider: ProviderCatalogEntry }> {
    return request(`/model-catalog/${encodeURIComponent(provider)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
  },

  async discoverProviderModels(provider: string): Promise<{ provider: ProviderCatalogEntry }> {
    return request(`/model-catalog/${encodeURIComponent(provider)}/discover`, { method: 'POST' })
  },

  // Provider Configs
  async getProviderConfigs(provider?: string): Promise<{
    providers: Record<string, PublicProviderConfig> | PublicProviderConfig
    allProviders?: Record<string, PublicProviderConfig>
    activeProvider?: string
  }> {
    const suffix = provider ? `?provider=${encodeURIComponent(provider)}` : ''
    return request(`/config/providers${suffix}`)
  },

  async saveProviderConfig(payload: {
    provider: string
    apiKey?: string
    baseUrl?: string
    model?: string
    thinkingMode?: ThinkingMode
  }): Promise<{
    ok: boolean
    providers: Record<string, PublicProviderConfig> | PublicProviderConfig
    allProviders?: Record<string, PublicProviderConfig>
    activeProvider?: string
  }> {
    return request('/config/providers', {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
  },

  async testProviderConnection(payload: {
    provider: string
    baseUrl?: string
    apiKey?: string
    model?: string
    thinkingMode?: ThinkingMode
    timeoutMs?: number
  }): Promise<ProviderTestResult> {
    return request('/providers/test', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  // Hotspots
  async getHotspots(params?: { platform?: string; genre?: string; query?: string }): Promise<{
    feedMeta?: HotspotFeedMeta
    liveItems?: LiveHotspotItem[]
    fallbackTemplates?: HotspotTrope[]
    platforms?: Array<{ id: string; name: string }>
    tropes: HotspotTrope[]
    hookFormulas?: unknown[]
    pacingTemplates?: unknown[]
  }> {
    const qs = new URLSearchParams()
    if (params?.platform && params?.platform !== 'all') qs.set('platform', params.platform)
    if (params?.genre && params?.genre !== 'all') qs.set('genre', params.genre)
    if (params?.query) qs.set('query', params.query)
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return request(`/hotspots${suffix}`)
  },

  async refreshHotspots(payload?: {
    sourceUrl?: string
    customItems?: LiveHotspotItem[]
    boardDate?: string
    sourceName?: string
  }): Promise<{ feedMeta?: HotspotFeedMeta; liveItems?: LiveHotspotItem[] }> {
    return request('/hotspots/refresh', {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    })
  },

  async generateConcept(payload: {
    tropeId?: string
    platform?: string
    genre?: string
    keywords?: string
    llmConfig?: CreateJobInput['llmConfig']
  }): Promise<GeneratedConcept> {
    return request('/hotspots/concept', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  // Jobs
  async listJobs(): Promise<{ jobs: JobRecord[] }> {
    return request('/jobs')
  },

  async createJob(input: CreateJobInput): Promise<{ job: JobRecord }> {
    return request('/jobs', {
      method: 'POST',
      body: JSON.stringify(input),
    })
  },

  async importManuscript(input: ManuscriptImportInput): Promise<ManuscriptImportResult> {
    return request('/jobs/import-txt', {
      method: 'POST',
      body: JSON.stringify(input),
    })
  },

  async getJob(id: string): Promise<{
    job: JobRecord
    chapters?: ChapterRecord[]
  }> {
    return request(`/jobs/${encodeURIComponent(id)}`)
  },

  async updateJobConfig(id: string, llmConfig: Partial<LLMConfig>): Promise<{ job: JobRecord }> {
    return request(`/jobs/${encodeURIComponent(id)}/config`, {
      method: 'PATCH',
      body: JSON.stringify({ llmConfig }),
    })
  },

  async resumeJob(id: string, llmConfig?: Partial<LLMConfig>): Promise<{ job: JobRecord }> {
    return request(`/jobs/${encodeURIComponent(id)}/resume`, {
      method: 'POST',
      ...(llmConfig ? { body: JSON.stringify({ llmConfig }) } : {}),
    })
  },

  async pauseJob(id: string): Promise<{ job: JobRecord }> {
    return request(`/jobs/${encodeURIComponent(id)}/pause`, {
      method: 'POST',
    })
  },

  async extendJob(
    id: string,
    payload: {
      addChapters?: number
      addWords?: number
      targetTotalWords?: number
      direction?: string
      finalizeBook?: boolean
      memeLevel?: string
      memeSourceMode?: string
    }
  ): Promise<{ job: JobRecord }> {
    return request(`/jobs/${encodeURIComponent(id)}/extend`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  async updateChapter(
    jobId: string,
    chapterNumber: number,
    payload: { title?: string; content: string; reverify?: boolean; recheckDownstream?: boolean }
  ): Promise<{ chapter: ChapterRecord; verification?: unknown; downstreamAudit?: unknown }> {
    return request(`/jobs/${encodeURIComponent(jobId)}/chapters/${chapterNumber}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
  },

  async updateDriftStatus(
    jobId: string,
    driftId: string,
    status: 'dismissed' | 'deferred',
    reason = 'human'
  ): Promise<DriftLedgerEntry> {
    return request(`/jobs/${encodeURIComponent(jobId)}/drifts/${encodeURIComponent(driftId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ status, reason }),
    })
  },

  async acceptAuditOverride(jobId: string, note = ''): Promise<{ job: JobRecord }> {
    return request(`/jobs/${encodeURIComponent(jobId)}/audit/accept`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    })
  },

  getExportUrl(jobId: string, format: 'txt' | 'md' | 'docx'): string {
    return `${API_BASE}/jobs/${encodeURIComponent(jobId)}/export?format=${format}`
  },

  getRevision(jobId: string): Promise<{ revision: RevisionRun | null; legacyRevision: RevisionRun | null }> {
    return request('/jobs/' + encodeURIComponent(jobId) + '/revisions')
  },

  editorSession(jobId: string, before?: number): Promise<EditorSession> {
    return request(editorPath(jobId) + (before === undefined ? '' : '?before=' + before))
  },

  saveEditorModel(jobId: string, model: EditorModelSelection): Promise<EditorSettings> {
    return request(editorPath(jobId) + '/settings', { method: 'PUT', body: JSON.stringify({ model }) })
  },

  askEditor(jobId: string, message: string, requestId: string): Promise<{ turn: EditorTurn }> {
    return request(editorPath(jobId) + '/chat', { method: 'POST', body: JSON.stringify({ message, requestId }) })
  },

  diagnoseRevision(jobId: string): Promise<{ accepted: boolean }> {
    return request(editorPath(jobId) + '/diagnose', { method: 'POST' })
  },

  dispatchEditorPlan(jobId: string, token: string): Promise<{ revision: RevisionRun }> {
    return request(editorPath(jobId) + '/dispatch', { method: 'POST', body: JSON.stringify({ token }) })
  },

  dispatchEditorRecovery(jobId: string, token: string): Promise<{ revision: RevisionRun }> {
    return request(editorPath(jobId) + '/recover', { method: 'POST', body: JSON.stringify({ token }) })
  },

  candidateRounds(jobId: string, runId: string): Promise<{ rounds: CandidateEvaluationRound[]; baseline: EvaluationRoundSummary | null }> {
    return request(roundsPath(jobId, runId))
  },

  candidateReport(jobId: string, runId: string, turn: number): Promise<CandidateEvaluationRound> {
    return request(roundsPath(jobId, runId) + '/' + turn)
  },

  changeCandidateEvaluation(jobId: string, runId: string, turn: number,
    body: { action: 'start' | 'pause' | 'restart'; flash?: RevisionSelection; pro?: RevisionSelection }): Promise<CandidateEvaluationRound> {
    return request(roundsPath(jobId, runId) + '/' + turn, { method: 'POST', body: JSON.stringify(body) })
  },

  startRevision(jobId: string, options: { editor: RevisionSelection; verifier: RevisionSelection; volumeSize: number; concurrency: number }): Promise<{ revision: RevisionRun }> {
    return request('/jobs/' + encodeURIComponent(jobId) + '/revisions', { method: 'POST', body: JSON.stringify(options) })
  },

  changeRevision(jobId: string, revisionId: string, action: RevisionAction, payload?: RevisionActionPayload): Promise<{ revision: RevisionRun }> {
    const path = '/jobs/' + encodeURIComponent(jobId) + '/revisions/' + encodeURIComponent(revisionId) + '/' + action
    return request(path, { method: 'POST', body: payload ? JSON.stringify(payload) : undefined })
  },

  revisionExportUrl(jobId: string, revisionId: string, kind: 'original' | 'candidate' | 'report' | 'ledger' | 'evaluation' | 'decisions' | `candidate-${number}` | `report-${number}`): string {
    return API_BASE + '/jobs/' + encodeURIComponent(jobId) + '/revisions/' + encodeURIComponent(revisionId) + '/export/' + kind
  },

  async evaluateJob(
    jobId: string,
    payload: {
      mode?: 'full' | 'flash_only' | 'pro_only'
      provider?: string
      flashProvider?: string
      flashModel?: string
      proProvider?: string
      proModel?: string
      useSavedConnection?: boolean
      useSavedFlashConnection?: boolean
      useSavedProConnection?: boolean
      batchSize?: number
      resumeEvaluation?: boolean
      restartEvaluation?: boolean
      repairFailedBatches?: boolean
      background?: boolean
    }
  ): Promise<{ started?: boolean; alreadyRunning?: boolean; job: JobRecord; evaluationReport?: BookEvaluationReport }> {
    return request(`/jobs/${encodeURIComponent(jobId)}/evaluate`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  async deAiJob(
    jobId: string,
    payload: {
      chapterNumbers?: number[]
      priorities?: string[]
      categories?: string[]
      provider?: string
      model?: string
      useSavedConnection?: boolean
      background?: boolean
    }
  ): Promise<{
    started?: boolean
    job: JobRecord
    updatedChapters?: Array<{
      chapterNumber: number
      title?: string
      beforeWords: number
      afterWords: number
      addressedIssues: number
    }>
    evaluationReport?: BookEvaluationReport | null
  }> {
    return request(`/jobs/${encodeURIComponent(jobId)}/de-ai`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },

  // Memes Wiki
  async getMemes(params?: {
    status?: string
    q?: string
    tag?: string
    limit?: number
    offset?: number
  }): Promise<{
    syncMeta: MemeSyncMeta
    items: MemeEntry[]
    total: number
    limit?: number
    offset?: number
    counts: MemeCounts
  }> {
    const qs = new URLSearchParams()
    if (params?.status && params.status !== 'all') qs.set('status', params.status)
    if (params?.q) qs.set('q', params.q)
    if (params?.tag) qs.set('tag', params.tag)
    qs.set('limit', String(params?.limit ?? 50))
    qs.set('offset', String(params?.offset ?? 0))
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return request(`/memes${suffix}`)
  },

  async syncMemes(payload?: {
    customItems?: unknown[]
    simulateFailure?: boolean
    verifyDetails?: boolean
    maxPages?: number
  }): Promise<{
    ok: boolean
    usingCache?: boolean
    error?: string
    syncMeta: MemeSyncMeta
    items: MemeEntry[]
    counts: MemeCounts
  }> {
    return request('/memes/sync', {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    })
  },

  async getMemeDetail(slug: string): Promise<{ item: MemeEntry }> {
    return request(`/memes/${encodeURIComponent(slug)}`)
  },

  // Acceptance Tests
  async runAcceptanceTest(
    suite:
      | 'all'
      | 'foreshadowing'
      | 'fault-injection'
      | 'timeout-resume'
      | 'empty-premise-auto-topic'
      | 'audit-gate'
      | 'memes-admission'
  ): Promise<{ passed: boolean; results: AcceptanceTestResult[] }> {
    return request('/test/run-acceptance', {
      method: 'POST',
      body: JSON.stringify({ suite }),
    })
  },
}

