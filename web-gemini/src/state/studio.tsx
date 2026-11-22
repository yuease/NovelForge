import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../lib/api'
import { navigate } from '../lib/router'
import type {
  CanonicalFacts,
  ChapterRecord,
  JobRecord,
  LLMConfig,
  RepairLogEntry,
  SSELogItem,
} from '../lib/types'

/**
 * Global studio state: job list, selected job, its chapters/facts and the live SSE stream.
 * Pages read everything through `useStudio()` – no prop drilling.
 */

export type ServiceState = 'checking' | 'online' | 'offline'

export interface ConceptPrefill {
  title?: string
  premise?: string
  genre?: string
  hotspotContext?: unknown
}

export interface ExtendPayload {
  addChapters?: number
  addWords?: number
  targetTotalWords?: number
  direction?: string
  finalizeBook?: boolean
  memeLevel?: string
  memeSourceMode?: string
}

export interface StudioValue {
  service: ServiceState
  jobs: JobRecord[]
  activeJobId: string | null
  /** Full record of the selected job (falls back to the list entry while loading). */
  activeJob: JobRecord | null
  chapters: ChapterRecord[]
  facts: CanonicalFacts | null
  repairLogs: RepairLogEntry[]
  sseLogs: SSELogItem[]
  sseConnected: boolean

  selectJob: (jobId: string | null) => void
  refreshJobs: () => Promise<void>
  loadJobDetails: (jobId: string) => Promise<void>
  clearLogs: () => void

  /** Registers a new job and selects it for its first action. */
  onJobCreated: (job: JobRecord, destination?: 'monitor' | 'evaluation') => void
  updateJobConfig: (jobId: string, llmConfig: Partial<LLMConfig>) => Promise<void>
  resumeJob: (jobId: string, llmConfig?: Partial<LLMConfig>) => Promise<void>
  pauseJob: (jobId: string) => Promise<void>
  extendJob: (jobId: string, payload: ExtendPayload) => Promise<void>
  replaceChapter: (chapter: ChapterRecord) => void

  /** Hotspot → Create hand-off. */
  prefill: ConceptPrefill | null
  sendConceptToCreate: (concept: ConceptPrefill) => void
  consumePrefill: () => void
}

const StudioContext = createContext<StudioValue | null>(null)

export function useStudio(): StudioValue {
  const value = useContext(StudioContext)
  if (!value) throw new Error('useStudio must be used inside <StudioProvider>')
  return value
}

const ACTIVE_JOB_STORAGE_KEY = 'novel_forge_active_job_id'

const SSE_EVENTS = [
  'snapshot',
  'job:stage',
  'job:progress',
  'chapter:started',
  'chapter:repair_started',
  'chapter:repaired',
  'facts:updated',
  'chapter:completed',
  'job:log',
  'job:paused',
  'job:error',
  'job:completed',
] as const

const RELOAD_ON = new Set(['job:stage', 'job:progress', 'chapter:completed', 'chapter:repaired', 'job:completed', 'job:error', 'job:paused'])

const STAGE_LABELS: Record<string, string> = {
  topic_selection: '构建选题',
  book_plan: '全书规划',
  chapter_outlines: '分章细纲',
  chapter_writing: '章节撰写',
  chapter_verify: '章节审校',
  chapter_repair: '章节返修',
  fact_extraction: '事实提取',
  arc_archive: '单元归档',
  volume_archive: '卷摘要归档',
  full_audit: '全书终审',
  completed: '已完成',
}

function formatSseMessage(type: string, data: Record<string, unknown>): string {
  if (type === 'snapshot' && data.job && typeof data.job === 'object') {
    const j = data.job as JobRecord
    return `已同步任务快照：${j.title || j.id}（${j.status}）`
  }
  if (type === 'job:log' && data.log && typeof data.log === 'object') {
    const l = data.log as { message?: string; stage?: string }
    return l.message || `[${l.stage || 'log'}] 事件`
  }
  if (type === 'chapter:started') return `开始创作第 ${data.chapterNumber}/${data.totalChapters} 章：${data.title || ''}`
  if (type === 'chapter:completed') {
    const ch = (data.chapter ?? {}) as { chapterNumber?: number; title?: string; wordCount?: number }
    return `第 ${ch.chapterNumber} 章《${ch.title || ''}》完成审校并入库（${ch.wordCount ?? 0} 字）`
  }
  if (type === 'chapter:repair_started') return `第 ${data.chapterNumber} 章触发自动修复（第 ${data.attempt}/${data.maxRetries} 次）`
  if (type === 'chapter:repaired') return `第 ${data.chapterNumber} 章修复完成：${data.resolved ? '已通过重审' : '仍有待处理问题'}`
  if (type === 'facts:updated') return `第 ${data.chapterNumber} 章事实已写入事实库`
  if (type === 'job:stage' || type === 'job:progress') {
    const stageKey = typeof data.stage === 'string' ? data.stage : ''
    return `阶段：${STAGE_LABELS[stageKey] || stageKey}（${data.progress ?? 0}%）`
  }
  if (type === 'job:completed') return '全书创作与终审全部完成'
  if (type === 'job:paused') return '任务已暂停并保存检查点'
  if (type === 'job:error') {
    const e = data.error
    const msg = e && typeof e === 'object' ? (e as { message?: string }).message || JSON.stringify(e) : String(e || '未知错误')
    return `任务异常：${msg}`
  }
  if (typeof data.message === 'string') return data.message
  return JSON.stringify(data)
}

