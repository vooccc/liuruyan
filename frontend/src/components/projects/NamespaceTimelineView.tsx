import { useState } from 'react'
import { Search, ChevronDown, ChevronRight } from 'lucide-react'
import { TimelineEntry } from '../../api/chat'

interface NamespaceTimelineViewProps {
  entries: TimelineEntry[]
  total: number
  loading?: boolean
  searchQuery: string
  onSearchChange: (q: string) => void
  onLoadMore: () => void
}

const TEMP_LABEL: Record<string, { text: string; cls: string }> = {
  burning: { text: '核心', cls: 'is-burning' },
  warm: { text: '重要', cls: 'is-warm' },
  cold: { text: '常规', cls: 'is-cold' },
}

function formatTimelineTime(ts?: string): string {
  if (!ts) return ''
  return ts.replace('T', ' ').slice(0, 16)
}

/**
 * 事件溯源视图：完整呈现 event / decision / cognition / detail / tags / temperature / session_id，
 * 支持关键词检索与分页加载更多。
 */
export default function NamespaceTimelineView({
  entries,
  total,
  loading = false,
  searchQuery,
  onSearchChange,
  onLoadMore,
}: NamespaceTimelineViewProps) {
  const [expandedDetails, setExpandedDetails] = useState<Record<number, boolean>>({})

  function toggleDetail(idx: number) {
    setExpandedDetails(prev => ({ ...prev, [idx]: !prev[idx] }))
  }

  return (
    <div className="ns-timeline-wrap">
      <div className="ns-timeline-toolbar">
        <div className="ns-search-box">
          <Search size={13} />
          <input
            className="namespace-new-input"
            value={searchQuery}
            onChange={e => onSearchChange(e.target.value)}
            placeholder="搜索时间线事件、决策、认知或标签..."
          />
        </div>
        <span className="ns-timeline-count">
          已加载 {entries.length} / {total} 条
        </span>
      </div>

      {entries.length === 0 ? (
        <div className="empty-state" style={{ padding: '28px 16px' }}>
          <p>{loading ? '时间线加载中...' : searchQuery ? '没有匹配的时间线记录' : '暂无事件溯源记录'}</p>
        </div>
      ) : (
        <div className="reflections-list">
          {entries.map((entry, idx) => {
            const tempMeta = entry.temperature ? TEMP_LABEL[String(entry.temperature)] : undefined
            const hasDecision = Boolean(entry.decision && String(entry.decision).trim() && entry.decision !== '无')
            const hasCognition = Boolean(entry.cognition && String(entry.cognition).trim())
            const hasDetail = Boolean(entry.detail && String(entry.detail).trim())
            const tags = Array.isArray(entry.tags) ? entry.tags : []
            const isExpanded = Boolean(expandedDetails[idx])

            return (
              <div key={`${entry.ts || ''}-${idx}`} className="reflection-item-card ns-tl-card">
                <div className="reflection-item-header">
                  <div className="ns-tl-title-row">
                    {tempMeta && (
                      <span className={`ns-temp-pill ${tempMeta.cls}`}>{tempMeta.text}</span>
                    )}
                    {entry.topic && (
                      <span className="ns-topic-badge" title={`归属树叉：${entry.topic}`}>
                        {String(entry.topic)}
                      </span>
                    )}
                    <span className="reflection-item-title">{String(entry.event ?? '（无标题）')}</span>
                  </div>
                  <span className="ns-tl-ts">{formatTimelineTime(entry.ts)}</span>
                </div>

                {hasDecision && (
                  <div className="ns-tl-field">
                    <span className="ns-tl-label">决策</span>
                    <span className="ns-tl-val">{String(entry.decision)}</span>
                  </div>
                )}

                {hasCognition && (
                  <div className="ns-tl-field">
                    <span className="ns-tl-label">认知</span>
                    <span className="ns-tl-val">{String(entry.cognition)}</span>
                  </div>
                )}

                {hasDetail && (
                  <div className="ns-tl-detail-block">
                    <button
                      type="button"
                      className="ns-tl-detail-toggle"
                      onClick={() => toggleDetail(idx)}
                    >
                      {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      <span>{isExpanded ? '收起详细上下文' : '展开详细上下文'}</span>
                    </button>
                    {isExpanded && (
                      <pre className="reflection-item-body ns-tl-detail-body">{String(entry.detail)}</pre>
                    )}
                  </div>
                )}

                {(tags.length > 0 || entry.session_id) && (
                  <div className="ns-tl-footer">
                    <div className="ns-tl-tags">
                      {tags.map((t, ti) => (
                        <span key={ti} className="ns-alias-chip">{String(t)}</span>
                      ))}
                    </div>
                    {entry.session_id && (
                      <span className="ns-tl-session" title={`来源会话：${entry.session_id}`}>
                        会话 {String(entry.session_id).slice(0, 8)}
                      </span>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {entries.length < total && (
        <div className="ns-timeline-more">
          <button className="text-btn" onClick={onLoadMore} disabled={loading}>
            {loading ? '加载中...' : `加载更早记录（剩余 ${total - entries.length} 条）`}
          </button>
        </div>
      )}
    </div>
  )
}
