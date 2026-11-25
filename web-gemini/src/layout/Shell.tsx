import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  AudioLines,
  BookOpen,
  Check,
  ChevronDown,
  ChevronsLeft,
  ChevronsUpDown,
  Layers3,
  Lightbulb,
  Menu,
  Moon,
  PenLine,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Sun,
  WifiOff,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '../lib/cn'
import { formatWords, jobStatusMeta } from '../lib/format'
import { EASE_EXIT, EASE_OUT, PAGE_MOTION, SPRING_INDICATOR, SPRING_SHEET } from '../lib/motion'
import { navigate } from '../lib/router'
import type { PageKey } from '../lib/router'
import type { JobRecord } from '../lib/types'
import { useStudio } from '../state/studio'
import { useTheme } from '../state/prefs'
import { BrandMark } from '../components/brand/GeminiStar'
import { Alert, AnimatedNumber, Button, IconButton, PopoverSurface, ProgressRing } from '../components/ui'
import EditorDesk from '../pages/EditorDesk'

export interface NavItem { key: PageKey; label: string; icon: LucideIcon }

export const NAV_GROUPS: ReadonlyArray<{ name: string; items: ReadonlyArray<NavItem> }> = [
  {
    name: '创作',
    items: [
      { key: 'monitor', label: '实时进度', icon: AudioLines },
      { key: 'create', label: '新建作品', icon: PenLine },
      { key: 'hotspots', label: '热点灵感', icon: Lightbulb },
      { key: 'reader', label: '成稿与导出', icon: BookOpen },
    ],
  },
  {
    name: '质量与设置',
    items: [
      { key: 'facts', label: '跨章事实', icon: ShieldCheck },
      { key: 'evaluation', label: '作品评估', icon: Sparkles },
      { key: 'models', label: '模型管理', icon: Layers3 },
    ],
  },
]
export const NAV_ITEMS = NAV_GROUPS.flatMap((g) => g.items)
const PAGE_TITLES: Record<PageKey, string> = {
  monitor: '实时进度',
  create: '新建作品',
  hotspots: '热点灵感',
  reader: '成稿与导出',
  facts: '跨章事实',
  evaluation: '作品评估',
  models: '模型管理',
  acceptance: '系统自检',
}
const BOOK_SCOPED_PAGES = new Set<PageKey>(['monitor', 'reader', 'facts', 'evaluation'])

const COLLAPSE_KEY = 'novel_forge_sidebar_collapsed'
const EDITOR_KEY = 'novel_forge_editor_open'
const EDITOR_WIDTH_KEY = 'novel_forge_editor_width'

function loadEditorWidth(): number | null {
  try {
    const raw = Number(localStorage.getItem(EDITOR_WIDTH_KEY))
    if (Number.isFinite(raw) && raw >= 340 && raw <= 640) return Math.round(raw)
  } catch { /* ignore */ }
  return null
}

const titleOf = (job: JobRecord) => job.title || job.bookPlan?.title || '未命名作品'