export function StudioProvider({ children }: { children: ReactNode }) {
  const [service, setService] = useState<ServiceState>('checking')
  const [jobs, setJobs] = useState<JobRecord[]>([])
  const [activeJobId, setActiveJobId] = useState<string | null>(() => localStorage.getItem(ACTIVE_JOB_STORAGE_KEY))
  const selectedId = useRef(activeJobId)
  const jobsSeq = useRef(0)
  const detailsSeq = useRef(0)
  const [jobDetail, setJobDetail] = useState<JobRecord | null>(null)
  const [chapters, setChapters] = useState<ChapterRecord[]>([])
  const [facts, setFacts] = useState<CanonicalFacts | null>(null)
  const [repairLogs, setRepairLogs] = useState<RepairLogEntry[]>([])
  const [sseLogs, setSseLogs] = useState<SSELogItem[]>([])
  const [sseConnected, setSseConnected] = useState(false)
  const [prefill, setPrefill] = useState<ConceptPrefill | null>(null)

  const selectJob = useCallback((jobId: string | null) => {
    if (selectedId.current === jobId) return
    selectedId.current = jobId
    setActiveJobId(jobId)
    setJobDetail(null)
    setChapters([])
    setFacts(null)
    setRepairLogs([])
    setSseLogs([])
  }, [])

  const mergeJob = useCallback((job: JobRecord) => {
    if (selectedId.current === job.id) setJobDetail(job)
    setJobs((prev) => prev.map((j) => (j.id === job.id ? job : j)))
  }, [])

  const refreshJobs = useCallback(async () => {
    const seq = ++jobsSeq.current
    try {
      const res = await api.listJobs()
      if (seq !== jobsSeq.current) return
      const list = Array.isArray(res.jobs) ? res.jobs : []
      setJobs(list)
      setService('online')
      const prev = selectedId.current
      if (!prev || !list.some((j) => j.id === prev)) {
        const preferred = list.find((j) => j.status === 'running')
          ?? list.find((j) => j.status === 'paused' || j.status === 'interrupted')
          ?? list[0]
        selectJob(preferred?.id ?? null)
      }
    } catch {
      if (seq === jobsSeq.current) setService('offline')
    }
  }, [selectJob])

  const loadJobDetails = useCallback(async (jobId: string) => {
    const isSelected = selectedId.current === jobId
    const seq = isSelected ? ++detailsSeq.current : -1
    try {
      const res = await api.getJob(jobId)
      if (res.job) {
        setJobs((prev) => prev.map((j) => (
          j.id === res.job.id && (!j.updatedAt || !res.job.updatedAt || j.updatedAt <= res.job.updatedAt) ? res.job : j
        )))
      }
      if (!isSelected || selectedId.current !== jobId || seq !== detailsSeq.current) return
      if (res.job) {
        setJobDetail(res.job)
        setFacts(res.job.canonicalFacts ?? null)
        setRepairLogs(res.job.repairLogs ?? [])
      }
      if (Array.isArray(res.chapters)) setChapters(res.chapters)
    } catch {
      /* next SSE event or poll will retry */
    }
  }, [])

  // Poll job list every 30s while visible.
  useEffect(() => {
    void refreshJobs()
    const onVisible = () => { if (document.visibilityState === 'visible') void refreshJobs() }
    const timer = window.setInterval(onVisible, 30_000)
    document.addEventListener('visibilitychange', onVisible)
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible) }
  }, [refreshJobs])

  useEffect(() => {
    if (activeJobId) {
      localStorage.setItem(ACTIVE_JOB_STORAGE_KEY, activeJobId)
      void loadJobDetails(activeJobId)
    } else {
      localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY)
    }
  }, [activeJobId, loadJobDetails])

  // SSE stream for the selected job.
  useEffect(() => {
    setSseConnected(false)
    if (!activeJobId) return
    const es = new EventSource(`/api/jobs/${encodeURIComponent(activeJobId)}/stream`)
    es.onopen = () => { setSseConnected(true); setService('online') }
    es.onerror = () => setSseConnected(false)

    const push = (type: string, data: Record<string, unknown>) => {
      const item: SSELogItem = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        type,
        message: formatSseMessage(type, data),
        chapterNumber: typeof data.chapterNumber === 'number' ? data.chapterNumber : undefined,
        stage: typeof data.stage === 'string' ? data.stage : undefined,
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
      }
      setSseLogs((prev) => [item, ...prev].slice(0, 200))
    }

    const handle = (type: string, raw: string) => {
      if (selectedId.current !== activeJobId) return
      try {
        const data = JSON.parse(raw) as Record<string, unknown>
        push(type, data)
        if (type === 'snapshot' && data.job && typeof data.job === 'object') {
          const j = data.job as JobRecord
          setJobDetail(j)
          if (j.canonicalFacts) setFacts(j.canonicalFacts)
          if (Array.isArray(j.repairLogs)) setRepairLogs(j.repairLogs)
        } else if (type === 'facts:updated' && data.canonicalFacts) {
          setFacts(data.canonicalFacts as CanonicalFacts)
        } else if (RELOAD_ON.has(type)) {
          void loadJobDetails(activeJobId)
        }
      } catch {
        push(type, { message: raw })
      }
    }

    for (const ev of SSE_EVENTS) {
      es.addEventListener(ev, (e) => { if (e instanceof MessageEvent) handle(ev, e.data) })
    }
    return () => { es.close(); setSseConnected(false) }
  }, [activeJobId, loadJobDetails])

  const onJobCreated = useCallback((job: JobRecord, destination: 'monitor' | 'evaluation' = 'monitor') => {
    jobsSeq.current += 1
    setJobs((prev) => [job, ...prev.filter((j) => j.id !== job.id)])
    selectJob(job.id)
    setJobDetail(job)
    setPrefill(null)
    navigate(destination)
  }, [selectJob])

  const updateJobConfig = useCallback(async (jobId: string, llmConfig: Partial<LLMConfig>) => {
    const res = await api.updateJobConfig(jobId, llmConfig)
    if (res.job) mergeJob(res.job)
    await loadJobDetails(jobId)
  }, [mergeJob, loadJobDetails])

  const resumeJob = useCallback(async (jobId: string, llmConfig?: Partial<LLMConfig>) => {
    const res = await api.resumeJob(jobId, llmConfig)
    if (res.job) mergeJob(res.job)
    await loadJobDetails(jobId)
  }, [mergeJob, loadJobDetails])

  const pauseJob = useCallback(async (jobId: string) => {
    const res = await api.pauseJob(jobId)
    if (res.job) mergeJob(res.job)
  }, [mergeJob])

  const extendJob = useCallback(async (jobId: string, payload: ExtendPayload) => {
    const res = await api.extendJob(jobId, payload)
    if (res.job) mergeJob(res.job)
    await loadJobDetails(jobId)
  }, [mergeJob, loadJobDetails])

  const replaceChapter = useCallback((updated: ChapterRecord) => {
    setChapters((prev) => prev.map((c) => (c.chapterNumber === updated.chapterNumber ? updated : c)))
    if (selectedId.current) void loadJobDetails(selectedId.current)
  }, [loadJobDetails])

  const sendConceptToCreate = useCallback((concept: ConceptPrefill) => {
    setPrefill(concept)
    navigate('create')
  }, [])

  const activeJob = jobDetail?.id === activeJobId ? jobDetail : jobs.find((j) => j.id === activeJobId) ?? null

  const value = useMemo<StudioValue>(() => ({
    service, jobs, activeJobId, activeJob, chapters, facts, repairLogs, sseLogs, sseConnected,
    selectJob, refreshJobs, loadJobDetails, clearLogs: () => setSseLogs([]),
    onJobCreated, updateJobConfig, resumeJob, pauseJob, extendJob, replaceChapter,
    prefill, sendConceptToCreate, consumePrefill: () => setPrefill(null),
  }), [service, jobs, activeJobId, activeJob, chapters, facts, repairLogs, sseLogs, sseConnected,
    selectJob, refreshJobs, loadJobDetails, onJobCreated, updateJobConfig, resumeJob, pauseJob, extendJob, replaceChapter,
    prefill, sendConceptToCreate])

  return <StudioContext.Provider value={value}>{children}</StudioContext.Provider>
}
