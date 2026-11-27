import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion, useScroll, useSpring } from 'motion/react'
import {
  ArrowLeft, ArrowRight, BookOpen, CheckCircle2, ChevronDown, Download, Edit3, FileText, Maximize2, Minimize2,
  Save, Search, Type, X,
} from 'lucide-react'
import { api } from '../lib/api'
import { cn } from '../lib/cn'
import { downloadFile } from '../lib/download'
import { COLLAPSE_MOTION, EASE_EXIT, EASE_OUT, SPRING_INDICATOR, TAB_MOTION } from '../lib/motion'
import { navigate } from '../lib/router'
import { formatNumber, messageOf } from '../lib/format'
import type { ChapterRecord } from '../lib/types'
import { useStudio } from '../state/studio'
import {
  Alert, Badge, Button, Card, Checkbox, EmptyState, IconButton, Input, PageHeader, Segmented, Select, useToast,
} from '../components/ui'

type FontSize = 'sm' | 'md' | 'lg'
const FONT: Record<FontSize, string> = { sm: 'text-[15px] leading-[1.9]', md: 'text-[17px] leading-[2]', lg: 'text-[20px] leading-[2.05]' }
const FONT_KEY = 'novel_forge_reader_font'

const chapterHeading = (c: ChapterRecord) => (c.title && /^第\s*\d+\s*章/.test(c.title) ? c.title : `第 ${c.chapterNumber} 章 ${c.title || ''}`.trim())

