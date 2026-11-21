import { Component, lazy, Suspense } from 'react'
import type { ErrorInfo, LazyExoticComponent, ReactNode } from 'react'
import { usePage } from './lib/router'
import type { PageKey } from './lib/router'
import { StudioProvider } from './state/studio'
import { Shell } from './layout/Shell'
import { Button, Skeleton, ToastProvider } from './components/ui'

const pages: Record<PageKey, LazyExoticComponent<() => ReactNode>> = {
  monitor: lazy(() => import('./pages/MonitorPage')),
  create: lazy(() => import('./pages/CreatePage')),
  hotspots: lazy(() => import('./pages/HotspotsPage')),
  reader: lazy(() => import('./pages/ReaderPage')),
  facts: lazy(() => import('./pages/FactsPage')),
  evaluation: lazy(() => import('./pages/EvaluationPage')),
  models: lazy(() => import('./pages/ModelsPage')),
  acceptance: lazy(() => import('./pages/AcceptancePage')),
}

class ErrorBoundary extends Component<{ children: ReactNode; resetKey: string }, { error: string | null }> {
  state = { error: null as string | null }
  static getDerivedStateFromError(error: Error) { return { error: error?.message || '界面渲染异常' } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('NovelForge UI error:', error, info) }
  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null })
  }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="mx-auto mt-16 max-w-md rounded-lg border border-line bg-surface p-6 text-center shadow-card">
        <div className="text-title font-medium text-danger">页面出错了</div>
        <p className="mt-2 break-all text-caption text-muted">{this.state.error}</p>
        <Button className="mt-5" variant="primary" onClick={() => this.setState({ error: null })}>重新渲染</Button>
      </div>
    )
  }
}

function PageFallback() {
  return (
    <div className="grid gap-4">
      <Skeleton className="h-8 w-56" />
      <Skeleton className="h-4 w-96 max-w-full" />
      <div className="mt-4 grid gap-4 md:grid-cols-3">
        <Skeleton className="h-36" /><Skeleton className="h-36" /><Skeleton className="h-36" />
      </div>
    </div>
  )
}

export default function App() {
  const page = usePage()
  const Page = pages[page]
  return (
    <ToastProvider>
      <StudioProvider>
        <Shell page={page}>
          <ErrorBoundary resetKey={page}>
            <Suspense fallback={<PageFallback />}>
              <Page />
            </Suspense>
          </ErrorBoundary>
        </Shell>
      </StudioProvider>
    </ToastProvider>
  )
}
