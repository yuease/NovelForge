export type ClassValue = string | number | bigint | boolean | null | undefined

/** Tiny className joiner. */
export function cn(...values: ClassValue[]): string {
  return values.filter((v) => typeof v === 'string' && v).join(' ')
}
