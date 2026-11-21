# NovelForge Web (Gemini) — Design & Code Guide

This is the rewritten frontend. **Read this before writing any page.**

## Stack
React 19 + TypeScript (strict, `noUnusedLocals`) + Vite + Tailwind v4 + `motion` (`import { motion, AnimatePresence } from 'motion/react'`) + `lucide-react`.
Relative imports only (no `@/` alias).

## Layout of `src/`
| Path | What |
|---|---|
| `lib/api.ts`, `lib/types.ts`, `lib/pricing.ts` | API client / types / pricing — same contract as the old UI. You may ADD methods to `api` (append at the end of the object, do not change existing ones). |
| `lib/format.ts` | `formatNumber`, `formatWords`, `formatTokens`, `formatDuration`, `formatDateTime`, `formatRelative`, `errorText`, `messageOf`, `jobStatusMeta`, `Tone` |
| `lib/router.ts` | hash router. `navigate('models')`. Pages: monitor, create, hotspots, reader, facts, evaluation, models, acceptance |
| `lib/cn.ts` | `cn(...classes)` |
| `state/studio.tsx` | `useStudio()` – jobs, activeJob, chapters, facts, repairLogs, sseLogs, sseConnected, selectJob, refreshJobs, loadJobDetails, clearLogs, onJobCreated, updateJobConfig, resumeJob, pauseJob, extendJob, replaceChapter, prefill/sendConceptToCreate/consumePrefill, service |
| `state/prefs.ts` | `useSessionState(key, init)` – use it for drafts that must survive page switches (pages unmount on navigation) |
| `components/ui` | UI kit (barrel `../components/ui`) |
| `components/model/ModelPicker.tsx` | `useModelCatalog`, `ModelPicker`, `ModelSelect`, `findProvider`, `resolveSelectedModel`, `isModelChoiceReady`, `notifyCatalogChanged` |
| `pages/XxxPage.tsx` | one default-exported component per page, **no props** – read state via `useStudio()` |

Pages may have private sub-components in `src/pages/<page>/...`.

## UI kit (all in `components/ui`)
- `Button` variant: `primary | gradient | secondary | tonal | ghost | danger`, size `sm|md|lg`, `loading`, `icon`, `iconRight`, `block`. `IconButton` (`label` required).
- `Card` variant `default | flat | tinted | glow` (glow = animated Gemini border, only for "AI is working now"), `padding none|sm|md|lg`, `interactive`, `selected`. `CardHeader {title, description, icon, actions}`, `SectionLabel`, `Divider`.
- `Field {label, hint, error, aside, children: (id) => control}`, `Input {leading, trailing}`, `Textarea`, `Select`, `Checkbox {checked, onChange(bool), label, description}`, `Switch`, `Slider {value,min,max,step,onChange,label,format}`.
- `Segmented {value, onChange, options:[{value,label,icon,count}], size}` – tabs / filters. `ChoiceCards {value,onChange,options:[{value,label,description,icon,badge}],columns}`.
- `Badge {tone, dot, live, icon}`, `StatusBadge {status}` (job status), `Tag`.
- `Progress {value,max,tone:'gradient'|Tone,size,animated}`, `ProgressRing {value,size,stroke,children}`.
- `Skeleton`, `Thinking {label}` (spinning Gemini star + gradient text — use for any LLM-in-progress state), `EmptyState {icon,title,description,action}`, `Alert {tone,title,children,action,onClose}`.
- `AnimatedNumber {value, format}`, `Stat {label,value,hint,icon,tone}`, `KeyValue {items}`.
- `useToast()` → `toast(msg, 'success'|'danger'|'info')`. `Dialog`, `Drawer` `{open,onClose,title,description,footer,width}`.
- `PageHeader {title, description, actions, eyebrow}`, `Stagger` + `StaggerItem` (list entrance), `Reveal {delay}`.
- `GeminiStar {size, spinning, pulse}`.

## Visual rules (strict)
1. **Colors: tokens only.** `bg-canvas/surface/surface-2/surface-3`, `border-line/line-strong`, `text-ink/body/muted`, `primary(-strong/-soft)`, `on-primary`, `success(-soft)`, `warning(-soft)`, `danger(-soft)`, brand dots `g-blue/g-red/g-yellow/g-green`. **Never** Tailwind palette (`sky-500`, `emerald-700`, `amber-*`, `rose-*`, `gray-*`…) and never hex in className. Must work in dark mode automatically.
2. **Gradient is a spice.** `nf-gradient-text`, `nf-gradient-bg`, `Button variant="gradient"`, `Card variant="glow"`, `Progress` – max ONE gradient CTA per page (the main action) + live/AI states.
3. **Type scale only:** `text-caption`(12) `text-body`(14) `text-title`(16) `text-headline`(22) `text-display`(32). No `text-xs/sm/[11px]`. Weights: `font-normal` / `font-medium` only (no bold).
4. **Radius:** `rounded-sm/md/lg/xl/full`. Buttons/chips are pills (`rounded-full`), cards `rounded-lg`, inputs `rounded-md`.
5. **One card level.** Never put a bordered box inside a bordered card. Group inside a card with `SectionLabel`, spacing, `Divider`, or `bg-surface-2` flat areas (`Card variant="flat"`).
6. **Short labels** (2–6 Chinese chars). Explanations go into `hint` / `description` / muted text.
7. Every page: `<PageHeader>` first. Main content max ~2 columns on desktop, collapses to 1 on mobile. Use `grid gap-6`.
8. Status tone mapping: running/info → primary (blue), success → green, warning/paused → warning (yellow), error → danger (red).
9. Interactive elements are `<button>`/`<a>`, never `div onClick`. Every hover must visibly change something.

## Motion rules
- Pages already get a fade/blur transition from the shell – do not add another on the page root.
- Lists/grids of cards: `Stagger` + `StaggerItem`. Tab content switching: `AnimatePresence mode="wait"` with small y/opacity.
- Expand/collapse: `motion.div` with `initial={{height:0,opacity:0}} animate={{height:'auto',opacity:1}}`.
- Live / AI working: `Thinking`, `Card variant="glow"`, `Badge live`, `Progress animated`.
- Numbers on dashboards: `AnimatedNumber`. Use `layout` for reordering lists.
- Keep durations 150–400ms, ease `[0.2,0,0,1]` or springs (stiffness ~400, damping ~35). No bouncing gimmicks.

## Verify
`npx tsc -b` must pass (ignore errors in files you don't own while others are working). Don't run `npm install` for new packages.
