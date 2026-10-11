import { useState, useRef, useEffect } from 'react'
import {
  Copy,
  ExternalLink,
  Globe,
  RefreshCcw,
  ShieldAlert,
  Terminal,
} from 'lucide-react'

/**
 * BrowserPreviewCard — 在 chat 中嵌入 URL 的 iframe 预览。
 *
 * 协议：后端 browser_preview 工具返回 result JSON 含 `_runtime_block: 'browser_preview'`
 * sentinel + url + name + is_localhost 等字段；runtimeBlocks 识别后渲染本卡片
 * （取代默认 ChainTool 的 tool_result UI）。
 *
 * 设计：
 * - iframe sandbox 严格但不过度：allow-scripts + allow-same-origin（让本地 dev server 完整加载）
 *   + allow-forms + allow-popups（让用户能正常交互）
 * - 工具栏：刷新 / 新窗口 / 复制 URL 三个动作（最常用）
 * - is_localhost 时给"如果空白可能是 server 没启动"提示
 * - 跨源时给"X-Frame-Options 可能拒绝"提示（用 onLoad/onError 探测无可靠 API，
 *   单纯按 hostname 启发式给提示）
 */

export interface BrowserPreviewPayload {
  _runtime_block: 'browser_preview'
  preview_id: string
  url: string
  name: string
  is_localhost: boolean
  is_dev_port?: boolean
  ts?: number
  agent_id?: string | null
  session_id?: string | null
  instruction?: string
}

export function BrowserPreviewCard({
  payload,
}: {
  payload: BrowserPreviewPayload
}) {
  const [refreshKey, setRefreshKey] = useState(0)
  const [copied, setCopied] = useState(false)
  const [iframeLoaded, setIframeLoaded] = useState(false)
  const [showLoadHint, setShowLoadHint] = useState(false)
  const iframeRef = useRef<HTMLIFrameElement | null>(null)

  // 6 秒后还没 onload 触发 → 提示用户可能加载失败（dev server 没启 / X-Frame 拒绝）
  // onerror 在 iframe 上对跨源拒绝是 silent 的，只能靠超时启发式
  useEffect(() => {
    setIframeLoaded(false)
    setShowLoadHint(false)
    const t = setTimeout(() => {
      if (!iframeLoaded) setShowLoadHint(true)
    }, 6000)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, payload.url])

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(payload.url)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // 剪贴板权限失败：兜底没动作（用户能看见 URL 自己手动选中复制）
    }
  }

  return (
    <div className="browser-preview-card" data-preview-id={payload.preview_id}>
      {/* 顶部工具栏：iconified label + URL + 操作 */}
      <div className="browser-preview-toolbar">
        <div className="browser-preview-meta">
          <span
            className={`browser-preview-badge ${payload.is_localhost ? 'localhost' : 'remote'}`}
            title={payload.is_localhost ? '本机 dev server' : '远程 URL'}
          >
            {payload.is_localhost ? <Terminal size={11} /> : <Globe size={11} />}
            <span>{payload.is_localhost ? 'localhost' : 'web'}</span>
          </span>
          <span className="browser-preview-name" title={payload.name}>
            {payload.name}
          </span>
        </div>
        <a
          className="browser-preview-url"
          href={payload.url}
          target="_blank"
          rel="noopener noreferrer"
          title={payload.url}
        >
          {payload.url}
        </a>
        <div className="browser-preview-actions">
          <button
            type="button"
            className="browser-preview-btn"
            onClick={() => setRefreshKey((k) => k + 1)}
            title="刷新预览"
          >
            <RefreshCcw size={13} />
          </button>
          <button
            type="button"
            className="browser-preview-btn"
            onClick={onCopy}
            title="复制 URL"
          >
            <Copy size={13} />
            {copied && <span className="browser-preview-copied">已复制</span>}
          </button>
          <button
            type="button"
            className="browser-preview-btn primary"
            onClick={() => window.open(payload.url, '_blank', 'noopener,noreferrer')}
            title="在新窗口打开"
          >
            <ExternalLink size={13} />
          </button>
        </div>
      </div>

      {/* iframe 主体 */}
      <div className="browser-preview-frame-wrap">
        <iframe
          key={`${refreshKey}-${payload.url}`}
          ref={iframeRef}
          src={payload.url}
          className="browser-preview-iframe"
          // sandbox：放开必要权限让 dev server 正常加载，禁止 top-navigation 和 modals
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
          loading="lazy"
          referrerPolicy="no-referrer"
          title={payload.name}
          onLoad={() => setIframeLoaded(true)}
        />
        {/* 加载超时提示：6s 内 onload 没触发 → 大概率是 dev server 关了 / 跨源拒绝 */}
        {showLoadHint && !iframeLoaded && (
          <div className="browser-preview-hint">
            <ShieldAlert size={14} />
            <div>
              <div className="browser-preview-hint-title">页面看起来没加载出来</div>
              <div className="browser-preview-hint-detail">
                {payload.is_localhost
                  ? '可能是 dev server 没启动，或端口跟 URL 不一致。点右上角"新窗口打开"在浏览器里直接看。'
                  : '部分网站设了 X-Frame-Options/CSP 拒绝被嵌入。点右上角"新窗口打开"绕过限制。'}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

/** 尝试把 tool_result 解析成 BrowserPreview payload；不是则返 null。
 *
 * 协议：后端返 JSON 字符串含 `_runtime_block: 'browser_preview'` sentinel。
 * 跟 AskUserQuestionCard 同款"工具结果 sentinel"模式。
 */
export function tryParseBrowserPreviewPayload(raw: string | undefined | null):
  | BrowserPreviewPayload
  | null {
  if (!raw || typeof raw !== 'string') return null
  // 快速排除：不以 { 开头或不含 sentinel
  if (!raw.includes('"_runtime_block"') || !raw.includes('"browser_preview"')) return null
  try {
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object') return null
    if (obj._runtime_block !== 'browser_preview') return null
    if (typeof obj.url !== 'string' || !obj.url) return null
    return obj as BrowserPreviewPayload
  } catch {
    return null
  }
}
