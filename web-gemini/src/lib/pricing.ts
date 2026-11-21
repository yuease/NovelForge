import type { JobRecord, LLMProvider, TokenUsageSummary } from './types'

// 2026-11 spot reference exchange rate (1 USD ≈ 6.67 CNY)
export const USD_TO_CNY = 6.67

export interface ModelRateSpec {
  id: string
  label: string
  inputCnyPer1M: number
  cacheReadCnyPer1M: number
  outputCnyPer1M: number
  currencyBasis: 'USD' | 'CNY'
  rateNote: string
  isApproximate?: boolean
}

function usdRate(
  id: string,
  label: string,
  inputUsd: number,
  outputUsd: number,
  cacheReadUsd: number,
  rateNote: string,
  isApproximate = false,
): ModelRateSpec {
  return {
    id,
    label,
    inputCnyPer1M: inputUsd * USD_TO_CNY,
    cacheReadCnyPer1M: cacheReadUsd * USD_TO_CNY,
    outputCnyPer1M: outputUsd * USD_TO_CNY,
    currencyBasis: 'USD',
    rateNote: `${rateNote} ($${inputUsd}/$${outputUsd}/1M)`,
    isApproximate,
  }
}

/**
 * DeepSeek Beijing peak hours: Mon-Fri 09:00-12:00 & 14:00-18:00 UTC+8.
 */
export function isDeepSeekPeakBeijingTime(now = new Date()): boolean {
  const utcMs = now.getTime() + now.getTimezoneOffset() * 60_000
  const bj = new Date(utcMs + 8 * 3600_000)
  const day = bj.getUTCDay()
  if (day === 0 || day === 6) return false
  const hour = bj.getUTCHours()
  return (hour >= 9 && hour < 12) || (hour >= 14 && hour < 18)
}

export function resolveModelAlias(modelName?: string, provider?: LLMProvider): string {
  const raw = String(modelName || '').trim().toLowerCase()
  if (!raw) {
    if (provider === 'vertex') return 'gemini-3.8-flash'
    if (provider === 'gemini') return 'gemini-2.5-flash'
    if (provider === 'deepseek') return 'deepseek-flash'
    if (provider === 'anthropic') return 'claude-sonnet'
    if (provider === 'openai') return 'gpt-4o'
    if (provider === 'mock') return 'mock'
    return ''
  }
  if (raw === '4a' || raw.includes('argon')) return 'gemini-4-argon'
  if (raw === '4h' || raw.includes('hydrogen')) return 'gemini-4-hydrogen'
  if (raw === '3.8f' || raw.includes('3.8-flash')) return 'gemini-3.8-flash'
  if (raw.includes('3.5-flash-lite') || raw.includes('flash-lite')) return 'gemini-3.5-flash-lite'
  if (raw.includes('3.5-flash')) return 'gemini-3.5-flash'
  if (raw.includes('3.1-pro')) return 'gemini-3.1-pro-preview'
  if (raw.includes('2.5-pro')) return 'gemini-2.5-pro'
  if (raw.includes('2.5-flash')) return 'gemini-2.5-flash'
  if (raw.includes('deepseek-v4-pro') || raw.includes('reasoner')) return 'deepseek-v4-pro'
  if (raw.includes('deepseek-flash') || raw.includes('deepseek-chat')) return 'deepseek-flash'
  if (raw.includes('sonnet')) return 'claude-sonnet'
  if (raw.includes('gpt-4o')) return 'gpt-4o'
  if (raw.includes('mock')) return 'mock'
  return raw
}

