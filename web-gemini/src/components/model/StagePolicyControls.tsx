import type {
  BudgetProfile,
  LLMProvider,
  StageBudgets,
  StageThinkingBucket,
  StageThinkingLevel,
  StageThinkingMap,
  ThinkingMode,
} from '../../lib/types'
import { Disclosure, Field, Input, Select } from '../ui'

const OPENAI_THINKING_MODEL_RE = /^(?:gemini-(?:2\.5|3(?:\.\d+)?|4(?:\.\d+)?|pro-latest|flash-latest|flash-lite-latest)|o[134]|gpt-[5-9])(?:-|\.|$)/i
const OPENAI_GEMINI_RE = /^gemini-(?:2\.5|3(?:\.\d+)?|4(?:\.\d+)?|pro-latest|flash-latest|flash-lite-latest)(?:-|$)/i
const ARGON_MODEL_RE = /^gemini-4-argon(?:-|$)/i

const STAGE_THINKING_ROWS: Array<{ key: StageThinkingBucket; label: string }> = [
  { key: 'book_plan', label: '全书总纲' },
  { key: 'chapter_outlines', label: '滚纲 / 细纲' },
  { key: 'chapter_writing', label: '写正文 / 返修' },
  { key: 'chapter_verify', label: '审校 / 局部回修' },
  { key: 'archive', label: '事实提取 / 归档' },
  { key: 'full_audit', label: '全书终审' },
]

const STAGE_BUDGET_ROWS: Array<{ key: keyof StageBudgets; label: string; balanced: number; longBook: number }> = [
  { key: 'chapter', label: '写章输入', balanced: 16384, longBook: 24576 },
  { key: 'arc', label: '滚纲输入', balanced: 16384, longBook: 24576 },
  { key: 'verify', label: '写中审校', balanced: 14336, longBook: 18432 },
  { key: 'repair', label: '章节返修', balanced: 12288, longBook: 16384 },
  { key: 'audit', label: '全书终审', balanced: 24576, longBook: 40960 },
]

export function supportsThinkingPolicy(provider: LLMProvider | string, model?: string, lightModel?: string): boolean {
  if (provider === 'deepseek' || provider === 'vertex' || provider === 'vertex-new') return true
  if (provider === 'openai' || provider === 'openai-compatible') {
    return OPENAI_THINKING_MODEL_RE.test(model ?? '') || OPENAI_THINKING_MODEL_RE.test(lightModel ?? '')
  }
  return false
}

export function cleanStageThinking(raw?: StageThinkingMap): StageThinkingMap | undefined {
  if (!raw) return undefined
  const out: StageThinkingMap = {}
  for (const { key } of STAGE_THINKING_ROWS) {
    const val = raw[key]
    if (val && val !== 'auto') out[key] = val
  }
  return Object.keys(out).length ? out : undefined
}

export function cleanStageBudgets(raw?: StageBudgets): StageBudgets | undefined {
  if (!raw) return undefined
  const out: StageBudgets = {}
  for (const { key } of STAGE_BUDGET_ROWS) {
    const n = raw[key]
    if (typeof n === 'number' && Number.isFinite(n) && (n === 0 || (n > 1024 && n <= 200000))) {
      out[key] = Math.round(n)
    }
  }
  return Object.keys(out).length ? out : undefined
}