export function Shell({ page, children }: { page: PageKey; children: ReactNode }) {
  const { service, refreshJobs, jobs, activeJob, selectJob } = useStudio()
  const { theme, toggle } = useTheme()
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === '1')
  const [mobileOpen, setMobileOpen] = useState(false)
  const [editorRequested, setEditorRequested] = useState(() => localStorage.getItem(EDITOR_KEY) === '1')
  const [editorWidth, setEditorWidth] = useState<number | null>(loadEditorWidth)
  const [editorResizing, setEditorResizing] = useState(false)
  const editorOpen = Boolean(editorRequested && activeJob)
  const pageTitle = PAGE_TITLES[page] ?? '工作台'

  useEffect(() => { document.title = `${pageTitle} · 墨枢` }, [pageTitle])
  useEffect(() => { setMobileOpen(false); window.scrollTo({ top: 0 }) }, [page])
  useEffect(() => {
    if (!mobileOpen) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMobileOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mobileOpen])

  const toggleCollapse = () => setCollapsed((c) => { localStorage.setItem(COLLAPSE_KEY, c ? '0' : '1'); return !c })
  const setEditorOpen = (open: boolean) => { setEditorRequested(open); localStorage.setItem(EDITOR_KEY, open ? '1' : '0') }
  const handleEditorWidth = useCallback((next: number | null) => {
    setEditorWidth(next)
    try {
      if (next === null) localStorage.removeItem(EDITOR_WIDTH_KEY)
      else localStorage.setItem(EDITOR_WIDTH_KEY, String(Math.round(next)))
    } catch { /* ignore */ }
  }, [])

  const editorWidthCss = editorWidth ? `${editorWidth}px` : 'clamp(350px,31vw,470px)'

  return (
    <div
      style={{ ['--editor-width' as string]: editorWidthCss }}
      className={cn('relative min-h-screen', editorResizing && 'select-none')}
    >
      <div className="nf-aura" aria-hidden><span /><span /><span /></div>

      {/* Desktop sidebar – locked to the exact same 240ms curve as the main content padding-left */}
      <aside
        style={{ width: collapsed ? 76 : 256 }}
        className="fixed inset-y-0 left-0 z-40 hidden flex-col border-r border-line/70 bg-canvas/82 backdrop-blur-xl transition-[width] duration-[240ms] ease-[var(--ease-standard)] lg:flex"
      >
        <SidebarContent page={page} collapsed={collapsed} navLayoutId="nav-active-desktop" onToggle={toggleCollapse} />
      </aside>

      {/* Mobile sidebar */}
      <AnimatePresence>
        {mobileOpen && (
          <div className="fixed inset-0 z-50 lg:hidden">
            <motion.div
              className="absolute inset-0 bg-black/30 backdrop-blur-[1.5px]"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.18, ease: EASE_OUT } }}
              exit={{ opacity: 0, transition: { duration: 0.15, ease: EASE_EXIT } }}
              onClick={() => setMobileOpen(false)}
            />
            <motion.aside
              initial={{ x: -272 }}
              animate={{ x: 0, transition: SPRING_SHEET }}
              exit={{ x: -272, transition: { duration: 0.19, ease: EASE_EXIT } }}
              className="absolute inset-y-0 left-0 flex w-[264px] flex-col bg-surface shadow-float"
            >
              <SidebarContent page={page} collapsed={false} navLayoutId="nav-active-mobile" onPickBook={() => setMobileOpen(false)} />
            </motion.aside>
          </div>
        )}
      </AnimatePresence>

      <div
        style={{ ['--sb' as string]: collapsed ? '76px' : '256px' }}
        className="relative z-10 min-h-screen transition-[padding-left] duration-[240ms] ease-[var(--ease-standard)] lg:pl-[var(--sb)]"
      >
        <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-line/50 bg-canvas/78 px-4 backdrop-blur-xl md:px-8">
          <IconButton label="打开导航" className="lg:hidden" onClick={() => setMobileOpen(true)}><Menu className="size-5" /></IconButton>
          <div className="flex min-w-0 items-center gap-2 text-body">
            <span className="hidden text-muted sm:inline">工作台</span>
            <span className="hidden text-line-strong sm:inline">/</span>
            <AnimatePresence mode="wait" initial={false}>
              <motion.span
                key={page}
                initial={{ opacity: 0, y: 5 }}
                animate={{ opacity: 1, y: 0, transition: { duration: 0.17, ease: EASE_OUT } }}
                exit={{ opacity: 0, y: -4, transition: { duration: 0.11, ease: EASE_EXIT } }}
                className="truncate font-medium text-ink"
              >
                {pageTitle}
              </motion.span>
            </AnimatePresence>
          </div>

          <div className="ml-auto flex items-center gap-1.5 sm:gap-3">
            {BOOK_SCOPED_PAGES.has(page) && jobs.length > 0 && (
              <TopBookSwitcher jobs={jobs} activeJob={activeJob} onSelect={selectJob} />
            )}
            <ServicePill service={service} />
            {activeJob && (
              <Button variant={editorOpen ? 'tonal' : 'secondary'} size="sm" aria-expanded={editorOpen} aria-controls="chief-editor-panel"
                icon={<Sparkles className="size-4" />} onClick={() => setEditorOpen(!editorOpen)}>问总编</Button>
            )}
            <IconButton label={theme === 'dark' ? '切换到浅色' : '切换到深色'} onClick={(e) => toggle(e)}>
              <AnimatePresence mode="wait" initial={false}>
                <motion.span
                  key={theme}
                  initial={{ rotate: -45, scale: 0.72, opacity: 0 }}
                  animate={{ rotate: 0, scale: 1, opacity: 1, transition: { duration: 0.18, ease: EASE_OUT } }}
                  exit={{ rotate: 45, scale: 0.72, opacity: 0, transition: { duration: 0.11, ease: EASE_EXIT } }}
                  className="inline-flex"
                >
                  {theme === 'dark' ? <Sun className="size-5" /> : <Moon className="size-5" />}
                </motion.span>
              </AnimatePresence>
            </IconButton>
            {page !== 'create' && (
              <Button variant="gradient" size="sm" icon={<Plus className="size-4" />} onClick={() => navigate('create')} className="max-sm:px-3">
                <span className="max-sm:hidden">新建作品</span>
              </Button>
            )}
          </div>
        </header>

        <div
          className={cn(
            !editorResizing && 'transition-[padding-right] duration-[260ms] ease-[var(--ease-standard)]',
            editorOpen && 'min-[1200px]:pr-[var(--editor-width)]',
          )}
        >
          <main id="workspace-content" className="@container/main mx-auto w-full max-w-[1440px] px-4 pb-16 pt-4 md:px-8">
            <AnimatePresence>
              {service === 'offline' && jobs.length === 0 && (
                <Alert
                  key="offline"
                  tone="warning"
                  title="暂时连不上工作台"
                  className="mb-6"
                  action={<Button size="sm" variant="secondary" icon={<RefreshCw className="size-3.5" />} onClick={() => void refreshJobs()}>重新连接</Button>}
                >
                  确认 Novel Forge 服务正在运行后重试。
                </Alert>
              )}
            </AnimatePresence>
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={page}
                initial={PAGE_MOTION.initial}
                animate={PAGE_MOTION.animate}
                exit={PAGE_MOTION.exit}
              >
                {children}
              </motion.div>
            </AnimatePresence>
          </main>
        </div>
      </div>
      {activeJob && (
        <EditorDesk
          open={editorOpen}
          onClose={() => setEditorOpen(false)}
          jobId={activeJob.id}
          title={titleOf(activeJob)}
          width={editorWidth}
          onWidthChange={handleEditorWidth}
          onResizingChange={setEditorResizing}
        />
      )}
    </div>
  )
}

