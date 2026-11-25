import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../lib/api'
import { messageOf } from '../lib/format'
import type { ProviderCatalogEntry } from '../lib/types'
import { Alert, Button, Card, Dialog, Field, Select, useToast } from '../components/ui'

const usageUrl = 'https://chatgpt.com/settings/usage'
const resultMessage: Record<string, string> = {
  denied: '本次授权已取消。',
  failed: '无法完成 ChatGPT 授权，请重新登录。',
  unavailable: 'OpenAI 登录服务暂不可用，请稍后再试。',
  permission_required: '已登录，请授权 NovelForge 使用你的 ChatGPT 计划。',
  storage_error: '本机 ChatGPT 凭据无法读取。',
}

export default function ChatGPTConnection({ entry, result, onUpdate }: {
  entry: ProviderCatalogEntry
  result: string | null
  onUpdate: (entry: ProviderCatalogEntry) => void
}) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [model, setModel] = useState('')
  const [message, setMessage] = useState(result ? resultMessage[result] || '' : '')
  const [quotaLimited, setQuotaLimited] = useState(false)
  const [welcome, setWelcome] = useState(false)
  const seenReturn = useRef(false)
  const chosen = entry.models.includes(model) ? model : entry.selectedModel
  const reconnect = entry.oauthState === 'reauthorize'
  const needsConsent = entry.oauthState === 'permission_required'

  const run = async (operation: () => Promise<void>) => {
    setBusy(true)
    setMessage('')
    try {
      await operation()
    } catch (error) {
      setMessage(messageOf(error, 'ChatGPT 操作未完成。'))
      if (error instanceof ApiError && error.code === 'SIWC_USAGE_LIMIT') setQuotaLimited(true)
    } finally {
      setBusy(false)
    }
  }
  const discover = async () => {
    const { provider } = await api.discoverProviderModels(entry.providerId)
    onUpdate(provider)
    setMessage(provider.models.length ? `已同步 ${provider.models.length} 个模型。` : '当前账号暂未返回可用文本模型。')
  }

  useEffect(() => {
    if (seenReturn.current) return
    seenReturn.current = true
    void api.siwcStatus().then((status) => setQuotaLimited(status.quotaLimited)).catch(() => {})
    if (!result) return
    window.history.replaceState(window.history.state, '', '#/models')
    if (result !== 'connected') return
    if (entry.profileId && localStorage.getItem(`siwc-welcome:${entry.profileId}`) !== '1') setWelcome(true)
    void run(discover)
  }, [])

  const closeWelcome = () => {
    if (entry.profileId) localStorage.setItem(`siwc-welcome:${entry.profileId}`, '1')
    setWelcome(false)
  }

  const login = (mode: 'login' | 'consent' | 'switch') => run(async () => {
    const { url } = await api.startSiwc(mode)
    window.location.assign(url)
  })
  const signOut = () => run(async () => {
    const status = await api.signOutSiwc()
    onUpdate({
      ...entry, configured: false, models: [], modelLabels: {}, selectedModel: '',
      oauthState: status.state, accountLabel: status.accountLabel, profileId: status.profileId,
      catalogVersion: (entry.catalogVersion ?? 0) + 1,
    })
    setQuotaLimited(false)
    setMessage(status.revocationConfirmed ? '已断开 ChatGPT 连接。'
      : '已清除本机凭据，远端撤销未确认。可在 ChatGPT 设置中断开此应用。')
  })

  return (
    <Card padding="lg" as="section" aria-label="ChatGPT 订阅">
      <h2 className="font-display text-headline text-ink">ChatGPT 订阅</h2>
      <p className="mt-2 text-body text-muted">授权后，总编可以使用你账号中符合条件的 ChatGPT 计划用量。</p>
      {entry.accountLabel && <p className="mt-3 break-all text-body text-ink">{entry.configured ? '当前账号' : '上次登录'} · {entry.accountLabel}</p>}
      <div className="my-5 flex flex-wrap items-center gap-2">
        {!entry.configured && <button type="button" disabled={busy} data-oai-siwc-button=""
          className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-black px-4 text-body font-medium text-white hover:bg-neutral-800 disabled:opacity-50"
          aria-label={needsConsent ? '授权使用 ChatGPT 计划' : 'Continue with ChatGPT，使用 ChatGPT 登录'}
          onClick={() => void login(needsConsent ? 'consent' : 'login')}>
          <img src="/chatgpt-sign-in.svg" alt="" className="size-5" />
          {needsConsent ? '授权使用 ChatGPT 计划' : reconnect ? '重新登录 ChatGPT' : 'Continue with ChatGPT'}
        </button>}
        {entry.configured && <Button size="sm" disabled={busy} data-oai-siwc-button="" onClick={() => void login('login')}>重新登录</Button>}
        {(entry.configured || entry.accountLabel) && <Button size="sm" disabled={busy}
          data-oai-siwc-button="" onClick={() => void login('switch')}>更换账号</Button>}
        {(entry.configured || needsConsent || reconnect) && <Button size="sm" disabled={busy}
          onClick={() => void signOut()}>断开连接</Button>}
        <a className="text-caption text-primary underline" target="_blank" rel="noreferrer" href={usageUrl}>管理用量</a>
      </div>
      {entry.configured && <div className="grid gap-3">
        <Field label="默认模型" hint="切换后立即生效">
          {(id) => <Select id={id} value={chosen} onChange={(event) => {
            const next = event.target.value
            setModel(next)
            if (next && next !== entry.selectedModel) {
              void run(async () => {
                const { provider } = await api.saveModelProvider(entry.providerId, { selectedModel: next })
                onUpdate(provider)
                toast('默认模型已更新', 'success')
              })
            }
          }} disabled={!entry.models.length || busy}>
            {!entry.models.length && <option value="">请先同步模型</option>}
            {entry.models.map((slug) => <option key={slug} value={slug}>{entry.modelLabels?.[slug] || slug}</option>)}
          </Select>}
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={busy} onClick={() => void run(discover)}>同步目录</Button>
          <Button size="sm" disabled={busy || !chosen} onClick={() => void run(async () => {
            const test = await api.testProviderConnection({ provider: entry.providerId, model: chosen })
            setQuotaLimited(test.code === 'SIWC_USAGE_LIMIT')
            const text = test.ok ? '模型连接正常，已完成测试回复。' : test.error || '测试未完成。'
            setMessage(text)
            toast(text, test.ok ? 'success' : 'danger')
          })}>测试连接</Button>
        </div>
      </div>}
      {(message || quotaLimited) && <div className="mt-4">
        <Alert tone={quotaLimited ? 'warning' : 'neutral'}>{message || '已达到 ChatGPT 计划或应用的用量上限。'}</Alert>
      </div>}
      <Dialog open={welcome} onClose={closeWelcome} title="已启用 ChatGPT 计划"
        description="总编选择此渠道后，符合条件的请求会使用你的 ChatGPT 计划用量。"
        footer={<Button variant="primary" onClick={closeWelcome}>知道了</Button>}>
        <a className="text-body text-primary underline" target="_blank" rel="noreferrer" href={usageUrl}>查看和管理 ChatGPT 用量</a>
      </Dialog>
    </Card>
  )
}
