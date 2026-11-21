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
