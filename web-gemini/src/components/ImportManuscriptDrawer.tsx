import { useId, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { FileUp } from 'lucide-react'
import { api } from '../lib/api'
import type { ManuscriptImportInput, ManuscriptImportResult, ManuscriptSplitMode } from '../lib/api'
import { messageOf } from '../lib/format'
import { COLLAPSE_MOTION } from '../lib/motion'
import { Alert, Button, Drawer, Field, Input, Select, Textarea } from './ui'

const MAX_REQUEST_BYTES = 5 * 1024 * 1024
const DEFAULT_CHUNK_WORDS = 2500

function decodeTextFile(bytes: Uint8Array): string {
  if (bytes[0] === 0xFF && bytes[1] === 0xFE) {
    return new TextDecoder('utf-16le', { fatal: true }).decode(bytes)
  }
  if (bytes[0] === 0xFE && bytes[1] === 0xFF) {
    return new TextDecoder('utf-16be', { fatal: true }).decode(bytes)
  }

  const utf8 = new TextDecoder('utf-8', { fatal: true })
  if (bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) {
    return utf8.decode(bytes)
  }
  try {
    return utf8.decode(bytes)
  } catch {
    return new TextDecoder('gb18030', { fatal: true }).decode(bytes)
  }
}

export default function ImportManuscriptDrawer({
  open, onClose, onImported,
}: {
  open: boolean
  onClose: () => void
  onImported: (result: ManuscriptImportResult) => void
}) {
  const formId = useId()
  const fileInput = useRef<HTMLInputElement>(null)
  const [text, setText] = useState('')
  const [title, setTitle] = useState('')
  const [fileName, setFileName] = useState('')
  const [splitMode, setSplitMode] = useState<ManuscriptSplitMode>('auto')
  const [chunkWords, setChunkWords] = useState(String(DEFAULT_CHUNK_WORDS))
  const [reading, setReading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const busy = reading || importing

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0]
    event.currentTarget.value = ''
    if (!file || busy) return
    setError(null)
    if (!/\.txt$/i.test(file.name)) {
      setError('请选择 .txt 文件，或将正文粘贴到下方。')
      return
    }
    // UTF-16 text can use twice the space of the resulting UTF-8 payload.
    if (file.size > MAX_REQUEST_BYTES * 2) {
      setError('文件太大，请拆成较小的 TXT 后再导入。')
      return
    }
    setReading(true)
    try {
      const decoded = decodeTextFile(new Uint8Array(await file.arrayBuffer()))
      if (!decoded.trim()) {
        setError('TXT 文件中没有正文。')
        return
      }
      if (decoded.includes('\0')) {
        setError('无法识别这份 TXT 的编码。请另存为 UTF-8 后再试。')
        return
      }
      setText(decoded)
      setFileName(file.name)
    } catch {
      setError('读取 TXT 失败。请另存为 UTF-8 后再试，或直接粘贴正文。')
    } finally {
      setReading(false)
    }
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !text.trim()) return
    setError(null)
    const limit = Number(chunkWords)
    if (splitMode === 'chunk' && (!Number.isSafeInteger(limit) || limit < 1)) {
      setError('每章字数请输入正整数。')
      return
    }
    const input: ManuscriptImportInput = { text, title: title.trim() || undefined, splitMode }
    if (splitMode === 'chunk') input.chunkWords = limit
    if (new TextEncoder().encode(JSON.stringify(input)).byteLength > MAX_REQUEST_BYTES) {
      setError('正文超过单次导入上限（5 MB），请拆成较短的文本。')
      return
    }

    setImporting(true)
    try {
      const result = await api.importManuscript(input)
      setText('')
      setTitle('')
      setFileName('')
      setSplitMode('auto')
      setChunkWords(String(DEFAULT_CHUNK_WORDS))
      onImported(result)
    } catch (err) {
      setError(messageOf(err, '导入失败，请稍后重试。'))
    } finally {
      setImporting(false)
    }
  }

  const close = () => { if (!busy) onClose() }

  return (
    <Drawer
      open={open}
      onClose={close}
      title="导入 TXT 作品"
      description="上传 TXT 或粘贴正文。导入后可直接阅读，也可以开始评估和修订。"
      width={560}
      footer={(
        <>
          <Button onClick={close} disabled={busy}>取消</Button>
          <Button type="submit" form={formId} variant="gradient" loading={importing} disabled={reading || !text.trim()}>
            导入作品
          </Button>
        </>
      )}
    >
      <form id={formId} className="grid gap-4" onSubmit={(event) => void submit(event)}>
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <input ref={fileInput} type="file" accept=".txt,text/plain" hidden onChange={(event) => void upload(event)} />
          <Button variant="tonal" icon={<FileUp className="size-4" />} loading={reading} disabled={importing} onClick={() => fileInput.current?.click()}>
            选择 TXT 文件
          </Button>
          {fileName && <span className="min-w-0 break-all text-caption text-muted">{fileName}</span>}
        </div>
        <Field label="正文">
          {(id) => (
            <Textarea id={id} value={text} className="min-h-48" disabled={busy} placeholder="在此粘贴正文。上传 TXT 后也可在这里检查或编辑。"
              onChange={(event) => { setText(event.target.value); setError(null) }} />
          )}
        </Field>
        <Field label="作品名（可选）">
          {(id) => <Input id={id} value={title} disabled={busy} placeholder="留空则从文本识别" onChange={(event) => setTitle(event.target.value)} />}
        </Field>
        <Field label="分章方式">
          {(id) => (
            <Select id={id} value={splitMode} disabled={busy} onChange={(event) => setSplitMode(event.target.value as ManuscriptSplitMode)}>
              <option value="auto">自动识别章节标题</option>
              <option value="single">整篇作为一章</option>
              <option value="chunk">按字数分章</option>
            </Select>
          )}
        </Field>
        <AnimatePresence initial={false}>
          {splitMode === 'chunk' && (
            <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
              <Field label="每章目标字数">
                {(id) => <Input id={id} type="number" min={1} step={1} required value={chunkWords} disabled={busy} onChange={(event) => setChunkWords(event.target.value)} />}
              </Field>
            </motion.div>
          )}
        </AnimatePresence>
        <AnimatePresence initial={false}>
          {error && (
            <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
              <Alert tone="danger" onClose={() => setError(null)}>{error}</Alert>
            </motion.div>
          )}
        </AnimatePresence>
      </form>
    </Drawer>
  )
}
