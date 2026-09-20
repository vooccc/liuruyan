/**
 * 内置目录 spotlight —— 借鉴 chatbox 风格：搜索 + tag 过滤 + 卡片点击添加。
 */

import { useMemo, useState } from 'react'
import { CheckCircle2, KeyRound, Search } from 'lucide-react'
import type { MCPCatalogEntry } from '../../../api/mcp'

interface Props {
  entries: MCPCatalogEntry[]
  installed: Set<string>
  onPick: (entry: MCPCatalogEntry) => void
}

export default function ServerRegistrySpotlight({ entries, installed, onPick }: Props) {
  const [q, setQ] = useState('')
  const [activeTag, setActiveTag] = useState<string | null>(null)

  const allTags = useMemo(() => {
    const set = new Set<string>()
    for (const e of entries) for (const t of e.tags) set.add(t)
    return Array.from(set).sort()
  }, [entries])

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return entries.filter((e) => {
      if (activeTag && !e.tags.includes(activeTag)) return false
      if (!needle) return true
      return (
        e.name.toLowerCase().includes(needle) ||
        e.title.toLowerCase().includes(needle) ||
        e.description.toLowerCase().includes(needle)
      )
    })
  }, [entries, q, activeTag])

  return (
    <div className="mcp-spotlight">
      <div className="mcp-spotlight-search">
        <Search size={16} />
        <input
          type="search"
          placeholder="搜索 MCP server（如 github / 浏览器 / 搜索…）"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {allTags.length > 0 ? (
        <div className="mcp-spotlight-tags">
          <button
            type="button"
            className={`mcp-tag-chip ${activeTag === null ? 'active' : ''}`}
            onClick={() => setActiveTag(null)}
          >
            全部
          </button>
          {allTags.map((tag) => (
            <button
              key={tag}
              type="button"
              className={`mcp-tag-chip ${activeTag === tag ? 'active' : ''}`}
              onClick={() => setActiveTag(activeTag === tag ? null : tag)}
            >
              {tag}
            </button>
          ))}
        </div>
      ) : null}
      <ul className="mcp-catalog-grid">
        {filtered.map((entry) => {
          const isInstalled = installed.has(entry.name)
          return (
            <li key={entry.name}>
              <button
                type="button"
                className="mcp-catalog-card"
                disabled={isInstalled}
                onClick={() => onPick(entry)}
              >
                <div className="mcp-catalog-card-icon">
                  <CatalogIcon entry={entry} />
                </div>
                <div className="mcp-catalog-card-body">
                  <div className="mcp-catalog-card-title-row">
                    <h4>{entry.title}</h4>
                    {isInstalled ? (
                      <span className="mcp-catalog-installed-pill">
                        <CheckCircle2 size={12} /> 已安装
                      </span>
                    ) : entry.requires_token ? (
                      <span
                        className="mcp-catalog-token-pill"
                        title="需要 API token 才能使用"
                      >
                        <KeyRound size={12} /> 需 token
                      </span>
                    ) : null}
                  </div>
                  <p className="mcp-catalog-card-desc">{entry.description}</p>
                  {entry.tags.length > 0 ? (
                    <div className="mcp-catalog-card-tags">
                      {entry.tags.map((t) => (
                        <span key={t} className="mcp-catalog-card-tag">
                          {t}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              </button>
            </li>
          )
        })}
      </ul>
      {filtered.length === 0 ? <p className="mcp-empty-inline">没有找到匹配的 server。</p> : null}
    </div>
  )
}

/**
 * Catalog 卡片图标。`onError` 时降级到首字母占位，避免一直显示破图。
 *
 * 注意：不能直接用 `entry.icon ? <img/> : <span/>`——浏览器拿到 404 会显示破图标
 * 而不是触发 React 的条件分支。必须监听 `<img onError>` 主动切换状态。
 */
function CatalogIcon({ entry }: { entry: MCPCatalogEntry }) {
  const [broken, setBroken] = useState(false)
  if (!entry.icon || broken) {
    return (
      <span className="mcp-catalog-card-icon-fallback">
        {entry.title[0]?.toUpperCase()}
      </span>
    )
  }
  return (
    <img
      src={entry.icon}
      alt=""
      loading="lazy"
      onError={() => setBroken(true)}
    />
  )
}
