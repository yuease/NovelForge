import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  AudioLines,
  BookOpen,
  ChevronsLeft,
  FlaskConical,
  Layers3,
  Lightbulb,
  Menu,
  Moon,
  PenLine,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Sun,
  WifiOff,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { cn } from '../lib/cn'
import { navigate } from '../lib/router'
import type { PageKey } from '../lib/router'
import { useStudio } from '../state/studio'
import { useTheme } from '../state/prefs'
import { BrandMark } from '../components/brand/GeminiStar'
import { Alert, Button, IconButton, ProgressRing } from '../components/ui'
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
      { key: 'acceptance', label: '运行验收', icon: FlaskConical },
    ],
  },
]
export const NAV_ITEMS = NAV_GROUPS.flatMap((g) => g.items)

const COLLAPSE_KEY = 'novel_forge_sidebar_collapsed'
const EDITOR_KEY = 'novel_forge_editor_open'

export function Shell({ page, children }: { page: PageKey; children: ReactNode }) {
  const { service, refreshJobs, jobs, activeJob } = useStudio()
  const { theme, toggle } = useTheme()
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === '1')
  const [mobileOpen, setMobileOpen] = useState(false)
  const [editorRequested, setEditorRequested] = useState(() => localStorage.getItem(EDITOR_KEY) === '1')
  const editorOpen = Boolean(editorRequested && activeJob)
  const current = NAV_ITEMS.find((n) => n.key === page)

  useEffect(() => { document.title = `${current?.label ?? '工作台'} · 墨枢` }, [current])
  useEffect(() => { setMobileOpen(false); window.scrollTo({ top: 0 }) }, [page])

  const toggleCollapse = () => setCollapsed((c) => { localStorage.setItem(COLLAPSE_KEY, c ? '0' : '1'); return !c })
  const setEditorOpen = (open: boolean) => { setEditorRequested(open); localStorage.setItem(EDITOR_KEY, open ? '1' : '0') }

  return (
    <div className="relative min-h-screen [--editor-width:clamp(350px,31vw,470px)]">
      <div className="nf-aura" aria-hidden><span /><span /><span /></div>

      {/* Desktop sidebar */}
      <motion.aside
        animate={{ width: collapsed ? 76 : 256 }}
        transition={{ type: 'spring', stiffness: 380, damping: 38 }}
        className="fixed inset-y-0 left-0 z-40 hidden flex-col border-r border-line/70 bg-canvas/80 backdrop-blur-xl lg:flex"
      >
        <SidebarContent page={page} collapsed={collapsed} onToggle={toggleCollapse} />
      </motion.aside>

      {/* Mobile sidebar */}
      <AnimatePresence>
        {mobileOpen && (
          <div className="fixed inset-0 z-50 lg:hidden">
            <motion.div className="absolute inset-0 bg-black/30" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={() => setMobileOpen(false)} />
            <motion.aside
              initial={{ x: -280 }}
              animate={{ x: 0 }}
              exit={{ x: -280 }}
              transition={{ type: 'spring', stiffness: 380, damping: 38 }}
              className="absolute inset-y-0 left-0 flex w-[264px] flex-col bg-surface shadow-float"
            >
              <SidebarContent page={page} collapsed={false} />
            </motion.aside>
          </div>
        )}
      </AnimatePresence>

      <div
        style={{ ['--sb' as string]: collapsed ? '76px' : '256px' }}
        className="relative z-10 min-h-screen transition-[padding] duration-300 ease-[var(--ease-emphasized)] lg:pl-[var(--sb)]"
      >
        <header className="sticky top-0 z-30 flex h-16 items-center gap-3 bg-canvas/75 px-4 backdrop-blur-xl md:px-8">
          <IconButton label="打开导航" className="lg:hidden" onClick={() => setMobileOpen(true)}><Menu className="size-5" /></IconButton>
          <div className="flex min-w-0 items-center gap-2 text-body">
            <span className="hidden text-muted sm:inline">工作台</span>
            <span className="hidden text-line-strong sm:inline">/</span>
            <AnimatePresence mode="wait">
              <motion.span
                key={page}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.18 }}
                className="truncate font-medium text-ink"
              >
                {current?.label}
              </motion.span>
            </AnimatePresence>
          </div>
          <div className="ml-auto flex items-center gap-1.5 sm:gap-3">
            <ServicePill service={service} />
            {activeJob && (
              <Button variant={editorOpen ? 'tonal' : 'secondary'} size="sm" aria-expanded={editorOpen} aria-controls="chief-editor-panel"
                icon={<Sparkles className="size-4" />} onClick={() => setEditorOpen(!editorOpen)}>问总编</Button>
            )}
            <IconButton label={theme === 'dark' ? '切换到浅色' : '切换到深色'} onClick={toggle}>
              <AnimatePresence mode="wait" initial={false}>
                <motion.span key={theme} initial={{ rotate: -90, opacity: 0 }} animate={{ rotate: 0, opacity: 1 }} exit={{ rotate: 90, opacity: 0 }} transition={{ duration: 0.2 }}>
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

        <div className={cn(editorOpen && 'min-[1200px]:pr-[var(--editor-width)]')}>
        <main id="workspace-content" className="mx-auto w-full max-w-[1440px] px-4 pb-16 pt-4 md:px-8">
          {service === 'offline' && jobs.length === 0 && (
            <Alert
              tone="warning"
              title="暂时连不上工作台"
              className="mb-6"
              action={<Button size="sm" variant="secondary" icon={<RefreshCw className="size-3.5" />} onClick={() => void refreshJobs()}>重新连接</Button>}
            >
              确认 Novel Forge 服务正在运行后重试。
            </Alert>
          )}
          <AnimatePresence mode="wait">
            <motion.div
              key={page}
              initial={{ opacity: 0, y: 14, filter: 'blur(4px)' }}
              animate={{ opacity: 1, y: 0, filter: 'blur(0px)' }}
              exit={{ opacity: 0, y: -8, filter: 'blur(2px)' }}
              transition={{ duration: 0.28, ease: [0.2, 0, 0, 1] }}
            >
              {children}
            </motion.div>
          </AnimatePresence>
        </main>
        </div>
      </div>
      {activeJob && <EditorDesk key={activeJob.id} open={editorOpen} onClose={() => setEditorOpen(false)}
        jobId={activeJob.id} title={activeJob.title || '当前作品'} />}
    </div>
  )
}