export default function ReaderPage() {
  const { activeJob: job, chapters, replaceChapter } = useStudio()
  const toast = useToast()
  const [selectedNum, setSelectedNum] = useState(1)
  const [query, setQuery] = useState('')
  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState('')
  const [draftContent, setDraftContent] = useState('')
  const [reverify, setReverify] = useState(false)
  const [recheckDownstream, setRecheckDownstream] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fontSize, setFontSize] = useState<FontSize>(() => (localStorage.getItem(FONT_KEY) as FontSize) || 'md')
  const [focus, setFocus] = useState(false)
  const [direction, setDirection] = useState(1)

  useEffect(() => { localStorage.setItem(FONT_KEY, fontSize) }, [fontSize])

  const sorted = useMemo(() => [...chapters].sort((a, b) => a.chapterNumber - b.chapterNumber), [chapters])
  useEffect(() => {
    if (sorted.length > 0 && !sorted.some((c) => c.chapterNumber === selectedNum)) setSelectedNum(sorted[0].chapterNumber)
  }, [sorted, selectedNum])
  const active = sorted.find((c) => c.chapterNumber === selectedNum) ?? null
  const position = sorted.findIndex((c) => c.chapterNumber === selectedNum)

  useEffect(() => { setEditing(false); setError(null) }, [job?.id, selectedNum])
  useEffect(() => { setFocus(false) }, [job?.id])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? sorted.filter((c) => `第${c.chapterNumber}章 ${c.title ?? ''}`.toLowerCase().includes(q)) : sorted
  }, [query, sorted])

  const select = (n: number) => {
    setDirection(n > selectedNum ? 1 : -1)
    setSelectedNum(n)
    if (!focus) document.getElementById('reader-chapter')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  const prev = position > 0 ? sorted[position - 1] : null
  const next = position >= 0 && position < sorted.length - 1 ? sorted[position + 1] : null

  // ← / → to turn pages (ignore form inputs, contentEditable, and browser modifier shortcuts)
  useEffect(() => {
    if (editing) return
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const target = e.target as HTMLElement | null
      if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
      if (e.key === 'ArrowLeft' && prev) {
        e.preventDefault()
        setDirection(-1)
        setSelectedNum(prev.chapterNumber)
        if (!focus) document.getElementById('reader-chapter')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      } else if (e.key === 'ArrowRight' && next) {
        e.preventDefault()
        setDirection(1)
        setSelectedNum(next.chapterNumber)
        if (!focus) document.getElementById('reader-chapter')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editing, focus, prev, next])

  const startEditing = () => {
    if (!active) return
    setDraftTitle(active.title ?? '')
    setDraftContent(active.content ?? '')
    setError(null)
    setEditing(true)
  }

  const save = async () => {
    if (!job || !active) return
    setSaving(true)
    setError(null)
    try {
      const res = await api.updateChapter(job.id, active.chapterNumber, {
        title: draftTitle.trim() || active.title,
        content: draftContent,
        ...(reverify ? { reverify: true } : {}),
        ...(recheckDownstream ? { recheckDownstream: true } : {}),
      })
      replaceChapter(res.chapter)
      setEditing(false)
      toast('章节已保存', 'success')
    } catch (err) {
      setError(messageOf(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  if (!job) {
    return (
      <>
        <PageHeader title="成稿与导出" description="阅读、修订和导出作品。" />
        <EmptyState icon={<BookOpen />} title="还没有选中作品" description="在「实时进度」中选择一部作品后，即可在这里阅读和导出。" action={<Button variant="primary" onClick={() => navigate('monitor')}>选择作品</Button>} />
      </>
    )
  }

  const title = job.title || job.bookPlan?.title || '未命名作品'
  const volumes = Array.isArray(job.bookPlan?.volumes) ? job.bookPlan.volumes : []

  return (
    <>
      <PageHeader
        eyebrow={job.genre}
        title={`《${title}》`}
        description={`已生成 ${sorted.length} / ${job.totalChapters} 章 · 共 ${formatNumber(job.totalWords)} 字`}
        actions={<ExportMenu jobId={job.id} bookTitle={title} completedChapters={sorted.length} totalChapters={job.totalChapters} />}
      />

      <div className="grid items-start gap-6 @[920px]/main:grid-cols-[300px_minmax(0,1fr)]">
        {/* TOC */}
        <Card padding="none" as="aside" className="@[920px]/main:sticky @[920px]/main:top-20">
          <div className="border-b border-line p-4">
            <Input leading={<Search />} type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="章号或标题" aria-label="搜索章节" className="rounded-full" />
          </div>
          {volumes.length > 0 && <Volumes volumes={volumes} />}
          <nav aria-label="章节目录" className="max-h-[300px] overflow-y-auto p-2 @[920px]/main:max-h-[calc(100vh-280px)]">
            {filtered.map((c) => {
              const sel = c.chapterNumber === selectedNum
              return (
                <button
                  key={c.chapterNumber}
                  type="button"
                  onClick={() => select(c.chapterNumber)}
                  aria-current={sel ? 'true' : undefined}
                  className={cn(
                    'relative flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left transition-[color,background-color,transform] duration-150 active:scale-[.985]',
                    sel ? 'text-primary' : 'text-body hover:bg-surface-2',
                  )}
                >
                  {sel && <motion.span layoutId="toc-active" className="absolute inset-0 rounded-md bg-primary-soft" transition={SPRING_INDICATOR} />}
                  <span className="relative min-w-0 flex-1">
                    <span className={cn('block truncate text-body', sel && 'font-medium')}>{chapterHeading(c)}</span>
                    <span className="block text-caption text-muted">
                      {formatNumber(c.wordCount ?? c.content?.length ?? 0)} 字{c.repairCount ? ` · 修复 ${c.repairCount} 次` : ''}
                    </span>
                  </span>
                  {c.verified !== false && <CheckCircle2 className="relative size-4 shrink-0 text-success" aria-label="已审校" />}
                </button>
              )
            })}
            {filtered.length === 0 && <p className="py-10 text-center text-caption text-muted">{sorted.length ? '没有符合条件的章节' : '暂无已生成的章节'}</p>}
          </nav>
        </Card>

        {/* Reader */}
        <Card as="article" padding="none" id="reader-chapter" className="scroll-mt-20 overflow-hidden">
          {active ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3 sm:px-8">
                <div className="flex items-center gap-2 text-caption text-muted">
                  <span className="tabular-nums">{position + 1} / {sorted.length}</span>
                  {active.verified !== false && <Badge tone="success" icon={<CheckCircle2 />}>已审校</Badge>}
                </div>
                <div className="flex items-center gap-1">
                  {!editing && (
                    <>
                      <Segmented
                        size="sm"
                        value={fontSize}
                        onChange={setFontSize}
                        ariaLabel="正文字号"
                        options={[
                          { value: 'sm', label: <Type className="size-3" /> },
                          { value: 'md', label: <Type className="size-3.5" /> },
                          { value: 'lg', label: <Type className="size-4" /> },
                        ]}
                      />
                      <IconButton label="专注阅读" size="sm" onClick={() => setFocus(true)}><Maximize2 className="size-4" /></IconButton>
                      <Button size="sm" variant="ghost" icon={<Edit3 className="size-4" />} onClick={startEditing}>编辑</Button>
                    </>
                  )}
                  {editing && (
                    <>
                      <Button size="sm" variant="ghost" icon={<X className="size-4" />} onClick={() => setEditing(false)}>取消</Button>
                      <Button size="sm" variant="primary" loading={saving} icon={<Save className="size-4" />} onClick={() => void save()}>保存</Button>
                    </>
                  )}
                </div>
              </div>

              <div className="px-5 py-8 sm:px-10 md:py-12">
                <AnimatePresence initial={false}>
                  {error && (
                    <motion.div {...COLLAPSE_MOTION} className="overflow-hidden">
                      <Alert tone="danger" className="mb-6" onClose={() => setError(null)}>{error}</Alert>
                    </motion.div>
                  )}
                </AnimatePresence>
                <AnimatePresence mode="wait" initial={false}>
                  {editing ? (
                    <motion.div key="editor-mode" {...TAB_MOTION} className="mx-auto grid max-w-[46rem] gap-4">
                      <input
                        value={draftTitle}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        placeholder="章节标题"
                        className="w-full border-0 border-b border-line bg-transparent pb-2 font-display text-headline text-ink transition-colors focus:border-primary focus:outline-none"
                      />
                      <textarea
                        rows={24}
                        value={draftContent}
                        onChange={(e) => setDraftContent(e.target.value)}
                        className={cn('w-full resize-y rounded-md bg-surface-2 p-5 font-serif text-ink transition-shadow focus:outline-none focus:ring-2 focus:ring-primary/30', FONT[fontSize])}
                      />
                      <div className="flex flex-wrap items-center justify-between gap-4">
                        <div className="flex flex-wrap gap-5">
                          <Checkbox checked={reverify} onChange={setReverify} label="保存后重新审校" />
                          <Checkbox checked={recheckDownstream} onChange={setRecheckDownstream} label="检查后续章节一致性" />
                        </div>
                        <span className="text-caption tabular-nums text-muted">{draftContent.length.toLocaleString()} 字</span>
                      </div>
                    </motion.div>
                  ) : (
                    <motion.div key="read-mode" {...TAB_MOTION}>
                      <AnimatePresence mode="wait" initial={false} custom={direction}>
                        <motion.div
                          key={active.chapterNumber}
                          custom={direction}
                          initial={{ opacity: 0, x: direction * 18 }}
                          animate={{ opacity: 1, x: 0, transition: { duration: 0.22, ease: EASE_OUT } }}
                          exit={{ opacity: 0, x: direction * -18, transition: { duration: 0.12, ease: EASE_EXIT } }}
                          className="mx-auto max-w-[42rem]"
                        >
                          <h2 className="mb-8 text-center font-serif text-headline font-normal text-ink">{chapterHeading(active)}</h2>
                          <Prose content={active.content} size={fontSize} />
                        </motion.div>
                      </AnimatePresence>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              {!editing && <PageTurner prev={prev} next={next} onSelect={select} />}
            </>
          ) : (
            <EmptyState className="m-6 border-0" icon={<FileText />} title="选择一章开始阅读" />
          )}
        </Card>
      </div>

      <FocusReader
        open={focus && !!active}
        onClose={() => setFocus(false)}
        chapter={active}
        chapters={sorted}
        size={fontSize}
        onSizeChange={setFontSize}
        bookTitle={title}
        direction={direction}
        prev={prev}
        next={next}
        onSelect={select}
      />
    </>
  )
}

function Prose({ content, size }: { content: string; size: FontSize }) {
  const paragraphs = useMemo(() => content.split(/\n+/).map((p) => p.trim()).filter(Boolean), [content])
  return (
    <div className={cn('font-serif tracking-[.02em] text-ink', FONT[size])}>
      {paragraphs.map((p, i) => <p key={i} className="mb-[1em] indent-[2em]">{p}</p>)}
    </div>
  )
}

function PageTurner({ prev, next, onSelect }: { prev: ChapterRecord | null; next: ChapterRecord | null; onSelect: (n: number) => void }) {
  return (
    <nav aria-label="章节翻页" className="grid grid-cols-2 border-t border-line">
      <button type="button" disabled={!prev} onClick={() => prev && onSelect(prev.chapterNumber)} className="group flex items-center gap-3 px-6 py-4 text-left transition-[background-color,transform] duration-150 hover:bg-surface-2 active:scale-[.99] disabled:pointer-events-none disabled:opacity-40">
        <ArrowLeft className="size-4 text-muted transition-transform duration-200 ease-[var(--ease-emphasized)] group-hover:-translate-x-1" />
        <span className="min-w-0"><span className="block text-caption text-muted">上一章</span><span className="block truncate text-body text-ink">{prev ? chapterHeading(prev) : '没有了'}</span></span>
      </button>
      <button type="button" disabled={!next} onClick={() => next && onSelect(next.chapterNumber)} className="group flex items-center justify-end gap-3 border-l border-line px-6 py-4 text-right transition-[background-color,transform] duration-150 hover:bg-surface-2 active:scale-[.99] disabled:pointer-events-none disabled:opacity-40">
        <span className="min-w-0"><span className="block text-caption text-muted">下一章</span><span className="block truncate text-body text-ink">{next ? chapterHeading(next) : '没有了'}</span></span>
        <ArrowRight className="size-4 text-muted transition-transform duration-200 ease-[var(--ease-emphasized)] group-hover:translate-x-1" />
      </button>
    </nav>
  )
}

function Volumes({ volumes }: { volumes: NonNullable<NonNullable<import('../lib/types').JobRecord['bookPlan']>['volumes']> }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border-b border-line">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center justify-between px-4 py-3 text-caption font-medium text-body transition-colors duration-150 hover:bg-surface-2">
        分卷 · {volumes.length} 卷
        <ChevronDown className={cn('size-4 transition-transform duration-200 ease-[var(--ease-emphasized)]', open && 'rotate-180')} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.ul {...COLLAPSE_MOTION} className="overflow-hidden px-4">
            {volumes.map((v, i) => {
              const volNo = v.volume ?? v.volumeNumber ?? i + 1
              const start = Array.isArray(v.chapters) ? v.chapters[0] : v.startChapter
              const end = Array.isArray(v.chapters) ? v.chapters[1] : v.endChapter
              return (
                <li key={i} className="pb-3 text-caption">
                  <span className="font-medium text-primary">第{volNo}卷 {v.title}</span>
                  {start && end && <span className="ml-1.5 text-muted">第{start}–{end}章</span>}
                  {v.summary && <p className="mt-0.5 line-clamp-2 text-muted">{v.summary}</p>}
                  {v.escalation?.pressure && (
                    <p title={v.escalation.pressure} className="mt-0.5 truncate text-[11px] text-body">压迫：{v.escalation.pressure}</p>
                  )}
                </li>
              )
            })}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  )
}

function ExportMenu({ jobId, bookTitle, completedChapters, totalChapters }: { jobId: string; bookTitle: string; completedChapters: number; totalChapters: number }) {
  const toast = useToast()
  const [downloading, setDownloading] = useState<'txt' | 'md' | 'docx' | null>(null)

  const handleExport = async (fmt: 'txt' | 'md' | 'docx', label: string) => {
    if (downloading) return
    setDownloading(fmt)
    try {
      await downloadFile(api.getExportUrl(jobId, fmt), `${bookTitle}.${fmt}`)
      const partial = totalChapters > 0 && completedChapters < totalChapters
        ? `（当前已完成的 ${completedChapters}/${totalChapters} 章）`
        : ''
      toast(`已导出 ${label}${partial}`, 'success')
    } catch (err) {
      toast(messageOf(err, `导出 ${label} 失败，请稍后重试`), 'danger')
    } finally {
      setDownloading(null)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="secondary"
        size="sm"
        loading={downloading === 'txt'}
        disabled={downloading !== null && downloading !== 'txt'}
        icon={<Download className="size-4" />}
        onClick={() => void handleExport('txt', 'TXT 正文')}
      >
        TXT
      </Button>
      <Button
        variant="secondary"
        size="sm"
        loading={downloading === 'md'}
        disabled={downloading !== null && downloading !== 'md'}
        icon={<FileText className="size-4" />}
        onClick={() => void handleExport('md', 'Markdown 文稿')}
      >
        Markdown
      </Button>
      <Button
        variant="gradient"
        size="sm"
        loading={downloading === 'docx'}
        disabled={downloading !== null && downloading !== 'docx'}
        icon={<Download className="size-4" />}
        title="番茄完本批量上传格式：标题1拆章、第1章 标题、无简介分隔线、单文件≤5MB"
        onClick={() => void handleExport('docx', '番茄投稿 DOCX')}
      >
        番茄投稿 DOCX
      </Button>
    </div>
  )
}

function FocusReader({
  open, onClose, chapter, chapters, size, onSizeChange, bookTitle, direction, prev, next, onSelect,
}: {
  open: boolean
  onClose: () => void
  chapter: ChapterRecord | null
  chapters: ChapterRecord[]
  size: FontSize
  onSizeChange: (s: FontSize) => void
  bookTitle: string
  direction: number
  prev: ChapterRecord | null
  next: ChapterRecord | null
  onSelect: (n: number) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const { scrollYProgress } = useScroll({ container: ref })
  const progress = useSpring(scrollYProgress, { stiffness: 320, damping: 36, restDelta: 0.001 })

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previousOverflow
    }
  }, [open, onClose])
  useEffect(() => { ref.current?.scrollTo({ top: 0 }) }, [chapter?.chapterNumber])

  return createPortal(
    <AnimatePresence>
      {open && chapter && (
        <motion.div
          role="dialog"
          aria-modal="true"
          aria-label="专注全屏阅读"
          initial={{ opacity: 0, scale: 0.985 }}
          animate={{ opacity: 1, scale: 1, transition: { duration: 0.24, ease: EASE_OUT } }}
          exit={{ opacity: 0, scale: 0.99, transition: { duration: 0.14, ease: EASE_EXIT } }}
          className="fixed inset-0 z-[80] bg-surface"
        >
          <motion.div className="nf-gradient-bg absolute inset-x-0 top-0 z-10 h-0.5 origin-left" style={{ scaleX: progress }} />
          <div className="absolute inset-x-0 top-0 z-10 flex flex-wrap items-center justify-between gap-3 border-b border-line/70 bg-surface/85 px-4 py-2.5 backdrop-blur sm:px-6">
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate text-body font-medium text-ink">《{bookTitle}》</span>
              <span className="hidden text-caption text-muted md:inline">· {chapterHeading(chapter)}</span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Select
                aria-label="快速选章"
                value={chapter.chapterNumber}
                onChange={(e) => onSelect(Number(e.target.value))}
                className="h-8 max-w-[200px] rounded-full text-caption"
              >
                {chapters.map((c) => (
                  <option key={c.chapterNumber} value={c.chapterNumber}>{chapterHeading(c)}</option>
                ))}
              </Select>
              <Segmented
                size="sm"
                value={size}
                onChange={onSizeChange}
                ariaLabel="正文字号"
                options={[
                  { value: 'sm', label: 'A-' },
                  { value: 'md', label: 'A' },
                  { value: 'lg', label: 'A+' },
                ]}
              />
              <span className="hidden text-caption text-muted lg:inline">← → 翻章 · Esc 退出</span>
              <Button size="sm" variant="ghost" icon={<Minimize2 className="size-4" />} onClick={onClose}>退出</Button>
            </div>
          </div>
          <div ref={ref} className="h-full overflow-y-auto px-6 pb-24 pt-24">
            <AnimatePresence mode="wait" initial={false} custom={direction}>
              <motion.div
                key={chapter.chapterNumber}
                custom={direction}
                initial={{ opacity: 0, x: direction * 18 }}
                animate={{ opacity: 1, x: 0, transition: { duration: 0.22, ease: EASE_OUT } }}
                exit={{ opacity: 0, x: direction * -18, transition: { duration: 0.12, ease: EASE_EXIT } }}
                className="mx-auto max-w-[42rem]"
              >
                <h2 className="mb-10 text-center font-serif text-[26px] font-normal text-ink">{chapterHeading(chapter)}</h2>
                <Prose content={chapter.content} size={size} />
                <div className="mt-16 flex items-center justify-between gap-4 border-t border-line pt-6">
                  <Button variant="ghost" disabled={!prev} icon={<ArrowLeft className="size-4" />} onClick={() => prev && onSelect(prev.chapterNumber)}>上一章</Button>
                  <span className="text-caption tabular-nums text-muted">{formatNumber(chapter.wordCount ?? chapter.content.length)} 字</span>
                  <Button variant="tonal" disabled={!next} iconRight={<ArrowRight className="size-4" />} onClick={() => next && onSelect(next.chapterNumber)}>下一章</Button>
                </div>
              </motion.div>
            </AnimatePresence>
          </div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
}
