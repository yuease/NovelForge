import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

type MarkdownNode = { type: string; value?: string; children?: MarkdownNode[]; url?: string }

function remarkEvidenceLinks(anchors: ReadonlyMap<string, string>) {
  const references = /（\s*E\d+(?:[\s、,，;；/]+E\d+)*\s*）|\(\s*E\d+(?:[\s、,，;；/]+E\d+)*\s*\)|\[\s*E\d+(?:[\s、,，;；/]+E\d+)*\s*\]|【\s*E\d+(?:[\s、,，;；/]+E\d+)*\s*】/gi

  function transform(node: MarkdownNode) {
    if (node.type === 'link' || node.type === 'linkReference' || !node.children) return
    node.children = node.children.flatMap((child) => {
      if (child.type !== 'text' || !child.value) {
        transform(child)
        return [child]
      }
      const value = child.value
      const parts: MarkdownNode[] = []
      let cursor = 0
      for (const group of value.matchAll(references)) {
        for (const ref of group[0].matchAll(/E\d+/gi)) {
          const url = anchors.get(ref[0].toUpperCase())
          if (!url) continue
          const offset = (group.index ?? 0) + (ref.index ?? 0)
          if (offset > cursor) parts.push({ type: 'text', value: value.slice(cursor, offset) })
          parts.push({ type: 'link', url, children: [{ type: 'text', value: ref[0] }] })
          cursor = offset + ref[0].length
        }
      }
      if (cursor === 0) return [child]
      if (cursor < value.length) parts.push({ type: 'text', value: value.slice(cursor) })
      return parts
    })
  }

  return transform
}

export default function EditorReplyMarkdown({ reply, evidencePrefix, evidenceIds, onEvidence }: {
  reply: string; evidencePrefix: string; evidenceIds: string[]; onEvidence: (id: string) => void
}) {
  const anchors = new Map(evidenceIds.map((id) => [id.toUpperCase(), '#' + evidencePrefix + id.toUpperCase()]))
  const destinations = new Map([...anchors].map(([id, href]) => [href, id]))
  return <div className="min-w-0 space-y-3 break-words text-body leading-relaxed text-ink
    [&_p]:whitespace-pre-line [&_strong]:font-semibold [&_h1]:text-title [&_h2]:text-title [&_h3]:text-body
    [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_ul]:list-disc [&_ol]:list-decimal
    [&_ul]:pl-5 [&_ol]:pl-5 [&_li+li]:mt-1 [&_li>p+p]:mt-2 [&_blockquote]:border-l-2 [&_blockquote]:border-line
    [&_blockquote]:pl-3 [&_blockquote]:text-muted [&_code]:rounded [&_code]:bg-canvas [&_code]:px-1
    [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-canvas [&_pre]:p-3 [&_pre_code]:px-0
    [&_th]:border [&_td]:border [&_th]:border-line [&_td]:border-line [&_th]:px-2 [&_td]:px-2
    [&_th]:py-1 [&_td]:py-1 [&_hr]:border-line">
    <Markdown remarkPlugins={[remarkGfm, [remarkEvidenceLinks, anchors]]} skipHtml disallowedElements={['img']}
      components={{
        a: ({ href, children }) => {
          const id = destinations.get(href ?? '')
          if (id) return <button type="button" onClick={() => onEvidence(id)}
            aria-label={'查看证据 ' + id} aria-controls={evidencePrefix + id}
            className="rounded-sm bg-primary/10 px-1 text-caption font-semibold leading-snug text-primary underline decoration-primary/40 underline-offset-2 hover:bg-primary/20 focus-visible:outline focus-visible:outline-primary">{children}</button>
          return href && /^https?:\/\//i.test(href)
            ? <a href={href} target="_blank" rel="noreferrer" className="text-primary underline underline-offset-2">{children}</a>
            : <span>{children}</span>
        },
        table: ({ children }) => <div className="max-w-full overflow-x-auto"><table className="w-full border-collapse text-caption">{children}</table></div>,
      }}>{reply}</Markdown>
  </div>
}