function useMenuDismiss(open: boolean, ref: React.RefObject<HTMLDivElement | null>, onClose: () => void) {
  useEffect(() => {
    if (!open) return
    const onPointer = (e: PointerEvent) => {
      if (e.target instanceof Node && !ref.current?.contains(e.target)) onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('pointerdown', onPointer)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, ref, onClose])
}

function BookSwitcherMenu({
  open, jobs, activeJobId, onSelect, onClose, placement,
}: {
  open: boolean
  jobs: JobRecord[]
  activeJobId?: string | null
  onSelect: (id: string) => void
  onClose: () => void
  placement: 'top' | 'sidebar' | 'sidebar-collapsed'
}) {
  const [query, setQuery] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (open) {
      setQuery('')
      requestAnimationFrame(() => searchRef.current?.focus())
    }
  }, [open])

  const q = query.trim().toLowerCase()
  const shown = jobs.filter((j) => !q || `${titleOf(j)} ${j.genre || ''}`.toLowerCase().includes(q))

  const posClass = placement === 'top'
    ? 'right-0 top-full mt-2 w-[min(22rem,calc(100vw-2rem))]'
    : placement === 'sidebar-collapsed'
      ? 'bottom-0 left-full ml-2 w-72'
      : 'bottom-full left-0 right-0 mb-2 min-w-[240px]'

  const origin = placement === 'top' ? 'top-right' : placement === 'sidebar-collapsed' ? 'left-bottom' : 'bottom-left'

  return (
    <PopoverSurface
      open={open}
      origin={origin}
      role="dialog"
      ariaLabel="切换当前作品"
      className={cn(
        'absolute z-50 flex max-h-[min(60dvh,26rem)] flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-float',
        posClass,
      )}
    >
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-line px-3 py-2">
        <span className="text-caption font-medium text-muted">切换作品（留在当前页）</span>
        <span className="text-[11px] tabular-nums text-muted">{jobs.length} 本</span>
      </div>
      {jobs.length > 4 && (
        <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2 text-muted">
          <Search className="size-3.5 shrink-0" aria-hidden />
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索书名或题材"
            className="min-w-0 flex-1 bg-transparent text-caption text-ink outline-none placeholder:text-muted"
          />
        </div>
      )}
      <div className="min-h-0 overflow-y-auto p-1.5">
        {shown.map((job) => {
          const active = job.id === activeJobId
          const meta = jobStatusMeta(job.status)
          return (
            <button
              key={job.id}
              type="button"
              onClick={() => { onSelect(job.id); onClose() }}
              className={cn(
                'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-[background-color,transform] duration-[140ms] ease-[var(--ease-standard)] hover:bg-surface-2 active:scale-[.985]',
                active && 'bg-primary-soft/65',
              )}
            >
              <span
                className={cn(
                  'size-2 shrink-0 rounded-full',
                  meta.tone === 'primary' && 'bg-g-blue',
                  meta.tone === 'success' && 'bg-g-green',
                  meta.tone === 'warning' && 'bg-g-yellow',
                  meta.tone === 'danger' && 'bg-g-red',
                  meta.tone === 'neutral' && 'bg-muted',
                )}
              />
              <span className="min-w-0 flex-1">
                <span className={cn('block truncate text-body', active ? 'font-medium text-primary' : 'text-ink')}>{titleOf(job)}</span>
                <span className="block text-[11px] tabular-nums text-muted">
                  {meta.label} · {job.completedChapters ?? 0}/{job.totalChapters} 章 · {formatWords(job.totalWords)} 字
                </span>
              </span>
              {active && <Check className="size-4 shrink-0 text-primary" aria-hidden />}
            </button>
          )
        })}
        {shown.length === 0 && <p className="py-5 text-center text-caption text-muted">没有匹配的作品</p>}
      </div>
      <div className="flex shrink-0 items-center justify-between border-t border-line px-2 py-1.5">
        <button
          type="button"
          onClick={() => { onClose(); navigate('create') }}
          className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-caption font-medium text-primary transition-colors duration-[140ms] hover:bg-surface-2"
        >
          <Plus className="size-3.5" />新建作品
        </button>
        <button
          type="button"
          onClick={() => { onClose(); navigate('monitor') }}
          className="rounded-md px-2 py-1.5 text-caption text-muted transition-colors duration-[140ms] hover:bg-surface-2 hover:text-ink"
        >
          查看实时进度
        </button>
      </div>
    </PopoverSurface>
  )
}