export function getOfficialModelRate(
  modelName?: string,
  provider?: LLMProvider,
  now = new Date(),
): ModelRateSpec {
  if (provider === 'mock') {
    return {
      id: 'mock',
      label: 'Mock 调试',
      inputCnyPer1M: 0,
      cacheReadCnyPer1M: 0,
      outputCnyPer1M: 0,
      currencyBasis: 'CNY',
      rateNote: 'Mock 本地免费模拟',
    }
  }

  const canonical = resolveModelAlias(modelName, provider)
  const ymd = now.toISOString().slice(0, 10)

  switch (canonical) {
    case 'mock':
      return {
        id: 'mock',
        label: 'Mock 调试',
        inputCnyPer1M: 0,
        cacheReadCnyPer1M: 0,
        outputCnyPer1M: 0,
        currencyBasis: 'CNY',
        rateNote: 'Mock 本地免费模拟',
      }

    case 'gemini-4-argon': {
      // Introductory rate $2/$10 through 2026-11-30; Standard $4/$20 starting 2026-12-01
      const isIntro = ymd <= '2026-11-30'
      return isIntro
        ? usdRate('gemini-4-argon', 'Gemini 4 Argon', 2.0, 10.0, 0.2, '官方首发优惠价')
        : usdRate('gemini-4-argon', 'Gemini 4 Argon', 4.0, 20.0, 0.4, '官方标准价')
    }

    case 'gemini-3.8-flash': {
      // Introductory rate $0.75/$3.75 through 2026-12-31; Standard $1.50/$7.50 starting 2027-01-01
      const isIntro = ymd <= '2026-12-31'
      return isIntro
        ? usdRate('gemini-3.8-flash', 'Gemini 3.8 Flash', 0.75, 3.75, 0.075, '官方优惠价')
        : usdRate('gemini-3.8-flash', 'Gemini 3.8 Flash', 1.5, 7.5, 0.15, '官方标准价')
    }

    case 'gemini-4-hydrogen':
      return usdRate('gemini-4-hydrogen', 'Gemini 4 Hydrogen', 1.5, 7.5, 0.075, 'Vertex Global 官方价')

    case 'gemini-3.1-pro-preview':
      return usdRate('gemini-3.1-pro-preview', 'Gemini 3.1 Pro Preview', 2.0, 12.0, 0.2, '官方 <=200K 档')

    case 'gemini-2.5-pro':
      return usdRate('gemini-2.5-pro', 'Gemini 2.5 Pro', 1.25, 10.0, 0.125, '官方 <=200K 档')

    case 'gemini-2.5-flash':
    case 'gemini-3.5-flash-lite':
      return usdRate(canonical, canonical, 0.3, 2.5, 0.03, '官方 Flash-Lite 价')

    case 'gemini-3.5-flash':
      return usdRate('gemini-3.5-flash', 'Gemini 3.5 Flash', 1.5, 9.0, 0.15, '官方历史参考价')

    case 'deepseek-flash': {
      const peak = isDeepSeekPeakBeijingTime(now)
      return {
        id: 'deepseek-flash',
        label: 'DeepSeek Flash',
        inputCnyPer1M: peak ? 2.0 : 1.0,
        cacheReadCnyPer1M: peak ? 0.04 : 0.02,
        outputCnyPer1M: peak ? 8.0 : 4.0,
        currencyBasis: 'CNY',
        rateNote: peak ? 'DeepSeek 官网高峰价 (¥2/¥8/1M)' : 'DeepSeek 官网空闲半价 (¥1/¥4/1M)',
      }
    }

    case 'deepseek-v4-pro': {
      const peak = isDeepSeekPeakBeijingTime(now)
      return {
        id: 'deepseek-v4-pro',
        label: 'DeepSeek V4 Pro',
        inputCnyPer1M: peak ? 9.0 : 4.5,
        cacheReadCnyPer1M: peak ? 0.3 : 0.15,
        outputCnyPer1M: peak ? 27.0 : 13.5,
        currencyBasis: 'CNY',
        rateNote: peak ? 'DeepSeek 官网高峰价 (¥9/¥27/1M)' : 'DeepSeek 官网空闲半价 (¥4.5/¥13.5/1M)',
      }
    }

    case 'gpt-4o':
      return usdRate('gpt-4o', 'GPT-4o', 2.5, 10.0, 1.25, 'OpenAI 官网价')

    case 'claude-sonnet':
      return usdRate('claude-sonnet', 'Claude Sonnet', 2.0, 10.0, 0.1, 'Anthropic 官网价')

    default: {
      const fallbackId = provider === 'deepseek' ? 'deepseek-flash' : provider === 'anthropic' ? 'claude-sonnet' : 'gemini-3.8-flash'
      const base = getOfficialModelRate(fallbackId, provider, now)
      return {
        ...base,
        id: modelName || base.id,
        label: modelName || base.label,
        rateNote: `参考 ${base.label} 费率估算`,
        isApproximate: true,
      }
    }
  }
}

