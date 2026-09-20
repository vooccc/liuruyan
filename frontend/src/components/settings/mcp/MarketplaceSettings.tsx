/**
 * Marketplace 订阅 URL 编辑器（M4.4）。
 *
 * 用户在这里加 URL，每个 URL 期望返回我们 catalog schema 的 JSON。
 * 改完即时 PUT 到后端 /api/mcp/marketplaces。
 */

import { useEffect, useState } from 'react'
import { Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { getMarketplaces, setMarketplaces } from '../../../api/mcp'

interface Props {
  /** registry tab 内嵌时按这个回调通知父刷 catalog */
  onCatalogChanged?: () => void
}

export default function MarketplaceSettings({ onCatalogChanged }: Props) {
  const [urls, setUrls] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      setUrls(await getMarketplaces())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const persist = async (next: string[]) => {
    setSaving(true)
    setError(null)
    try {
      const saved = await setMarketplaces(next)
      setUrls(saved)
      onCatalogChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const handleAdd = async () => {
    const u = draft.trim()
    if (!u) return
    if (!/^https?:\/\//.test(u)) {
      setError('URL 必须以 http:// 或 https:// 开头')
      return
    }
    if (urls.includes(u)) {
      setError('该 URL 已订阅')
      return
    }
    const next = [...urls, u]
    setDraft('')
    await persist(next)
  }

  const handleRemove = async (u: string) => {
    await persist(urls.filter((x) => x !== u))
  }

  return (
    <div className="mcp-marketplace-settings">
      <h4>Marketplace 订阅</h4>
      <p className="mcp-form-hint">
        从远程 URL 拉 MCP server 目录条目（schema 同内置目录）。1 小时缓存；
        URL 拉失败不影响内置和其它订阅。同名条目以内置为准。
      </p>

      <div className="mcp-marketplace-add">
        <input
          type="url"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="https://example.com/mcp-catalog.json"
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void handleAdd()
            }
          }}
        />
        <button
          type="button"
          className="mcp-btn-primary"
          disabled={saving || !draft.trim()}
          onClick={() => void handleAdd()}
        >
          <Plus size={14} /> 添加
        </button>
      </div>

      {error ? <div className="mcp-error-banner">{error}</div> : null}

      {loading && urls.length === 0 ? (
        <div className="mcp-empty-inline">
          <Loader2 size={14} className="lr-spin" /> 加载中…
        </div>
      ) : urls.length === 0 ? (
        <p className="mcp-empty-inline">
          （暂无订阅。例：raycast / 自家镜像 / 团队内部 catalog）
        </p>
      ) : (
        <ul className="mcp-marketplace-list">
          {urls.map((u) => (
            <li key={u}>
              <code>{u}</code>
              <button
                type="button"
                className="mcp-icon-btn mcp-icon-btn-danger"
                onClick={() => void handleRemove(u)}
                disabled={saving}
                title="移除"
              >
                <Trash2 size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}

      {onCatalogChanged ? (
        <button
          type="button"
          className="mcp-link-btn"
          onClick={() => onCatalogChanged()}
        >
          <RefreshCw size={12} /> 强制刷新目录
        </button>
      ) : null}
    </div>
  )
}