function TopBookSwitcher({ jobs, activeJob, onSelect }: { jobs: JobRecord[]; activeJob: JobRecord | null; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useMenuDismiss(open, ref, () => setOpen(false))

  return (
    <div ref={ref} className="relative hidden md:block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label="切换当前作品"
        className={cn(
          'inline-flex h-8 max-w-[220px] items-center gap-1.5 rounded-full border border-line bg-surface px-3 text-caption font-medium text-ink transition-[background-color,border-color,color,transform] duration-[150ms] ease-[var(--ease-standard)] hover:border-line-strong hover:bg-surface-2 active:scale-[.97]',
          open && 'border-primary bg-primary-soft/60 text-primary',
        )}
      >
        <BookOpen className="size-3.5 shrink-0 text-primary" aria-hidden />
        <span className="truncate">{activeJob ? titleOf(activeJob) : '选择作品'}</span>
        <ChevronDown className={cn('size-3.5 shrink-0 text-muted transition-transform duration-[180ms] ease-[var(--ease-standard)]', open && 'rotate-180')} aria-hidden />
      </button>
      <BookSwitcherMenu open={open} jobs={jobs} activeJobId={activeJob?.id} onSelect={onSelect} onClose={() => setOpen(false)} placement="top" />
    </div>
  )
}

function ServicePill({ service }: { service: 'checking' | 'online' | 'offline' }) {
  const label = service === 'online' ? '服务在线' : service === 'offline' ? '未连接' : '连接中'
  return (
    <span
      role="status"
      className={cn(
        'hidden h-8 items-center gap-2 rounded-full px-3 text-caption font-medium transition-colors duration-[200ms] ease-[var(--ease-standard)] sm:inline-flex',
        service === 'online' && 'bg-success-soft text-success',
        service === 'offline' && 'bg-danger-soft text-danger',
        service === 'checking' && 'bg-surface-2 text-muted',
      )}
    >
      {service === 'offline' ? <WifiOff className="size-3.5" /> : (
        <span className="relative flex size-2">
          {service === 'online' && <span className="absolute inline-flex size-full animate-ping rounded-full bg-g-green opacity-50" />}
          <span className={cn('relative inline-flex size-2 rounded-full transition-colors duration-[200ms]', service === 'online' ? 'bg-g-green' : 'bg-muted')} />
        </span>
      )}
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={label}
          initial={{ opacity: 0, y: 3 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -3 }}
          transition={{ duration: 0.14, ease: EASE_OUT }}
        >
          {label}
        </motion.span>
      </AnimatePresence>
    </span>
  )
}

function SidebarContent({
  page, collapsed, navLayoutId, onToggle, onPickBook,
}: {
  page: PageKey
  collapsed: boolean
  navLayoutId: string
  onToggle?: () => void
  onPickBook?: () => void
}) {
  const { jobs, activeJob, selectJob } = useStudio()
  const [switcherOpen, setSwitcherOpen] = useState(false)
  const switcherRef = useRef<HTMLDivElement>(null)
  useMenuDismiss(switcherOpen, switcherRef, () => setSwitcherOpen(false))

  const progressPct = Math.round(activeJob?.progress ?? ((activeJob?.completedChapters ?? 0) / Math.max(1, activeJob?.totalChapters ?? 1)) * 100)

  return (
    <>
      <div className={cn('flex h-16 shrink-0 items-center', collapsed ? 'justify-center px-2' : 'justify-between px-5')}>
        <a href="#/monitor" className="group rounded-md" aria-label="墨枢工作台">
          <BrandMark collapsed={collapsed} />
        </a>
        {onToggle && !collapsed && (
          <IconButton label="收起侧栏" size="sm" onClick={onToggle}><ChevronsLeft className="size-4" /></IconButton>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto overflow-x-hidden px-3 py-2" aria-label="工作台导航">
        {NAV_GROUPS.map((group) => (
          <div key={group.name} className="mb-5">
            {!collapsed ? (
              <div className="mb-1.5 truncate px-3 text-caption font-medium text-muted">{group.name}</div>
            ) : (
              <div className="mx-auto mb-2 h-px w-6 bg-line" />
            )}
            <ul className="grid gap-0.5">
              {group.items.map((item) => {
                const active = item.key === page
                const Icon = item.icon
                return (
                  <li key={item.key}>
                    <a
                      href={`#/${item.key}`}
                      aria-current={active ? 'page' : undefined}
                      title={collapsed ? item.label : undefined}
                      className={cn(
                        'group relative flex h-11 items-center gap-3 rounded-full text-body transition-[color,background-color,transform] duration-[150ms] ease-[var(--ease-standard)] active:scale-[.98]',
                        collapsed ? 'justify-center px-0' : 'px-4',
                        active ? 'font-medium text-primary' : 'text-body hover:bg-surface-2 hover:text-ink',
                      )}
                    >
                      {active && (
                        <motion.span
                          layoutId={navLayoutId}
                          className="absolute inset-0 rounded-full bg-primary-soft"
                          transition={SPRING_INDICATOR}
                        />
                      )}
                      <Icon className="relative z-10 size-5 shrink-0 transition-transform duration-[160ms] ease-[var(--ease-standard)] group-hover:scale-105" aria-hidden />
                      {!collapsed && <span className="relative z-10 truncate">{item.label}</span>}
                    </a>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </nav>

      {activeJob && (
        <div ref={switcherRef} className={cn('relative shrink-0 p-3', collapsed && 'px-2')}>
          <button
            type="button"
            onClick={() => setSwitcherOpen((v) => !v)}
            aria-expanded={switcherOpen}
            aria-haspopup="dialog"
            aria-label="切换当前作品"
            title={collapsed ? `切换作品：${titleOf(activeJob)}` : '点击在当前页切换作品'}
            className={cn(
              'group flex w-full items-center gap-3 rounded-lg border border-line bg-surface p-3 text-left transition-[border-color,box-shadow,transform,background-color] duration-[160ms] ease-[var(--ease-standard)] hover:border-line-strong hover:shadow-raised active:scale-[.985]',
              activeJob.status === 'running' && 'nf-glow-border',
              switcherOpen && 'border-primary',
              collapsed && 'justify-center p-2',
            )}
          >
            <ProgressRing value={progressPct} size={collapsed ? 40 : 44} stroke={4}>
              <span className="text-[10px] font-medium tabular-nums text-ink">
                <AnimatedNumber value={progressPct} format={(v) => `${Math.round(v)}%`} />
              </span>
            </ProgressRing>
            {!collapsed && (
              <>
                <div className="min-w-0 flex-1">
                  <div className="text-caption text-muted">当前作品</div>
                  <div className="truncate text-body font-medium text-ink">{titleOf(activeJob)}</div>
                  <div className="text-caption tabular-nums text-muted">{activeJob.completedChapters ?? 0} / {activeJob.totalChapters} 章</div>
                </div>
                <ChevronsUpDown className="size-4 shrink-0 text-muted transition-colors duration-[160ms] group-hover:text-primary" aria-hidden />
              </>
            )}
          </button>
          <BookSwitcherMenu
            open={switcherOpen && jobs.length > 0}
            jobs={jobs}
            activeJobId={activeJob.id}
            onSelect={(id) => { selectJob(id); onPickBook?.() }}
            onClose={() => setSwitcherOpen(false)}
            placement={collapsed ? 'sidebar-collapsed' : 'sidebar'}
          />
        </div>
      )}
      {onToggle && collapsed && (
        <div className="flex justify-center pb-4">
          <IconButton label="展开侧栏" size="sm" onClick={onToggle}><ChevronsLeft className="size-4 rotate-180" /></IconButton>
        </div>
      )}
    </>
  )
}
