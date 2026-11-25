import type { Transition, Variants } from 'motion/react'

/**
 * NovelForge unified Apple-grade motion language.
 *
 * Principles:
 * - Every transition explains causality (origin, destination, state flip).
 * - One coherent physics family across the entire app: crisp, light, interruptible.
 * - GPU-composited properties only (transform, opacity, clip-path) — never animate width/filter blur on large surfaces.
 * - Enter with poised spring/deceleration; exit faster and cleaner so repeated actions feel effortless.
 */

/** Apple-like deceleration curve (fast initial response, silk-smooth settle, zero bounce). */
export const EASE_OUT = [0.22, 1, 0.36, 1] as const
/** Fast, crisp exit curve so disappearing elements never feel sticky. */
export const EASE_EXIT = [0.4, 0, 1, 1] as const
/** Symmetrical curve for layout shifts that move both directions equally (e.g. sidebar rail). */
export const EASE_STANDARD = [0.25, 0.1, 0.25, 1] as const

export const DURATION = {
  micro: 0.14,
  fast: 0.18,
  base: 0.24,
  panel: 0.28,
  data: 0.48,
} as const

/** Crisp magnetic spring for sliding indicators, toggles, and active pills. Interruptible mid-flight. */
export const SPRING_INDICATOR: Transition = {
  type: 'spring',
  stiffness: 520,
  damping: 38,
  mass: 0.8,
}

/** Poised, zero-overshoot spring for sheets, drawers, dialogs, and the Chief Editor panel. */
export const SPRING_SHEET: Transition = {
  type: 'spring',
  stiffness: 420,
  damping: 40,
  mass: 0.9,
}

/** Light tactile spring for small icons, checkmarks, and badge pops. */
export const SPRING_TACTILE: Transition = {
  type: 'spring',
  stiffness: 560,
  damping: 34,
  mass: 0.72,
}

/** Smooth page route change: pure opacity + subtle vertical rise (zero blur, 60/120fps safe). */
export const PAGE_MOTION = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0, transition: { duration: 0.22, ease: EASE_OUT } },
  exit: { opacity: 0, y: -4, transition: { duration: 0.12, ease: EASE_EXIT } },
}

/** Fast content swap for tabs, detail panes, and book switches. */
export const TAB_MOTION = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0, transition: { duration: 0.18, ease: EASE_OUT } },
  exit: { opacity: 0, y: -4, transition: { duration: 0.11, ease: EASE_EXIT } },
}

/** Smooth height + opacity reveal for collapsibles, disclosures, and conditional form groups. */
export const COLLAPSE_MOTION = {
  initial: { height: 0, opacity: 0 },
  animate: {
    height: 'auto',
    opacity: 1,
    transition: {
      height: { duration: 0.24, ease: EASE_OUT },
      opacity: { duration: 0.18, delay: 0.04, ease: EASE_OUT },
    },
  },
  exit: {
    height: 0,
    opacity: 0,
    transition: {
      opacity: { duration: 0.11, ease: EASE_EXIT },
      height: { duration: 0.2, ease: EASE_OUT },
    },
  },
}

export type PopoverOrigin = 'top' | 'top-left' | 'top-right' | 'bottom' | 'bottom-left' | 'bottom-right' | 'left-bottom'

const POPOVER_ORIGIN_CSS: Record<PopoverOrigin, string> = {
  top: 'top center',
  'top-left': 'top left',
  'top-right': 'top right',
  bottom: 'bottom center',
  'bottom-left': 'bottom left',
  'bottom-right': 'bottom right',
  'left-bottom': 'bottom left',
}

/** Popover surface grows cleanly out of its trigger anchor and retreats back into it. */
export function popoverMotion(origin: PopoverOrigin) {
  const fromBelow = origin.startsWith('bottom')
  const fromLeft = origin === 'left-bottom'
  const offsetY = fromLeft ? 0 : fromBelow ? 5 : -5
  const offsetX = fromLeft ? -6 : 0
  return {
    style: { transformOrigin: POPOVER_ORIGIN_CSS[origin] },
    initial: { opacity: 0, scale: 0.96, y: offsetY, x: offsetX },
    animate: {
      opacity: 1,
      scale: 1,
      y: 0,
      x: 0,
      transition: { duration: 0.17, ease: EASE_OUT },
    },
    exit: {
      opacity: 0,
      scale: 0.97,
      y: offsetY * 0.6,
      x: offsetX * 0.6,
      transition: { duration: 0.11, ease: EASE_EXIT },
    },
  }
}

/** Subtle horizontal micro-shake for error alerts — communicates rejection without drama. */
export const ERROR_SHAKE_ANIMATE = {
  opacity: 1,
  y: 0,
  x: [0, -4, 4, -2.5, 1.5, 0],
  transition: {
    opacity: { duration: 0.16, ease: EASE_OUT },
    y: { duration: 0.18, ease: EASE_OUT },
    x: { duration: 0.34, ease: EASE_OUT },
  },
}

/** Capped list entrance so long lists (50+ items) never queue multi-second delays. */
export const STAGGER_PARENT: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.024, delayChildren: 0.01 } },
}

export const STAGGER_CHILD: Variants = {
  hidden: { opacity: 0, y: 7 },
  show: { opacity: 1, y: 0, transition: { duration: 0.24, ease: EASE_OUT } },
}

/** Per-index bounded stagger helper (hard cap at 160ms so item #50 appears immediately). */
export function boundedDelay(index: number, step = 0.02, cap = 0.16): number {
  return Math.min(index * step, cap)
}
