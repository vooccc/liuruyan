import { useMemo, useState } from 'react'
import {
  AlertCircle,
  Copy,
} from 'lucide-react'

/**
 * ReadFileCard — read_file 的结构化代码预览。
 *
 * 后端 read_file 返回 JSON：path + cat -n 格式 content（"行号\\t代码"）。
 * 本卡片作为 RuntimeToolBlock 的 customResult 嵌入，外层已有 Wrench 头 +
 * 工具名 + 路径摘要 + 状态徽章 + 折叠控制，所以这里只负责代码渲染。
 */

export interface ReadFileLine {
  lineno: number
  text: string
}

export interface ReadFileCardPayload {
  path: string
  lines: ReadFileLine[]
  totalLines?: number
  showingLines?: string
  truncated?: boolean
  hint?: string
  error?: string
  lang?: string
}

function langFromPath(path: string): string | undefined {
  const m = path.match(/\.([^.]+)$/)
  if (!m) return undefined
  const ext = m[1].toLowerCase()
  const map: Record<string, string> = {
    ts: 'typescript',
    tsx: 'typescript',
    js: 'javascript',
    jsx: 'javascript',
    py: 'python',
    rs: 'rust',
    go: 'go',
    java: 'java',
    kt: 'kotlin',
    cs: 'csharp',
    cpp: 'cpp',
    c: 'c',
    h: 'c',
    hpp: 'cpp',
    css: 'css',
    scss: 'scss',
    html: 'html',
    json: 'json',
    md: 'markdown',
    yaml: 'yaml',
    yml: 'yaml',
    toml: 'toml',
    sh: 'shell',
    bash: 'shell',
    ps1: 'powershell',
    sql: 'sql',
  }
  return map[ext]
}

/** 解析 backend cat -n 文本："123\\tcode line" */
export function parseCatNContent(content: string): ReadFileLine[] {
  if (!content) return []
  return content.split('\n').map((line) => {
    const tab = line.indexOf('\t')
    if (tab === -1) return { lineno: 0, text: line }
    const no = Number.parseInt(line.slice(0, tab), 10)
    return {
      lineno: Number.isFinite(no) ? no : 0,
      text: line.slice(tab + 1),
    }
  })
}

export function tryParseReadFilePayload(
  raw: string | undefined | null,
  toolName?: string,
): ReadFileCardPayload | null {
  if (!raw || typeof raw !== 'string') return null
  if (toolName !== 'read_file') return null
  if (!raw.trim().startsWith('{')) return null

  let obj: Record<string, unknown>
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    obj = parsed as Record<string, unknown>
  } catch {
    return null
  }

  const path = typeof obj.path === 'string' ? obj.path : ''
  const error = typeof obj.error === 'string' ? obj.error : undefined
  const content = typeof obj.content === 'string' ? obj.content : undefined

  if (!path && !error) return null

  if (error && !content) {
    return {
      path: path || '(unknown)',
      lines: [],
      error,
      lang: langFromPath(path),
    }
  }

  if (typeof content !== 'string') return null

  return {
    path,
    lines: parseCatNContent(content),
    totalLines: typeof obj.total_lines === 'number' ? obj.total_lines : undefined,
    showingLines: typeof obj.showing_lines === 'string' ? obj.showing_lines : undefined,
    truncated: obj.truncated === true,
    hint: typeof obj.hint === 'string' ? obj.hint : undefined,
    lang: langFromPath(path),
  }
}

export function ReadFileCard({ payload }: { payload: ReadFileCardPayload }) {
  const [pathCopied, setPathCopied] = useState(false)

  const rangeLabel = useMemo(() => {
    if (payload.showingLines) return `L${payload.showingLines}`
    if (payload.lines.length > 0) {
      const first = payload.lines[0].lineno
      const last = payload.lines[payload.lines.length - 1].lineno
      if (first && last) return first === last ? `L${first}` : `L${first}-${last}`
    }
    return ''
  }, [payload.showingLines, payload.lines])

  const onCopyPath = async () => {
    try {
      await navigator.clipboard.writeText(payload.path)
      setPathCopied(true)
      setTimeout(() => setPathCopied(false), 1500)
    } catch {
      /* clipboard 不可用 */
    }
  }

  return (
    <div className="read-file-card" data-lang={payload.lang || undefined}>
      <div className="read-file-info-bar">
        {payload.lang && (
          <span className="read-file-lang-badge">{payload.lang}</span>
        )}
        {rangeLabel && (
          <span className="read-file-range">{rangeLabel}</span>
        )}
        {typeof payload.totalLines === 'number' && (
          <span className="read-file-total">{payload.totalLines} 行</span>
        )}
        <button
          type="button"
          className="read-file-copy-btn"
          onClick={onCopyPath}
          title="复制完整路径"
        >
          <Copy size={12} />
          {pathCopied && <span className="read-file-copied-flash">已复制</span>}
        </button>
      </div>

      {payload.error && (
        <div className="read-file-error">
          <AlertCircle size={13} /> {payload.error}
        </div>
      )}

      {!payload.error && payload.lines.length > 0 && (
        <div className="read-file-scroll">
          <table className="read-file-table">
            <tbody>
              {payload.lines.map((line, i) => (
                <tr key={`${line.lineno}-${i}`} className="read-file-row">
                  <td className="read-file-lineno">{line.lineno || ''}</td>
                  <td className="read-file-code">
                    <pre>{line.text || ' '}</pre>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {payload.truncated && (
        <div className="read-file-truncated-note">
          {payload.hint || '文件较长，仅显示部分行。可让 agent 用 offset/limit 继续读。'}
        </div>
      )}
    </div>
  )
}
