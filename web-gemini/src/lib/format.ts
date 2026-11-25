import type { JobErrorObject, JobStatus } from './types'

export const numberFormat = new Intl.NumberFormat('zh-CN')

export function formatNumber(value: number | null | undefined): string {
  return numberFormat.format(Math.round(value ?? 0))
}

/** 12345 -> "1.2 万" */
export function formatWords(value: number | null | undefined): string {
  const n = value ?? 0
  if (n >= 10_000) return `${(n / 10_000).toFixed(n >= 100_000 ? 0 : 1)} 万`
  return formatNumber(n)
}

export function formatTokens(value: number | null | undefined): string {
  const n = value ?? 0
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

export function formatDuration(ms: number | null | undefined): string {
  const s = Math.round((ms ?? 0) / 1000)
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分 ${s % 60} 秒`
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const t = new Date(value).getTime()
  if (Number.isNaN(t)) return value
  const diff = (Date.now() - t) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  return formatDateTime(value)
}

export function errorText(err: string | JobErrorObject | null | undefined): string {
  if (!err) return ''
  if (typeof err === 'string') return err
  return err.message || err.code || JSON.stringify(err)
}

export function messageOf(err: unknown, fallback = '操作失败'): string {
  return err instanceof Error ? err.message : typeof err === 'string' ? err : fallback
}

export type Tone = 'neutral' | 'primary' | 'success' | 'warning' | 'danger'

export const JOB_STATUS: Record<JobStatus, { label: string; tone: Tone; live?: boolean }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '创作中', tone: 'primary', live: true },
  paused: { label: '已暂停', tone: 'warning' },
  interrupted: { label: '已中断', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  error: { label: '异常', tone: 'danger' },
}

export function jobStatusMeta(status: JobStatus | string | undefined) {
  return JOB_STATUS[(status ?? 'pending') as JobStatus] ?? { label: status ?? '未知', tone: 'neutral' as Tone }
}

/* ---------------- Unified copy dictionaries ---------------- */

export const READINESS_META: Record<string, { label: string; tone: Tone }> = {
  ready_to_submit: { label: '可准备投稿', tone: 'success' },
  revise_recommended: { label: '建议精修', tone: 'warning' },
  major_rework: { label: '建议大修结构', tone: 'danger' },
}

export const PRIORITY_META: Record<'P0' | 'P1' | 'P2', { label: string; short: string; tone: Tone }> = {
  P0: { label: '必须修', short: 'P0', tone: 'danger' },
  P1: { label: '建议改', short: 'P1', tone: 'warning' },
  P2: { label: '可打磨', short: 'P2', tone: 'neutral' },
}

export const FORESHADOW_STATUS_LABEL: Record<string, string> = {
  planted: '已埋设',
  reinforced: '已强化',
  paid_off: '已回收',
  open: '待回收',
  pending: '待回收',
  resolved: '已回收',
}

export const ISSUE_TYPE_LABEL: Record<string, string> = {
  injury_contradiction: '伤势矛盾',
  secret_leak: '越权知情',
  ai_flavor: 'AI 表达痕迹',
  item_contradiction: '道具位置冲突',
  item_location: '道具位置冲突',
  timeline_contradiction: '时间线冲突',
  timeline_conflict: '时间线冲突',
  character_ooc: '人物言行偏差',
  pov_violation: '视角越界',
  placeholder: '占位符残留',
  word_count: '字数偏差',
  style_rule: '文风问题',
  canon_conflict: '设定冲突',
  continuity: '前后衔接问题',
  fact_conflict: '事实冲突',
  issue: '待核对问题',
}

export function issueTypeLabel(raw: string | undefined): string {
  if (!raw) return '待核对问题'
  return ISSUE_TYPE_LABEL[raw] ?? ISSUE_TYPE_LABEL[raw.toLowerCase()] ?? raw
}

export interface FriendlyErrorInfo {
  title: string
  summary: string
  detail?: string
}

/**
 * Turns raw backend / upstream provider errors into calm, actionable Chinese
 * while tucking raw JSON / HTTP stack details behind an optional disclosure.
 */
export function friendlyError(err: string | JobErrorObject | null | undefined): FriendlyErrorInfo {
  const raw = errorText(err).trim()
  const code = typeof err === 'object' && err ? (err.code ?? '') : ''
  if (!raw) return { title: '遇到一点问题', summary: '本次操作未完成，可稍后重试。' }

  const jsonStart = raw.search(/\{\s*"error"|HTTP\s*\d{3}|finishReason|Internal server error|AI Studio/i)
  const cleanHead = jsonStart > 6 ? raw.slice(0, jsonStart).replace(/[:：(\s]+$/, '').trim() : ''
  const hasRawTech = jsonStart >= 0 || /[{}]|error_code|status=\d|Traceback/i.test(raw)

  if (code === 'CONTENT_BLOCKED' || /内容安全拦截|content policy|blocked by Gemini|content_filter|PROHIBITED_CONTENT/i.test(raw)) {
    return {
      title: '触发模型内容安全审核',
      summary: '当前段落或章纲触发了上游模型的安全限制，系统已自动停在安全断点（不会触发熔断）。可以微调本章章纲措辞，或切换其他模型继续。',
      detail: raw,
    }
  }
  if (code === 'QUOTA_EXCEEDED' || /额度已耗尽|quota|rate limit.*hour/i.test(raw)) {
    return {
      title: '模型额度已达上限',
      summary: '当前模型账号的配额暂时用完，已保留当前断点。切换其他可用模型即可立刻继续，或等待额度恢复。',
      detail: hasRawTech ? raw : undefined,
    }
  }
  if (code === 'CIRCUIT_OPEN' || /连接已熔断/.test(raw)) {
    return {
      title: '模型服务暂时繁忙',
      summary: cleanHead || '上游连接连续异常，系统已自动冷却保护。等待片刻后继续，或切换到其他已配置模型。',
      detail: hasRawTech ? raw : undefined,
    }
  }
  if (code === 'OUTLINE_PARSE_FAILED' || /章纲未通过校验/.test(raw)) {
    return {
      title: '章纲格式未通过检查',
      summary: cleanHead || '模型返回的章节细纲结构不完整，已安全保存断点，直接点「继续创作」即可重新生成本段细纲。',
      detail: hasRawTech ? raw : undefined,
    }
  }
  if (code === 'EMPTY_RESPONSE' || /returned empty content|返回空内容/i.test(raw)) {
    return {
      title: '模型本次未返回正文',
      summary: '上游模型返回了空白结果，已保留断点，点击「继续创作」或更换模型重试即可。',
      detail: raw,
    }
  }
  if (code === 'TIMEOUT' || /超时|timeout/i.test(raw)) {
    return {
      title: '模型响应超时',
      summary: '本次生成耗时超过等待上限，已保存当前进度，可直接从断点继续。',
      detail: hasRawTech ? raw : undefined,
    }
  }
  if (code === 'CONFIG_REQUIRED' || /API Key|API 地址/.test(raw)) {
    return {
      title: '模型连接尚未配置完整',
      summary: cleanHead || raw,
      detail: hasRawTech ? raw : undefined,
    }
  }
  if (hasRawTech && cleanHead) {
    return { title: '任务已暂停在安全断点', summary: cleanHead, detail: raw }
  }
  if (hasRawTech) {
    return { title: '上游模型服务返回异常', summary: '本次请求未能成功完成，已保留断点，可稍后重试或切换模型。', detail: raw }
  }
  return { title: '任务已暂停在安全断点', summary: raw }
}