export function StagePolicyControls({
  provider,
  model,
  lightModel,
  disabled,
  thinkingMode,
  onThinkingModeChange,
  stageThinking,
  onStageThinkingChange,
  budgetProfile,
  onBudgetProfileChange,
  stageBudgets,
  onStageBudgetsChange,
}: {
  provider: LLMProvider
  model?: string
  lightModel?: string
  disabled?: boolean
  thinkingMode: ThinkingMode
  onThinkingModeChange: (value: ThinkingMode) => void
  stageThinking: StageThinkingMap
  onStageThinkingChange: (value: StageThinkingMap) => void
  budgetProfile: BudgetProfile
  onBudgetProfileChange: (value: BudgetProfile) => void
  stageBudgets: StageBudgets
  onStageBudgetsChange: (value: StageBudgets) => void
}) {
  const showThinking = supportsThinkingPolicy(provider, model, lightModel)
  const isOpenAiGemini =
    (provider === 'openai' || provider === 'openai-compatible')
    && (OPENAI_GEMINI_RE.test(model ?? '') || OPENAI_GEMINI_RE.test(lightModel ?? ''))
  const isArgon = ARGON_MODEL_RE.test(model ?? '')

  const thinkingHint = isArgon
    ? '已针对 gemini-4-argon 自动校准：总纲 / 滚纲 / 终审及重考据正文自动映射 high 档，审校 low，提取与归档压至 minimal。'
    : isOpenAiGemini
      ? '默认分阶段控档：总纲 / 滚纲 / 终审 medium，重考据正文自动开启，审校 low，事实提取与归档 minimal。'
      : '重算术 / 考据题材正文自动开启思考；如需为总纲、终审或归档单独指定档位，可在下方微调。'

  const updateBudget = (key: keyof StageBudgets, text: string) => {
    const next = { ...stageBudgets }
    if (!text.trim()) {
      delete next[key]
    } else {
      const n = Number(text)
      if (Number.isFinite(n)) next[key] = Math.round(n)
    }
    onStageBudgetsChange(next)
  }

  return (
    <div className="grid gap-4">
      {showThinking && (
        <div className="grid gap-2.5">
          <Field label="分阶段思考策略" hint={thinkingHint}>
            {(id) => (
              <Select
                id={id}
                value={thinkingMode}
                onChange={(e) => onThinkingModeChange(e.target.value as ThinkingMode)}
                disabled={disabled}
              >
                <option value="auto">分阶段自动（推荐）</option>
                <option value="enabled">核心阶段全部开启</option>
                <option value="disabled">全部关闭 / 最低档</option>
              </Select>
            )}
          </Field>
          <Disclosure title="按阶段微调思考档位（可选）">
            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
              {STAGE_THINKING_ROWS.map(({ key, label }) => (
                <Field key={key} label={label}>
                  {(id) => (
                    <Select
                      id={id}
                      value={stageThinking[key] ?? 'auto'}
                      disabled={disabled}
                      onChange={(e) => onStageThinkingChange({
                        ...stageThinking,
                        [key]: e.target.value as StageThinkingLevel,
                      })}
                    >
                      <option value="auto">跟随全局</option>
                      {isOpenAiGemini || provider === 'openai' ? (
                        <>
                          <option value="minimal">关闭 (minimal)</option>
                          <option value="low">低档 (low)</option>
                          <option value="medium">中档 (medium)</option>
                          <option value="high">高档 (high)</option>
                        </>
                      ) : (
                        <>
                          <option value="off">关闭思考</option>
                          <option value="medium">开启思考</option>
                        </>
                      )}
                    </Select>
                  )}
                </Field>
              ))}
            </div>
          </Disclosure>
        </div>
      )}

      <div className="grid gap-2.5">
        <Field
          label="分阶段输入预算"
          hint={
            budgetProfile === 'long_book'
              ? '适合 100–200+ 章超长篇与 Gemini 1M 模型：写章/滚纲 24K、审校 18K、返修 16K、终审 40K。'
              : budgetProfile === 'custom'
                ? '单位 tokens（>1024，上限 200,000；留空沿用标准均衡，填 0 不设上限）。'
                : '标准均衡：写章/滚纲 16K、审校 14K、返修 12K、终审 24K（高信噪比，防长上下文稀释）。'
          }
        >
          {(id) => (
            <Select
              id={id}
              value={budgetProfile}
              onChange={(e) => onBudgetProfileChange(e.target.value as BudgetProfile)}
              disabled={disabled}
            >
              <option value="balanced">标准均衡（16K · 终审 24K）</option>
              <option value="long_book">长篇扩容（24K · 终审 40K）</option>
              <option value="custom">自定义各阶段预算</option>
            </Select>
          )}
        </Field>
        {budgetProfile === 'custom' && (
          <div className="grid grid-cols-1 gap-2.5 rounded-md bg-surface-2 p-3 sm:grid-cols-2">
            {STAGE_BUDGET_ROWS.map(({ key, label, balanced }) => (
              <Field key={key} label={label} className={key === 'audit' ? 'sm:col-span-2' : undefined}>
                {(id) => (
                  <Input
                    id={id}
                    type="number"
                    min={0}
                    max={200000}
                    step={1024}
                    disabled={disabled}
                    placeholder={`默认 ${balanced}`}
                    value={stageBudgets[key] ?? ''}
                    onChange={(e) => updateBudget(key, e.target.value)}
                  />
                )}
              </Field>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
