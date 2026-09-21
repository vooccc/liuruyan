import { useCallback, useEffect, useState } from 'react'
import { Pin, Pencil, X, Plus, Trash2 } from 'lucide-react'
import {
  type ScopeItem,
  type ScopeItemType,
  type SessionPinnedState,
  getSessionPinned,
  updateSessionPinnedGuideline,
  updateSessionPinnedScope,
} from '../api/chat'

const SCOPE_TYPE_LABELS: Record<ScopeItemType, string> = {
  file: '文件',
  directory: '目录',
  code_context: 'Symbol',
  rule: '规则',
  code_map: 'Code Map',
}

const SCOPE_TYPES: ScopeItemType[] = ['file', 'directory', 'code_context', 'rule', 'code_map']

interface PinnedBarProps {
  sessionId: string | null
  /** 外部递增触发重新拉取（@ mention 选中后通知 PinnedBar 刷新）。 */
  refreshKey?: number
}

interface PinnedEditorProps {
  initialGuideline: string | null
  initialScope: ScopeItem[]
  onSave: (guideline: string | null, scope: ScopeItem[]) => Promise<void>
  onClose: () => void
}

function PinnedEditor({ initialGuideline, initialScope, onSave, onClose }: PinnedEditorProps) {
  const [guideline, setGuideline] = useState<string>(initialGuideline || '')
  const [scope, setScope] = useState<ScopeItem[]>(() => initialScope.map((it) => ({ ...it })))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const updateItem = useCallback((idx: number, patch: Partial<ScopeItem>) => {
    setScope((prev) => prev.map((it, i) => (i === idx ? { ...it, ...patch } : it)))
  }, [])

  const removeItem = useCallback((idx: number) => {
    setScope((prev) => prev.filter((_, i) => i !== idx))
  }, [])

  const addItem = useCallback(() => {
    setScope((prev) => [...prev, { type: 'file', path: '' }])
  }, [])

  const handleSave = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      const cleanedScope = scope
        .map((it) => ({ ...it, path: it.path.trim(), label: it.label?.trim() || undefined }))
        // code_map 允许空 path（后端兜底找 _docs/CODE_MAP.md），其它类型必须有 path
        .filter((it) => it.type === 'code_map' || it.path.length > 0)
      const cleanedGuideline = guideline.trim() ? guideline : null
      await onSave(cleanedGuideline, cleanedScope)
      onClose()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }, [guideline, scope, onSave, onClose])

  return (
    <div className="pinned-modal-overlay" onClick={onClose}>
      <div className="pinned-modal" onClick={(e) => e.stopPropagation()}>
        <div className="pinned-modal-header">
          <span>编辑 Pinned Context</span>
          <button type="button" className="pinned-icon-btn" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>

        <div className="pinned-modal-body">
          <div className="pinned-section">
            <label className="pinned-section-label">
              会话级 Guideline
              <span className="pinned-section-hint">（每轮对话自动注入到 system 末尾）</span>
            </label>
            <textarea
              className="pinned-guideline-input"
              value={guideline}
              onChange={(e) => setGuideline(e.target.value)}
              placeholder="例：始终用中文回复；优先改上游不要 workaround；禁止重构超出本次需求的代码"
              rows={4}
            />
          </div>

          <div className="pinned-section">
            <div className="pinned-section-row">
              <label className="pinned-section-label">
                Scope Items
                <span className="pinned-section-hint">（每轮重新解析为最新内容）</span>
              </label>
              <button type="button" className="pinned-add-btn" onClick={addItem}>
                <Plus size={14} />
                添加
              </button>
            </div>
            {scope.length === 0 ? (
              <div className="pinned-empty-hint">还没有钉任何 scope。点「添加」开始。</div>
            ) : (
              <div className="pinned-scope-table">
                {scope.map((item, idx) => (
                  <div key={idx} className="pinned-scope-row">
                    <select
                      className="pinned-scope-type"
                      value={item.type}
                      onChange={(e) => updateItem(idx, { type: e.target.value as ScopeItemType })}
                    >
                      {SCOPE_TYPES.map((t) => (
                        <option key={t} value={t}>{SCOPE_TYPE_LABELS[t]}</option>
                      ))}
                    </select>
                    <input
                      className="pinned-scope-path"
                      type="text"
                      placeholder={item.type === 'code_context' ? 'symbol 名（如 SessionManager）' : item.type === 'code_map' ? '可留空，默认 _docs/CODE_MAP.md' : '相对路径（如 backend/main.py）'}
                      value={item.path}
                      onChange={(e) => updateItem(idx, { path: e.target.value })}
                    />
                    <input
                      className="pinned-scope-label"
                      type="text"
                      placeholder="显示名（可选）"
                      value={item.label || ''}
                      onChange={(e) => updateItem(idx, { label: e.target.value })}
                    />
                    <button
                      type="button"
                      className="pinned-icon-btn"
                      onClick={() => removeItem(idx)}
                      aria-label="删除"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {error && <div className="pinned-error">{error}</div>}
        </div>

        <div className="pinned-modal-footer">
          <button type="button" className="pinned-btn-secondary" onClick={onClose} disabled={saving}>取消</button>
          <button type="button" className="pinned-btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
        <PinnedStyle />
      </div>
    </div>
  )
}

export default function PinnedBar({ sessionId, refreshKey = 0 }: PinnedBarProps) {
  const [state, setState] = useState<SessionPinnedState | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)

  const refresh = useCallback(async (sid: string) => {
    try {
      const data = await getSessionPinned(sid)
      setState(data)
    } catch {
      setState(null)
    }
  }, [])

  useEffect(() => {
    if (!sessionId) {
      setState(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const data = await getSessionPinned(sessionId)
        if (!cancelled) setState(data)
      } catch {
        if (!cancelled) setState(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId, refreshKey])

  const handleRemoveItem = useCallback(async (idx: number) => {
    if (!sessionId || !state) return
    const next = state.scope.filter((_, i) => i !== idx)
    try {
      const result = await updateSessionPinnedScope(sessionId, next)
      setState((prev) => (prev ? { ...prev, scope: result.scope } : prev))
    } catch {
      // 回滚靠 refresh
      void refresh(sessionId)
    }
  }, [sessionId, state, refresh])

  const handleSave = useCallback(async (guideline: string | null, scope: ScopeItem[]) => {
    if (!sessionId) return
    // 串行保存：guideline 先、scope 后；任意一步失败抛给 editor 显示
    await updateSessionPinnedGuideline(sessionId, guideline)
    const result = await updateSessionPinnedScope(sessionId, scope)
    setState({
      session_id: sessionId,
      guideline,
      scope: result.scope,
    })
  }, [sessionId])

  if (!sessionId) return null
  const hasGuideline = !!state?.guideline?.trim()
  const hasScope = (state?.scope?.length ?? 0) > 0
  const hasAny = hasGuideline || hasScope

  return (
    <>
      <div className={`pinned-bar ${hasAny ? '' : 'pinned-bar--empty'}`}>
        {hasGuideline && (
          <div className="pinned-guideline-chip" title={state!.guideline || ''}>
            <Pin size={12} />
            <span className="pinned-guideline-chip-text">
              {(state!.guideline || '').length > 60
                ? `${(state!.guideline || '').slice(0, 60)}…`
                : state!.guideline}
            </span>
          </div>
        )}
        {hasScope && state!.scope.map((item, idx) => {
          const label = item.label || (item.type === 'code_context' ? item.path : item.path.split(/[\\/]/).pop() || item.path)
          return (
            <div key={`${item.type}:${item.path}:${idx}`} className="pinned-scope-chip" title={`${SCOPE_TYPE_LABELS[item.type]}: ${item.path}`}>
              <span className="pinned-scope-chip-type">{SCOPE_TYPE_LABELS[item.type]}</span>
              <span className="pinned-scope-chip-label">{label || '(default)'}</span>
              <button
                type="button"
                className="pinned-scope-chip-remove"
                onClick={() => handleRemoveItem(idx)}
                aria-label={`移除 ${label}`}
              >
                <X size={11} />
              </button>
            </div>
          )
        })}
        <button
          type="button"
          className="pinned-edit-btn"
          onClick={() => setEditorOpen(true)}
          title="编辑 Pinned Context"
        >
          {hasAny ? <Pencil size={12} /> : <><Pin size={12} /><span>Pin context</span></>}
        </button>
      </div>

      {editorOpen && (
        <PinnedEditor
          initialGuideline={state?.guideline ?? null}
          initialScope={state?.scope ?? []}
          onSave={handleSave}
          onClose={() => setEditorOpen(false)}
        />
      )}
      <PinnedStyle />
    </>
  )
}

function PinnedStyle() {
  return (
    <style>{`
      .pinned-bar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        padding: 5px 14px 2px;
        border-top: 1px solid var(--border-light, rgba(0,0,0,0.06));
        background: var(--bg-elevated, rgba(0,0,0,0.02));
        font-size: 12px;
      }
      .pinned-bar--empty {
        background: transparent;
        border-top-color: transparent;
      }
      .pinned-guideline-chip,
      .pinned-scope-chip {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 8px;
        border-radius: 999px;
        background: var(--bg-card, rgba(0,0,0,0.05));
        color: var(--text-primary, #333);
        max-width: 320px;
        line-height: 1;
        border: 1px solid var(--border-light, rgba(0,0,0,0.08));
      }
      .pinned-guideline-chip {
        background: rgba(120, 100, 200, 0.10);
        border-color: rgba(120, 100, 200, 0.25);
        color: rgba(80, 60, 160, 0.95);
      }
      .pinned-guideline-chip-text {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        font-weight: 500;
      }
      .pinned-scope-chip-type {
        font-size: 10.5px;
        opacity: 0.65;
        font-weight: 500;
      }
      .pinned-scope-chip-label {
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
        font-size: 11.5px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 220px;
      }
      .pinned-scope-chip-remove {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 14px;
        height: 14px;
        padding: 0;
        margin-left: 2px;
        border: none;
        background: transparent;
        cursor: pointer;
        opacity: 0.5;
        border-radius: 50%;
        color: inherit;
      }
      .pinned-scope-chip-remove:hover {
        opacity: 1;
        background: rgba(0,0,0,0.1);
      }
      .pinned-edit-btn {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 9px;
        border-radius: 999px;
        border: 1px dashed var(--border-light, rgba(0,0,0,0.18));
        background: transparent;
        color: var(--text-secondary, #666);
        cursor: pointer;
        font-size: 11.5px;
      }
      .pinned-edit-btn:hover {
        border-style: solid;
        color: var(--text-primary, #333);
        background: var(--bg-card, rgba(0,0,0,0.04));
      }

      /* Modal */
      .pinned-modal-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.42);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
      }
      .pinned-modal {
        width: min(680px, 92vw);
        max-height: 86vh;
        display: flex;
        flex-direction: column;
        background: var(--bg-card, #fff);
        color: var(--text-primary, #222);
        border-radius: 12px;
        box-shadow: 0 18px 48px rgba(0,0,0,0.32);
        overflow: hidden;
      }
      .pinned-modal-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 14px 18px;
        border-bottom: 1px solid var(--border-light, rgba(0,0,0,0.08));
        font-weight: 600;
      }
      .pinned-modal-body {
        padding: 16px 18px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 18px;
      }
      .pinned-modal-footer {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
        padding: 12px 18px;
        border-top: 1px solid var(--border-light, rgba(0,0,0,0.08));
      }
      .pinned-section {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .pinned-section-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
      }
      .pinned-section-label {
        font-size: 13px;
        font-weight: 500;
      }
      .pinned-section-hint {
        font-size: 11.5px;
        font-weight: 400;
        color: var(--text-secondary, #666);
        margin-left: 6px;
      }
      .pinned-guideline-input {
        width: 100%;
        font-family: inherit;
        font-size: 13px;
        padding: 9px 11px;
        border-radius: 8px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
        background: var(--bg-card, #fff);
        color: inherit;
        resize: vertical;
        min-height: 80px;
      }
      .pinned-add-btn {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 4px 10px;
        font-size: 12px;
        border-radius: 6px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
        background: var(--bg-card, #fff);
        cursor: pointer;
        color: inherit;
      }
      .pinned-add-btn:hover {
        background: var(--bg-elevated, rgba(0,0,0,0.04));
      }
      .pinned-empty-hint {
        font-size: 12px;
        color: var(--text-secondary, #777);
        padding: 8px 0;
        font-style: italic;
      }
      .pinned-scope-table {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .pinned-scope-row {
        display: grid;
        grid-template-columns: 110px 1fr 130px 28px;
        gap: 6px;
        align-items: center;
      }
      .pinned-scope-type,
      .pinned-scope-path,
      .pinned-scope-label {
        font-family: inherit;
        font-size: 12.5px;
        padding: 6px 8px;
        border-radius: 6px;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
        background: var(--bg-card, #fff);
        color: inherit;
        min-width: 0;
      }
      .pinned-scope-path {
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      }
      .pinned-icon-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 4px;
        border: none;
        background: transparent;
        border-radius: 6px;
        cursor: pointer;
        color: var(--text-secondary, #666);
      }
      .pinned-icon-btn:hover {
        background: var(--bg-elevated, rgba(0,0,0,0.06));
        color: var(--text-primary, #222);
      }
      .pinned-error {
        color: #c0392b;
        font-size: 12.5px;
        padding: 6px 0;
      }
      .pinned-btn-secondary,
      .pinned-btn-primary {
        padding: 6px 14px;
        font-size: 13px;
        border-radius: 6px;
        cursor: pointer;
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
      }
      .pinned-btn-secondary {
        background: var(--bg-card, #fff);
        color: var(--text-primary, #222);
      }
      .pinned-btn-primary {
        background: var(--accent, #5b6cff);
        color: #fff;
        border-color: transparent;
      }
      .pinned-btn-primary:disabled,
      .pinned-btn-secondary:disabled {
        opacity: 0.55;
        cursor: not-allowed;
      }
    `}</style>
  )
}
