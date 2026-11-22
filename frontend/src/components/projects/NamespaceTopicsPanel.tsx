import { useEffect, useState } from 'react'
import { GitBranch, GitMerge, Plus, Save, Trash2, X } from 'lucide-react'
import {
  deleteNamespaceTopic,
  getNamespaceTopic,
  mergeNamespaceTopics,
  updateNamespaceTopic,
  type NamespaceTopic,
} from '../../api/chat'
import { useConfirmDialog } from '../ConfirmDialog'

interface NamespaceTopicsPanelProps {
  nsId: string
  agentId: string
  topics: NamespaceTopic[]
  onTopicsChanged: (nextTopics: NamespaceTopic[]) => void
  onError: (message: string) => void
}

/**
 * 项目专题树叉面板（极简按需展开）：
 * - 平时只占一行轻巧的树叉标签栏；
 * - 点击任意树叉展开正文，支持就地编辑、删除或合并/重命名到其他树叉。
 */
export default function NamespaceTopicsPanel({
  nsId,
  agentId,
  topics,
  onTopicsChanged,
  onError,
}: NamespaceTopicsPanelProps) {
  const [selectedTopic, setSelectedTopic] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  // 新建树叉
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')

  // 合并 / 重命名树叉
  const [merging, setMerging] = useState(false)
  const [mergeTarget, setMergeTarget] = useState('')

  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  // 切换项目时重置展开状态
  useEffect(() => {
    setSelectedTopic(null)
    setContent('')
    setEditing(false)
    setCreating(false)
    setMerging(false)
  }, [nsId])

  useEffect(() => {
    if (!selectedTopic) return
    let cancelled = false
    setLoading(true)
    setEditing(false)
    setMerging(false)
    getNamespaceTopic(nsId, selectedTopic, agentId)
      .then(res => {
        if (!cancelled) {
          setContent(res.content || '')
          setDraft(res.content || '')
        }
      })
      .catch(e => {
        if (!cancelled) onError(`加载树叉失败: ${(e as Error).message || e}`)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [nsId, agentId, selectedTopic, onError])

  async function handleSaveTopic() {
    if (!selectedTopic) return
    setSaving(true)
    try {
      const res = await updateNamespaceTopic(nsId, selectedTopic, draft, agentId)
      setContent(draft)
      setEditing(false)
      onTopicsChanged(res.topics || [])
    } catch (e) {
      onError(`保存树叉失败: ${(e as Error).message || e}`)
    }
    setSaving(false)
  }

  async function handleCreateTopic() {
    const name = newName.trim()
    if (!name) return
    setSaving(true)
    try {
      const initial = `# ${name}\n\n`
      const res = await updateNamespaceTopic(nsId, name, initial, agentId)
      onTopicsChanged(res.topics || [])
      setCreating(false)
      setNewName('')
      setSelectedTopic(res.topic)
      setContent(initial)
      setDraft(initial)
      setEditing(true)
    } catch (e) {
      onError(`创建树叉失败: ${(e as Error).message || e}`)
    }
    setSaving(false)
  }

  async function handleDeleteTopic(topicName: string) {
    const ok = await confirm(
      '删除专题树叉',
      `确定删除树叉「${topicName}」？timeline.jsonl 中的原始历史事件仍保留。`,
      'danger',
      '删除',
    )
    if (!ok) return
    try {
      const res = await deleteNamespaceTopic(nsId, topicName, agentId)
      onTopicsChanged(res.topics || [])
      if (selectedTopic === topicName) {
        setSelectedTopic(null)
        setContent('')
      }
    } catch (e) {
      onError(`删除树叉失败: ${(e as Error).message || e}`)
    }
  }

  async function handleMergeTopic() {
    if (!selectedTopic) return
    const target = mergeTarget.trim()
    if (!target || target === selectedTopic) return
    setSaving(true)
    try {
      const res = await mergeNamespaceTopics(nsId, selectedTopic, target, { agentId })
      onTopicsChanged(res.topics || [])
      setMerging(false)
      setMergeTarget('')
      setSelectedTopic(res.target_topic)
    } catch (e) {
      onError(`合并树叉失败: ${(e as Error).message || e}`)
    }
    setSaving(false)
  }

  const activeMeta = selectedTopic ? topics.find(t => t.name === selectedTopic) : undefined

  return (
    <div className="ns-topics-section">
      <div className="ns-topics-header">
        <span className="ns-topics-title">
          <GitBranch size={13} />
          <span>专题树叉 ({topics.length})</span>
        </span>
        <button
          type="button"
          className="ns-btn-xs"
          onClick={() => setCreating(v => !v)}
          title="手动新建专题树叉"
        >
          <Plus size={11} /> 新建树叉
        </button>
      </div>

      {creating && (
        <div className="ns-topic-create-row">
          <input
            className="namespace-new-input"
            placeholder="专题名称（如：架构与边界 / 证据链，≤8字佳）"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') void handleCreateTopic()
              if (e.key === 'Escape') setCreating(false)
            }}
            autoFocus
          />
          <button
            type="button"
            className="icon-btn-sm success"
            disabled={saving || !newName.trim()}
            onClick={() => void handleCreateTopic()}
          >
            创建
          </button>
          <button type="button" className="icon-btn-sm" onClick={() => setCreating(false)}>
            取消
          </button>
        </div>
      )}

      {topics.length > 0 ? (
        <div className="ns-topic-pills">
          {topics.map(t => {
            const isSelected = selectedTopic === t.name
            return (
              <button
                key={t.name}
                type="button"
                className={`ns-topic-pill ${isSelected ? 'active' : ''}`}
                onClick={() => setSelectedTopic(isSelected ? null : t.name)}
                title={t.summary || `${t.name} (${t.chars} 字)`}
              >
                <span>{t.name}</span>
                <span className="ns-topic-pill-chars">{t.chars}字</span>
              </button>
            )
          })}
        </div>
      ) : (
        <div className="ns-meta-empty" style={{ marginTop: 4 }}>
          暂未长出专题树叉；随着对话深入会自动按主题归枝，也可手动新建
        </div>
      )}

      {selectedTopic && (
        <div className="ns-topic-viewer">
          <div className="ns-topic-viewer-bar">
            <div className="ns-topic-viewer-title">
              <strong>{selectedTopic}</strong>
              {activeMeta?.updated_at && (
                <span className="ns-topic-updated">更新于 {activeMeta.updated_at.replace('T', ' ')}</span>
              )}
            </div>
            <div className="ns-topic-viewer-actions">
              {!editing ? (
                <>
                  <button type="button" className="ns-btn-xs" onClick={() => setEditing(true)}>
                    编辑
                  </button>
                  <button
                    type="button"
                    className="ns-btn-xs"
                    onClick={() => { setMerging(v => !v); setMergeTarget('') }}
                    title="重命名或合并到另一个树叉"
                  >
                    <GitMerge size={11} /> 合并/改名
                  </button>
                  <button
                    type="button"
                    className="ns-btn-xs danger"
                    onClick={() => void handleDeleteTopic(selectedTopic)}
                    title="删除此树叉"
                  >
                    <Trash2 size={11} />
                  </button>
                  <button
                    type="button"
                    className="ns-btn-xs"
                    onClick={() => setSelectedTopic(null)}
                    title="收起"
                  >
                    <X size={11} />
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="icon-btn-sm success"
                    disabled={saving}
                    onClick={() => void handleSaveTopic()}
                  >
                    <Save size={12} /> {saving ? '...' : '保存'}
                  </button>
                  <button
                    type="button"
                    className="icon-btn-sm"
                    onClick={() => { setEditing(false); setDraft(content) }}
                  >
                    取消
                  </button>
                </>
              )}
            </div>
          </div>

          {merging && (
            <div className="ns-topic-merge-row">
              <input
                className="namespace-new-input"
                list={`ns-merge-targets-${nsId}`}
                placeholder="输入目标树叉名（选已有则合并，新名则重命名）"
                value={mergeTarget}
                onChange={e => setMergeTarget(e.target.value)}
              />
              <datalist id={`ns-merge-targets-${nsId}`}>
                {topics
                  .filter(t => t.name !== selectedTopic)
                  .map(t => <option key={t.name} value={t.name} />)}
              </datalist>
              <button
                type="button"
                className="icon-btn-sm success"
                disabled={saving || !mergeTarget.trim() || mergeTarget.trim() === selectedTopic}
                onClick={() => void handleMergeTopic()}
              >
                确认
              </button>
              <button type="button" className="icon-btn-sm" onClick={() => setMerging(false)}>
                取消
              </button>
            </div>
          )}

          {loading ? (
            <div className="ns-meta-empty">加载中...</div>
          ) : editing ? (
            <textarea
              className="memory-editor"
              value={draft}
              onChange={e => setDraft(e.target.value)}
              rows={9}
            />
          ) : (
            <pre className="memory-text ns-topic-pre">{content}</pre>
          )}
        </div>
      )}
      {confirmDialog}
    </div>
  )
}
