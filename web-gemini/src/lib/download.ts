/**
 * Safe file download helper: fetches the resource first so backend errors
 * stay in-page as clear toasts instead of navigating to a raw JSON error page.
 */
function parseFilename(header: string | null, fallback: string): string {
  if (!header) return fallback
  const utf8 = header.match(/filename\*\s*=\s*UTF-8''([^;\n]+)/i)
  if (utf8?.[1]) {
    try { return decodeURIComponent(utf8[1].trim().replace(/^["']|["']$/g, '')) } catch { /* ignore */ }
  }
  const plain = header.match(/filename\s*=\s*("?)([^";\n]+)\1/i)
  if (plain?.[2]) return plain[2].trim()
  return fallback
}

export async function downloadFile(url: string, fallbackName: string): Promise<string> {
  const res = await fetch(url)
  if (!res.ok) {
    let msg = `导出失败（状态 ${res.status}）`
    try {
      const data = await res.json()
      if (typeof data?.error === 'string' && data.error) msg = data.error
      else if (typeof data?.message === 'string' && data.message) msg = data.message
    } catch {
      const text = await res.text().catch(() => '')
      if (text && text.length < 240) msg = text
    }
    throw new Error(msg)
  }
  const blob = await res.blob()
  const filename = parseFilename(res.headers.get('content-disposition'), fallbackName)
  const href = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = href
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(href), 2000)
  }
  return filename
}