function ServicePill({ service }: { service: 'checking' | 'online' | 'offline' }) {
  const label = service === 'online' ? '服务在线' : service === 'offline' ? '未连接' : '连接中'
  return (
    <span
      role="status"
      className={cn(
        'hidden h-8 items-center gap-2 rounded-full px-3 text-caption font-medium sm:inline-flex',
        service === 'online' && 'bg-success-soft text-success',
        service === 'offline' && 'bg-danger-soft text-danger',
        service === 'checking' && 'bg-surface-2 text-muted',
      )}
    >
      {service === 'offline' ? <WifiOff className="size-3.5" /> : (
        <span className="relative flex size-2">
          {service === 'online' && <span className="absolute inline-flex size-full animate-ping rounded-full bg-g-green opacity-50" />}
          <span className={cn('relative inline-flex size-2 rounded-full', service === 'online' ? 'bg-g-green' : 'bg-muted')} />
        </span>
      )}
      {label}
    </span>
  )
}

function SidebarContent({ page, collapsed, onToggle }: { page: PageKey; collapsed: boolean; onToggle?: () => void }) {
  const { activeJob } = useStudio()
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
              <div className="mb-1.5 px-3 text-caption font-medium text-muted">{group.name}</div>
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
                        'group relative flex h-11 items-center gap-3 rounded-full text-body transition-colors duration-200',
                        collapsed ? 'justify-center px-0' : 'px-4',
                        active ? 'font-medium text-primary' : 'text-body hover:bg-surface-2 hover:text-ink',
                      )}
                    >
                      {active && (
                        <motion.span
                          layoutId="nav-active"
                          className="absolute inset-0 rounded-full bg-primary-soft"
                          transition={{ type: 'spring', stiffness: 500, damping: 40 }}
                        />
                      )}
                      <Icon className="relative z-10 size-5 shrink-0 transition-transform duration-200 group-hover:scale-110" aria-hidden />
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
        <div className={cn('shrink-0 p-3', collapsed && 'px-2')}>
          <a
            href="#/monitor"
            title={collapsed ? activeJob.title : undefined}
            className={cn(
              'group flex items-center gap-3 rounded-lg border border-line bg-surface p-3 transition-all duration-200 hover:border-line-strong hover:shadow-raised',
              activeJob.status === 'running' && 'nf-glow-border',
              collapsed && 'justify-center p-2',
            )}
          >
            <ProgressRing value={activeJob.progress ?? ((activeJob.completedChapters ?? 0) / Math.max(1, activeJob.totalChapters)) * 100} size={collapsed ? 40 : 44} stroke={4}>
              <span className="text-[10px] font-medium tabular-nums text-ink">{Math.round(activeJob.progress ?? 0)}%</span>
            </ProgressRing>
            {!collapsed && (
              <div className="min-w-0 flex-1">
                <div className="text-caption text-muted">当前作品</div>
                <div className="truncate text-body font-medium text-ink">{activeJob.title || activeJob.bookPlan?.title || activeJob.id}</div>
                <div className="text-caption tabular-nums text-muted">{activeJob.completedChapters ?? 0} / {activeJob.totalChapters} 章</div>
              </div>
            )}
          </a>
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