export function computeUsageCostCny(usage: Partial<TokenUsageSummary> | null | undefined, rate: ModelRateSpec): number {
  if (!usage) return 0
  const promptTotal = Math.max(0, Number(usage.promptTokens) || 0)
  const cacheHits = Math.min(promptTotal, Math.max(0, Number(usage.cacheHitTokens) || 0))
  const uncachedPrompt = Math.max(0, promptTotal - cacheHits)
  const completion = Math.max(0, Number(usage.completionTokens) || 0)

  return (
    (uncachedPrompt * rate.inputCnyPer1M +
      cacheHits * rate.cacheReadCnyPer1M +
      completion * rate.outputCnyPer1M) /
    1_000_000
  )
}

export interface JobCostEstimate {
  spentCny: number
  predictedTotalCny: number
  remainingCny: number
  primaryRate: ModelRateSpec
  auxRate: ModelRateSpec | null
  predictionBasis: 'mock' | 'completed' | 'measured_chapters' | 'word_budget'
  basisLabel: string
}

const SETUP_STAGES = new Set(['topic_selection', 'book_plan', 'chapter_outlines'])
const AUX_STAGES = new Set(['chapter_verify', 'fact_extraction'])

export function estimateJobCostCny(
  job: JobRecord | null | undefined,
  fallbackModel?: string,
  now = new Date(),
): JobCostEstimate {
  const provider = job?.llmConfig?.provider || 'mock'
  const primaryModelName = job?.llmConfig?.model || fallbackModel
  const lightModelName = job?.llmConfig?.lightModel
  const primaryRate = getOfficialModelRate(primaryModelName, provider, now)
  const auxRate =
    lightModelName && resolveModelAlias(lightModelName, provider) !== resolveModelAlias(primaryModelName, provider)
      ? getOfficialModelRate(lightModelName, provider, now)
      : null

  if (provider === 'mock') {
    return {
      spentCny: 0,
      predictedTotalCny: 0,
      remainingCny: 0,
      primaryRate,
      auxRate: null,
      predictionBasis: 'mock',
      basisLabel: 'Mock 调试免费',
    }
  }

  // 1. Compute actual spent cost so far
  let spentCny = 0
  const byModel = job?.tokenUsageByModel
  if (byModel && Object.keys(byModel).length > 0) {
    for (const [mName, bucket] of Object.entries(byModel)) {
      const mRate = getOfficialModelRate(mName, provider, now)
      spentCny += computeUsageCostCny(bucket, mRate)
    }
  } else if (job?.stageStats && Object.keys(job.stageStats).length > 0 && auxRate) {
    for (const [stageKey, st] of Object.entries(job.stageStats)) {
      const rate = AUX_STAGES.has(stageKey) ? auxRate : primaryRate
      spentCny += computeUsageCostCny(st, rate)
    }
  } else {
    spentCny = computeUsageCostCny(job?.tokenUsage, primaryRate)
  }

  if (job?.status === 'completed') {
    return {
      spentCny,
      predictedTotalCny: spentCny,
      remainingCny: 0,
      primaryRate,
      auxRate,
      predictionBasis: 'completed',
      basisLabel: '已完本 · 按官网费率实算',
    }
  }

  const totalChapters = Math.max(1, Number(job?.totalChapters) || 1)
  const completedChapters = Math.max(0, Number(job?.completedChapters) || 0)

  // 2. Project remaining cost
  let remainingCny = 0
  let predictionBasis: JobCostEstimate['predictionBasis'] = 'word_budget'
  let basisLabel = '按目标字数与调用密度估算'

  if (completedChapters >= 1 && (job?.tokenUsage?.totalTokens || 0) > 0) {
    // Separate one-time setup usage from chapter loop usage when stageStats is available
    let setupPrompt = 0
    let setupCompletion = 0
    let setupCache = 0
    if (job?.stageStats) {
      for (const [k, st] of Object.entries(job.stageStats)) {
        if (SETUP_STAGES.has(k)) {
          setupPrompt += Number(st.promptTokens) || 0
          setupCompletion += Number(st.completionTokens) || 0
          setupCache += Number(st.cacheHitTokens) || 0
        }
      }
    }

    const totPrompt = Math.max(0, Number(job?.tokenUsage?.promptTokens) || 0)
    const totCompletion = Math.max(0, Number(job?.tokenUsage?.completionTokens) || 0)
    const totCache = Math.max(0, Number(job?.tokenUsage?.cacheHitTokens) || 0)

    const chPrompt = Math.max(0, totPrompt - setupPrompt)
    const chCompletion = Math.max(0, totCompletion - setupCompletion)
    const chCache = Math.max(0, totCache - setupCache)

    const remainingChapters = Math.max(0, totalChapters - completedChapters)
    const scale = remainingChapters / completedChapters

    const remUsage: TokenUsageSummary = {
      promptTokens: chPrompt * scale,
      completionTokens: chCompletion * scale,
      totalTokens: (chPrompt + chCompletion) * scale,
      cacheHitTokens: chCache * scale,
    }

    if (auxRate) {
      // With lightModel enabled: ~45% tokens stay on primary model (chapter prose), ~55% run on lightModel (word adjust, verify, facts)
      const remPrimaryCost = computeUsageCostCny(remUsage, primaryRate) * 0.45
      const remAuxCost = computeUsageCostCny(remUsage, auxRate) * 0.55
      remainingCny = remPrimaryCost + remAuxCost
    } else {
      remainingCny = computeUsageCostCny(remUsage, primaryRate)
    }

    // Reserve ~1 average chapter on primaryModel for final full-book audit if not yet done
    if (!job?.stageStats?.full_audit) {
      remainingCny +=
        computeUsageCostCny(
          {
            promptTokens: chPrompt / completedChapters,
            completionTokens: chCompletion / completedChapters,
            cacheHitTokens: chCache / completedChapters,
          },
          primaryRate,
        ) * 0.5
    }

    predictionBasis = 'measured_chapters'
    basisLabel = `按已完 ${completedChapters} 章均耗推算`
  } else {
    const remainingChapters = Math.max(0, totalChapters - completedChapters)
    const targetWords = Math.max(2000, Number(job?.targetWords) || totalChapters * 2000)
    const wordsPerCh = targetWords / totalChapters

    // Per-chapter heuristic: ~22k prompt + ~4.8k output tokens (write + verify + fact extraction)
    const chPrompt = 22_000 * remainingChapters
    const chCompletion = wordsPerCh * 1.5 * 1.75 * remainingChapters

    if (auxRate) {
      remainingCny =
        computeUsageCostCny({ promptTokens: chPrompt * 0.35, completionTokens: chCompletion * 0.55 }, primaryRate) +
        computeUsageCostCny({ promptTokens: chPrompt * 0.65, completionTokens: chCompletion * 0.45 }, auxRate)
    } else {
      remainingCny = computeUsageCostCny({ promptTokens: chPrompt, completionTokens: chCompletion }, primaryRate)
    }
  }

  return {
    spentCny,
    predictedTotalCny: spentCny + remainingCny,
    remainingCny,
    primaryRate,
    auxRate,
    predictionBasis,
    basisLabel,
  }
}

export function formatCny(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '¥0.00'
  if (value < 0.01) return '<¥0.01'
  return `¥${value.toFixed(2)}`
}

